# Menagerie xHCI USB Driver (v0.4) — Design Notes

**Status (Oct 22-23, 2026):** Partial bring-up. PCI scan, controller init,
port detection/reset, and Enable Slot work under QEMU (`qemu-xhci` +
`usb-kbd`). **Address Device fails with TRB Error (completion code 5)** —
a QEMU xhci quirk or subtle spec detail not yet resolved. USB enumeration
and HID keyboard are blocked on this.

## Orum Compiler Bug Found & Fixed

While bringing up xHCI, discovered a genuine codegen bug in `orum/x86.py`
`load_sized()`: 4-byte loads emitted `0x0F 0xB7` (`movzx r64, r/m16` —
a **16-bit** load). There is no `movzx r64, r/m32`; the correct instruction
is `mov r32, r/m32` (0x8B), which the CPU zero-extends to 64 bits.

**Fix:** `load_sized` now uses `mov_rm32` for unsigned 32-bit loads and
`movsxd` (REX.W + 0x63) for signed. `store_sized` was already correct.

**Test update:** `tests/test_v0004.py::test_load_inside_loop_in_binary`
enshrined the bug (expected `movzwq` for a `u32` load). Updated to expect
the correct 32-bit `mov`. Full suite: **319/319 green**.

This bug was invisible in v0.2 because the only 32-bit MMIO read was
`fb_stride` (1024, fits in 16 bits). xHCI HCSPARAMS1 (`0x08001040`) exposed
it: driver read `0x00001040` (0 ports) while QEMU monitor showed 8 ports.

## Register Map (xHCI 1.x)

All MMIO via volatile `raw *u32` + `peek32`/`poke32` (now correct).

| Offset | Register | Notes |
|--------|----------|-------|
| +0x00 | CAPLENGTH/HCIVERSION | low byte = cap length (0x40 on QEMU) |
| +0x04 | HCSPARAMS1 | bits 31:24 = # ports (8) |
| +0x14 | DBOFF | doorbell array offset |
| +0x18 | RTSOFF | runtime regs offset |
| cap+0x00 | USBCMD | bit 0=RS, bit 1=HCRST |
| cap+0x04 | USBSTS | bit 0=HCHalted, bit 11=CNR |
| cap+0x08 | PAGESIZE | |
| cap+0x18 | DNCTRL | |
| cap+0x28 | DCBAAP lo | Device Context Base Address Array |
| cap+0x30 | CONFIG | max slots enabled |
| cap+0x400+ | PORTSC[n] | per-port; n*0x10 |

**PORTSC bits:** 0=CCS, 1=PED, 4=PR, 9=PP, **13:10=Port Speed**
( NOT 12:9 — bit 9 is PP! ), 17=PRC, 21:22=PLC.

Speed values: 1=Full, 2=Low, 3=High, 4=Super.

## Memory Layout (identity-mapped, 0x1100000 region)

| Address | Use |
|---------|-----|
| 0x1100000 | DCBAA (256 entries × 8 bytes) |
| 0x1100040 | Input Context (64B align) |
| 0x1100060 | Slot Context (in input ctx) |
| 0x1100080 | EP0 Context (in input ctx) |
| 0x1100140 | Device Context (64B align) |
| 0x1100240 | Command Ring (16 TRBs) |
| 0x1100340 | Event Ring Segment Table |
| 0x1100380 | Event Ring (32 TRBs) |
| 0x1100580 | EP0 Transfer Ring (16 TRBs) |
| 0x1100680 | Interrupt IN Ring (16 TRBs) |
| 0x1100780 | Descriptor buffer (256B) |
| 0x1100880 | HID report buffer (8B) |

## Ring Protocol

**Command Ring:** 16 TRBs, link TRB at index 15 (type 6, TC=1).
Producer cycle bit starts at 1. Doorbell 0 to dispatch.

**Event Ring:** 32 TRBs, single segment. Consumer cycle bit starts at 1.
After consuming an event, update ERDP (runtime + 0x20) with
`(dequeue_addr | 8)` — bit 3 clears EHB.

**Transfer Rings:** 16 TRBs each, link TRB at index 15. DCS=1 initially.

## Bring-up Sequence (working)

1. **PCI scan:** Walk bus 0, dev 0-31, func 0-7. Match class 0x0C,
   subclass 0x03, prog-if 0x30. Read BAR0, mask off low 4 bits.
   QEMU: found at `0xFEBB0000`.
2. **Halt:** If USBCMD.RS=1, clear RS, wait for USBSTS.HCHalted=1
   (use `xh_wait_set`, NOT `xh_wait_clr` — the bit SETS on halt).
3. **Reset:** Set USBCMD.HCRST, wait for HCRST=0, wait for CNR=0.
4. **Init:** Program DCBAAP, CONFIG (max slots), DNCTRL.
5. **Rings:** Set CRCR (command ring + RCS=1), ERSTSZ=1, ERSTBA,
   ERDP (dequeue + EHB clear).
6. **Start:** Set USBCMD.RS, wait for HCHalted=0, wait for CNR=0.
7. **Port scan:** For each port, check CCS=1. Reset via PR bit,
   wait for PR=0, clear change bits (preserve PP), verify PED=1.
   Read speed from bits 13:10.
8. **Enable Slot:** Command TRB type 8, wait for completion (type 33),
   extract slot ID from bits 31:24 of DW3.

## Known Gap: Address Device TRB Error

**Symptom:** Address Device command (type 11) returns completion code 5
(TRB Error) under QEMU `qemu-xhci`.

**Verified correct:**
- Input Context 64-byte aligned at 0x1100040
- Input Control: drop=0, add=0x3 (A0|A1)
- Slot Context: speed=3 (HS), entries=1
- EP0 Context: MPS=64 (for HS), type=4 (control), CErr=3,
  ring=0x1100580, DCS=1
- DCBAA[slot] set to 0x1100140 (64-byte aligned)
- Command TRB: type=11, slot=1, cycle correct

**Hypotheses:**
- QEMU xhci quirk with USB2 ports (keyboard on port 5, a USB2 port)
- Subtle input context field (route string, hub/MTT, etc.)
- Device not responding as expected after reset

**Next steps:**
- Try USB3 port or different device model
- Compare against working xHCI driver (e.g., Linux) via tracing
- Test on real hardware (MacBook Air) when USB stick available

## HID Keyboard (designed, not yet tested)

Once Address Device works:
1. GET_DESCRIPTOR (device, 18B) → read bMaxPacketSize0
2. GET_DESCRIPTOR (config 9B) → wTotalLength → GET_DESCRIPTOR (full)
3. Parse for HID interface (class 3, subclass 1, protocol 1)
4. SET_CONFIGURATION, SET_PROTOCOL (boot, value 0)
5. Configure interrupt IN endpoint via Configure Endpoint command
6. Ring IN transfers, poll for transfer events, parse 8-byte boot reports
7. HID usage → ASCII via table, feed to keyboard ring buffer
8. Set `xh_st(136)=1` to stand down PS/2 (avoid double-input)

HID tables at 0x7988/0x7A88 (lowmem, alongside PS/2 tables).

## Files

- `kernel/kmain.orum`: xHCI driver (§ xHCI driver sections)
- `boot/prove_xhci.py`: QEMU test (to be completed when Address Device works)
