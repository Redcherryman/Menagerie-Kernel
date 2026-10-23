# Orum Custom Bootloader — v0.008

**Status:** PASS — QEMU pixel proof verified 2026-10-22.  
**Local only:** `boot/` is scaffolding, never pushed to GitHub (per Orum repo policy).

## Problem

GRUB 2.12 cannot boot the Orum freestanding kernel:
- LOAD segments >4KB fail
- Adjacent LOAD segments fail  
- 64-bit trampoline won't start under GRUB

The 32→64-bit trampoline (`boot/mkboot.py`) and `examples/fb_pixel.orum` are
proven correct. The bootloader was the blocker.

## Solution

Bypass GRUB entirely with a minimal custom bootloader:
- 512-byte boot sector (real-mode BIOS)
- VBE framebuffer setup
- Disk load via `int 0x13`
- Fake multiboot2 info block (so `fb_pixel.orum` is unchanged)
- Jump to the proven trampoline

## Disk Layout

```
LBA 0:      512-byte boot sector (0xAA55 signature)
LBA 1..N:   Trampoline stub image → loaded to 0x100000 (1MB)
LBA ..M:    Orum flat image → loaded to 0x400000 (4MB)
```

Built by `boot/mkcustom.py` → `boot/disk.img` (5632 bytes, 11 sectors).

## Boot Flow

### 1. Real-mode setup (0x7C00)

```
cli
xor ax,ax; mov ds,ax; mov es,ax; mov ss,ax
mov sp,0x7C00
sti
mov [0x7E00],dl          ; save boot drive
```

Serial debug: (none yet)

### 2. VBE: Set 1024x768 linear framebuffer

```
mov ax,0x4F02            ; VBE set mode
mov bx,0x4118            ; 1024x768, LFB bit (0x4000)
int 0x10
cmp ax,0x004F
jne fail1                ; '1' via teletype if failed
```

Serial: `V`

**QEMU quirk:** SeaBIOS reports 24bpp for mode 0x4118, but the Bochs VBE
hardware supports 32bpp. The BIOS call initializes the display; we then
force 32bpp via Bochs VBE I/O ports (see step 4).

### 3. VBE: Get mode info

```
xor ax,ax; mov es,ax
mov ax,0x4F01            ; VBE get mode info
mov cx,0x4118
mov di,0x8000            ; ES:DI = buffer
int 0x10
cmp ax,0x004F
jne fail2                ; '2' if failed
```

Serial: `I`

Mode info layout at 0x8000 (VBE ModeInfoBlock):
- +16: BytesPerScanLine (u16)
- +18: XResolution (u16) → 1024
- +20: YResolution (u16) → 768
- +25: BitsPerPixel (u8) → 24 (ignored, we force 32)
- +40: PhysBasePtr (u32) → 0xFD000000 (QEMU std VGA)

### 4. Bochs VBE: Force 32bpp

QEMU's SeaBIOS does not expose 32bpp VBE modes via BIOS. Use Bochs VBE
I/O ports (0x1CE index, 0x1CF data) to set 1024x768x32:

```
mov dx,0x1CE; mov ax,1; out dx,ax    ; XRES index
mov dx,0x1CF; mov ax,1024; out dx,ax
mov dx,0x1CE; mov ax,2; out dx,ax    ; YRES index
mov dx,0x1CF; mov ax,768; out dx,ax
mov dx,0x1CE; mov ax,3; out dx,ax    ; BPP index
mov dx,0x1CF; mov ax,32; out dx,ax
mov dx,0x1CE; mov ax,4; out dx,ax    ; ENABLE index
mov dx,0x1CF; mov ax,0x41; out dx,ax ; enable + LFB
```

Serial: `B`

**Note:** Must set XRES/YRES/BPP together; touching BPP alone resets resolution.

### 5. Fake multiboot2 info block at 0x9000

`fb_pixel.orum` expects a multiboot2 info struct. The bootloader constructs
a minimal fake block so the Orum program is unchanged:

