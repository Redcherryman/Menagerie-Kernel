# tools/ — kernel tests (hosted Orum)

This directory holds **hosted** Orum programs (`.orum`) that test the
Menagerie kernel. Per the repo rule this is kernel code + tests only:
the kernel itself lives in `kernel/kmain.orum`; the image builders
(PE32+/FAT writers, build orchestrator) live in Myriad-OS
(`myriad/tools/`), because booting is the OS's job.

Rules for this directory:
- Hosted mode only: `fopen`/`fread`/`fwrite`/`fclose`/`fseek` and
  `heap_alloc` are allowed here. No Linux syscalls in `kernel/` ever.
- Each tool documents its file interface in its header comment (hosted
  Orum has no argv yet, so inputs/outputs are fixed paths or constants).
- The prove tools are OFFLINE verifiers: the old Python scripts
  orchestrated QEMU (subprocess, unix sockets, sleeps) — none of which
  hosted Orum can do — so each tool verifies already-produced artifacts
  (screendump PPMs, serial logs) instead: prints PASS/FAIL, exits 0/1.

Tools:
- `pcheck.orum` — shared verification helpers (declarations only): P6
  PPM header parsing (`ppm_open` returns an fd + pixel-data file
  offset), seek-based pixel reads (`ppm_pix_fd`), `count_nonbg_fd`,
  byte-needle search (`find_sub`, `needle_ok`, `seq_ok`), serial-log
  "tick N" validation (`check_ticks`), and `read_all` (whole file
  into a heap buffer).
- `font.orum` — the Menagerie console font as Orum data: 54 hand-drawn
  8x16 glyphs (864 bytes) + the 128-byte ASCII->glyph index table, as
  byte-array literals. `font_glyph(i)` returns a pointer to glyph i's
  16 bytes; `font_index()` returns the index table. Declarations only.
  Used by `prove_kernel.orum` to pixel-verify the console banner.
- `prove_fb.orum`, `prove_kernel.orum`, `prove_v02.orum`,
  `prove_xhci.orum`, `prove_uefi.orum` — the kernel's test suite.
  See each file's header for its fixed input paths.

Conventions (Orum v0.014 realities, verified by experiment):
- `take x from mod` is plain file inclusion with cycle dedup; a
  module's top-level statements WOULD run on inclusion — but when the
  entry file defines `fn _start`, the compiler jumps straight to it
  and never emits any top-level statements. `_start` must end with
  `exit(0)` (the stub jumps, not calls: returning segfaults).
- Module-level constants are NOT visible inside `fn` bodies — pass
  what a function needs as parameters (max 6 args per call).
- Building: every tool with a `take` needs `base_dir` pointing here,
  e.g. `compile_source(open("prove_fb.orum").read(), base_dir="tools")`.
  `build_binary()` does not accept `base_dir`; use `compile_source`
  and write/chmod the output yourself.
- The hosted heap is 1 MiB: never buffer a whole multi-MB artifact
  (a 1024x768 PPM is 2.25 MiB). The prove tools stream pixels with
  fseek + 3-byte fread instead.
- `match` is a reserved word (pattern matching); `out` is reserved
  (asm `in`/`out`/`clobber`); no `and`/`or` — nest `if`s instead.
  No `break`/`continue` — exit loops with done-flags.
