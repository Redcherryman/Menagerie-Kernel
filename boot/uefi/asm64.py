"""Tiny x86-64 assembler: labels + RIP-relative fixups."""
import struct


# register numbers
RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = 0, 1, 2, 3, 4, 5, 6, 7
R8, R9, R10, R11, R12, R13, R14, R15 = 8, 9, 10, 11, 12, 13, 14, 15


class A64:
    def __init__(self):
        self.buf = bytearray()
        self.labels = {}
        self.fixups = []  # (pos, label, kind) kind in ('rel32','rip32')

    # -- plumbing ---------------------------------------------------------
    def L(self, name):
        if name in self.labels:
            raise ValueError("dup label " + name)
        self.labels[name] = len(self.buf)

    def db(self, *vs):
        self.buf += bytes(vs)

    def dw(self, v):
        self.buf += struct.pack("<H", v)

    def dd(self, v):
        self.buf += struct.pack("<I", v & 0xFFFFFFFF)

    def dq(self, v):
        self.buf += struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)

    def ascii(self, s):
        self.buf += s.encode("ascii") + b"\x00"

    def utf16(self, s):
        self.buf += s.encode("utf-16-le") + b"\x00\x00"

    def _fix(self, label, kind):
        self.fixups.append((len(self.buf), label, kind))
        self.buf += b"\x00" * (1 if kind == "rel8" else 4)

    @staticmethod
    def _rex(w, reg, rm):
        b = 0x40 | (w << 3)
        if reg >= 8:
            b |= 0x04
        if rm >= 8:
            b |= 0x01
        return b

    @staticmethod
    def _modrm(mod, reg, rm):
        return (mod << 6) | ((reg & 7) << 3) | (rm & 7)

    def _mem(self, base, disp):
        """modrm+SIB+disp bytes for [base+disp], reg field zeroed."""
        out = bytearray()
        if base == RSP:
            if disp == 0:
                out += bytes([0x04, 0x24])
            elif -128 <= disp <= 127:
                out += bytes([0x44, 0x24, disp & 0xFF])
            else:
                out += bytes([0x84, 0x24]) + struct.pack("<i", disp)
        else:
            if disp == 0 and base != RBP:
                out += bytes([0x00 | (base & 7)])
            elif -128 <= disp <= 127:
                out += bytes([0x40 | (base & 7), disp & 0xFF])
            else:
                out += bytes([0x80 | (base & 7)]) + struct.pack("<i", disp)
        return out

    def _op_mem(self, opc, rex, reg, base, disp):
        mb = self._mem(base, disp)
        mb[0] |= (reg & 7) << 3
        if rex:
            self.buf.append(rex)
        self.buf.append(opc)
        self.buf += mb

    # -- data movement ----------------------------------------------------
    def push_r(self, r):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0x50 | (r & 7))

    def pop_r(self, r):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0x58 | (r & 7))

    def mov_rr(self, dst, src):
        self.buf.append(self._rex(1, dst, src))
        self.buf.append(0x8B)
        self.buf.append(self._modrm(3, dst, src))

    def mov_r_imm64(self, r, imm):
        self.buf.append(self._rex(1, 0, r))
        self.buf.append(0xB8 | (r & 7))
        self.buf += struct.pack("<Q", imm & 0xFFFFFFFFFFFFFFFF)

    def mov_r_imm32sx(self, r, imm):
        """mov r64, imm32 (sign-extended)."""
        self.buf.append(self._rex(1, 0, r))
        self.buf.append(0xC7)
        self.buf.append(self._modrm(3, 0, r))
        self.buf += struct.pack("<i", imm)

    def mov_r32_imm32(self, r, imm):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0xB8 | (r & 7))
        self.buf += struct.pack("<I", imm & 0xFFFFFFFF)

    def lea_rip(self, r, label):
        self.buf.append(self._rex(1, r, 0))
        self.buf.append(0x8D)
        self.buf.append(self._modrm(0, r, 5))
        self._fix(label, "rip32")

    def lea_rva(self, r, target_rva, text_rva):
        """RIP-relative LEA to an absolute RVA (for .kdata/.ldata)."""
        cur_rva = text_rva + len(self.buf) + 7
        disp = target_rva - cur_rva
        self.buf.append(self._rex(1, r, 0))
        self.buf.append(0x8D)
        self.buf.append(self._modrm(0, r, 5))
        self.buf += struct.pack("<i", disp)

    def mov_r_m(self, r, base, disp):
        self._op_mem(0x8B, self._rex(1, r, base), r, base, disp)

    def mov_m_r(self, base, disp, r):
        self._op_mem(0x89, self._rex(1, r, base), r, base, disp)

    def mov_r32_m(self, r, base, disp):
        rex = 0x40
        if r >= 8:
            rex |= 0x04
        if base >= 8:
            rex |= 0x01
        self._op_mem(0x8B, rex if rex != 0x40 else 0, r, base, disp)

    def mov_r32_r32(self, dst, src):
        rex = 0x40
        if src >= 8:
            rex |= 0x04
        if dst >= 8:
            rex |= 0x01
        if rex != 0x40:
            self.buf.append(rex)
        self.buf.append(0x89)
        self.buf.append(self._modrm(3, src, dst))

    def lea_m(self, r, base, disp):
        mb = self._mem(base, disp)
        mb[0] |= (r & 7) << 3
        self.buf.append(self._rex(1, r, base))
        self.buf.append(0x8D)
        self.buf += mb

    def mov_m64_imm32(self, base, disp, imm):
        mb = self._mem(base, disp)
        self.buf.append(self._rex(1, 0, base))
        self.buf.append(0xC7)
        self.buf += mb
        self.buf += struct.pack("<I", imm & 0xFFFFFFFF)

    def test_al_al(self):
        self.buf += bytes([0x84, 0xC0])

    def jmp_reg(self, r):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0xFF)
        self.buf.append(self._modrm(3, 4, r))

    def mov_r_mabs64(self, r, addr):
        self.buf.append(self._rex(1, r, 0))
        self.buf.append(0x8B)
        self.buf.append(self._modrm(0, r, 5))
        self.buf += struct.pack("<I", addr)

    def mov_mabs_r64(self, addr, r):
        self.buf.append(self._rex(1, r, 0))
        self.buf.append(0x89)
        self.buf.append(self._modrm(0, r, 5))
        self.buf += struct.pack("<I", addr)

    def mov_mabs_r32(self, addr, r):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0x89)
        self.buf.append(self._modrm(0, r, 5))
        self.buf += struct.pack("<I", addr)

    def mov_m32abs_imm(self, addr, imm):
        self.buf.append(0xC7)
        self.buf.append(self._modrm(0, 0, 5))
        self.buf += struct.pack("<I", addr)
        self.buf += struct.pack("<I", imm & 0xFFFFFFFF)

    def mov_m16abs_imm(self, addr, imm):
        self.buf.append(0x66)
        self.buf.append(0xC7)
        self.buf.append(self._modrm(0, 0, 5))
        self.buf += struct.pack("<I", addr)
        self.buf += struct.pack("<H", imm & 0xFFFF)

    def mov_m8abs_imm(self, addr, imm):
        self.buf.append(0xC6)
        self.buf.append(self._modrm(0, 0, 5))
        self.buf += struct.pack("<I", addr)
        self.buf.append(imm & 0xFF)

    # -- arithmetic / logic ------------------------------------------------
    def sub_rsp(self, n):
        self.buf += bytes([0x48, 0x83, 0xEC, n & 0xFF])

    def add_rsp(self, n):
        self.buf += bytes([0x48, 0x83, 0xC4, n & 0xFF])

    def test_rax_rax(self):
        self.buf += bytes([0x48, 0x85, 0xC0])

    def xor_r32(self, r):
        rex = 0x40 | (0x04 if r >= 8 else 0) | (0x01 if r >= 8 else 0)
        if rex != 0x40:
            self.buf.append(rex)
        self.buf.append(0x31)
        self.buf.append(self._modrm(3, r, r))

    def inc_r32(self, r):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0xFF)
        self.buf.append(self._modrm(3, 0, r))

    def cmp_r32(self, a, b):
        """cmp r32 a, r32 b."""
        rex = 0x40
        if a >= 8:
            rex |= 0x01
        if b >= 8:
            rex |= 0x04
        if rex != 0x40:
            self.buf.append(rex)
        self.buf.append(0x39)
        self.buf.append(self._modrm(3, b, a))

    def cmp_m32_imm(self, base, disp, imm):
        self._op_mem(0x81, 0, 7, base, disp)
        self.buf += struct.pack("<I", imm & 0xFFFFFFFF)

    def shl_r64_imm8(self, r, n):
        self.buf.append(self._rex(1, 0, r))
        self.buf.append(0xC1)
        self.buf.append(self._modrm(3, 4, r))
        self.buf.append(n & 0xFF)

    def mov_ax_imm16(self, imm):
        self.buf += bytes([0x66, 0xB8]) + struct.pack("<H", imm & 0xFFFF)

    def mov_sreg_r16(self, sreg, r):
        """mov Sreg, r16. sreg: 0=ES 2=SS 3=DS."""
        self.buf.append(0x8E)
        self.buf.append(self._modrm(3, sreg, r))

    # -- control flow ------------------------------------------------------
    def call_reg(self, r):
        if r >= 8:
            self.buf.append(0x41)
        self.buf.append(0xFF)
        self.buf.append(self._modrm(3, 2, r))

    def call_mem(self, base, disp):
        self._op_mem(0xFF, 0, 2, base, disp)

    def call_rel(self, label):
        self.buf.append(0xE8)
        self._fix(label, "rel32")

    def jmp_rel8(self, label):
        self.buf.append(0xEB)
        self._fix(label, "rel8")

    def jmp_rel32(self, label):
        self.buf.append(0xE9)
        self._fix(label, "rel32")

    def jcc_rel8(self, opc, label):
        self.buf.append(opc)
        self._fix(label, "rel8")

    def jnz_rel8(self, label):
        self.jcc_rel8(0x75, label)

    def jz_rel8(self, label):
        self.jcc_rel8(0x74, label)

    def jae_rel8(self, label):
        self.jcc_rel8(0x73, label)

    def jne_rel32(self, label):
        self.buf.append(0x0F)
        self.buf.append(0x85)
        self._fix(label, "rel32")

    def jnz_rel32(self, label):
        self.jne_rel32(label)

    def jae_rel32(self, label):
        self.buf.append(0x0F)
        self.buf.append(0x83)
        self._fix(label, "rel32")

    # -- misc --------------------------------------------------------------
    def rep_movsb(self):
        self.buf += bytes([0xF3, 0xA4])

    def cli(self):
        self.buf.append(0xFA)

    def hlt(self):
        self.buf.append(0xF4)

    def ret(self):
        self.buf.append(0xC3)

    def retfq(self):
        self.buf += bytes([0x48, 0xCF])

    def lgdt_mem(self, base, disp):
        mb = self._mem(base, disp)
        mb[0] |= 2 << 3
        self.buf.append(0x0F)
        self.buf.append(0x01)
        self.buf += mb

    def out_dx_al(self):
        self.buf.append(0xEE)

    def lodsb(self):
        self.buf.append(0xAC)

    def push_imm8(self, v):
        self.buf += bytes([0x6A, v & 0xFF])

    def resolve(self):
        for pos, label, kind in self.fixups:
            if label not in self.labels:
                raise ValueError("unresolved label " + label)
            tgt = self.labels[label]
            if kind == "rel32" or kind == "rip32":
                disp = tgt - (pos + 4)
                struct.pack_into("<i", self.buf, pos, disp)
            elif kind == "rel8":
                disp = tgt - (pos + 1)
                if not -128 <= disp <= 127:
                    raise ValueError(f"rel8 out of range for {label}")
                self.buf[pos] = disp & 0xFF
            else:
                raise ValueError("bad fixup kind " + kind)
        self.fixups = []
