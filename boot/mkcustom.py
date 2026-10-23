#!/usr/bin/env python3
"""mkcustom.py — build a raw disk image with a custom bootloader for Menagerie.

LOCAL-ONLY scaffolding. Never pushed anywhere.

Disk layout:
  LBA 0: 512-byte boot sector (16-bit real mode @ 0x7C00)
  LBA 1..: trampoline stub image (loaded to 0x100000)
  LBA ..: Orum flat image (text -> 0x400000, data -> 0x401000)
  LBA ..: lowmem image (loaded DIRECTLY to 0x5000):
           +0x0000 IDT area (zeros; the kernel builds the IDT at runtime)
           +0x1000 ISR stubs (256 x 16B, hand-assembled)
           +0x2000 common ISR handler (hand-assembled, on_irq patched in)
           +0x2100 font (54 hand-drawn 8x16 glyphs — original, no font IP)
           +0x2460 glyph index (128B: ASCII -> glyph idx, 0xFF = none)
           +0x24E0 fb_addr u64 | +0x24E8 fb_stride u32
           +0x24EC tick u64 | +0x24F4 IDT descriptor (16B)
           +0x2504 kb_head u64 | +0x250C kb_tail u64
           +0x2514 cursor_x u64 | +0x251C cursor_y u64
           +0x2524 shift_state u64 | +0x252C e0skip u64
           +0x2534 keyboard ring buffer (256B)
           +0x2634 line buffer (128B)
           +0x26B4 scancode table (128B) | +0x2734 shifted table (128B)
           +0x27B4 NUL-terminated strings (see STRINGS)

Heap (resolved by the build, see apply_heap_relocs):
  0x2000000 - 0x3000000 (16MB): __orum_heap_start / __orum_heap_end.
  Identity-mapped by the trampoline's 0-4GB page tables.

Boot sector flow:
  1. VBE BIOS: AX=0x4F02/BX=0x4118 -> 1024x768x32 linear framebuffer.
     AX=0x4F01/CX=0x4118 -> mode info to 0x8000 (PhysBasePtr @+40,
     pitch @+16, xres @+18, yres @+20, bpp @+25).
  2. Build a FAKE multiboot2 info block at 0x9000: u32 total_size=48,
     u32 reserved=0, framebuffer tag (type 8, size 32) with the VBE values,
     end tag. The kernel parses this unchanged.
  3. int 0x13 AH=0x42: load stub -> bounce 0x10000, orum -> bounce+stub_bytes,
     lowmem -> 0x5000 directly.
  4. Enable A20 (BIOS 0x15/0x2401 + port 0x92).
  5. 32-bit protected mode: copy stub -> 0x100000, orum -> 0x400000.
  6. Far jump to the PROVEN trampoline at 0x100058 with EBX=0x9000.

Serial debug codes on COM1 (0x3F8): V I B 1 2 3 A P C at each step.
Fail codes via teletype: '1' vbe-set, '2' vbe-info, '3' disk.
"""

import os
import struct
import sys

MENAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORUM_DIR = os.path.expanduser("~/workspace/orum")
sys.path.insert(0, ORUM_DIR)
sys.path.insert(0, MENAG_DIR)

from orum.driver import build_binary
from boot.mkboot import build_code, gdt_bytes, align_up, parse_elf, STUB_BASE
from boot.font_data import build_font

KERNEL_SRC = os.path.join(MENAG_DIR, "kernel", "kmain.orum")
TMP_ELF = "/tmp/menagerie.elf"
DISK_IMG = os.path.join(MENAG_DIR, "boot", "disk.img")

# Heap: provided by the kernel via resolved relocations (see apply_heap_relocs).
HEAP_START = 0x2000000
HEAP_END = 0x3000000

# Lowmem image layout (loaded at 0x5000)
LOWMEM_BASE = 0x5000
IDT_OFF = 0x0000       # 0x5000: IDT, 4KB (kernel builds it)
STUBS_OFF = 0x1000     # 0x6000: 256 ISR stubs x 16B
COMMON_OFF = 0x2000    # 0x7000: common ISR handler
FONT_OFF = 0x2100      # 0x7100: 54 glyphs x 16B
GINDEX_OFF = 0x2460    # 0x7460: glyph index, 128B
FB_ADDR_OFF = 0x24E0   # 0x74E0: fb_addr u64
FB_STRIDE_OFF = 0x24E8  # 0x74E8: fb_stride u32
TICK_OFF = 0x24EC      # 0x74EC: tick counter u64
IDTDESC_OFF = 0x24F4   # 0x74F4: IDT descriptor (16B)
KB_HEAD_OFF = 0x2504   # 0x7504: kb ring head u64
KB_TAIL_OFF = 0x250C   # 0x750C: kb ring tail u64
CUR_X_OFF = 0x2514     # 0x7514: cursor_x u64
CUR_Y_OFF = 0x251C     # 0x751C: cursor_y u64
SHIFT_OFF = 0x2524     # 0x7524: shift state u64
E0SKIP_OFF = 0x252C    # 0x752C: e0-prefix skip flag u64
KBRING_OFF = 0x2534    # 0x7534: keyboard ring buffer (256B)
LINEBUF_OFF = 0x2634   # 0x7634: line buffer (128B)
SCAN_OFF = 0x26B4      # 0x76B4: scancode table (128B)
SCANS_OFF = 0x2734     # 0x7734: shifted scancode table (128B)
STRS_OFF = 0x27B4      # 0x77B4: strings

# String addresses (ABSOLUTE; mirrored in kmain.orum).
_s = LOWMEM_BASE + STRS_OFF
def _str(b):
    global _s
    addr, _s = _s, _s + len(b)
    return addr
