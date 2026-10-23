# boot/ — the Menagerie loader

This is the real boot infrastructure for the Menagerie kernel
(transplanted here from the Orum freestanding experiments — the orum
tree is compiler-only now).

- `mkcustom.py` — builds the disk image: compiles `kernel/kmain.orum`
  with the Orum compiler (`--freestanding`), assembles the 512-byte
  real-mode boot sector (VBE video, disk loads), the 32→64 trampoline,
  the 256 ISR stubs + common handler, the font and strings; writes
  `disk.img`.
- `mkboot.py` — the proven 32→64-bit trampoline (hand-assembled bytes).
- `prove_kernel.py` — the v0.1 proof: boots QEMU, asserts console text
  pixel-correct, tick counter advancing (interrupts live), serial log
  sane. Fails loudly.
- `prove_fb.py`, `orum_fb.elf`, `disk.img` — v0.008 pixel-proof artifacts,
  kept for history.

Orum itself has no kernel and never will — it is the language this
loader and kernel are written with. The compiler lives at
`~/workspace/orum/` and is never modified from here.
