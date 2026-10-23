# Menagerie UEFI loader (v0.3)

A PE32+ EFI application (`boot/uefi/BOOTX64.EFI`) that boots the
Menagerie kernel on UEFI firmware — the bridge from QEMU to Aiden's
MacBook Air. Hand-assembled in Python (`boot/uefi/`), no toolchain.

## Layout

- `boot/uefi/asm64.py` — tiny x86-64 assembler (labels, RIP-relative
  fixups; fixed a real bug: rel8 placeholders were 4 bytes, left 3
  stray zeros on fall-through).
- `boot/uefi/loader_code.py` — the loader itself (`efi_main` + data).
- `boot/uefi/pe_fat.py` — PE32+ assembly, FAT16 + MBR builders.
- `boot/uefi/mkuefi.py` — driver: compiles the kernel, builds the
  .efi, writes the QEMU test image and the USB image.
- `boot/uefi/prove_uefi.py` — QEMU/OVMF proof (mirrors prove_v02.py).

## PE32+

- `ImageBase 0x200000`, no relocations (UEFI loads at ImageBase).
- Subsystem 10 (EFI_APPLICATION), 3 sections:
  - `.text` — loader code (RIP-relative throughout).
  - `.kdata` — kernel flat image → copied to **0x400000**.
  - `.ldata` — lowmem image → copied to **0x5000**.
- Entry: `efi_main(ImageHandle, SystemTable)`, MS x64 ABI.

## What the loader does

1. `LocateProtocol` → GOP. Scans modes for **1024x768, 32bpp,
   PixelFormat BGRX**; `SetMode`. Fatal (serial + ConOut) if absent.
2. `AllocatePages(AllocateAddress)`: kernel → 0x400000 (LoaderCode),
   lowmem → 0x5000, info → 0x9000, heap → 0x2000000 (16MB),
   stack → 0x1500000 (64KB), all LoaderData except the kernel.
3. `rep movsb` the two images into place (RIP-relative sources).
4. Builds the **same 48-byte info block** as the BIOS loader at
   0x9000 (framebuffer tag type 8 + end tag). The kernel is unchanged.
5. `GetMemoryMap` → `ExitBootServices` (retry loop on stale MapKey).
6. `cli`; patches and loads a GDT identical to the trampoline's
   (0x18 = 64-bit code, 0x20 = data — the kernel's IDT hardcodes 0x18);
   far-jumps via `retfq`; sets `rsp = 0x1510000`, `rdi = 0x9000`;
   jumps to `kmain` (0x400198).
7. Debug output the whole way on serial 0x3F8 (`UEFI: ...`) and on
   ConOut for the big milestones (visible on real hardware).

## Boot contract deltas vs BIOS

- UEFI is already 64-bit: **no trampoline**. The loader installs the
  GDT itself so the kernel's 0x18 selector keeps working.
- Paging is UEFI's own identity map (4KB pages, all RAM) — the kernel
  never installs its own tables, same as before.
- Everything else in `docs/boot-contract.md` holds: info block layout,
  lowmem map, IDT/PIC/PIT/keyboard all port-I/O.

## Disk images

- QEMU test: 32MB, MBR (type 0xEF) + FAT16 at LBA 2048,
  `/EFI/BOOT/BOOTX64.EFI`.
- USB: 128MB, same layout + `README.txt`, at `menagerie-usb.img`.
- Flashing + real-hardware notes: `USB-README.md`.

## Proof

`boot/prove_uefi.py`: boots QEMU with OVMF (`-drive if=pflash`
CODE+VARS), asserts loader serial milestones, kernel banner, heap,
keyboard via `sendkey`, help, strictly increasing ticks, and saves a
**closeup** screenshot (`shots/v0003-uefi-shell.png`, console area
cropped and 2x-scaled per Aiden's feedback).

## Status (Oct 22, 2026)

**BLOCKED at QEMU/OVMF integration.** The loader, PE, FAT, MBR, and GPT
are all verified correct by independent tools (objdump, mtools, manual
byte-level parsing), but OVMF 2024.02 (Ubuntu) consistently reports
`BdsDxe: failed to load Boot0001 ... Not Found` — it sees the disk but
won't load `\EFI\BOOT\BOOTX64.EFI`.

Tried: IDE/SATA/virtio/USB transports, MBR (0xEF) and GPT (ESP GUID),
active flag, CHS values, disk signature, FAT16 (ours) and FAT32
(mkfs.fat), raw superfloppy, tiny test PE, full loader PE, q35 and pc
machines. All fail identically. The disk is readable by mtools and the
PE is valid per objdump — this is an OVMF/firmware issue, not a disk
format issue.

The USB image (`menagerie-usb.img`) is built and SHOULD work on real
hardware (MacBook Air), which has a real UEFI implementation, not
OVMF. But it has NOT been proven in QEMU.