S_MENAGERIE = _str(b"menagerie\x00")
S_TICK = _str(b"tick \x00")
S_CRLF = _str(b"\r\n\x00")
S_PROMPT = _str(b"> \x00")
S_UNKNOWN = _str(b"unknown command: \x00")
S_NOTIMPL = _str(b"not implemented yet: \x00")
S_HEAP_OK = _str(b"heap ok\x00")
S_HEAP_FAIL = _str(b"heap FAIL\x00")
S_KB_READY = _str(b"keyboard ready\x00")
S_HELP_HDR = _str(b"commands:\x00")
S_HELP_1 = _str(b"say help clear restart off\x00")
S_HELP_2 = _str(b"list run where copy move burn\x00")
S_HELP_3 = _str(b"newdir show page edit match\x00")
S_HELP_4 = _str(b"search tasks stop space size\x00")
S_HELP_5 = _str(b"past\x00")
S_HELP_NOTE = _str(b"file commands need a filesystem\x00")
C_SAY = _str(b"say\x00")
C_HELP = _str(b"help\x00")
C_CLEAR = _str(b"clear\x00")
C_RESTART = _str(b"restart\x00")
C_OFF = _str(b"off\x00")
C_LIST = _str(b"list\x00")
C_RUN = _str(b"run\x00")
C_WHERE = _str(b"where\x00")
C_COPY = _str(b"copy\x00")
C_MOVE = _str(b"move\x00")
C_BURN = _str(b"burn\x00")
C_NEWDIR = _str(b"newdir\x00")
C_SHOW = _str(b"show\x00")
C_PAGE = _str(b"page\x00")
C_EDIT = _str(b"edit\x00")
C_MATCH = _str(b"match\x00")
C_SEARCH = _str(b"search\x00")
C_TASKS = _str(b"tasks\x00")
C_STOP = _str(b"stop\x00")
C_SPACE = _str(b"space\x00")
C_SIZE = _str(b"size\x00")
C_PAST = _str(b"past\x00")
S_XHCI_KEY = _str(b"xhci: key \x00")
S_XHCI_READY = _str(b"xhci: keyboard ready\x00")
S_XHCI_NODEV = _str(b"xhci: no usb keyboard\x00")
S_XHCI_FOUND = _str(b"xhci: controller found\x00")
S_XH_HEX = _str(b"0x\x00")
S_XHCI_STAGE = _str(b"xhci: stage \x00")
HID_OFF = _s
HIDS_OFF = _s + 256
_s += 512
del _s, _str
LOWMEM_SIZE = HIDS_OFF + 256 - LOWMEM_BASE + 64  # strings + HID tables + slack
assert LOWMEM_BASE + LOWMEM_SIZE < 0x7C00, "lowmem collides with boot sector"

STRINGS = {
    S_MENAGERIE: b"menagerie\x00",
    S_TICK: b"tick \x00",
    S_CRLF: b"\r\n\x00",
    S_PROMPT: b"> \x00",
    S_UNKNOWN: b"unknown command: \x00",
    S_NOTIMPL: b"not implemented yet: \x00",
    S_HEAP_OK: b"heap ok\x00",
    S_HEAP_FAIL: b"heap FAIL\x00",
    S_KB_READY: b"keyboard ready\x00",
    S_HELP_HDR: b"commands:\x00",
    S_HELP_1: b"say help clear restart off\x00",
    S_HELP_2: b"list run where copy move burn\x00",
    S_HELP_3: b"newdir show page edit match\x00",
    S_HELP_4: b"search tasks stop space size\x00",
    S_HELP_5: b"past\x00",
    S_HELP_NOTE: b"file commands need a filesystem\x00",
    C_SAY: b"say\x00",
    C_HELP: b"help\x00",
    C_CLEAR: b"clear\x00",
    C_RESTART: b"restart\x00",
    C_OFF: b"off\x00",
    C_LIST: b"list\x00",
    C_RUN: b"run\x00",
    C_WHERE: b"where\x00",
    C_COPY: b"copy\x00",
    C_MOVE: b"move\x00",
    C_BURN: b"burn\x00",
    C_NEWDIR: b"newdir\x00",
    C_SHOW: b"show\x00",
    C_PAGE: b"page\x00",
    C_EDIT: b"edit\x00",
    C_MATCH: b"match\x00",
    C_SEARCH: b"search\x00",
    C_TASKS: b"tasks\x00",
    C_STOP: b"stop\x00",
    C_SPACE: b"space\x00",
    C_SIZE: b"size\x00",
    C_PAST: b"past\x00",
    S_XHCI_KEY: b"xhci: key \x00",
    S_XHCI_READY: b"xhci: keyboard ready\x00",
    S_XHCI_NODEV: b"xhci: no usb keyboard\x00",
    S_XHCI_FOUND: b"xhci: controller found\x00",
    S_XH_HEX: b"0x\x00",
    S_XHCI_STAGE: b"xhci: stage \x00",
}

