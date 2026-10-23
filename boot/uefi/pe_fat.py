"""PE32+ image assembly and FAT16 image building for the UEFI loader."""
import struct


def align_up(n, a):
    return (n + a - 1) // a * a


IMAGE_BASE = 0x200000
SECT_ALIGN = 0x1000
FILE_ALIGN = 0x200


def build_pe(text, kdata, ldata, entry_offset):
    """Assemble the PE32+ .efi. Returns (file_bytes, text_rva, kdata_rva,
    ldata_rva). No relocations: UEFI loads it at IMAGE_BASE."""
    text_rva = SECT_ALIGN
    kdata_rva = text_rva + align_up(len(text), SECT_ALIGN)
    ldata_rva = kdata_rva + align_up(len(kdata), SECT_ALIGN)

    def pad(b, a):
        return bytes(b) + b"\x00" * (align_up(len(b), a) - len(b))

    text_raw = pad(text, FILE_ALIGN)
    kdata_raw = pad(kdata, FILE_ALIGN)
    ldata_raw = pad(ldata, FILE_ALIGN)

    size_image = align_up(ldata_rva + len(ldata), SECT_ALIGN)
    size_headers = FILE_ALIGN

    # DOS header
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)

    out = bytearray()
    out += dos
    out += b"PE\0\0"
    # COFF header
    out += struct.pack("<HHIIIHH", 0x8664, 3, 0, 0, 0, 0xF0, 0x0022)
    # Optional header (PE32+)
    oh = bytearray(0xF0)
    struct.pack_into("<H", oh, 0, 0x020B)                       # magic
    struct.pack_into("<I", oh, 4, len(text_raw))               # SizeOfCode
    struct.pack_into("<I", oh, 8, len(kdata_raw) + len(ldata_raw))
    struct.pack_into("<I", oh, 16, text_rva + entry_offset)     # entry
    struct.pack_into("<I", oh, 20, text_rva)                    # BaseOfCode
    struct.pack_into("<Q", oh, 24, IMAGE_BASE)
    struct.pack_into("<I", oh, 32, SECT_ALIGN)
    struct.pack_into("<I", oh, 36, FILE_ALIGN)
    struct.pack_into("<I", oh, 56, size_image)
    struct.pack_into("<I", oh, 60, size_headers)
    struct.pack_into("<H", oh, 68, 10)                         # EFI_APPLICATION
    struct.pack_into("<Q", oh, 72, 0x100000)                   # stack reserve
    struct.pack_into("<Q", oh, 80, 0x10000)                    # stack commit
    struct.pack_into("<Q", oh, 88, 0x100000)                   # heap reserve
    struct.pack_into("<Q", oh, 96, 0x1000)                     # heap commit
    struct.pack_into("<I", oh, 108, 16)                        # No. RVA+sizes
    out += oh
    # section headers
    raw_ptr = size_headers
    for name, vsize, rva, raw in (
            (b".text\0\0\0", len(text), text_rva, text_raw),
            (b".kdata\0\0", len(kdata), kdata_rva, kdata_raw),
            (b".ldata\0\0", len(ldata), ldata_rva, ldata_raw)):
        sh = bytearray(40)
        sh[0:8] = name
        struct.pack_into("<IIII", sh, 8, vsize, rva, len(raw), raw_ptr)
        struct.pack_into("<I", sh, 36,
                         0x60000020 if name.startswith(b".text")
                         else 0x40000040)
        out += sh
        raw_ptr += len(raw)
    assert len(out) <= size_headers, "headers overflow"
    out += b"\x00" * (size_headers - len(out))
    out += text_raw + kdata_raw + ldata_raw
    return bytes(out), text_rva, kdata_rva, ldata_rva


