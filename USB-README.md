# Menagerie on your MacBook Air — USB boot instructions

This image (`menagerie-usb.img`, 128MB) boots the Menagerie kernel on
real UEFI hardware. It's the same kernel you've seen in QEMU, loaded by
the UEFI loader instead of the BIOS one.

## What to do (on your Windows 10 Pro MacBook)

1. **Flash the image to a USB stick.** On Windows, use
   [Rufus](https://rufus.ie) → select the stick → "DD Image" mode →
   pick `menagerie-usb.img` → Start. (DD mode, not ISO mode.)
   Or from a terminal: `dd if=menagerie-usb.img of=\\.\PhysicalDriveN bs=4M`
   — triple-check the drive number first.

2. **Boot it.** Plug in the stick, reboot, hold **Option** at the chime.
   Pick the orange "EFI Boot" drive. (rEFInd will also list it as
   `BOOTX64.EFI` on the USB if you boot to rEFInd first.)

3. **What you should see (current build):** the `Menagerie UEFI loader`
   banner on screen — then it stops. There is a known, unresolved hang
   when the loader asks the firmware for the graphics (GOP) mode, so
   the full boot described below hasn't been proven yet. If you see the
   banner, the PE binary loads fine on your Air and the problem is
   isolated to the graphics call — that alone is useful data. A photo
   of exactly where it stops tells us what's next.

   Target once the graphics call is fixed: a 1024x768 screen, heap
   self-test result, `keyboard ready`, and a `>` shell prompt with a
   ticking counter. `say` / `help` / `clear` work.

## Honest caveats

- **Known issue: hang at the graphics call.** Right now the loader
  prints its banner and then hangs when asking the firmware for the
  GOP display mode. A banner-then-hang on the real Air is the expected
  result for this build — not a bad flash or a bad stick. If you test
  it, report exactly what appears (photo of the screen) and the loader
  gets fixed from there.

- **No Secure Boot issue:** your 2013 Air predates Apple's Secure Boot
  (that started with the T2 chip in 2018). Nothing to disable.
- **Keyboard won't work yet.** Your MacBook's internal keyboard is USB,
  and the kernel only speaks PS/2 so far. Once the graphics call is
  fixed and you reach the shell prompt, you won't be able to type into
  it. The USB (xHCI) keyboard driver is the next big milestone — that's
  the one that makes this usable on real iron.
- **GOP mode:** once the hang is fixed, the loader insists on
  1024x768x32. If your firmware doesn't offer it, you'll get a
  "FATAL: no 1024x768x32 mode" message instead of a boot. (Apple's GOP
  usually offers it.)
- **This is a tech demo, not an OS yet.** No filesystem, no USB, no
  networking. It proves Orum → Menagerie boots on your actual machine.

If it doesn't boot, tell me exactly what you see (photo of the screen
helps more than anything) and I'll fix the loader.