# Scancode set 1 -> ASCII. 0x00 = no character. Shift press/release and
# 0xE0 prefixes are handled in code, not here.
_SCAN_BASE = {
    0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5",
    0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
    0x0C: "-", 0x0D: "=", 0x0E: "\x08",
    0x10: "q", 0x11: "w", 0x12: "e", 0x13: "r", 0x14: "t",
    0x15: "y", 0x16: "u", 0x17: "i", 0x18: "o", 0x19: "p",
    0x1A: "?", 0x1B: "?", 0x1C: "\n",
    0x1E: "a", 0x1F: "s", 0x20: "d", 0x21: "f", 0x22: "g",
    0x23: "h", 0x24: "j", 0x25: "k", 0x26: "l", 0x27: ";",
    0x28: "'", 0x29: "?",
    0x2B: "?", 0x2C: "z", 0x2D: "x", 0x2E: "c", 0x2F: "v",
    0x30: "b", 0x31: "n", 0x32: "m", 0x33: ",", 0x34: ".",
    0x35: "/", 0x39: " ",
}
_SCAN_SHIFT = {
    0x02: "!", 0x03: "@", 0x04: "#", 0x05: "$", 0x06: "%",
    0x07: "^", 0x08: "&", 0x09: "*", 0x0A: "(", 0x0B: ")",
    0x0C: "_", 0x0D: "+", 0x0E: "\x08",
    0x10: "q", 0x11: "w", 0x12: "e", 0x13: "r", 0x14: "t",
    0x15: "y", 0x16: "u", 0x17: "i", 0x18: "o", 0x19: "p",
    0x1A: "?", 0x1B: "?", 0x1C: "\n",
    0x1E: "a", 0x1F: "s", 0x20: "d", 0x21: "f", 0x22: "g",
    0x23: "h", 0x24: "j", 0x25: "k", 0x26: "l", 0x27: ":",
    0x28: '"', 0x29: "?",
    0x2B: "?", 0x2C: "z", 0x2D: "x", 0x2E: "c", 0x2F: "v",
    0x30: "b", 0x31: "n", 0x32: "m", 0x33: "<", 0x34: ">",
    0x35: "?", 0x39: " ",
}


def _build_scan_table(mapping):
    t = bytearray(128)
    for sc, ch in mapping.items():
        t[sc] = ord(ch)
    return bytes(t)

SCAN_TABLE = _build_scan_table(_SCAN_BASE)
SCAN_SHIFT_TABLE = _build_scan_table(_SCAN_SHIFT)

# USB HID boot-protocol usage ID -> ASCII. 0x00 = no character.
# Only glyphs present in the 54-glyph console font are mapped; the rest
# are 0 (dropped). Shifted letters stay lowercase (no uppercase glyphs).
_HID_BASE = {
    0x04: "a", 0x05: "b", 0x06: "c", 0x07: "d", 0x08: "e", 0x09: "f",
    0x0A: "g", 0x0B: "h", 0x0C: "i", 0x0D: "j", 0x0E: "k", 0x0F: "l",
    0x10: "m", 0x11: "n", 0x12: "o", 0x13: "p", 0x14: "q", 0x15: "r",
    0x16: "s", 0x17: "t", 0x18: "u", 0x19: "v", 0x1A: "w", 0x1B: "x",
    0x1C: "y", 0x1D: "z",
    0x1E: "1", 0x1F: "2", 0x20: "3", 0x21: "4", 0x22: "5",
    0x23: "6", 0x24: "7", 0x25: "8", 0x26: "9", 0x27: "0",
    0x28: "\n", 0x2A: "\x08", 0x2C: " ",
    0x2D: "-", 0x2E: "=",
    0x33: ";", 0x34: "'", 0x36: ",", 0x37: ".", 0x38: "/",
}
_HID_SHIFT = {
    0x04: "a", 0x05: "b", 0x06: "c", 0x07: "d", 0x08: "e", 0x09: "f",
    0x0A: "g", 0x0B: "h", 0x0C: "i", 0x0D: "j", 0x0E: "k", 0x0F: "l",
    0x10: "m", 0x11: "n", 0x12: "o", 0x13: "p", 0x14: "q", 0x15: "r",
    0x16: "s", 0x17: "t", 0x18: "u", 0x19: "v", 0x1A: "w", 0x1B: "x",
    0x1C: "y", 0x1D: "z",
    0x1E: "!", 0x25: "*", 0x26: "(", 0x27: ")",
    0x28: "\n", 0x2A: "\x08", 0x2C: " ",
    0x2E: "+", 0x33: ":", 0x34: '"', 0x36: "<", 0x37: ">", 0x38: "?",
}


def _build_hid_table(mapping):
    t = bytearray(256)
    for kc, ch in mapping.items():
        t[kc] = ord(ch)
    return bytes(t)

HID_TABLE = _build_hid_table(_HID_BASE)
HID_SHIFT_TABLE = _build_hid_table(_HID_SHIFT)




# --------------------------------------------------------------------------
# tiny 16-bit assembler with labels
# --------------------------------------------------------------------------
class B16:
    def __init__(self, base=0x7C00):
        self.base = base
        self.buf = bytearray()
        self.labels = {}
        self.fixups = []  # (off, kind, label)

    def L(self, name):
        if name in self.labels:
            raise ValueError(f"dup label {name}")
        self.labels[name] = len(self.buf)

    def db(self, *vs):
        for v in vs:
            self.buf.append(v & 0xFF)

    def dw(self, v):
        self.buf += struct.pack("<H", v & 0xFFFF)

    def dd(self, v):
        self.buf += struct.pack("<I", v & 0xFFFFFFFF)

    def emit(self, *bs):
        self.db(*bs)

    def jcc8(self, op, label):
        self.fixups.append((len(self.buf) + 1, "rel8", label))
        self.db(op, 0)

    def jcc16(self, op2, label):
        """0F op2, rel16 — for far conditional jumps."""
        self.fixups.append((len(self.buf) + 2, "rel16", label))
        self.db(0x0F, op2, 0, 0)

    def abs16(self, label):
        """dw holding the linear address of a label (for imm16/disp16)."""
        self.fixups.append((len(self.buf), "abs16", label))
        self.dw(0)

    def resolve(self):
        for off, kind, label in self.fixups:
            if label not in self.labels:
                raise ValueError(f"unknown label {label}")
            t = self.base + self.labels[label]
            if kind == "rel8":
                rel = t - (self.base + off + 1)
                if not -128 <= rel <= 127:
                    raise ValueError(f"rel8 {label} out of range: {rel}")
                struct.pack_into("b", self.buf, off, rel)
            elif kind == "rel16":
                rel = t - (self.base + off + 2)
                if not -32768 <= rel <= 32767:
                    raise ValueError(f"rel16 {label} out of range: {rel}")
                struct.pack_into("<h", self.buf, off, rel)
            elif kind == "abs16":
                if t > 0xFFFF:
                    raise ValueError(f"abs16 {label} = {t:#x} > 64K")
                struct.pack_into("<H", self.buf, off, t)
            else:
                raise ValueError(kind)


