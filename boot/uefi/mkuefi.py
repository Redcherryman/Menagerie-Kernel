#!/usr/bin/env python3
"""mkuefi.py — build the Menagerie UEFI loader (PE32+ .efi).

Assembles a PE32+ EFI application by hand (no toolchain dependency),
embedding the kernel flat image and lowmem image as data sections.
The loader: finds GOP, sets 1024x768x32, copies kernel -> 0x400000 and
lowmem -> 0x5000, builds the SAME 48-byte boot info block the BIOS
loader uses (the kernel is unchanged), calls ExitBootServices(),
installs the trampoline-identical GDT, and jumps to kmain in 64-bit
mode (UEFI is already 64-bit — no 32->64 trampoline needed).

Also builds FAT16 disk images carrying /EFI/BOOT/BOOTX64.EFI.
"""
import os
import struct
import sys

MENAG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
ORUM_DIR = os.path.expanduser("~/workspace/orum")
sys.path.insert(0, ORUM_DIR)
sys.path.insert(0, MENAG_DIR)

from orum.driver import build_binary
from boot.mkboot import parse_elf
from boot import mkcustom

UEFI_DIR = os.path.join(MENAG_DIR, "boot", "uefi")
EFI_OUT = os.path.join(UEFI_DIR, "BOOTX64.EFI")
QEMU_IMG = os.path.join(UEFI_DIR, "uefi-test.img")
USB_IMG = os.path.join(MENAG_DIR, "menagerie-usb.img")

# --------------------------------------------------------------------------
# tiny x86-64 assembler (labels + RIP-relative fixups)
# --------------------------------------------------------------------------

# register numbers
RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = 0, 1, 2, 3, 4, 5, 6, 7
R8, R9, R10, R11, R12, R13, R14, R15 = 8, 9, 10, 11, 12, 13, 14, 15


from .asm64 import A64, RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI, R8, R9, R10, R11, R12, R13, R14, R15


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def build_kernel_flat():
    """Compile kmain.orum and return (flat_bytes, kmain_phys, on_irq_phys)."""
    print("== building menagerie kernel ==")
    src = open(mkcustom.KERNEL_SRC).read()
    build_binary(src, "/tmp/menagerie-uefi.elf", freestanding=True,
                 entry="kmain")
    orum_data, orum_loads, kmain = parse_elf("/tmp/menagerie-uefi.elf")
    on_irq = mkcustom.find_on_irq(orum_data, orum_loads)
    base = orum_loads[0]["vaddr"]
    flat_len = (orum_loads[-1]["vaddr"] + orum_loads[-1]["memsz"]) - base
    flat = bytearray(flat_len)
    for seg in orum_loads:
        off = seg["vaddr"] - base
        flat[off:off + seg["filesz"]] = orum_data[seg["offset"]:
                                                  seg["offset"] + seg["filesz"]]
    flat = mkcustom.apply_heap_relocs(flat, base, "/tmp/menagerie-uefi.elf")
    kmain_phys = 0x400000 + (kmain - base)
    on_irq_phys = 0x400000 + (on_irq - base)
    print(f"orum flat: {len(flat)} bytes, kmain=0x{kmain_phys:x}, "
          f"on_irq=0x{on_irq_phys:x}")
    assert base == 0x400000, f"kernel linked at 0x{base:x}, want 0x400000"
    return bytes(flat), kmain_phys, on_irq_phys


def build_efi():
    """Assemble the full BOOTX64.EFI. Returns path."""
    from .loader_code import build_loader
    from .pe_fat import build_pe, align_up, SECT_ALIGN

    flat, kmain_phys, on_irq_phys = build_kernel_flat()
    lowmem = mkcustom.build_lowmem(on_irq_phys)
    print(f"lowmem: {len(lowmem)} bytes")

    pages_kernel = align_up(len(flat), 4096) // 4096
    pages_lowmem = align_up(len(lowmem), 4096) // 4096

    # two passes: RVAs depend on .text size, which is stable across passes
    text_rva = SECT_ALIGN
    t1, e1 = build_loader(kmain_phys, len(flat), len(lowmem),
                          pages_kernel, pages_lowmem,
                          text_rva, 0, 0)
    kdata_rva = text_rva + align_up(len(t1), SECT_ALIGN)
    ldata_rva = kdata_rva + align_up(len(flat), SECT_ALIGN)
    text, entry_off = build_loader(kmain_phys, len(flat), len(lowmem),
                                   pages_kernel, pages_lowmem,
                                   text_rva, kdata_rva, ldata_rva)
    assert len(text) == len(t1), "loader size unstable across passes"

    pe, tr, kr, lr = build_pe(text, flat, lowmem, entry_off)
    assert tr == text_rva and kr == kdata_rva and lr == ldata_rva
    open(EFI_OUT, "wb").write(pe)
    print(f"wrote {EFI_OUT}: {len(pe)} bytes "
          f"(text {len(text)}B, kdata {len(flat)}B, ldata {len(lowmem)}B)")
    return EFI_OUT


def build_images():
    """Build the .efi plus QEMU test image and USB image. Returns paths."""
    from .pe_fat import build_uefi_disk
    efi = build_efi()
    efi_bytes = open(efi, "rb").read()

    qemu_img = build_uefi_disk(efi_bytes, 32, 8)
    open(QEMU_IMG, "wb").write(qemu_img)
    print(f"wrote {QEMU_IMG}: {len(qemu_img)} bytes (32MB FAT16)")

    readme = open(os.path.join(MENAG_DIR, "USB-README.md"), "rb").read()
    usb_img = build_uefi_disk(efi_bytes, 128, 16,
                            extra_files=[("README  TXT", readme)])
    open(USB_IMG, "wb").write(usb_img)
    print(f"wrote {USB_IMG}: {len(usb_img)} bytes (128MB FAT16)")
    return efi, QEMU_IMG, USB_IMG


def main():
    return build_images()


if __name__ == "__main__":
    main()
