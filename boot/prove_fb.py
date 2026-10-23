#!/usr/bin/env python3
"""prove_fb.py — PROVE the Orum framebuffer pixel.

LOCAL-ONLY scaffolding. Never pushed anywhere.

1. Builds examples/fb_pixel.orum -> freestanding ELF (kmain).
2. Runs mkboot.py -> boot/orum_fb.elf (multiboot2 + trampoline).
3. Boots it in QEMU (-vga std, display none, monitor on unix socket).
4. screendump -> PPM, then ASSERTS exact pixel colors at exact coordinates.
   Any mismatch = FAIL.
"""

import os
import socket
import struct
import subprocess
import sys
import time

ORUM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ORUM_DIR)

from orum.driver import build_binary
from boot import mkboot  # noqa: E402  (we call main via argv below)

FB_SRC = os.path.join(ORUM_DIR, "examples", "fb_pixel.orum")
FB_ELF = "/tmp/fb_pixel.elf"
BOOT_ELF = os.path.join(ORUM_DIR, "boot", "orum_fb.elf")
PPM = "/tmp/fb.ppm"
MON_SOCK = "/tmp/qmon"

# (x, y) -> (r, g, b); framebuffer is XRGB8888
BG = (0x00, 0x10, 0x20)  # 0x001020 dark blue
EXPECT = {
    (10, 10): (255, 0, 0),      # red
    (100, 50): (0, 255, 0),     # green
    (200, 100): (0, 0, 255),    # blue
    (50, 200): (255, 255, 255), # white
    (305, 302): (255, 255, 0),  # yellow (inside rectangle)
    (300, 300): (255, 255, 0),  # yellow (rect corner)
    (405, 105): (255, 0, 255),  # magenta (diagonal)
    (400, 100): (255, 0, 255),  # magenta (diagonal start)
    (0, 0): BG,                 # background
    (500, 500): BG,             # background
    (700, 700): BG,             # background
}


def read_ppm(path):
    data = open(path, "rb").read()
    assert data[:2] == b"P6", "not a P6 PPM"
    # parse header: P6 <ws> W H <ws> MAXVAL <ws>
    i = 2
    tokens = []
    while len(tokens) < 3:
        while data[i:i+1].isspace():
            i += 1
        if data[i:i+1] == b"#":
            while data[i:i+1] != b"\n":
                i += 1
            continue
        j = i
        while not data[j:j+1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    w, h, maxval = int(tokens[0]), int(tokens[1]), int(tokens[2])
    while data[i:i+1].isspace():
        i += 1
    px = data[i:i + w * h * 3]
    assert len(px) == w * h * 3, f"short pixel data: {len(px)}"
    return w, h, px


def main():
    print("== building orum program ==")
    src = open(FB_SRC).read()
    build_binary(src, FB_ELF, freestanding=True, entry="kmain")
    print("== building boot image ==")
    # mkboot.main reads sys.argv; emulate the CLI
    old_argv = sys.argv
    sys.argv = ["mkboot.py", FB_ELF, BOOT_ELF]
    try:
        mkboot.main()
    finally:
        sys.argv = old_argv

    for f in (PPM, MON_SOCK):
        if os.path.exists(f):
            os.unlink(f)

    print("== booting qemu ==")
    qemu = subprocess.Popen(
        ["qemu-system-x86_64", "-kernel", BOOT_ELF, "-vga", "std",
         "-display", "none", "-m", "128",
         "-monitor", f"unix:{MON_SOCK},server,nowait",
         "-no-reboot"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # wait for the monitor socket
        for _ in range(100):
            if os.path.exists(MON_SOCK):
                break
            time.sleep(0.1)
        time.sleep(3)  # let it boot and paint
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(MON_SOCK)
        s.settimeout(10)
        # drain the banner
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall(b"screendump " + PPM.encode() + b"\n")
        time.sleep(2)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall(b"quit\n")
        s.close()
    finally:
        time.sleep(1)
        qemu.poll()
        if qemu.returncode is None:
            qemu.kill()

    if not os.path.exists(PPM):
        print("FAIL: no screendump produced")
        return 1
    w, h, px = read_ppm(PPM)
    print(f"screendump: {w}x{h}")

    fails = []
    for (x, y), (er, eg, eb) in sorted(EXPECT.items()):
        if x >= w or y >= h:
            fails.append(f"({x},{y}): outside {w}x{h}")
            continue
        off = (y * w + x) * 3
        r, g, b = px[off], px[off + 1], px[off + 2]
        status = "ok" if (r, g, b) == (er, eg, eb) else "MISMATCH"
        print(f"  ({x},{y}): got ({r},{g},{b}) want ({er},{eg},{eb}) {status}")
        if (r, g, b) != (er, eg, eb):
            fails.append(f"({x},{y}): got ({r},{g},{b}) want ({er},{eg},{eb})")

    if fails:
        print(f"FAIL: {len(fails)} pixel mismatches")
        for f in fails:
            print("   ", f)
        return 1
    print(f"PASS: all {len(EXPECT)} pixels correct — Orum painted the framebuffer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