def serial(ch):
    """mov dx,0x3F8; mov al,ch; out dx,al  (7 bytes, 16-bit)"""
    return [0xBA, 0xF8, 0x03, 0xB0, ord(ch), 0xEE]


def find_on_irq(orum_data, orum_loads):
    """Locate on_irq's entry address via its magic NOP marker.

    The ELF symtab only carries the entry symbol, so on_irq() starts with
    a unique 8-byte NOP (see kernel/kmain.orum). We scan each PT_LOAD for
    the marker, then backtrack to the nearest function prologue
    (push rbp; mov rbp,rsp = 55 48 89 E5). Fails loudly if ambiguous.
    """
    MARKER = bytes([0x0F, 0x1F, 0x84, 0x00, 0x00, 0x00, 0x00, 0x00])
    PROLOGUE = bytes([0x55, 0x48, 0x8B, 0xEC])  # push rbp; mov rbp,rsp (8B form)
    for seg in orum_loads:
        blob = orum_data[seg["offset"]:seg["offset"] + seg["filesz"]]
        idx = 0
        while True:
            m = blob.find(MARKER, idx)
            if m < 0:
                break
            p = blob.rfind(PROLOGUE, 0, m)
            if p >= 0 and m - p < 64:
                return seg["vaddr"] + p
            idx = m + 1
    raise ValueError("on_irq marker/prologue not found in ELF")