def build_fat16(efi_bytes, fs_sectors, spc, extra_files=(), hidden=0):
    """Minimal FAT16 *filesystem* of exactly fs_sectors sectors, with
    /EFI/BOOT/BOOTX64.EFI (+ optional root files as (name83, bytes)).
    `hidden` = LBA where this filesystem starts (for the BPB).
    Returns filesystem bytes."""
    SECT = 512
    total = fs_sectors
    reserved = 32
    root_entries = 512
    root_sects = root_entries * 32 // SECT
    fat_sects = 1
    while True:
        data = total - reserved - 2 * fat_sects - root_sects
        clusters = data // spc
        need = (clusters * 2 + SECT - 1) // SECT
        if need <= fat_sects:
            break
        fat_sects = need
    assert 4085 < clusters <= 65525, f"bad FAT16 cluster count {clusters}"
    root_sec = reserved + 2 * fat_sects
    data_sec = root_sec + root_sects

    img = bytearray(total * SECT)

    def w8(o, v):
        img[o] = v & 0xFF

    def w16(o, v):
        struct.pack_into("<H", img, o, v)

    def w32(o, v):
        struct.pack_into("<I", img, o, v)

    # BPB
    img[0:3] = b"\xEB\x3C\x90"
    img[3:11] = b"MSDOS5.0"
    w16(11, SECT)
    w8(13, spc)
    w16(14, reserved)
    w8(16, 2)
    w16(17, root_entries)
    w16(21, 0xF8)
    w16(22, fat_sects)
    w16(24, 32)
    w16(26, 64)
    w32(32, total)
    w32(28, hidden)
    w8(36, 0x80)
    w8(38, 0x29)
    w32(39, 0x12345678)
    img[43:54] = b"MENAGERIE  "
    img[54:62] = b"FAT16   "
    w16(510, 0xAA55)

    # FATs
    fat = [0] * (clusters + 2)
    fat[0] = 0xFFF8
    fat[1] = 0xFFFF

    def fat_write():
        for f in range(2):
            base = (reserved + f * fat_sects) * SECT
            for i, v in enumerate(fat):
                struct.pack_into("<H", img, base + i * 2, v)

    def clus_sec(c):
        return data_sec + (c - 2) * spc

    def dent(name83, attr, cluster, size):
        assert len(name83) == 11, f"bad 8.3 name {name83!r}"
        e = bytearray(32)
        e[0:11] = name83.encode("ascii")
        e[11] = attr
        struct.pack_into("<H", e, 26, cluster)
        struct.pack_into("<I", e, 28, size)
        return e

    def write_chain(start, data):
        n = (len(data) + spc * SECT - 1) // (spc * SECT)
        for i in range(n):
            c = start + i
            chunk = data[i * spc * SECT:(i + 1) * spc * SECT]
            o = clus_sec(c) * SECT
            img[o:o + len(chunk)] = chunk
            fat[c] = start + i + 1 if i + 1 < n else 0xFFFF

    nxt = [4]  # next free cluster (2,3 reserved for dirs)

    def alloc_chain(data):
        s = nxt[0]
        write_chain(s, data)
        nxt[0] = s + (len(data) + spc * SECT - 1) // (spc * SECT)
        return s

    # directories: cluster 2 = EFI, cluster 3 = BOOT
    fat[2] = 0xFFFF
    fat[3] = 0xFFFF
    o = clus_sec(2) * SECT
    img[o:o + 32] = dent("BOOT       ", 0x10, 3, 0)
    img[o + 32:o + 64] = dent("..         ", 0x10, 0, 0)
    o = clus_sec(3) * SECT
    fc = alloc_chain(efi_bytes)
    img[o:o + 32] = dent("BOOTX64 EFI", 0x20, fc, len(efi_bytes))
    img[o + 32:o + 64] = dent("..         ", 0x10, 2, 0)
    # root dir: EFI subdir + optional extra files
    o = root_sec * SECT
    img[o:o + 32] = dent("EFI        ", 0x10, 2, 0)
    pos = 32
    for name83, data in extra_files:
        fc = alloc_chain(data)
        img[o + pos:o + pos + 32] = dent(name83, 0x20, fc, len(data))
        pos += 32
    fat_write()
    return bytes(img)


