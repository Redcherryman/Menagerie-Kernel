#!/usr/bin/env python3
"""mkboot.py — build a bootable multiboot2 ELF from an Orum freestanding ELF.

LOCAL-ONLY scaffolding for the Orum framebuffer test. Never pushed anywhere.

QEMU `-kernel` boots multiboot images in 32-bit protected mode, but Orum
emits 64-bit code. This script:
  1. Reads the Orum freestanding ELF (built with --freestanding --entry kmain).
  2. Builds a stub image at 0x100000 containing:
     - a multiboot2 header (magic 0xE85250D6, arch 0, framebuffer tag
       requesting 1024x768x32),
     - a 32-bit trampoline (own stack, GDT, identity page tables for the
       low 4GB with 2MB pages, PAE + EFER.LME + paging, far jump to 64-bit),
     - 64-bit glue that passes the multiboot2 info pointer (ebx, zero-extended)
       in rdi and calls Orum's kmain, then halts.
  3. Emits a single ELF whose entry is the stub, with the Orum PT_LOAD
     segments copied at their original vaddrs.

Usage: mkboot.py <orum-freestanding.elf> <out-boot.elf>
"""

import struct
import sys

STUB_BASE = 0x100000


# --------------------------------------------------------------------------
# tiny two-pass assembler for the stub
# --------------------------------------------------------------------------
class Asm:
    def __init__(self, base):
        self.base = base
        self.buf = bytearray()
        self.labels = {}
        self.fixups = []  # (buf_off, kind, label, extra)

    def label(self, name):
        self.labels[name] = len(self.buf)

    def emit(self, b):
        self.buf += b

    def u8(self, v):
        self.buf += struct.pack("<B", v & 0xFF)

    def u16(self, v):
        self.buf += struct.pack("<H", v & 0xFFFF)

    def u32(self, v):
        self.buf += struct.pack("<I", v & 0xFFFFFFFF)

    def u64(self, v):
        self.buf += struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)

    # absolute 32-bit address of a label (for imm32/disp32)
    def abs32(self, label, or_mask=0):
        self.fixups.append((len(self.buf), "abs32", label, or_mask))
        self.u32(0)

    # 8-bit relative jump to a label
    def jcc8(self, opcode, label):
        self.fixups.append((len(self.buf) + 1, "rel8", label, 0))
        self.u8(opcode)
        self.u8(0)

    # RIP-relative 32-bit displacement to a label (for 8B 3D / disp32 in 64-bit)
    def rip32(self, label):
        self.fixups.append((len(self.buf), "rip32", label, 0))
        self.u32(0)

    def resolve(self, data_addrs, extra_vals):
        # data_addrs: name -> absolute address; extra_vals: name -> int
        addrs = dict(data_addrs)
        addrs.update(extra_vals)
        addrs.update({k: self.base + v for k, v in self.labels.items()})
        for off, kind, label, extra in self.fixups:
            if label not in addrs:
                raise ValueError(f"unknown label for fixup: {label}")
            target = addrs[label]
            if kind == "abs32":
                struct.pack_into("<I", self.buf, off, (target | extra) & 0xFFFFFFFF)
            elif kind == "rel8":
                # rel8 is relative to the end of the 2-byte jcc instruction
                rel = target - (self.base + off + 1)
                if not -128 <= rel <= 127:
                    raise ValueError(f"rel8 out of range for {label}: {rel}")
                struct.pack_into("<b", self.buf, off, rel)
            elif kind == "rip32":
                # rel32 relative to end of the 6-byte instruction (8B 3D + disp32)
                rel = target - (self.base + off + 4)
                struct.pack_into("<i", self.buf, off, rel)
            else:
                raise ValueError(kind)


def align_up(n, a):
    return (n + a - 1) & ~(a - 1)


