"""UEFI loader code generation (x86-64, hand-assembled).

Builds the .text section of the PE32+ loader: efi_main + helpers + data.
See mkuefi.py for the A64 assembler.
"""
from .asm64 import (A64, RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI,
                     R8, R9, R10, R12, R13, R14, R15)

# fixed physical addresses (must match docs/boot-contract.md)
P_KERNEL = 0x400000
P_LOWMEM = 0x5000
P_INFO = 0x9000
P_HEAP = 0x2000000
P_STACK = 0x1500000
STACK_TOP = P_STACK + 16 * 4096          # 0x1510000
MMAP_BYTES = 32 * 4096

# local slots (rbp-relative)
O_IMG, O_ST, O_BS, O_CON = -64, -72, -80, -88
O_GOP, O_MISZ, O_MINF = -96, -104, -112
O_FB, O_PITCH = -120, -128
O_MMSZ, O_MMBUF, O_KEY, O_DSZ, O_DVER = -136, -144, -152, -160, -168

# EFI structure offsets
ST_CONOUT, ST_BS = 64, 96
BS_ALLOC, BS_GETMAP, BS_EBS, BS_LOCPROTO = 40, 56, 224, 312
GOP_QUERY, GOP_SETMODE, GOP_MODE = 0, 8, 24