```
0x9000: u32 total_size = 48
0x9004: u32 reserved = 0
0x9008: u16 type = 8 (framebuffer tag)
0x900A: u16 flags = 0
0x900C: u32 size = 32
0x9010: u64 framebuffer_addr = [0x8028] (PhysBasePtr)
0x9018: u32 framebuffer_pitch = 4096 (1024*4, hardcoded for 32bpp)
0x901C: u32 framebuffer_width = [0x8012] (1024)
0x9020: u32 framebuffer_height = [0x8014] (768)
0x9024: u8  framebuffer_bpp = 32 (hardcoded)
0x9025: u8  framebuffer_type = 1 (indexed? actually direct)
0x9026: u16 reserved = 0 (padding)
0x9028: u16 type = 0 (end tag)
0x902A: u16 flags = 0
0x902C: u32 size = 8
```

Total: 8 (header) + 32 (fb tag) + 8 (end) = 48 bytes. ✓

The Orum code parses this exactly like a real multiboot2 info struct.

### 6. Disk load via int 0x13

Uses LBA packet (AH=0x42) to load to a bounce buffer at 0x10000 (64KB),
avoiding the 1MB boundary in real mode:

```
; DAP1: load stub (LBA 1..) → 0x10000
mov si,dap1
mov dl,[0x7E00]
mov ah,0x42
int 0x13
jc fail3                 ; '3' if failed
; Serial: '1'

; DAP2: load orum (LBA 1+stub_sectors..) → 0x10000+stub_bytes
mov si,dap2
mov dl,[0x7E00]
mov ah,0x42
int 0x13
jc fail3
; Serial: '2'
```

DAP (Disk Address Packet, 16 bytes):
- +0: u16 size = 0x10
- +2: u16 sectors
- +4: u16 offset, +6: u16 segment (destination)
- +8: u64 LBA

### 7. Enable A20

```
mov ax,0x2401
int 0x15                 ; BIOS enable A20
in al,0x92
or al,2
out 0x92,al              ; Fast A20 gate
```

Serial: `A`

### 8. 32-bit protected mode

```
cli
lgdt [gdtr]              ; GDT: null, 32-bit code (0x08), 32-bit data (0x10)
mov eax,cr0
or al,1                  ; PE bit
mov cr0,eax
jmp 0x08:pm32            ; far jump to 32-bit code
```

Serial (in pm32): `P`

GDT (at 0x7D96, 24 bytes):
- 0x00: null
- 0x08: 32-bit code, base 0, 4GB (0xCF9A000000FFFF)
- 0x10: 32-bit data, base 0, 4GB (0xCF92000000FFFF)

### 9. Copy to final locations (32-bit)

```
mov ax,0x10
mov ds,ax; mov es,ax; mov ss,ax
mov esp,0x7C00
; Serial: 'P' (already sent)

; Copy stub: 0x10000 → 0x100000 (1MB)
mov esi,0x10000
mov edi,0x100000
mov ecx,stub_bytes/4
rep movsd

; Copy orum: 0x10000+stub_bytes → 0x400000 (4MB)
mov esi,0x10000+stub_bytes
mov edi,0x400000
mov ecx,orum_bytes/4
rep movsd
; Serial: 'C'
```

### 10. Jump to trampoline

```
mov ebx,0x9000           ; EBX = multiboot2 info pointer
jmp 0x08:0x100058        ; far jump to trampoline entry
```

The trampoline (`boot/mkboot.py:build_code`) is linked at 0x100000 (STUB_BASE),
entry at 0x100058. It:
1. Saves EBX (mb2 pointer)
2. Sets up 4-level paging (PML4 at 0x1000000, 2MB pages, 0-4GB mapped)
3. Enables PAE, LME, paging
4. Far jumps to 64-bit code
5. Calls `kmain` (0x400198) with RDI = mb2 pointer
6. Halts on return

Serial from boot sector: `V I B 1 2 A P C` (then trampoline takes over, no serial)

## Handoff Contract

For `fb_pixel.orum` (or any freestanding Orum program) to run unchanged:

1. **CPU state at trampoline entry (0x100058):**
   - 32-bit protected mode, paging DISABLED
   - GDT with 32-bit code (0x08) and data (0x10) segments
   - EBX = physical address of multiboot2 info block
   - Stack: any (trampoline sets ESP=0x100A000)

