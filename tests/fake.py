"""A pretend FM-1 for the tests: its boot ROM, the loader, and a 1 MiB flash.

It is written from the protocol, not from rom.py, so a mistake in one shows up
against the other: it deciphers and checks what it is sent the way the chip does.
"""

from __future__ import annotations

import os
import struct

from fm1zero import pkg as pkgmod
from fm1zero import rom


def make_flash(smk: bool = False, seed: int = 1) -> bytearray:
    flash = bytearray(os.urandom(rom.FLASH_SIZE) if seed is None else _noise(rom.FLASH_SIZE, seed))
    if smk:
        flash[rom.SMK_MARK:rom.SMK_MARK + 7] = b"trimdat"
    else:
        flash[rom.SMK_MARK:rom.SMK_MARK + 7] = b"\xFF" * 7
    return flash


def _noise(n: int, seed: int) -> bytes:
    out = bytearray()
    x = seed * 2654435761 & 0xFFFFFFFF
    while len(out) < n:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        out += struct.pack("<I", x)
    return bytes(out[:n])


def make_fwsc(product: str, flash_image: bytes) -> bytes:
    """A package in the documented format: markers, ciphered header and table, a type-0 flash image."""
    off = 0x40 + 0x50
    entry = bytearray(0x50)
    struct.pack_into("<H", entry, 0, 0)
    struct.pack_into("<H", entry, 4, pkgmod.crc16(flash_image))
    struct.pack_into("<II", entry, 8, off, len(flash_image))
    entry_enc = pkgmod.enc(bytes(entry))
    header = bytearray(0x40)
    struct.pack_into("<H", header, 2, pkgmod.crc16(entry_enc))
    struct.pack_into("<H", header, 8, 1)
    struct.pack_into("<H", header, 0, pkgmod.crc16(bytes(header[2:])))
    image = pkgmod.enc(bytes(header)) + entry_enc + flash_image
    out = bytearray()
    for i in range(pkgmod.BLOCKS):
        out += image[i * pkgmod.KEEP:(i + 1) * pkgmod.KEEP]
        out.append((ord(product[i]) + i + 1) & 0xFF if i < len(product) else pkgmod.UNUSED)
    out += image[pkgmod.BLOCKS * pkgmod.KEEP:]
    return bytes(out)


class FakeFm1:
    """A Storage: command() as the real USB transport has it."""

    def __init__(self, flash: bytearray | None = None, key: int = rom.FM1_KEY, flash_id: int = 0x856014,
                 usb_buffer: int = 0x1000):
        self.flash = flash if flash is not None else make_flash()
        self.key = key
        self.flash_id = flash_id
        self.usb_buffer = usb_buffer
        self.ram = bytearray(rom.LOADER_MAX)
        self.loaded = 0
        self.running = False  # the loader
        self.reset = False
        self.erased: list[int] = []
        self.programmed: list[int] = []
        self.fail_write_at: int | None = None  # a write here is corrupted
        self.die_after_writes: int | None = None  # it stops answering after this many sector erases
        self.lose_erase_answer_at: int | None = None  # this sector is erased, but its answer never comes
        self.corrupt_protected = False  # a fault: the first write also changes a boot sector
        self.rows: dict[int, bytes] = {}
        self.map = b""

    def command(self, cdb: bytes, data_in: int = 0, data_out: bytes = b"", timeout_ms: int = 2000):
        assert len(cdb) in (6, 16)
        if self.reset or (self.die_after_writes is not None and len(self.erased) > self.die_after_writes):
            return None
        if cdb[0] == 0x12:
            return (bytes(8) + b"WL80    " + b"UBOOT1.00       " + b"1.00")[:data_in]
        cmd = struct.unpack_from(">H", cdb, 0)[0]
        a = cdb[2:]
        if not self.running:
            if cmd == rom.WRITE_MEMORY:
                addr, n, _zero = struct.unpack_from(">IHB", a, 0)
                crc = struct.unpack_from("<H", a, 7)[0]
                if n != len(data_out) or rom.crc16(data_out) != crc:
                    return None
                plain = rom.cipher(data_out)
                at = addr - rom.LOADER_AT
                self.ram[at:at + n] = plain
                self.loaded = max(self.loaded, at + n)
                return b""
            if cmd == rom.JUMP:
                addr = struct.unpack_from(">I", a, 0)[0]
                if addr == rom.LOADER_AT and bytes(self.ram[:self.loaded]) == rom.load_loader():
                    self.running = True
                return None  # the jump's answer is lost
            return None
        if cmd == rom.READ_KEY:
            k = rom.cipher(struct.pack("<H", self.key))
            return self._answer(cdb, bytes(4) + bytes([k[1], k[0]]))
        if cmd == rom.GET_ONLINE_DEVICE:
            return self._answer(cdb, bytes(2) + struct.pack("<I", self.flash_id))
        if cmd == rom.GET_USB_BUFFER:
            return self._answer(cdb, struct.pack(">I", self.usb_buffer))
        if cmd == rom.IDENTIFY:
            return self._answer(cdb, b"MFLR1.3" + bytes(6) + bytes([120]))
        if cmd == rom.UNIQUE_ID:
            return self._answer(cdb, bytes.fromhex("0102030405060708"))
        if cmd == rom.READ_FLASH:
            addr, n = struct.unpack_from(">IH", a, 0)
            if n != data_in or n > self.usb_buffer or addr + n > len(self.flash):
                return None
            return bytes(self.flash[addr:addr + n])
        if cmd == rom.ERASE_SECTOR:
            addr = struct.unpack_from(">I", a, 0)[0]
            self.erased.append(addr)
            self.flash[addr:addr + rom.SECTOR] = b"\xFF" * rom.SECTOR
            if self.corrupt_protected:
                self.flash[0x100] ^= 0xFF
                self.corrupt_protected = False
            if self.lose_erase_answer_at == addr:
                return None
            return self._answer(cdb, b"")
        if cmd == rom.WRITE_FLASH:
            addr, n, _zero = struct.unpack_from(">IHB", a, 0)
            crc = struct.unpack_from("<H", a, 7)[0]
            if n != len(data_out) or rom.crc16(data_out) != crc:
                return None
            self.programmed.append(addr)
            data = bytearray(data_out)
            if self.fail_write_at is not None and addr == self.fail_write_at:
                data[0] ^= 0x01
            for i, b in enumerate(data):  # NOR flash: bits only clear
                self.flash[addr + i] &= b
            return b""
        if cmd == rom.SCREEN_INIT:
            return self._answer(cdb, bytes([1]))
        if cmd == rom.SCREEN_ROW:
            self.rows[a[0]] = bytes(data_out)
            return b""
        if cmd == rom.SCREEN_MAP:
            self.map = bytes(data_out)
            return b""
        if cmd == rom.RESET:
            self.reset = True
            return None
        return None

    @staticmethod
    def _answer(cdb: bytes, rest: bytes) -> bytes:
        return (cdb[:2] + rest).ljust(16, b"\0")
