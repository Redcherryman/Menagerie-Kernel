# Menagerie boot contract

What the loader (`boot/mkcustom.py` + boot sector) guarantees the kernel
on entry to `fn kmain`. If you change the loader, this document is the
checklist. If you change the kernel's assumptions, update this file.

## Entry conditions

- CPU is in 64-bit long mode, paging enabled, identity-mapping 0–4GB
  with 2MB pages (built by the proven trampoline at 0x100058).
- Interrupts DISABLED (IF clear). The kernel enables them itself.
- `rdi` = physical address of the boot info block (fake multiboot2).
- Stack: valid, 16-byte aligned, top at 0x100A000 (set by trampoline).
- The kernel image is at 0x400000 (flat: text then data, from the ELF).

## Boot info block (at 0x9000, pointed to by rdi)

48 bytes, multiboot2 tag layout (the kernel parses it, see `read_u16/32/64`):

| offset | size | content                              |
|--------|------|--------------------------------------|
| 0      | u32  | total_size = 48                      |
| 4      | u32  | reserved = 0                         |
| 8      | u16  | tag type = 8 (framebuffer)           |
| 10     | u16  | tag flags                            |
| 12     | u32  | tag size = 32                        |
| 16     | u64  | framebuffer physical address         |
| 24     | u32  | pitch (bytes per row)                |
| 28     | u32  | width in pixels                      |
| 32     | u32  | height in pixels                     |
| 36     | u8   | bits per pixel (= 32)                |
| 37     | u8   | framebuffer type (= 1, indexed color)|
| 38     | u16  | reserved                             |
| 40     | u16  | end tag type = 0                     |
| 42     | u16  | end tag flags = 0                    |
| 44     | u32  | end tag size = 8                     |

The VBE BIOS sets 1024x768x32; pitch is 4096.

## Low memory map (all below 1MB)

| address       | size  | content                                              |
|---------------|-------|------------------------------------------------------|
| 0x5000        | 4KB   | IDT area — zeros from the image; the KERNEL builds  |
|               |       | the 256-entry IDT here at runtime (`build_idt`)     |
| 0x6000        | 4KB   | ISR stubs, 256 x 16B: `push imm8 vec; jmp common`   |
| 0x7000        | 80B   | common ISR handler: saves regs, `rdi` = vector,     |
|               |       | calls the Orum `on_irq` fn (address patched at      |
|               |       | build time), restores, `iretq`                       |
| 0x7100        | 352B  | font: 22 hand-drawn 8x16 glyphs (see kernel source  |
|               |       | for the index table)                                 |
| 0x7300        | 48B   | NUL-terminated strings: "menagerie", "tick ", "\r\n"|
| 0x7500        | 8B    | tick counter (u64), written by IRQ0 handler          |
| 0x7510        | 10B   | IDT pseudo-descriptor scratch (kernel writes it)    |
| 0x7600        | 32B   | scratch (unused in v0.1)                             |
| 0x7C00        | 512B  | boot sector (BIOS-loaded)                            |
| 0x7E00        | 1B    | boot drive number (used by boot sector)              |
| 0x8000        | 256B  | VBE mode info                                        |
| 0x9000        | 48B   | boot info block (above)                              |
| 0x10000       | —     | disk bounce buffer (loader use only)                 |
| 0x100000      | —     | trampoline stub                                      |
| 0x400000      | —     | kernel flat image                                    |
| 0x1000000+    | —     | page tables; stack top 0x100A000                     |

## GDT (trampoline's, still live)

- 0x08: 32-bit code, 0x10: 32-bit data, 0x18: 64-bit code.
- IDT entries use code selector **0x18**, type **0x8E**
  (present, DPL 0, 64-bit interrupt gate).

## What the kernel must do (v0.1 checklist)

1. Parse the boot info block → framebuffer address/pitch/dimensions.
2. Build the IDT at 0x5000 (256 entries → stubs at 0x6000).
3. `lidt` the 10-byte descriptor (limit 4095, base 0x5000).
4. Remap the PIC (master → 0x20, slave → 0x28), unmask.
5. Program the PIT (channel 0, mode 3, 100Hz).
6. `sti` only after 1–5 are complete.
7. IRQ handlers EOI the PIC; IRQ0 increments the tick at 0x7500.

## For the future UEFI loader

Same contract, different mechanics: PE32+ instead of a boot sector,
GOP instead of VBE (native 32bpp), `ExitBootServices()` before entry.
The 48-byte info block layout and the low-memory map are the stable ABI —
a UEFI loader provides the same and the kernel does not change.