def build_code(kmain_addr):
    """Assemble the 32-bit trampoline + 64-bit glue. Returns (code, labels)."""
    a = Asm(STUB_BASE + 0x58)  # code starts after mb2 header at 0x18

    # ---- 32-bit trampoline ----
    a.emit(b"\xFA")                       # cli
    a.emit(b"\x89\x1D")                   # mov [mbi_save], ebx
    a.abs32("mbi_save")
    a.emit(b"\xBC")                       # mov esp, STACK_TOP
    a.abs32("stack_top")

    # PML4[0] = PDPT | 3   (C7 05 disp32 imm32)
    a.emit(b"\xC7\x05")
    a.abs32("pml4")
    a.abs32("pdpt", or_mask=0x3)
    # PDPT[k] = PDk | 3
    for k in range(4):
        a.emit(b"\xC7\x05")
        # disp32 = pdpt + k*8 ; can't add in abs32, so use a label per entry
        a.abs32(f"pdpt{k}")
        a.abs32(f"pd{k}", or_mask=0x3)

    # fill 4 PDs with 2MB pages: 2048 entries
    a.emit(b"\xBF")                       # mov edi, PD0
    a.abs32("pd0")
    a.emit(b"\xB8")                       # mov eax, 0x83
    a.u32(0x83)
    a.emit(b"\xB9")                       # mov ecx, 2048
    a.u32(2048)
    a.label("fill_loop")
    a.emit(b"\x89\x07")                   # mov [edi], eax
    a.emit(b"\x83\xC7\x08")               # add edi, 8
    a.emit(b"\x05")                       # add eax, 0x200000
    a.u32(0x200000)
    a.emit(b"\x49")                       # dec ecx
    a.jcc8(0x75, "fill_loop")             # jnz fill_loop

    a.emit(b"\x0F\x01\x15")               # lgdt [gdtr]
    a.abs32("gdtr")
    a.emit(b"\x0F\x20\xE0")               # mov eax, cr4
    a.emit(b"\x83\xC8\x20")               # or eax, 0x20 (PAE)
    a.emit(b"\x0F\x22\xE0")               # mov cr4, eax
    a.emit(b"\xB9")                       # mov ecx, 0xC0000080 (EFER)
    a.u32(0xC0000080)
    a.emit(b"\x0F\x32")                   # rdmsr
    a.emit(b"\x0D")                       # or eax, 0x100 (LME)
    a.u32(0x100)
    a.emit(b"\x0F\x30")                   # wrmsr
    a.emit(b"\xB8")                       # mov eax, PML4
    a.abs32("pml4")
    a.emit(b"\x0F\x22\xD8")               # mov cr3, eax
    a.emit(b"\x0F\x20\xC0")               # mov eax, cr0
    a.emit(b"\x0D")                       # or eax, 0x80000000 (PG)
    a.u32(0x80000000)
    a.emit(b"\x0F\x22\xC0")               # mov cr0, eax
    a.emit(b"\xEA")                       # far jmp 0x18:code64
    a.abs32("code64")
    a.u16(0x18)

    # ---- 64-bit glue ----
    a.label("code64")
    a.emit(b"\x66\x31\xC0")               # xor ax, ax
    a.emit(b"\x8E\xD8")                   # mov ds, ax
    a.emit(b"\x8E\xC0")                   # mov es, ax
    a.emit(b"\x8E\xD0")                   # mov ss, ax
    a.emit(b"\xBC")                       # mov esp, STACK_TOP (zero-extends)
    a.abs32("stack_top")
    a.emit(b"\x8B\x3D")                   # mov edi, [mbi_save] (RIP-relative)
    a.rip32("mbi_save")
    a.emit(b"\x48\xB8")                   # mov rax, KMAIN
    a.u64(kmain_addr)
    a.emit(b"\xFF\xD0")                   # call rax
    a.emit(b"\xFA")                       # cli
    a.label("halt")
    a.emit(b"\xF4")                       # hlt
    a.jcc8(0xEB, "halt")                  # jmp halt

    return a