def build_uefi_disk(efi_bytes, total_mb, spc, extra_files=(),
                   gpt=False):
    """Full partitioned disk with FAT16 ESP.
    MBR mode: MBR (type 0xEF) at LBA 0, filesystem at LBA 2048.
    GPT mode: protective MBR + GPT header + ESP at LBA 2048.
    Returns disk bytes."""
    SECT = 512
    PART_LBA = 2048
    total = total_mb * 1024 * 1024 // SECT
    fs = build_fat16(efi_bytes, total - PART_LBA, spc, extra_files,
                     hidden=PART_LBA)
    img = bytearray(total * SECT)
    img[PART_LBA * SECT:PART_LBA * SECT + len(fs)] = fs
    if not gpt:
        # MBR partition entry
        pe = 446
        img[pe + 4] = 0xEF
        struct.pack_into("<I", img, pe + 8, PART_LBA)
        struct.pack_into("<I", img, pe + 12, total - PART_LBA)
        struct.pack_into("<H", img, 510, 0xAA55)
    else:
        # Protective MBR
        img[446 + 4] = 0xEE
        struct.pack_into("<I", img, 446 + 8, 1)
        struct.pack_into("<I", img, 446 + 12, total - 1)
        struct.pack_into("<H", img, 510, 0xAA55)
        # GPT header at LBA 1
        gh = PART_LBA * 0  # placeholder, real offset below
        o = SECT
        img[o:o + 8] = b"EFI PART"
        struct.pack_into("<I", img, o + 8, 0x00010000)   # revision
        struct.pack_into("<I", img, o + 12, 92)          # header size
        # CRC32 (patch later), reserved
        struct.pack_into("<Q", img, o + 24, 1)           # current LBA
        struct.pack_into("<Q", img, o + 32, total - 1)   # backup LBA
        struct.pack_into("<Q", img, o + 40, 34)          # first usable
        struct.pack_into("<Q", img, o + 48, total - 34)  # last usable
        # disk GUID (random-ish fixed)
        img[o + 56:o + 72] = bytes.fromhex("1234567890abcdef1234567890abcdef")
        struct.pack_into("<Q", img, o + 72, 2)           # part entries LBA
        struct.pack_into("<I", img, o + 76, 128)         # num entries
        struct.pack_into("<I", img, o + 80, 128)         # entry size
        # partition entry at LBA 2: ESP GUID, unique GUID, LBA range, name
        peo = 2 * SECT
        # type GUID C12A7328-F81F-11D2-BA4B-00A0C93EC93B (LE mixed-endian)
        img[peo:peo + 16] = bytes.fromhex("28a3c1121ff8d211ba4b00a0c93ec93b")
        img[peo + 16:peo + 32] = bytes.fromhex("abcdef1234567890abcdef1234567890")
        struct.pack_into("<Q", img, peo + 32, PART_LBA)
        struct.pack_into("<Q", img, peo + 40, total - 1)
        struct.pack_into("<Q", img, peo + 48, 0)         # attrs
        img[peo + 56:peo + 58] = "E".encode("utf-16-le")
        img[peo + 58:peo + 60] = "S".encode("utf-16-le")
        img[peo + 60:peo + 62] = "P".encode("utf-16-le")
        # CRC32 of header (with CRC field zeroed)
        import zlib
        struct.pack_into("<I", img, o + 16, 0)
        crc = zlib.crc32(img[o:o + 92]) & 0xFFFFFFFF
        struct.pack_into("<I", img, o + 16, crc)
        # backup GPT at end (header + entries)
        img[(total - 1) * SECT:(total - 1) * SECT + 92] = img[o:o + 92]
        struct.pack_into("<Q", img, (total - 1) * SECT + 24, total - 1)
        struct.pack_into("<Q", img, (total - 1) * SECT + 32, 1)
        # copy partition entries to backup location
        img[(total - 33) * SECT:(total - 32) * SECT] = img[2 * SECT:3 * SECT]
    return bytes(img)
