#!/usr/bin/env python3
"""prove_kernel.py — PROVE Menagerie v0.1 boots with live interrupts.

Boots the disk image in QEMU, then ASSERTS:
  1. Console text "menagerie" is rendered correctly (pixel-checked
     against the font data — not just "something is on screen").
  2. The tick counter ADVANCES between two screendumps (interrupts live).
  3. The serial log shows "menagerie" + increasing "tick N" lines.

FAILs loudly on any mismatch. Exit 0 only on full PASS.
"""
import os
import re
import socket
import subprocess
import sys
import time

MENAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MENAG_DIR)

import boot.mkcustom as mkcustom
from boot.mkcustom import FONT_GLYPHS

DISK_IMG = os.path.join(MENAG_DIR, "boot", "disk.img")
PPM1 = "/tmp/k1.ppm"
PPM2 = "/tmp/k2.ppm"
MON_SOCK = "/tmp/kmon.sock"
SERIAL_LOG = "/tmp/kserial.log"

BG = 0x001020
WHITE = 0xFFFFFF
YELLOW = 0xFFFF00

# glyph indices for "menagerie": m e n a g e r i e
MENAGERIE = [18, 14, 19, 12, 15, 14, 20, 16, 14]
TEXT_X, TEXT_Y = 16, 16
TICK_X, TICK_Y = 16, 40
DIGIT_X = 56  # "tick " is 5 chars -> digits start at 16+5*8


def read_ppm(path):
    data = open(path, "rb").read()
    assert data[:2] == b"P6", "not a P6 PPM"
    parts = data.split(b"\n", 4)
    w, h = int(parts[1].split()[0]), int(parts[1].split()[1])
    off = sum(len(p) + 1 for p in parts[:3])
    px = data[off:]
    assert len(px) == w * h * 3, f"short pixel data: {len(px)}"
    return w, h, px


def pix(px, w, x, y):
    i = (y * w + x) * 3
    return (px[i] << 16) | (px[i + 1] << 8) | px[i + 2]


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def main():
    print("== building disk image ==")
    mkcustom.main()

    for f in (PPM1, PPM2, MON_SOCK, SERIAL_LOG):
        if os.path.exists(f):
            os.unlink(f)

    print("== booting qemu ==")
    qemu = subprocess.Popen(
        ["qemu-system-x86_64", "-drive", f"file={DISK_IMG},format=raw",
         "-vga", "std", "-display", "none", "-m", "128",
         "-serial", f"file:{SERIAL_LOG}",
         "-monitor", f"unix:{MON_SOCK},server,nowait",
         "-no-reboot"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if os.path.exists(MON_SOCK):
                break
            time.sleep(0.1)
        time.sleep(5)  # boot + paint + some ticks

        def snap(path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(MON_SOCK)
            s.settimeout(10)
            try:
                s.recv(4096)
            except socket.timeout:
                pass
            s.sendall(b"screendump " + path.encode() + b"\n")
            time.sleep(2)
            try:
                s.recv(4096)
            except socket.timeout:
                pass
            s.close()

        snap(PPM1)
        time.sleep(5)  # let the tick counter advance
        snap(PPM2)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(MON_SOCK)
        s.sendall(b"quit\n")
        s.close()
    finally:
        time.sleep(1)
        if qemu.poll() is None:
            qemu.kill()

    if not os.path.exists(PPM1) or not os.path.exists(PPM2):
        return fail("no screendump produced")

    w1, h1, px1 = read_ppm(PPM1)
    w2, h2, px2 = read_ppm(PPM2)
    print(f"screendumps: {w1}x{h1}, {w2}x{h2}")
    if (w1, h1) != (1024, 768) or (w2, h2) != (1024, 768):
        return fail("unexpected screendump resolution")

    # --- check 1: "menagerie" rendered pixel-correct ---
    print("== check 1: console text ==")
    checked = 0
    for ci, gi in enumerate(MENAGERIE):
        glyph = FONT_GLYPHS[gi]
        gx = TEXT_X + ci * 8
        set_checked = clear_checked = 0
        for row in range(16):
            byte = glyph[row]
            for col in range(8):
                is_set = (byte >> (7 - col)) & 1
                got = pix(px1, w1, gx + col, TEXT_Y + row)
                if is_set and set_checked < 6:
                    if got != WHITE:
                        return fail(
                            f"glyph {gi} row {row} col {col}: "
                            f"got 0x{got:06X}, want white")
                    set_checked += 1
                elif not is_set and clear_checked < 3 and row > 0:
                    if got != BG:
                        return fail(
                            f"glyph {gi} row {row} col {col}: "
                            f"got 0x{got:06X}, want bg")
                    clear_checked += 1
            if set_checked >= 6 and clear_checked >= 3:
                break
        checked += set_checked + clear_checked
    print(f"  text ok: {checked} pixels verified across 9 glyphs")

    # --- check 2: tick advances between dumps ---
    print("== check 2: tick counter advances ==")
    # "tick " label region must be non-background in dump 1
    label_live = sum(
        1 for yy in range(TICK_Y, TICK_Y + 16)
        for xx in range(TICK_X, TICK_X + 40)
        if pix(px1, w1, xx, yy) != BG)
    if label_live < 20:
        return fail(f"tick label not drawn ({label_live} live pixels)")
    # digit region: x in [56, 216), y in [40, 56)
    diff = sum(
        1 for yy in range(TICK_Y, TICK_Y + 16)
        for xx in range(DIGIT_X, DIGIT_X + 160)
        if pix(px1, w1, xx, yy) != pix(px2, w2, xx, yy))
    print(f"  digit-region pixels changed between dumps: {diff}")
    if diff < 30:
        return fail("tick digits did not advance (interrupts dead?)")
    print("  tick advances: interrupts provably live")

    # --- check 3: serial log ---
    print("== check 3: serial log ==")
    if not os.path.exists(SERIAL_LOG):
        return fail("no serial log")
    log = open(SERIAL_LOG, "rb").read()
    print(f"  serial log: {len(log)} bytes")
    if b"menagerie\r\n" not in log:
        return fail("'menagerie' banner missing from serial log")
    ticks = [int(m.group(1)) for m in re.finditer(rb"tick (\d+)\r\n", log)]
    print(f"  tick lines: {ticks[:8]}{'...' if len(ticks) > 8 else ''}")
    if len(ticks) < 2:
        return fail(f"fewer than 2 tick lines in serial log")
    if any(b <= a for a, b in zip(ticks, ticks[1:])):
        return fail("tick values not strictly increasing")
    if any(t % 100 != 0 for t in ticks):
        return fail("tick lines not on 100-tick boundaries")

    print(f"PASS: menagerie v0.1 boots, console works, "
          f"timer IRQ live ({len(ticks)} tick reports, {ticks[-1]} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
