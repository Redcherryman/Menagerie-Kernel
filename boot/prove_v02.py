#!/usr/bin/env python3
"""prove_v02.py — PROVE Menagerie v0.2: keyboard, heap, shell.

Boots the disk image in QEMU, then ASSERTS:
  1. Boot banner "menagerie" on serial.
  2. Heap self-test: "heap ok" on serial (list[int] alloc+verify).
  3. "keyboard ready" on serial.
  4. Keyboard: monitor `sendkey` types "say hi" + Enter ->
     serial shows the shell echoing and printing "hi".
  5. `help` -> command list on serial.
  6. Unknown command -> "unknown command: frobnicate".
  7. Locked-but-unimplemented command -> "not implemented yet: list".
  8. Backspace editing: "say ab" + backspace + "c" -> prints "ac".
  9. `clear` -> screendump is (nearly) all background pixels.
 10. Timer ticks on serial, strictly increasing (interrupts live).

Saves screenshots to menagerie/shots/. FAILs loudly on any mismatch.
Exit 0 only on full PASS.
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

DISK_IMG = os.path.join(MENAG_DIR, "boot", "disk.img")
SHOTS = os.path.join(MENAG_DIR, "shots")
PPM_SHELL = "/tmp/v02-shell.ppm"
PPM_CLEAR = "/tmp/v02-clear.ppm"
MON_SOCK = "/tmp/v02mon.sock"
SERIAL_LOG = "/tmp/v02serial.log"

BG = 0x001020


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


class Mon:
    def __init__(self, path):
        self.path = path

    def cmd(self, c, wait=0.3):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.path)
        s.settimeout(10)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall(c.encode() + b"\n")
        time.sleep(wait)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.close()

    def type(self, text):
        for ch in text:
            if ch == " ":
                self.cmd("sendkey spc")
            elif ch == "\n":
                self.cmd("sendkey ret", 0.6)
            else:
                self.cmd(f"sendkey {ch}")

    def snap(self, path):
        self.cmd(f"screendump {path}", 2.0)


def serial_text():
    with open(SERIAL_LOG, "rb") as f:
        return f.read().decode("latin1")


def main():
    print("== building disk image ==")
    mkcustom.main()

    for f in (PPM_SHELL, PPM_CLEAR, MON_SOCK, SERIAL_LOG):
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
    mon = Mon(MON_SOCK)
    try:
        for _ in range(100):
            if os.path.exists(MON_SOCK):
                break
            time.sleep(0.1)
        else:
            return fail("monitor socket never appeared")

        # --- wait for the shell prompt ---
        print("== waiting for 'keyboard ready' ==")
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1)
            if os.path.exists(SERIAL_LOG) and "keyboard ready" in serial_text():
                break
        else:
            return fail("'keyboard ready' never appeared on serial")

        log = serial_text()
        print("== check 1+2+3: boot, heap, keyboard-ready ==")
        for needle in ("menagerie", "heap ok", "keyboard ready"):
            if needle not in log:
                return fail(f"{needle!r} missing from serial log")
        if "heap FAIL" in log:
            return fail("heap self-test FAILED in kernel")
        print("  boot banner ok, heap ok, keyboard ready")

        # --- check 4: say ---
        print("== check 4: keyboard + say ==")
        mon.type("say hi\n")
        time.sleep(1)
        log = serial_text()
        # prompt, echoed input, then say's output on its own line.
        # (Timer tick lines may interleave on serial; be tolerant.)
        ei = log.find("say hi")
        if ei < 0 or "\nhi\r\n" not in log[ei:]:
            return fail("'say hi' did not echo and print 'hi'")
        print("  say hi -> hi")

        # --- check 5: help ---
        print("== check 5: help ==")
        mon.type("help\n")
        time.sleep(1)
        log = serial_text()
        for needle in ("commands:", "say help clear restart off",
                       "list run where copy move burn",
                       "file commands need a filesystem"):
            if needle not in log:
                return fail(f"help output missing {needle!r}")
        print("  help lists commands")

        # --- screenshot: shell in action (right after help output) ---
        print("== screenshot: shell ==")
        mon.snap(PPM_SHELL)
        w, h, px = read_ppm(PPM_SHELL)
        live = sum(1 for yy in range(0, 200) for xx in range(0, 1024, 4)
                   if pix(px, w, xx, yy) != BG)
        print(f"  shell shot: {w}x{h}, {live} non-bg pixels in top rows")
        if live < 100:
            return fail("shell screenshot shows no text")

        # --- check 6: unknown command ---
        print("== check 6: unknown command ==")
        mon.type("frobnicate\n")
        time.sleep(1)
        if "unknown command: frobnicate" not in serial_text():
            return fail("unknown command not reported")
        print("  unknown command reported")

        # --- check 7: not implemented ---
        print("== check 7: locked command, no filesystem ==")
        mon.type("list\n")
        time.sleep(1)
        if "not implemented yet: list" not in serial_text():
            return fail("'not implemented yet: list' missing")
        print("  honest 'not implemented yet'")

        # --- check 8: backspace editing ---
        print("== check 8: backspace ==")
        mon.type("say ab")
        mon.cmd("sendkey backspace")
        mon.type("c\n")
        time.sleep(1)
        if not re.search(r"ac\r\n", serial_text()):
            return fail("backspace editing failed (expected 'ac')")
        print("  backspace edits the line")

        # --- check 9: clear ---
        print("== check 9: clear ==")
        mon.type("clear\n")
        time.sleep(1)
        mon.snap(PPM_CLEAR)
        w, h, px = read_ppm(PPM_CLEAR)
        total = w * h
        nonbg = sum(1 for yy in range(h) for xx in range(0, w, 8)
                    if pix(px, w, xx, yy) != BG)
        # prompt "> " = 2 glyphs redrawn after clear; allow a small budget
        print(f"  clear shot: {nonbg * 8} of {total} pixels non-bg")
        if nonbg * 8 > total * 0.01:
            return fail("screen not cleared")
        print("  screen cleared")

        # --- check 10: ticks increase ---
        print("== check 10: timer ticks ==")
        log = serial_text()
        ticks = [int(m.group(1)) for m in re.finditer(r"tick (\d+)\r\n", log)]
        print(f"  tick lines: {ticks[:6]}{'...' if len(ticks) > 6 else ''}")
        if len(ticks) < 2:
            return fail("fewer than 2 tick lines")
        if any(b <= a for a, b in zip(ticks, ticks[1:])):
            return fail("ticks not strictly increasing")
        print("  interrupts live")

        mon.cmd("quit", 1.0)
    finally:
        time.sleep(1)
        if qemu.poll() is None:
            qemu.kill()

    # save screenshots
    os.makedirs(SHOTS, exist_ok=True)
    import shutil
    shutil.copy(PPM_SHELL, os.path.join(SHOTS, "v0002-shell.ppm"))
    shutil.copy(PPM_CLEAR, os.path.join(SHOTS, "v0002-clear.ppm"))
    # also PNGs for easy viewing
    try:
        from PIL import Image
        for name in ("v0002-shell", "v0002-clear"):
            Image.open(os.path.join(SHOTS, name + ".ppm")).save(
                os.path.join(SHOTS, name + ".png"))
        print("screenshots: shots/v0002-shell.png, shots/v0002-clear.png")
    except ImportError:
        print("screenshots: shots/v0002-shell.ppm, shots/v0002-clear.ppm")

    print("PASS: menagerie v0.2 — keyboard, heap, shell all proven")
    return 0


if __name__ == "__main__":
    sys.exit(main())
