#!/usr/bin/env python3
"""prove_uefi.py — PROVE the Menagerie UEFI loader.

Builds BOOTX64.EFI + FAT test image, boots QEMU with OVMF (UEFI),
then ASSERTS:
  1. Loader serial messages (GOP located, mode set, kernel copied,
     boot services exited).
  2. Kernel boot banner "menagerie" on serial.
  3. Heap self-test "heap ok", "keyboard ready".
  4. Keyboard: monitor `sendkey` types "say hi" -> "hi" on serial.
  5. `help` lists commands.
  6. Timer ticks strictly increasing (interrupts live).
  7. Closeup screenshot shows console text (Aiden: no more bad shots).

Saves closeup screenshots to menagerie/shots/v0003-uefi-*.png.
FAILs loudly on any mismatch. Exit 0 only on full PASS.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import time

MENAG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, MENAG_DIR)

from boot.uefi.mkuefi import build_images

SHOTS = os.path.join(MENAG_DIR, "shots")
PPM_SHELL = "/tmp/v03-shell.ppm"
MON_SOCK = "/tmp/v03mon.sock"
SERIAL_LOG = "/tmp/v03serial.log"
OVMF_CODE = "/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS = "/tmp/v03-ovmf-vars.fd"

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
        s.settimeout(15)
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


def closeup(src_ppm, dst_png, x0=0, y0=0, x1=1024, y1=256, scale=2):
    """Crop the console area and scale up for a readable screenshot."""
    from PIL import Image
    img = Image.open(src_ppm).crop((x0, y0, x1, y1))
    img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    img.save(dst_png)
    print(f"  closeup: {dst_png} ({img.width}x{img.height})")


def main():
    print("== building UEFI image ==")
    _, test_img, _ = build_images()

    for f in (PPM_SHELL, MON_SOCK, SERIAL_LOG):
        if os.path.exists(f):
            os.unlink(f)
    shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.fd", OVMF_VARS)

    print("== booting qemu with OVMF ==")
    qemu = subprocess.Popen(
        ["qemu-system-x86_64",
         "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
         "-drive", f"if=pflash,format=raw,file={OVMF_VARS}",
         "-drive", f"file={test_img},format=raw",
         "-vga", "std", "-display", "none", "-m", "256",
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

        print("== waiting for kernel 'keyboard ready' (OVMF is slow) ==")
        deadline = time.time() + 150
        while time.time() < deadline:
            time.sleep(2)
            if os.path.exists(SERIAL_LOG):
                t = serial_text()
                if "keyboard ready" in t:
                    break
                for bad in ("FATAL", "failed to load Boot"):
                    if bad in t and "keyboard ready" not in t:
                        pass  # keep waiting; loader may retry
        else:
            log = serial_text() if os.path.exists(SERIAL_LOG) else ""
            print("--- serial tail ---")
            print(log[-2000:])
            return fail("'keyboard ready' never appeared on serial")

        log = serial_text()
        print("== check 1: UEFI loader milestones ==")
        for needle in ("UEFI: GOP located", "UEFI: mode 1024x768x32 set",
                       "UEFI: kernel copied", "UEFI: boot services exited"):
            if needle not in log:
                return fail(f"loader milestone missing: {needle!r}")
        print("  loader: GOP ok, mode set, kernel copied, EBS done")

        print("== check 2+3: kernel boot, heap, keyboard ==")
        for needle in ("menagerie", "heap ok", "keyboard ready"):
            if needle not in log:
                return fail(f"{needle!r} missing from serial log")
        if "heap FAIL" in log:
            return fail("heap self-test FAILED in kernel")
        print("  kernel booted via UEFI, heap ok, keyboard ready")

        print("== check 4: say ==")
        mon.type("say hi\n")
        time.sleep(1)
        log = serial_text()
        ei = log.find("say hi")
        if ei < 0 or "\nhi\r\n" not in log[ei:]:
            return fail("'say hi' did not echo and print 'hi'")
        print("  say hi -> hi")

        print("== check 5: help ==")
        mon.type("help\n")
        time.sleep(1)
        if "commands:" not in serial_text():
            return fail("help output missing")
        print("  help lists commands")

        print("== screenshot: shell closeup ==")
        mon.snap(PPM_SHELL)
        w, h, px = read_ppm(PPM_SHELL)
        live = sum(1 for yy in range(0, 200) for xx in range(0, 1024, 4)
                   if pix(px, w, xx, yy) != BG)
        print(f"  shell shot: {w}x{h}, {live} non-bg pixels in top rows")
        if live < 100:
            return fail("shell screenshot shows no text")
        os.makedirs(SHOTS, exist_ok=True)
        closeup(PPM_SHELL, os.path.join(SHOTS, "v0003-uefi-shell.png"))

        print("== check 6: ticks increase ==")
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

    print("PASS: menagerie v0.3 UEFI loader — kernel boots on UEFI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