def multiboot2_header():
    h = bytearray()
    magic = 0xE85250D6
    arch = 0
    # fb tag (20B) + 4B pad + end tag (8B) = 32; total header 16 + 32 = 48
    hlen = 48
    checksum = (0x100000000 - (magic + arch + hlen)) & 0xFFFFFFFF
    h += struct.pack("<IIII", magic, arch, hlen, checksum)
    # framebuffer tag: type 5, flags 0, size 20, 1024x768x32
    h += struct.pack("<HHIIII", 5, 0, 20, 1024, 768, 32)
    h += b"\x00" * 4                      # pad so end tag is 8-aligned
    h += struct.pack("<HHI", 0, 0, 8)     # end tag
    assert len(h) == 48, len(h)
    return bytes(h)


def gdt_bytes():
    gdt = bytearray()
    gdt += b"\x00" * 8                                    # null
    gdt += bytes([0xFF, 0xFF, 0x00, 0x00, 0x00, 0x9A, 0xCF, 0x00])  # 0x08 32-bit code
    gdt += bytes([0xFF, 0xFF, 0x00, 0x00, 0x00, 0x92, 0xCF, 0x00])  # 0x10 32-bit data
    gdt += bytes([0xFF, 0xFF, 0x00, 0x00, 0x00, 0x9A, 0xAF, 0x00])  # 0x18 64-bit code
    assert len(gdt) == 32
    return bytes(gdt)