def build_loader(kmain_phys, flat_len, lowmem_len,
                 pages_kernel, pages_lowmem,
                 text_rva, kdata_rva, ldata_rva):
    a = A64()

    def bs_call(off):
        a.mov_r_m(RAX, RBP, O_BS)
        a.sub_rsp(40)
        a.call_mem(RAX, off)
        a.add_rsp(40)

    def gop_call(off):
        a.mov_r_m(RAX, RBP, O_GOP)
        a.sub_rsp(40)
        a.call_mem(RAX, off)
        a.add_rsp(40)

    def ser_str(label):
        a.push_r(RSI)
        a.lea_rip(RSI, label)
        a.call_rel("ser_puts")
        a.pop_r(RSI)

    def con_str(label):
        a.lea_rip(RSI, label)
        a.call_rel("con_puts")

    def do_alloc(memtype, pages, addr_label, fail_label):
        a.mov_r32_imm32(RCX, 1)            # AllocateAddress
        a.mov_r32_imm32(RDX, memtype)
        a.mov_r32_imm32(R8, pages)
        a.lea_rip(R9, addr_label)
        bs_call(BS_ALLOC)
        a.test_rax_rax()
        a.jnz_rel32(fail_label)

    # ---------------- efi_main ----------------
    a.L("efi_main")
    a.push_r(RBP)
    a.mov_rr(RBP, RSP)
    for r in (RBX, R12, R13, R14, R15):
        a.push_r(r)
    a.sub_rsp(8)
    a.sub_rsp(128)
    a.mov_m_r(RBP, O_IMG, RCX)             # ImageHandle
    a.mov_m_r(RBP, O_ST, RDX)              # SystemTable
    a.mov_r_m(RAX, RDX, ST_BS)
    a.mov_m_r(RBP, O_BS, RAX)              # BootServices
    a.mov_r_m(RAX, RDX, ST_CONOUT)
    a.mov_m_r(RBP, O_CON, RAX)             # ConOut

    ser_str("s_boot")
    con_str("c_boot")

    # ---- LocateProtocol(GOP) ----
    a.lea_rip(RCX, "gop_guid")
    a.xor_r32(RDX)
    a.lea_m(R8, RBP, O_GOP)
    bs_call(BS_LOCPROTO)
    a.test_rax_rax()
    a.jnz_rel32("fatal_gop")
    ser_str("s_gop")

    # ---- find 1024x768x32 GOP mode ----
    a.mov_r_m(R13, RBP, O_GOP)             # r13 = GOP iface
    a.mov_r_m(R14, R13, GOP_MODE)          # r14 = Mode*
    a.mov_r32_m(R15, R14, 0)               # r15d = MaxMode
    a.xor_r32(R12)                        # index
    a.L("find_mode")
    a.cmp_r32(R12, R15)
    a.jae_rel32("no_mode")
    a.mov_rr(RCX, R13)                    # QueryMode(gop, idx, &sz, &info)
    a.mov_r32_r32(RDX, R12)
    a.lea_m(R8, RBP, O_MISZ)
    a.lea_m(R9, RBP, O_MINF)
    gop_call(GOP_QUERY)
    a.test_rax_rax()
    a.jnz_rel32("next_mode")
    a.mov_r_m(RAX, RBP, O_MINF)
    a.cmp_m32_imm(RAX, 4, 1024)
    a.jnz_rel32("next_mode")
    a.cmp_m32_imm(RAX, 8, 768)
    a.jnz_rel32("next_mode")
    a.cmp_m32_imm(RAX, 12, 1)             # PixelFormat BGRX32
    a.jnz_rel32("next_mode")
    a.mov_rr(RCX, R13)                    # SetMode(gop, idx)
    a.mov_r32_r32(RDX, R12)
    gop_call(GOP_SETMODE)
    a.test_rax_rax()
    a.jnz_rel32("next_mode")
    a.mov_r_m(R14, R13, GOP_MODE)          # refresh Mode*
    a.mov_r_m(R10, R14, 24)                # FrameBufferBase
    a.mov_m_r(RBP, O_FB, R10)
    a.mov_r_m(RAX, R14, 8)                 # Info*
    a.mov_r32_m(R10, RAX, 20)              # PixelsPerScanLine
    a.shl_r64_imm8(R10, 2)                 # pitch = ppsl * 4
    a.mov_m_r(RBP, O_PITCH, R10)
    a.jmp_rel32("mode_done")
    a.L("next_mode")
    a.inc_r32(R12)
    a.jmp_rel32("find_mode")
    a.L("no_mode")
    ser_str("s_nomode")
    con_str("c_nomode")
    a.jmp_rel32("spin")
    a.L("mode_done")
    ser_str("s_mode")
    con_str("c_mode")

    # ---- allocate fixed regions ----
    do_alloc(1, pages_kernel, "addr_kernel", "fail_ak")
    do_alloc(2, pages_lowmem, "addr_lowmem", "fail_al")
    do_alloc(2, 1, "addr_info", "fail_ai")
    do_alloc(2, 4096, "addr_heap", "fail_ah")     # 16MB heap
    do_alloc(2, 16, "addr_stack", "fail_as")      # 64KB stack
    a.xor_r32(RCX)                               # mmap buf: any pages
    a.mov_r32_imm32(RDX, 2)
    a.mov_r32_imm32(R8, 32)
    a.lea_m(R9, RBP, O_MMBUF)
    bs_call(BS_ALLOC)
    a.test_rax_rax()
    a.jnz_rel32("fail_am")
    ser_str("s_alloc")

    # ---- copy kernel + lowmem into place ----
    a.lea_rva(RSI, kdata_rva, text_rva)
    a.mov_r_imm32sx(RDI, P_KERNEL)
    a.mov_r_imm32sx(RCX, flat_len)
    a.rep_movsb()
    a.lea_rva(RSI, ldata_rva, text_rva)
    a.mov_r_imm32sx(RDI, P_LOWMEM)
    a.mov_r_imm32sx(RCX, lowmem_len)
    a.rep_movsb()
    ser_str("s_copied")

    # ---- 48-byte boot info block at 0x9000 (same ABI as BIOS loader) ----
    IB = P_INFO
    a.mov_m32abs_imm(IB + 0, 48)
    a.mov_m32abs_imm(IB + 4, 0)
    a.mov_m16abs_imm(IB + 8, 8)
    a.mov_m16abs_imm(IB + 10, 0)
    a.mov_m32abs_imm(IB + 12, 32)
    a.mov_r_m(RAX, RBP, O_FB)
    a.mov_mabs_r64(IB + 16, RAX)
    a.mov_r_m(RAX, RBP, O_PITCH)
    a.mov_mabs_r32(IB + 24, RAX)
    a.mov_m32abs_imm(IB + 28, 1024)
    a.mov_m32abs_imm(IB + 32, 768)
    a.mov_m8abs_imm(IB + 36, 32)
    a.mov_m8abs_imm(IB + 37, 1)
    a.mov_m16abs_imm(IB + 38, 0)
    a.mov_m16abs_imm(IB + 40, 0)
    a.mov_m16abs_imm(IB + 42, 0)
    a.mov_m32abs_imm(IB + 44, 8)

    # ---- ExitBootServices (retry on stale MapKey) ----
    a.L("ebs")
    a.mov_m64_imm32(RBP, O_MMSZ, MMAP_BYTES)
    a.lea_m(RCX, RBP, O_MMSZ)
    a.mov_r_m(RDX, RBP, O_MMBUF)
    a.lea_m(R8, RBP, O_KEY)
    a.lea_m(R9, RBP, O_DSZ)
    a.sub_rsp(40)
    a.lea_m(RAX, RBP, O_DVER)
    a.mov_m_r(RSP, 32, RAX)
    a.mov_r_m(RAX, RBP, O_BS)
    a.call_mem(RAX, BS_GETMAP)
    a.add_rsp(40)
    a.test_rax_rax()
    a.jnz_rel32("fail_map")
    a.mov_r_m(RCX, RBP, O_IMG)
    a.mov_r_m(RDX, RBP, O_KEY)
    bs_call(BS_EBS)
    a.test_rax_rax()
    a.jnz_rel8("ebs")
    ser_str("s_ebs")
    con_str("c_booting")

    # ---- enter the kernel (64-bit already; install our GDT, jump) ----
    a.cli()
    a.lea_rip(RAX, "gdt")
    a.lea_rip(RCX, "gdt_desc")
    a.mov_m_r(RCX, 2, RAX)                 # gdt_desc.base = &gdt
    a.lgdt_mem(RCX, 0)
    a.push_imm8(0x18)
    a.lea_rip(RAX, "far64")
    a.push_r(RAX)
    a.retfq()                             # CS=0x18, far jump to far64
    a.L("far64")
    a.mov_ax_imm16(0x20)
    a.mov_sreg_r16(3, RAX)                # ds = ax
    a.mov_sreg_r16(0, RAX)                # es = ax
    a.mov_sreg_r16(2, RAX)                # ss = ax
    a.mov_r_imm32sx(RSP, STACK_TOP)
    a.mov_r_imm32sx(RDI, P_INFO)           # rdi = info block
    a.mov_r_imm64(RAX, kmain_phys)
    a.jmp_reg(RAX)

    # ---- fatal handlers ----
    a.L("fatal_gop")
    ser_str("s_nogop")
    con_str("c_nogop")
    a.jmp_rel32("spin")
    for lbl in ("fail_ak", "fail_al", "fail_ai", "fail_ah",
                "fail_as", "fail_am", "fail_map"):
        a.L(lbl)
        ser_str("s_afail")
        a.jmp_rel32("spin")
    a.L("spin")
    a.hlt()
    a.jmp_rel32("spin")

    # ---- subroutines ----
    a.L("ser_puts")                       # rsi -> ascii cstr
    a.L("ser_loop")
    a.lodsb()
    a.test_al_al()
    a.jz_rel8("ser_done")
    a.mov_r32_imm32(RDX, 0x3F8)
    a.out_dx_al()
    a.jmp_rel8("ser_loop")
    a.L("ser_done")
    a.ret()

    a.L("con_puts")                       # rsi -> utf16 cstr
    a.mov_r_m(RCX, RBP, O_CON)
    a.mov_rr(RDX, RSI)
    a.sub_rsp(48)                         # RSP%16==8 here -> 8 before call
    a.mov_r_m(RAX, RCX, 8)                # OutputString
    a.call_reg(RAX)
    a.add_rsp(48)
    a.ret()

    # ---- data ----
    a.L("gop_guid")
    a.db(0xDE, 0xA9, 0x42, 0x90, 0xDC, 0x23, 0x38, 0x4A,
         0x96, 0xFB, 0x7A, 0xDE, 0xD0, 0x80, 0x51, 0x6A)
    a.L("gdt")
    a.dq(0)
    a.db(0xFF, 0xFF, 0x00, 0x00, 0x00, 0x9A, 0xCF, 0x00)  # 0x08
    a.db(0xFF, 0xFF, 0x00, 0x00, 0x00, 0x92, 0xCF, 0x00)  # 0x10
    a.db(0xFF, 0xFF, 0x00, 0x00, 0x00, 0x9A, 0xAF, 0x00)  # 0x18
    a.db(0xFF, 0xFF, 0x00, 0x00, 0x00, 0x92, 0xCF, 0x00)  # 0x20
    a.L("gdt_desc")
    a.dw(39)
    a.dq(0)                                            # patched at runtime
    a.L("addr_kernel")
    a.dq(P_KERNEL)
    a.L("addr_lowmem")
    a.dq(P_LOWMEM)
    a.L("addr_info")
    a.dq(P_INFO)
    a.L("addr_heap")
    a.dq(P_HEAP)
    a.L("addr_stack")
    a.dq(P_STACK)

    a.L("s_boot")
    a.ascii("UEFI: loader start\r\n")
    a.L("s_gop")
    a.ascii("UEFI: GOP located\r\n")
    a.L("s_mode")
    a.ascii("UEFI: mode 1024x768x32 set\r\n")
    a.L("s_alloc")
    a.ascii("UEFI: memory allocated\r\n")
    a.L("s_copied")
    a.ascii("UEFI: kernel copied\r\n")
    a.L("s_ebs")
    a.ascii("UEFI: boot services exited\r\n")
    a.L("s_nogop")
    a.ascii("UEFI: FATAL no GOP\r\n")
    a.L("s_nomode")
    a.ascii("UEFI: FATAL no 1024x768x32 mode\r\n")
    a.L("s_afail")
    a.ascii("UEFI: FATAL allocation failed\r\n")

    a.L("c_boot")
    a.utf16("Menagerie UEFI loader\r\n")
    a.L("c_mode")
    a.utf16("GOP 1024x768x32\r\n")
    a.L("c_booting")
    a.utf16("Booting kernel...\r\n")
    a.L("c_nogop")
    a.utf16("FATAL: no GOP\r\n")
    a.L("c_nomode")
    a.utf16("FATAL: no 1024x768x32 mode\r\n")

    a.resolve()
    return bytes(a.buf), a.labels["efi_main"]