2. **Memory layout:**
   - 0x100000: trampoline stub (320 bytes, from `build_stub_image`)
   - 0x400000: Orum flat image (text at 0x400000, data at 0x401000)
   - 0x9000: multiboot2 info block (48 bytes, see above)
   - 0x1000000+: page tables (16MB, zeroed RAM)
   - 0xFD000000: framebuffer (1024x768x32, 3MB)

3. **Trampoline guarantees:**
   - Transitions to 64-bit long mode
   - Identity maps 0-4GB with 2MB pages (present|writable)
   - Calls `kmain` with RDI = mb2 pointer (System V ABI)
   - kmain address is hardcoded in stub (from ELF symtab)

4. **Orum program requirements:**
   - Compiled with `--freestanding --entry kmain`
   - `fn kmain(mbi: raw *u8)` — takes mb2 pointer, parses framebuffer tag
   - Uses volatile `rawptr` for framebuffer writes
   - Halts via `cli; hlt` loop (no exit syscall)

## QEMU Test

```bash
qemu-system-x86_64 \
  -drive file=boot/disk.img,format=raw \
  -vga std \
  -display none \
  -monitor unix:/tmp/qemu-mon.sock,server,nowait \
  -serial stdio \
  -m 256
```

Serial output: `VIB12APC` (all steps OK)

Screendump via monitor: `screendump /tmp/fb.ppm`

**Pixel verification (PASS 2026-10-22):**
- (10,10): 0xFF0000 (red) ✓
- (100,50): 0x00FF00 (green) ✓
- (200,100): 0x0000FF (blue) ✓
- (50,200): 0xFFFFFF (white) ✓
- (305,302): 0xFFFF00 (yellow rect) ✓
- (405,105): 0xFF00FF (magenta diagonal) ✓
- (0,0), (500,700): 0x001020 (dark blue bg) ✓

## Files

- `boot/mkcustom.py` — Disk image builder (boot sector assembler + layout)
- `boot/disk.img` — Built disk image (5632 bytes)
- `boot/mkboot.py` — Proven 32→64 trampoline (reused, not modified)
- `examples/fb_pixel.orum` — Unchanged Orum program
- `docs/bootloader.md` — This file

## Future: UEFI Loader

For real hardware (MacBook Air 2013, UEFI via rEFInd), a UEFI loader must:

1. **Be a PE32+ executable** (not a BIOS boot sector)
   - UEFI firmware loads `.efi` files, not 512-byte sectors
   - Must have PE32+ headers, `.text`, `.reloc` sections

2. **Use GOP instead of VBE**
   - `EFI_GRAPHICS_OUTPUT_PROTOCOL` replaces `int 0x10`
   - `Gop->SetMode()` for 1024x768x32 (or native panel res)
   - `Gop->Mode->FrameBufferBase` gives the LFB address
   - No Bochs ports needed; GOP natively supports 32bpp

3. **Handoff contract (same as BIOS version):**
   - Build the SAME fake multiboot2 block at 0x9000 (or any address)
     - Framebuffer tag type 8 with GOP values
     - End tag
   - Load trampoline stub to 0x100000
   - Load Orum image to 0x400000
   - Jump to 0x100058 with EBX (or RDI?) = mb2 pointer
   - **Note:** UEFI is already in 64-bit long mode! The 32→64 trampoline
     is unnecessary. A UEFI loader can:
     - Option A: Reuse trampoline by dropping to 32-bit first (complex)
     - Option B: Write a 64-bit trampoline that sets up paging and calls kmain
     - Option C: Set up paging directly in the UEFI app and call kmain
   - The `fb_pixel.orum` does NOT need to change; only the loader does.

4. **Memory map:**
   - UEFI memory map replaces the BIOS memory layout
   - Must `ExitBootServices()` before jumping to kernel
   - Framebuffer address comes from GOP, not VBE PhysBasePtr
   - Page tables can be built with UEFI-allocated pages

5. **No BIOS interrupts:**
   - `int 0x10`, `int 0x13`, `int 0x15` do not exist in UEFI
   - Disk I/O via `EFI_BLOCK_IO_PROTOCOL`
   - All hardware access via UEFI protocols or direct MMIO

The multiboot2 info block format is the stable ABI between loader and
Orum program. As long as the UEFI loader constructs the same tag layout,
`fb_pixel.orum` runs unchanged.