def resolve_symbol(elf_path, name):
    """Find a symbol's value in the ELF symtab (same walk as parse_elf)."""
    data = open(elf_path, "rb").read()
    assert data[:4] == b"\x7fELF" and data[4] == 2, "not a 64-bit ELF"
    (e_phoff, e_shoff, _flags, _ehsize, e_phentsize, e_phnum,
     e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<QQIHHHHHH", data, 0x20)
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        (sh_name, sh_type, _fl, _ad, sh_offset, sh_size, sh_link,
         _info, _al, sh_entsize) = struct.unpack_from("<IIQQQQIIQQ", data, off)
        if sh_type == 2:  # SHT_SYMTAB
            n = sh_size // sh_entsize
            soff = e_shoff + sh_link * e_shentsize
            (_n2, _t2, _f2, _a2, str_off, _sz2, _l2, _i2, _al2,
             _e2) = struct.unpack_from("<IIQQQQIIQQ", data, soff)
            for j in range(n):
                eoff = sh_offset + j * sh_entsize
                (st_name, _inf, _oth, _shn, st_value,
                 _sz) = struct.unpack_from("<IBBHQQ", data, eoff)
                end = data.index(b"\x00", str_off + st_name)
                sname = data[str_off + st_name:end].decode()
                if sname == name:
                    return st_value
    raise ValueError(f"symbol {name!r} not found in {elf_path}")


def build_isr_block(handler_addr):
    """256 ISR stubs + common handler. Stubs live at 0x6000, common at 0x7000.

    Per-vector stub (16 bytes): push imm8 vec; jmp rel32 common; 9x nop.
    Common handler (80 bytes): save 15 GP regs, rdi = vector number,
    call handler_addr (the Orum on_irq fn), restore, drop vector, iretq.
    """
    COMMON = LOWMEM_BASE + COMMON_OFF  # 0x7000
    STUBS = LOWMEM_BASE + STUBS_OFF    # 0x6000
    block = bytearray(0x1000)
    for v in range(256):
        s = v * 16
        stub_addr = STUBS + s
        block[s] = 0x6A
        block[s + 1] = v
        block[s + 2] = 0xE9
        rel = COMMON - (stub_addr + 7)
        struct.pack_into("<i", block, s + 3, rel)
        for k in range(7, 16):
            block[s + k] = 0x90
    c = bytearray()
    # push r15..r8, rax, rcx, rdx, rsi, rdi, rbp, rbx  (23 bytes)
    c += bytes([0x41, 0x57, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54,
                0x41, 0x53, 0x41, 0x52, 0x41, 0x51, 0x41, 0x50,
                0x50, 0x51, 0x52, 0x56, 0x57, 0x55, 0x53])
    c += bytes([0x48, 0x8B, 0xBC, 0x24, 0x78, 0x00, 0x00, 0x00])  # mov rdi,[rsp+120]
    c += bytes([0x48, 0x83, 0xEC, 0x08])                        # sub rsp,8
    c += bytes([0x48, 0xB8]) + struct.pack("<Q", handler_addr)   # mov rax,handler
    c += bytes([0xFF, 0xD0])                                    # call rax
    c += bytes([0x48, 0x83, 0xC4, 0x08])                        # add rsp,8
    # pop rbx, rbp, rdi, rsi, rdx, rcx, rax, r8..r15  (23 bytes)
    c += bytes([0x5B, 0x5D, 0x5F, 0x5E, 0x5A, 0x59, 0x58,
                0x41, 0x58, 0x41, 0x59, 0x41, 0x5A, 0x41, 0x5B,
                0x41, 0x5C, 0x41, 0x5D, 0x41, 0x5E, 0x41, 0x5F])
    c += bytes([0x48, 0x83, 0xC4, 0x08])                        # add rsp,8 (drop vec)
    c += bytes([0x48, 0xCF])                                    # iretq
    assert len(c) == 80, f"common handler is {len(c)} bytes, expected 80"
    return bytes(block) + bytes(c)


def build_lowmem(handler_addr):
    """The lowmem image loaded at 0x5000: IDT area, ISR stubs, font, strings."""
    img = bytearray(LOWMEM_SIZE)
    isr = build_isr_block(handler_addr)
    img[STUBS_OFF:STUBS_OFF + 0x1000] = isr[:0x1000]
    img[COMMON_OFF:COMMON_OFF + 80] = isr[0x1000:0x1000 + 80]
    font, gindex = build_font()
    img[FONT_OFF:FONT_OFF + len(font)] = font
    img[GINDEX_OFF:GINDEX_OFF + len(gindex)] = gindex
    img[SCAN_OFF:SCAN_OFF + len(SCAN_TABLE)] = SCAN_TABLE
    img[SCANS_OFF:SCANS_OFF + len(SCAN_SHIFT_TABLE)] = SCAN_SHIFT_TABLE
    img[HID_OFF - LOWMEM_BASE:HID_OFF - LOWMEM_BASE + 256] = HID_TABLE
    img[HIDS_OFF - LOWMEM_BASE:HIDS_OFF - LOWMEM_BASE + 256] = HID_SHIFT_TABLE
    for off, s in STRINGS.items():
        img[off - LOWMEM_BASE:off - LOWMEM_BASE + len(s)] = s
    return bytes(img)


def apply_heap_relocs(flat, base, elf_path):
    """Resolve __orum_heap_start/__orum_heap_end R_X86_64_64 relocations.

    The Orum compiler emits movabs placeholders for the heap bounds with
    relocations in .rela.text against the UND symbols. The kernel (this
    build) defines them: write HEAP_START/HEAP_END into the flat image
    at each relocation site. Fails loudly if anything is unexpected.
    """
    import struct as _st
    data = open(elf_path, "rb").read()
    assert data[:4] == b"\x7fELF" and data[4] == 2
    (e_phoff, e_shoff, _fl, _eh, _pe, _pn,
     e_shentsize, e_shnum, e_shstrndx) = _st.unpack_from("<QQIHHHHHH", data, 0x20)
    sym_names = {}
    rela_off = rela_size = rela_entsize = rela_link = None
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        (sh_name, sh_type, _f2, _ad, sh_offset, sh_size, sh_link,
         _info, _al, sh_entsize) = _st.unpack_from("<IIQQQQIIQQ", data, off)
        if sh_type == 2:  # SHT_SYMTAB
            soff = e_shoff + sh_link * e_shentsize
            (_a, _b, _c, _d, str_off, _e, _f3, _g, _h,
             _i) = _st.unpack_from("<IIQQQQIIQQ", data, soff)
            n = sh_size // sh_entsize
            for j in range(n):
                eoff = sh_offset + j * sh_entsize
                (st_name, _in, _ot, _sh, _sv,
                 _sz) = _st.unpack_from("<IBBHQQ", data, eoff)
                end = data.index(b"\x00", str_off + st_name)
                sym_names[j] = data[str_off + st_name:end].decode()
        elif sh_type == 4:  # SHT_RELA
            rela_off, rela_size, rela_entsize, rela_link = (
                sh_offset, sh_size, sh_entsize, sh_link)
    if rela_off is None:
        raise ValueError("no .rela.text in freestanding ELF")
    n = rela_size // rela_entsize
    patched = 0
    for j in range(n):
        (r_offset, r_info, r_addend) = _st.unpack_from(
            "<QQq", data, rela_off + j * rela_entsize)
        r_type, sym_idx = r_info & 0xFFFFFFFF, r_info >> 32
        name = sym_names.get(sym_idx, "?")
        if r_type != 1:  # R_X86_64_64
            raise ValueError(f"unexpected reloc type {r_type} for {name}")
        if name == "__orum_heap_start":
            val = HEAP_START
        elif name == "__orum_heap_end":
            val = HEAP_END
        else:
            raise ValueError(f"unexpected UND symbol {name}")
        foff = r_offset - base
        if not (0 <= foff <= len(flat) - 8):
            raise ValueError(f"reloc {name} at 0x{r_offset:x} outside flat image")
        _st.pack_into("<Q", flat, foff, val + r_addend)
        patched += 1
    if patched == 0:
        raise ValueError("no heap relocations found — heap would be dead")
    print(f"heap relocs: patched {patched} sites "
          f"(start=0x{HEAP_START:x}, end=0x{HEAP_END:x})")
    return flat


def build_boot_sector(stub_sectors, orum_sectors, low_sectors,
                      stub_bytes, orum_bytes):
    a = B16(0x7C00)

    # ---- setup ----
    a.emit(0xFA)                          # cli
    a.emit(0x31, 0xC0)                    # xor ax,ax
    a.emit(0x8E, 0xD8)                    # mov ds,ax
    a.emit(0x8E, 0xC0)                    # mov es,ax
    a.emit(0x8E, 0xD0)                    # mov ss,ax
    a.emit(0xBC, 0x00, 0x7C)              # mov sp,0x7C00
    a.emit(0xFB)                          # sti
    a.emit(0x88, 0x16, 0x00, 0x7E)        # mov [0x7E00],dl  (boot drive)

    # ---- VBE: set 1024x768x32 LFB ----
    a.emit(0xB8, 0x02, 0x4F)              # mov ax,0x4F02
    a.emit(0xBB, 0x18, 0x41)              # mov bx,0x4118
    a.emit(0xCD, 0x10)                    # int 0x10
    a.emit(0x3D, 0x4F, 0x00)              # cmp ax,0x004F
    a.jcc16(0x85, "fail1")                 # jne fail1
    a.emit(*serial("V"))

    # ---- VBE: mode info -> 0x8000 ----
    a.emit(0x31, 0xC0)                    # xor ax,ax
    a.emit(0x8E, 0xC0)                    # mov es,ax
    a.emit(0xB8, 0x01, 0x4F)              # mov ax,0x4F01
    a.emit(0xB9, 0x18, 0x41)              # mov cx,0x4118
    a.emit(0xBF, 0x00, 0x80)              # mov di,0x8000
    a.emit(0xCD, 0x10)                    # int 0x10
    a.emit(0x3D, 0x4F, 0x00)              # cmp ax,0x004F
    a.jcc16(0x85, "fail2")                 # jne fail2
    a.emit(*serial("I"))
    a.emit(0x31, 0xC0)                    # xor ax,ax
    a.emit(0x8E, 0xD8)                    # mov ds,ax (restore, BIOS may clobber)

    # ---- Bochs VBE: force 32bpp via I/O ports ----
    # QEMU SeaBIOS reports 24bpp for 0x4118; the Bochs VBE hardware
    # supports 32bpp. Set XRES/YRES/BPP via ports 0x1CE/0x1CF.
    # (Must set all three; touching BPP alone resets the resolution.)
    a.emit(0xBA, 0xCE, 0x01)              # mov dx,0x1CE
    a.emit(0xB8, 0x01, 0x00)              # mov ax,1 (XRES index)
    a.emit(0xEF)                          # out dx,ax
    a.emit(0xBA, 0xCF, 0x01)              # mov dx,0x1CF
    a.emit(0xB8, 0x00, 0x04)              # mov ax,1024
    a.emit(0xEF)                          # out dx,ax
    a.emit(0xBA, 0xCE, 0x01)              # mov dx,0x1CE
    a.emit(0xB8, 0x02, 0x00)              # mov ax,2 (YRES index)
    a.emit(0xEF)
    a.emit(0xBA, 0xCF, 0x01)
    a.emit(0xB8, 0x00, 0x03)              # mov ax,768
    a.emit(0xEF)
    a.emit(0xBA, 0xCE, 0x01)
    a.emit(0xB8, 0x03, 0x00)              # mov ax,3 (BPP index)
    a.emit(0xEF)
    a.emit(0xBA, 0xCF, 0x01)
    a.emit(0xB8, 0x20, 0x00)              # mov ax,32
    a.emit(0xEF)
    a.emit(0xBA, 0xCE, 0x01)
    a.emit(0xB8, 0x04, 0x00)              # mov ax,4 (ENABLE index)
    a.emit(0xEF)
    a.emit(0xBA, 0xCF, 0x01)
    a.emit(0xB8, 0x41, 0x00)              # mov ax,0x41 (enable+LFB)
    a.emit(0xEF)
    a.emit(*serial("B"))

    # ---- fake mb2 info @ 0x9000 ----
    a.emit(0xBF, 0x00, 0x90)              # mov di,0x9000
    a.emit(0x66, 0xC7, 0x05, 0x30, 0x00, 0x00, 0x00)      # mov dword [di],48
    a.emit(0x66, 0xC7, 0x45, 0x04, 0, 0, 0, 0)           # mov dword [di+4],0
    a.emit(0x66, 0xC7, 0x45, 0x08, 8, 0, 0, 0)           # mov dword [di+8],8
    a.emit(0x66, 0xC7, 0x45, 0x0C, 0x20, 0, 0, 0)        # mov dword [di+12],32
    a.emit(0x66, 0xA1, 0x28, 0x80)                       # mov eax,[0x8028]
    a.emit(0x66, 0x89, 0x45, 0x10)                       # mov [di+16],eax
    a.emit(0x66, 0xC7, 0x45, 0x14, 0, 0, 0, 0)           # mov dword [di+20],0
    # pitch: hardcoded 4096 (1024*4) for 32bpp; VBE info still says 24bpp
    a.emit(0x66, 0xC7, 0x45, 0x18, 0x00, 0x10, 0, 0)      # mov dword [di+24],4096
    a.emit(0x66, 0x0F, 0xB7, 0x06, 0x12, 0x80)           # movzx eax,word [0x8012]
    a.emit(0x66, 0x89, 0x45, 0x1C)                       # mov [di+28],eax
    a.emit(0x66, 0x0F, 0xB7, 0x06, 0x14, 0x80)           # movzx eax,word [0x8014]
    a.emit(0x66, 0x89, 0x45, 0x20)                       # mov [di+32],eax
    # bpp: hardcoded 32 (we forced it via Bochs ports)
    a.emit(0xC6, 0x45, 0x24, 0x20)                       # mov byte [di+36],32
    a.emit(0xC6, 0x45, 0x25, 0x01)                       # mov byte [di+37],1
    a.emit(0x66, 0xC7, 0x45, 0x26, 0, 0, 0, 0)           # mov dword [di+38],0
    a.emit(0xC7, 0x45, 0x28, 0x00, 0x00)                 # mov word [di+40],0 (end tag type)
    a.emit(0xC7, 0x45, 0x2A, 0x00, 0x00)                 # mov word [di+42],0 (end tag flags)
    a.emit(0x66, 0xC7, 0x45, 0x2C, 0x08, 0, 0, 0)        # mov dword [di+44],8 (end tag size)

    # ---- disk reads (int 0x13 AH=0x42) ----
    a.emit(0xBE); a.abs16("dap1")         # mov si,dap1
    a.emit(0x8A, 0x16, 0x00, 0x7E)        # mov dl,[0x7E00]
    a.emit(0xB4, 0x42)                    # mov ah,0x42
    a.emit(0xCD, 0x13)                    # int 0x13
    a.jcc16(0x82, "fail3")                 # jc fail3
    a.emit(*serial("1"))
    a.emit(0xBE); a.abs16("dap2")         # mov si,dap2
    a.emit(0x8A, 0x16, 0x00, 0x7E)
    a.emit(0xB4, 0x42)
    a.emit(0xCD, 0x13)
    a.jcc16(0x82, "fail3")
    a.emit(*serial("2"))
    # dap3: lowmem image -> 0x5000 directly (segment 0x0500:0x0000)
    # (no serial progress char here: boot sector is at the 510-byte limit;
    # the teletype fail codes still identify disk errors)
    a.emit(0xBE); a.abs16("dap3")         # mov si,dap3
    a.emit(0x8A, 0x16, 0x00, 0x7E)
    a.emit(0xB4, 0x42)
    a.emit(0xCD, 0x13)
    a.jcc16(0x82, "fail3")

    # ---- A20 ----
    a.emit(0xB8, 0x01, 0x24)              # mov ax,0x2401
    a.emit(0xCD, 0x15)                    # int 0x15
    a.emit(0xE4, 0x92)                    # in al,0x92
    a.emit(0x0C, 0x02)                    # or al,2
    a.emit(0xE6, 0x92)                    # out 0x92,al
    a.emit(*serial("A"))

    # ---- protected mode ----
    a.emit(0xFA)                          # cli
    a.emit(0x0F, 0x01, 0x16); a.abs16("gdtr")  # lgdt [gdtr]
    a.emit(0x0F, 0x20, 0xC0)              # mov eax,cr0
    a.emit(0x0C, 0x01)                    # or al,1
    a.emit(0x0F, 0x22, 0xC0)              # mov cr0,eax
    a.emit(0xEA); a.abs16("pm32"); a.dw(0x08)  # jmp 0x08:pm32

    # ---- pm32: 32-bit ----
    a.L("pm32")
    a.emit(0x66, 0xB8, 0x10, 0x00)        # mov ax,0x10
    a.emit(0x8E, 0xD8)                    # mov ds,ax
    a.emit(0x8E, 0xC0)                    # mov es,ax
    a.emit(0x8E, 0xD0)                    # mov ss,ax
    a.emit(0xBC, 0x00, 0x7C, 0x00, 0x00)  # mov esp,0x7C00
    a.emit(0xBA, 0xF8, 0x03, 0x00, 0x00)  # mov edx,0x3F8
    a.emit(0xB0, 0x50)                    # mov al,'P'
    a.emit(0xEE)                          # out dx,al
    # copy stub: 0x10000 -> 0x100000
    a.emit(0xBE, 0x00, 0x00, 0x01, 0x00)  # mov esi,0x10000
    a.emit(0xBF, 0x00, 0x00, 0x10, 0x00)  # mov edi,0x100000
    a.emit(0xB9); a.dd(stub_bytes // 4)   # mov ecx,count
    a.emit(0xF3, 0xA5)                    # rep movsd
    # copy orum: 0x10000+stub_bytes -> 0x400000
    a.emit(0xBE); a.dd(0x10000 + stub_bytes)
    a.emit(0xBF, 0x00, 0x00, 0x40, 0x00)  # mov edi,0x400000
    a.emit(0xB9); a.dd(orum_bytes // 4)
    a.emit(0xF3, 0xA5)
    a.emit(0xB0, 0x43)                    # mov al,'C'
    a.emit(0xEE)                          # out dx,al
    a.emit(0xBB, 0x00, 0x90, 0x00, 0x00)  # mov ebx,0x9000
    # jmp 0x08:0x100058 (trampoline)
    a.emit(0xEA, 0x58, 0x00, 0x10, 0x00, 0x08, 0x00)

    # ---- fail handlers ----
    a.L("fail1")
    a.emit(0xB0, 0x31)                    # mov al,'1'
    a.jcc8(0xEB, "fail")
    a.L("fail2")
    a.emit(0xB0, 0x32)
    a.jcc8(0xEB, "fail")
    a.L("fail3")
    a.emit(0xB0, 0x33)
    a.L("fail")
    a.emit(0xB4, 0x0E)                    # mov ah,0x0E
    a.emit(0xBB, 0x07, 0x00)              # mov bx,0x0007
    a.emit(0xCD, 0x10)                    # int 0x10
    a.emit(0xFA)                          # cli
    a.L("hang")
    a.emit(0xF4)                          # hlt
    a.emit(0xEB, 0xFE)                    # jmp hang

    # ---- data ----
    a.L("dap1")
    a.db(0x10, 0)
    a.dw(stub_sectors)
    a.dw(0x0000)
    a.dw(0x1000)                          # 0x10000
    a.dd(1); a.dd(0)                      # LBA 1
    a.L("dap2")
    a.db(0x10, 0)
    a.dw(orum_sectors)
    a.dw(stub_bytes & 0xFFFF)
    a.dw(0x1000)                          # 0x10000+stub_bytes
    lba2 = 1 + stub_sectors
    a.dd(lba2 & 0xFFFFFFFF); a.dd(0)
    a.L("dap3")
    a.db(0x10, 0)
    a.dw(low_sectors)
    a.dw(0x0000)                          # offset 0
    a.dw(0x0500)                          # segment 0x0500 -> linear 0x5000
    lba3 = 1 + stub_sectors + orum_sectors
    a.dd(lba3 & 0xFFFFFFFF); a.dd(0)
    a.L("gdt")
    a.dd(0)
    a.dd(0)                               # null
    # 0x08: 32-bit code, base 0, 4GB
    a.db(0xFF, 0xFF, 0x00, 0x00, 0x00, 0x9A, 0xCF, 0x00)
    # 0x10: 32-bit data, base 0, 4GB
    a.db(0xFF, 0xFF, 0x00, 0x00, 0x00, 0x92, 0xCF, 0x00)
    a.L("gdtr")
    a.dw(23)
    a.abs16("gdt")

    a.resolve()
    code = bytes(a.buf)
    if len(code) > 510:
        raise ValueError(f"boot sector too big: {len(code)} > 510")
    sec = bytearray(512)
    sec[0:len(code)] = code
    sec[510] = 0x55
    sec[511] = 0xAA
    return bytes(sec)


def build_stub_image(kmain_addr):
    """Build the trampoline stub image (loaded at 0x100000), reusing mkboot."""
    PT_BASE = 0x1000000
    PDPT_ADDR = 0x1001000
    PD_ADDRS = [0x1002000, 0x1003000, 0x1004000, 0x1005000]
    STACK_TOP = 0x100A000

    a = build_code(kmain_addr)
    code_len = len(a.buf)
    code_off = 0x58
    data_off = align_up(code_off + code_len, 8)
    d = {"mbi_save": data_off}
    data_off += 4
    data_off = align_up(data_off, 8)
    d["gdt"] = data_off
    data_off += 32
    d["gdtr"] = data_off
    data_off += 6
    stub_len = align_up(data_off, 16)
    d["pml4"] = PT_BASE - STUB_BASE
    d["pdpt"] = PDPT_ADDR - STUB_BASE
    for k in range(4):
        d[f"pd{k}"] = PD_ADDRS[k] - STUB_BASE
        d[f"pdpt{k}"] = (PDPT_ADDR + k * 8) - STUB_BASE
    d["stack_top"] = STACK_TOP - STUB_BASE

    a = build_code(kmain_addr)
    addrs = {k: STUB_BASE + v for k, v in d.items()}
    a.resolve(addrs, {})

    stub = bytearray(stub_len)
    stub[code_off:code_off + len(a.buf)] = a.buf
    g = gdt_bytes()
    stub[d["gdt"]:d["gdt"] + 32] = g
    struct.pack_into("<H", stub, d["gdtr"], 31)
    struct.pack_into("<I", stub, d["gdtr"] + 2, STUB_BASE + d["gdt"])
    return bytes(stub)


def main():
    print("== checking kernel string addresses ==")
    ksrc = open(KERNEL_SRC).read()
    for _name, _addr in sorted(
            [(n, v) for n, v in list(globals().items())
             if n.startswith(("S_", "C_")) and isinstance(v, int)]):
        if f"0x{_addr:x}" not in ksrc and f"0x{_addr:X}" not in ksrc:
            raise ValueError(
                f"kernel source missing address 0x{_addr:x} for {_name} — "
                f"update kmain.orum")
    print("string addresses: all present in kmain.orum")

    print("== building menagerie kernel ==")
    src = open(KERNEL_SRC).read()
    build_binary(src, TMP_ELF, freestanding=True, entry="kmain")
    orum_data, orum_loads, kmain = parse_elf(TMP_ELF)
    on_irq = find_on_irq(orum_data, orum_loads)
    print(f"orum: {len(orum_loads)} PT_LOADs, kmain=0x{kmain:x}, on_irq=0x{on_irq:x}")

    print("== building stub image ==")
    stub = build_stub_image(kmain)
    print(f"stub: {len(stub)} bytes")

    print("== building lowmem image ==")
    lowmem = build_lowmem(on_irq)
    print(f"lowmem: {len(lowmem)} bytes -> 0x{LOWMEM_BASE:x}")

    print("== building orum flat image ==")
    base = orum_loads[0]["vaddr"]
    flat_len = (orum_loads[-1]["vaddr"] + orum_loads[-1]["memsz"]) - base
    flat = bytearray(flat_len)
    for seg in orum_loads:
        off = seg["vaddr"] - base
        flat[off:off + seg["filesz"]] = orum_data[seg["offset"]:seg["offset"] + seg["filesz"]]
    print(f"orum flat: {len(flat)} bytes at 0x{base:x}")

    print("== resolving heap relocations ==")
    flat = apply_heap_relocs(flat, base, TMP_ELF)

    def pad_up(b):
        n = (len(b) + 511) // 512
        return bytes(b) + b"\x00" * (n * 512 - len(b)), n

    stub_padded, stub_sectors = pad_up(stub)
    orum_padded, orum_sectors = pad_up(bytes(flat))
    low_padded, low_sectors = pad_up(lowmem)
    stub_bytes = stub_sectors * 512
    orum_bytes = orum_sectors * 512
    print(f"stub: {stub_sectors} sectors, orum: {orum_sectors} sectors, "
          f"lowmem: {low_sectors} sectors")

    print("== assembling boot sector ==")
    sector = build_boot_sector(stub_sectors, orum_sectors, low_sectors,
                               stub_bytes, orum_bytes)
    print(f"boot sector: {len(sector)} bytes, signature "
          f"{sector[510]:#x}{sector[511]:#x}")

    print("== writing disk image ==")
    disk = bytearray()
    disk += sector
    disk += stub_padded
    disk += orum_padded
    disk += low_padded
    open(DISK_IMG, "wb").write(bytes(disk))
    print(f"wrote {DISK_IMG}: {len(disk)} bytes "
          f"({len(disk)//512} sectors)")
    return DISK_IMG


if __name__ == "__main__":
    main()
