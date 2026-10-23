#!/usr/bin/env python3
"""orum_pe.py — Compile Orum source to UEFI PE32+.

Bridge until Orum has native PE output:
1. Compiles .orum with orumc --freestanding.
2. Extracts .text from the ELF.
3. Builds PE32+ via pe_fat.build_pe.
"""
import os
import sys
import struct

ORUM_DIR = os.path.expanduser("~/workspace/orum")
MENAG_DIR = os.path.expanduser("~/workspace/menagerie")
sys.path.insert(0, ORUM_DIR)
sys.path.insert(0, MENAG_DIR)

from orum.driver import compile_source
from boot.uefi.pe_fat import build_pe, SECT_ALIGN, align_up


def orum_to_pe(source_path, entry="main"):
    """Compile Orum file to PE32+ bytes. Returns (pe_bytes, text_rva)."""
    src = open(source_path).read()
    elf_bytes = compile_source(src, freestanding=True, entry=entry)

    # Parse ELF to find .text
    e_shoff = struct.unpack('<Q', elf_bytes[0x28:0x30])[0]
    e_shentsize = struct.unpack('<H', elf_bytes[0x3A:0x3C])[0]
    e_shnum = struct.unpack('<H', elf_bytes[0x3C:0x3E])[0]
    e_shstrndx = struct.unpack('<H', elf_bytes[0x3E:0x40])[0]
    sh = e_shoff + e_shstrndx * e_shentsize
    sh_offset = struct.unpack('<Q', elf_bytes[sh+24:sh+32])[0]
    shstr = elf_bytes[sh_offset:]

    text_bytes = None
    entry_off = 0
    for i in range(e_shnum):
        sh = e_shoff + i * e_shentsize
        name_off = struct.unpack('<I', elf_bytes[sh:sh+4])[0]
        name = shstr[name_off:].split(b'\0')[0]
        if name == b'.text':
            offset = struct.unpack('<Q', elf_bytes[sh+24:sh+32])[0]
            size = struct.unpack('<Q', elf_bytes[sh+32:sh+40])[0]
            text_bytes = elf_bytes[offset:offset+size]
            break

    if text_bytes is None:
        raise ValueError("No .text in ELF")

    # Find entry offset: look for the entry symbol in symtab?
    # For now, assume entry is at start of .text (Orum puts entry first)
    # TODO: parse symtab for exact entry offset
    entry_off = 0

    pe, tr, kr, lr = build_pe(text_bytes, b'', b'', entry_off)
    return pe


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help=".orum file")
    ap.add_argument("-o", "--output", required=True, help="output .efi")
    ap.add_argument("--entry", default="main", help="entry symbol")
    args = ap.parse_args()
    pe = orum_to_pe(args.source, entry=args.entry)
    open(args.output, "wb").write(pe)
    print(f"wrote {args.output}: {len(pe)} bytes")
