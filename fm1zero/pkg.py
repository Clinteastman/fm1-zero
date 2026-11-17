"""The .fwsc firmware package: who it is for, and the flash image inside it.

A port of fm1buddy's lib/fm1core/src/fm1.cpp (the package part), itself a port
of foma's web/fm1pkg.js (Copyright (C) 2026 Leo Kuroshita, Hugelton
Instruments). GPL-3.0-only.

A package is the "logical image" with one marker byte put after every 47 bytes
of its first 20 (or more) 48-byte blocks. The markers spell the product it is
for ("FM-1_015"). The logical image is a 64-byte ciphered header, up to eight
80-byte ciphered file entries, then the files. File type 0 is flash.bin: the
flash as it should be, from address 0.
"""

from __future__ import annotations

import binascii
import hashlib
import struct
from dataclasses import dataclass

BLOCKS = 20  # identity blocks every package has
MAX_BLOCKS = 64
BLK = 0x30
KEEP = 0x2F
UNUSED = 0x7D
MIN_SIZE = BLOCKS * BLK
MAX_SIZE = 16 * 1024 * 1024


class PackageError(ValueError):
    """The file is not a package this tool can use."""


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/XMODEM (poly 0x1021), continuing from crc."""
    return binascii.crc_hqx(bytes(data), crc)


def enc(data: bytes) -> bytes:
    """JieLi's ENC stream cipher. Its own inverse."""
    out = bytearray(data)
    key = 0xFFFF
    for i in range(len(out)):
        out[i] ^= key & 0xFF
        key = ((key << 1) ^ (0x1021 if key & 0x8000 else 0)) & 0xFFFF
    return bytes(out)


def _id_char(marker: int, index: int) -> bool:
    return 0x20 <= ((marker - index - 1) & 0xFF) < 0x7F


def _marker(raw: bytes, k: int) -> int:
    return raw[k * BLK + KEEP] if (k + 1) * BLK <= len(raw) else -1


def _id_blocks(raw: bytes) -> int:
    k = BLOCKS
    while k < MAX_BLOCKS:
        m = _marker(raw, k)
        if m < 0 or m == UNUSED or not _id_char(m, k):
            break
        k += 1
    return k if k > BLOCKS and _marker(raw, k) == UNUSED else BLOCKS


def _block_count(raw: bytes) -> int:
    k = _id_blocks(raw)
    while k < MAX_BLOCKS and _marker(raw, k) == UNUSED:
        k += 1
    return k


def _id_text(raw: bytes, ids: int) -> str | None:
    out = []
    for i in range(ids):
        m = raw[i * BLK + KEEP]
        if m == UNUSED:
            continue
        if not _id_char(m, i) or len(out) >= 23:
            return None
        out.append(chr((m - i - 1) & 0xFF))
    return "".join(out) or None


def _logical(raw: bytes, blocks: int) -> bytes:
    parts = [raw[i * BLK:i * BLK + KEEP] for i in range(blocks)]
    parts.append(raw[blocks * BLK:])
    return b"".join(parts)


def _entries(image: bytes):
    """The file table: (type, crc, offset, length) for each entry. None if the header is bad."""
    if len(image) < 0x40:
        return None
    header = enc(image[:0x40])
    count = struct.unpack_from("<H", header, 8)[0]
    if crc16(header[2:0x40]) != struct.unpack_from("<H", header, 0)[0]:
        return None
    if not 1 <= count <= 8 or len(image) < 0x40 + 0x50 * count:
        return None
    if crc16(image[0x40:0x40 + 0x50 * count]) != struct.unpack_from("<H", header, 2)[0]:
        return None
    out = []
    for i in range(count):
        e = enc(image[0x40 + i * 0x50:0x40 + (i + 1) * 0x50])
        ftype, crc = struct.unpack_from("<H", e, 0)[0], struct.unpack_from("<H", e, 4)[0]
        off, length = struct.unpack_from("<II", e, 8)
        out.append((ftype, crc, off, length))
    return out


def _intact(image: bytes) -> bool:
    entries = _entries(image)
    if entries is None:
        return False
    for ftype, crc, off, length in entries:
        if off > len(image) or length > len(image) - off:
            return False
        if ftype in (0, 100) and crc16(image[off:off + length]) != crc:
            return False
    return True


def model_of(product: str) -> str:
    """'ota-SMK-37 Elite_016' -> 'SMK-37 Elite'."""
    if product[:4].lower() == "ota-":
        product = product[4:]
    cut = product.rfind("_")
    return product[:cut] if cut >= 0 else product


def is_smk(product: str) -> bool:
    """The SMK-37 family: the FM-1's chip in a keyboard, under several names."""
    m = model_of(product).upper()
    return m.startswith("SMK-37 ") or m.startswith("SMK-37+") or m == "MKE-P37" or m.startswith("STARRYKEY-37")


def is_fm1(product: str) -> bool:
    return model_of(product).upper() == "FM-1"


def same_hardware(a: str, b: str) -> bool:
    """a and b run the same firmwares. Never an FM-1 and an SMK-37."""
    return model_of(a).lower() == model_of(b).lower() or (is_smk(a) and is_smk(b))


@dataclass
class Package:
    product: str  # "FM-1_904"
    sha256: str  # of the .fwsc file
    file_size: int
    image: bytes  # the logical image
    flash_at: int  # where flash.bin is in image
    flash_len: int  # its length: the flash from 0 to here

    @property
    def flash(self) -> bytes:
        return self.image[self.flash_at:self.flash_at + self.flash_len]


def parse(raw: bytes) -> Package:
    """Check a whole .fwsc and return what is in it. Raises PackageError."""
    if not MIN_SIZE <= len(raw) <= MAX_SIZE:
        raise PackageError("That isn't a firmware package.")
    blocks = _block_count(raw)
    product = _id_text(raw, _id_blocks(raw))
    if not product:
        raise PackageError("That isn't a firmware package.")
    image = _logical(raw, blocks)
    if blocks != BLOCKS and not _intact(image):
        # the unused markers past block 20 were there by chance
        again = _logical(raw, BLOCKS)
        if _intact(again):
            image, blocks = again, BLOCKS
            product = _id_text(raw, BLOCKS) or product
    if not _intact(image):
        raise PackageError("The package is damaged (its checks don't match).")
    for ftype, _crc, off, length in _entries(image) or []:
        if ftype == 0:
            return Package(product, hashlib.sha256(raw).hexdigest(), len(raw), image, off, length)
    raise PackageError("That package has no flash image in it.")


def load(path) -> Package:
    with open(path, "rb") as f:
        raw = f.read(MAX_SIZE + 1)
    return parse(raw)