# --------------------------------------------------------------------------
# ELF helpers
# --------------------------------------------------------------------------
def parse_elf(path):
    data = open(path, "rb").read()
    assert data[:4] == b"\x7fELF" and data[4] == 2, "not a 64-bit ELF"
    (e_phoff, e_shoff, _flags, _ehsize, e_phentsize, e_phnum,
     e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<QQIHHHHHH", data, 0x20)
    loads = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        (p_type, p_flags, p_offset, p_vaddr, _p_paddr,
         p_filesz, p_memsz, p_align) = struct.unpack_from("<IIQQQQQQ", data, off)
        if p_type == 1:  # PT_LOAD
            loads.append(dict(offset=p_offset, vaddr=p_vaddr, filesz=p_filesz,
                              memsz=p_memsz, flags=p_flags, align=p_align))
    # find kmain in symtab
    kmain = None
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        (sh_name, sh_type, _fl, _ad, sh_offset, sh_size, sh_link,
         _info, _al, sh_entsize) = struct.unpack_from("<IIQQQQIIQQ", data, off)
        if sh_type == 2:  # SHT_SYMTAB
            n = sh_size // sh_entsize
            # linked strtab
            soff = e_shoff + sh_link * e_shentsize
            (_n2, _t2, _f2, _a2, str_off, _sz2, _l2, _i2, _al2,
             _e2) = struct.unpack_from("<IIQQQQIIQQ", data, soff)
            for j in range(n):
                eoff = sh_offset + j * sh_entsize
                (st_name, _inf, _oth, _shn, st_value,
                 _sz) = struct.unpack_from("<IBBHQQ", data, eoff)
                end = data.index(b"\x00", str_off + st_name)
                name = data[str_off + st_name:end].decode()
                if name == "kmain":
                    kmain = st_value
    if kmain is None:
        raise ValueError("kmain symbol not found in " + path)
    return data, loads, kmain


def write_boot_elf(out_path, stub_img, orum_data, orum_loads):
    # file layout: ELF hdr (64) + phdrs, then stub at file offset 0 of its
    # own region... simpler: stub image starts the file content region.
    # We place: [ELF hdr][phdrs][pad to 0x1000][stub][pad to 0x1000][orum segs...]
    # Note: Orum segments are merged into a single PHDR (see below), so n_phdr
    # is 2 if there are Orum segments, else 1.
    n_phdr = 2 if orum_loads else 1
    phoff = 52
    stub_off = align_up(phoff + n_phdr * 32, 0x1000)
    out = bytearray()
    out += b"\x00" * stub_off
    out[stub_off:stub_off + len(stub_img)] = stub_img
    cur = align_up(stub_off + len(stub_img), 0x1000)
    seg_offs = []
    for seg in orum_loads:
        # keep p_offset % align == p_vaddr % align (both 0x1000-aligned here)
        assert seg["vaddr"] % 0x1000 == 0
        out += b"\x00" * (cur - len(out))
        seg_offs.append(cur)
        out[cur:cur + seg["filesz"]] = orum_data[seg["offset"]:seg["offset"] + seg["filesz"]]
        cur += align_up(seg["filesz"], 0x1000)

    def phdr(p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz, p_align):
        # ELF32 Phdr: type, offset, vaddr, paddr, filesz, memsz, flags, align
        return struct.pack("<IIIIIIII", p_type, p_offset, p_vaddr,
                           p_vaddr, p_filesz, p_memsz, p_flags, p_align)

    phdrs = b""
    phdrs += phdr(1, 7, stub_off, STUB_BASE, len(stub_img), len(stub_img), 0x1000)
    # Merge Orum segments into a single PHDR to avoid GRUB bug with adjacent LOADs.
    # The Orum compiler emits text at 0x400000 and data at 0x401000 (adjacent);
    # GRUB 2.12 fails to boot when LOADs are contiguous. We combine them into one
    # RWE segment covering the full range, preserving vaddrs.
    if orum_loads:
        first = orum_loads[0]
        last = orum_loads[-1]
        merged_vaddr = first["vaddr"]
        merged_end = last["vaddr"] + last["memsz"]
        merged_memsz = merged_end - merged_vaddr
        # Build merged image: copy each segment to its offset within the merged range
        merged_filesz = 0
        merged_img = bytearray()
        # We need file offsets; seg_offs has them. Find max end.
        max_file_end = 0
        for seg, soff in zip(orum_loads, seg_offs):
            seg_file_end = soff + seg["filesz"]
            if seg_file_end > max_file_end:
                max_file_end = seg_file_end
        # The merged segment's file data: from first seg_off to max_file_end,
        # but we need to handle gaps. Simpler: create a contiguous image.
        # For now, assume segments are contiguous in file (they are, via align_up).
        merged_off = seg_offs[0]
        merged_filesz = max_file_end - merged_off
        # Use RWE (7) to cover both text (R-E) and data (RW)
        phdrs += phdr(1, 7, merged_off, merged_vaddr, merged_filesz,
                      merged_memsz, 0x1000)
    # (old per-segment loop removed)

    # Section headers: NULL + .multiboot (so GRUB finds the mb2 header)
    # .multiboot section: header at stub file offset 0 -> vaddr STUB_BASE
    shoff = align_up(len(out), 16)
    out += b"\x00" * (shoff - len(out))
    # Elf32_Shdr: name, type, flags, addr, offset, size, link, info, addralign, entsize
    shdrs = bytearray()
    shdrs += struct.pack("<IIIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)  # NULL
    shdrs += struct.pack("<IIIIIIIIII",
        1,          # sh_name (offset in shstrtab)
        1,          # sh_type = PROGBITS
        0x2,        # sh_flags = ALLOC
        STUB_BASE,  # sh_addr: mb2 header vaddr
        stub_off,   # sh_offset: mb2 header file offset
        48,         # sh_size
        0, 0, 8, 0)
    # shstrtab section header (third)
    shdrs += struct.pack("<IIIIIIIIII",
        12,         # sh_name: offset of ".shstrtab" in shstrtab
        3,          # sh_type = STRTAB
        0,          # sh_flags
        0,          # sh_addr
        0,          # sh_offset (patched below)
        0,          # sh_size (patched below)
        0, 0, 1, 0)
    shstr_off = shoff + len(shdrs)
    shstr = b"\x00.multiboot\x00.shstrtab\x00"
    # patch shstrtab header offset/size
    struct.pack_into("<II", shdrs, 2*40 + 16, shstr_off, len(shstr))
    out[shoff:shoff+len(shdrs)] = shdrs
    out += b"\x00" * (shstr_off - len(out))
    out[shstr_off:shstr_off+len(shstr)] = shstr

    ehdr = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + b"\x00" * 9,  # ident: ELF32
        2,      # ET_EXEC
        3,      # EM_386 (GRUB mb2 boots ELF32 reliably; entry is 32-bit code)
        1,      # version
        STUB_BASE + 0x58,  # e_entry: the stub code (header at 0x18)
        phoff,
        shoff,  # e_shoff
        0,      # flags
        52,     # e_ehsize
        32,     # e_phentsize
        n_phdr,
        40,     # e_shentsize
        3,      # e_shnum
        2,      # e_shstrndx
    )
    out[0:len(ehdr)] = ehdr
    out[phoff:phoff + len(phdrs)] = phdrs
    open(out_path, "wb").write(bytes(out))
    print(f"wrote {out_path}: {len(out)} bytes, entry=0x{STUB_BASE:x}, "
          f"{len(orum_loads)} orum segments")


def main():
    orum_path, out_path = sys.argv[1], sys.argv[2]
    orum_data, orum_loads, kmain = parse_elf(orum_path)
    print(f"orum: {len(orum_loads)} PT_LOADs, kmain=0x{kmain:x}")

    # --- assemble stub (two passes: measure, then place data) ---
    # NOTE: GRUB's multiboot2 loader fails on LOAD segments >4KB (tested).
    # Keep the stub image small (<4KB). Page tables and stack live at fixed
    # high addresses (16MB+), NOT in the ELF — they're just zeroed RAM.
    PT_BASE = 0x1000000      # 16MB: PML4
    PDPT_ADDR = 0x1001000
    PD_ADDRS = [0x1002000, 0x1003000, 0x1004000, 0x1005000]
    STACK_TOP = 0x100A000    # 16KB stack at 0x1006000-0x100A000

    a = build_code(kmain)
    code_len = len(a.buf)
    code_off = 0x58
    data_off = align_up(code_off + code_len, 8)
    # data layout (only small items in the stub image)
    d = {}
    d["mbi_save"] = data_off
    data_off += 4
    data_off = align_up(data_off, 8)
    d["gdt"] = data_off
    data_off += 32
    d["gdtr"] = data_off
    data_off += 6
    stub_len = align_up(data_off, 16)
    # fixed high addresses (not in ELF)
    d["pml4"] = PT_BASE - STUB_BASE          # store as offset, resolved below
    d["pdpt"] = PDPT_ADDR - STUB_BASE
    for k in range(4):
        d[f"pd{k}"] = PD_ADDRS[k] - STUB_BASE
        d[f"pdpt{k}"] = (PDPT_ADDR + k * 8) - STUB_BASE
    d["stack_top"] = STACK_TOP - STUB_BASE

    # second pass with real addresses
    a = build_code(kmain)
    # resolve: stub-relative for small data, absolute for high addresses
    addrs = {}
    for k, v in d.items():
        if k in ("pml4", "pdpt", "stack_top") or k.startswith("pd"):
            addrs[k] = STUB_BASE + v  # = fixed high address
        else:
            addrs[k] = STUB_BASE + v
    a.resolve(addrs, {})

    stub = bytearray(stub_len)
    stub[0x18:0x18+48] = multiboot2_header()
    stub[code_off:code_off + len(a.buf)] = a.buf
    # data area: gdt + gdtr (d[] are offsets from STUB_BASE)
    g = gdt_bytes()
    stub[d["gdt"]:d["gdt"] + 32] = g
    struct.pack_into("<H", stub, d["gdtr"], 31)
    struct.pack_into("<I", stub, d["gdtr"] + 2, STUB_BASE + d["gdt"])
    write_boot_elf(out_path, bytes(stub), orum_data, orum_loads)


if __name__ == "__main__":
    main()
