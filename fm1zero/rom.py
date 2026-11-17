"""The FM-1's boot loader (JieLi WL82 ROM, USB 4C4A:8057) and the flash loader run in its RAM.

A port of fm1buddy's lib/fm1core/src/uboot.cpp (GPL-3.0-only). The protocol is
as FM-1-transporter (kurogedelic, MIT) and jl-uboot-tool (kagaimiq, MIT)
describe it. The loader is m-bootloader's (jvitkauskas, MIT) with fm1buddy's
screen and reset commands: data/mflr-loader.bin.

Everything here talks through a Storage object with one method:

    command(cdb: bytes, data_in: int = 0, data_out: bytes = b"", timeout_ms: int = 2000) -> bytes | None

It sends one 16-byte command. With data_in it returns the bytes read; with
data_out it returns b"" once they are taken. None means it failed.
"""

from __future__ import annotations

import binascii
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

VID = 0x4C4A
PID = 0x8057
LOADER_AT = 0x1C20000
LOADER_MAX = 0x8000
IO = 512
IO_MAX = 0x1000  # the biggest read tried: as fm1buddy's (the loader says 4096)

# the ROM's
WRITE_MEMORY = 0xFB06
JUMP = 0xFB08
# the loader's
READ_FLASH = 0xFD05
WRITE_FLASH = 0xFB04
ERASE_SECTOR = 0xFB01
READ_KEY = 0xFC09
GET_ONLINE_DEVICE = 0xFC0A
GET_USB_BUFFER = 0xFC14
IDENTIFY = 0xFC40
UNIQUE_ID = 0xFC42
SCREEN_INIT = 0xFC43
SCREEN_ROW = 0xFC44
SCREEN_MAP = 0xFC47
RESET = 0xFC48

SECTOR = 0x1000
FLASH_SIZE = 0x100000
WRITE_FROM = 0x4000  # below: what the chip starts from. Never written
WRITE_TO = 0xFF000  # above: key and MAC. Never written
FM1_KEY = 0x980F
FM1_FLASHES = {0x856014: "Puya P25Q80", 0xB36014: "UCUN UC25HQ80", 0xEB6014: "1 MiB NOR"}
NEW_CHIP = 0xB36014  # the flash in newer FM-1s: firmwares marked chip "old" must not go on it
SMK_CAL = 0xC1000  # an SMK-37's factory calibration sector. Never written
SMK_MARK = 0xC1224

# sector states on the FM-1's own screen (the loader's default colours)
MAP_NONE, MAP_DIFFERS, MAP_WRITTEN, MAP_SAME, MAP_READ, MAP_FAILED = 0, 1, 2, 3, 4, 5

_MAGIC = bytes([0xC3, 0xCF, 0xC0, 0xE8, 0xCE, 0xD2, 0xB0, 0xAE, 0xC4, 0xE3, 0xA3, 0xAC, 0xD3, 0xF1, 0xC1, 0xD6])


class RomError(RuntimeError):
    """The boot loader or the flash loader did not do what was asked."""


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/XMODEM (poly 0x1021), continuing from crc."""
    return binascii.crc_hqx(bytes(data), crc)


def cipher(data: bytes) -> bytes:
    """JieLi's jl_crc_cipher with key 0xFFFFFFFF. Its own inverse."""
    crc = crc16(b"\xFF\xFF", 0xFFFF)
    out = bytearray(data)
    for i in range(len(out)):
        crc = crc16(_MAGIC[i % 16:i % 16 + 1], crc)
        out[i] ^= crc & 0xFF
    return bytes(out)


def cdb_of(cmd: int, args: bytes = b"") -> bytes:
    """A command's 16 bytes: cmd (big endian), its arguments, the rest 0xFF."""
    return (struct.pack(">H", cmd) + args).ljust(16, b"\xFF")


def writable(addr: int) -> bool:
    """One whole sector, inside the part of the flash a firmware install may write."""
    return addr % SECTOR == 0 and WRITE_FROM <= addr and addr + SECTOR <= WRITE_TO


def smk_mark(cal_sector: bytes) -> bool:
    off = SMK_MARK - SMK_CAL
    return cal_sector[off:off + 7] == b"trimdat"


def load_loader() -> bytes:
    data = (Path(__file__).parent / "data" / "mflr-loader.bin").read_bytes()
    if not 0 < len(data) <= LOADER_MAX:
        raise RomError("The loader file is the wrong size.")
    return data


@dataclass
class Chip:
    key: int = 0
    flash_id: int = 0
    size: int = 0
    io: int = IO
    uid: str = ""
    mhz: int = 0
    rom: str = ""
    loader: str = ""

    @property
    def flash_name(self) -> str:
        return FM1_FLASHES.get(self.flash_id, "unknown")

    @property
    def supported(self) -> bool:
        return self.key == FM1_KEY and self.flash_id in FM1_FLASHES and self.size == FLASH_SIZE


@dataclass
class Rom:
    """An FM-1 in its boot loader, with our loader running after attach()."""

    dev: object  # a Storage
    chip: Chip = field(default_factory=Chip)
    sleep: object = time.sleep

    def _exec(self, cmd: int, args: bytes = b"", tries: int = 3, ms: int = 2000) -> bytes | None:
        """A command that answers 16 bytes, its own command echoed first."""
        cdb = cdb_of(cmd, args)
        for _ in range(tries):
            resp = self.dev.command(cdb, data_in=16, timeout_ms=ms)
            if resp is not None and len(resp) == 16 and resp[:2] == cdb[:2]:
                return resp
        return None

    def attach(self) -> Chip:
        """Put the loader in the chip's RAM, run it, and ask what the chip is."""
        inquiry = None
        self.sleep(1.0)  # it answers some time after it is there
        for _ in range(30):
            inquiry = self.dev.command(bytes([0x12, 0, 0, 0, 36, 0]), data_in=36, timeout_ms=1000)
            if inquiry is not None and len(inquiry) == 36:
                break
            inquiry = None
            self.sleep(0.3)
        if inquiry is None:
            raise RomError("Its boot loader didn't answer.")
        self.chip.rom = " ".join(
            part for part in (_text(inquiry[8:16]), _text(inquiry[16:32])) if part)

        # the loader goes in exactly as fm1buddy sends it. Only if the first piece isn't taken is the chip asked
        # whether our loader is running already: an install that stopped part way leaves it there, to try again
        already = False
        loader = load_loader()
        for off in range(0, len(loader), IO):
            piece = cipher(loader[off:off + IO])
            args = struct.pack(">IHB", LOADER_AT + off, len(piece), 0) + struct.pack("<H", crc16(piece))
            cdb = cdb_of(WRITE_MEMORY, args)
            if not any(self.dev.command(cdb, data_out=piece) is not None for _ in range(3)):
                resp = self._exec(IDENTIFY, tries=1, ms=1000) if off == 0 else None
                if resp is not None and resp[2:6] == b"MFLR":
                    already = True
                    break
                raise RomError("Its boot loader didn't take the loader.")

        # run it. The jump's own answer may be lost, so ask READ_KEY until the loader answers
        resp = None
        for _ in range(3):
            if not already:
                self._exec(JUMP, struct.pack(">IH", LOADER_AT, 1), tries=1)
            resp = self._exec(READ_KEY, struct.pack(">I", 0x00AC6900))
            if resp is not None:
                break
        if resp is None:
            raise RomError("The loader didn't start.")
        plain = cipher(bytes([resp[7], resp[6]]))
        self.chip.key = plain[0] | plain[1] << 8
        if self.chip.key != FM1_KEY and (resp[7] | resp[6] << 8) == FM1_KEY:
            self.chip.key = FM1_KEY  # as it comes, not ciphered

        resp = self._exec(GET_ONLINE_DEVICE)
        if resp is None:
            self.leave()
            raise RomError("The loader didn't say what flash it has.")
        self.chip.flash_id = struct.unpack_from("<I", resp, 4)[0]
        cap = self.chip.flash_id & 0xFF
        self.chip.size = 1 << cap if 0x10 <= cap <= 0x18 else FLASH_SIZE

        # reads as big as it really takes: what it says is tried, not trusted
        self.chip.io = IO
        resp = self._exec(GET_USB_BUFFER, tries=2, ms=1000)
        if resp is not None:
            size = min(struct.unpack_from(">I", resp, 2)[0], IO_MAX) // IO * IO
            while size > IO:
                got = self.dev.command(cdb_of(READ_FLASH, struct.pack(">IH", 0, size & 0xFFFF)),
                                       data_in=size)
                if got is not None and len(got) == size:
                    self.chip.io = size
                    break
                size //= 2

        resp = self._exec(IDENTIFY, tries=1, ms=1000)
        if resp is not None and resp[2:6] == b"MFLR":
            self.chip.loader = _text(resp[6:9])
            self.chip.mhz = resp[15]
        resp = self._exec(UNIQUE_ID, tries=1, ms=1000)
        if resp is not None and any(b not in (0, 0xFF) for b in resp[2:10]):
            self.chip.uid = resp[2:10].hex().upper()
        return self.chip

    def read(self, addr: int, length: int) -> bytes:
        """length bytes of the flash from addr."""
        out = bytearray()
        io = max(self.chip.io, IO)
        while len(out) < length:
            n = min(io, length - len(out))
            cdb = cdb_of(READ_FLASH, struct.pack(">IH", addr + len(out), n & 0xFFFF))
            got = None
            for _ in range(3):
                got = self.dev.command(cdb, data_in=n)
                if got is not None and len(got) == n:
                    break
                got = None
            if got is None:
                raise RomError("It stopped answering.")
            out += got
        return bytes(out)

    def write_sector(self, addr: int, data: bytes) -> None:
        """One sector: erased, written, and read back. Only where a firmware may be written."""
        if not writable(addr) or len(data) != SECTOR:
            raise RomError("That isn't somewhere it writes (%06X)." % addr)
        if self._exec(ERASE_SECTOR, struct.pack(">I", addr), tries=3, ms=5000) is None:
            raise RomError("It didn't erase.")
        for off in range(0, SECTOR, IO):
            piece = data[off:off + IO]
            args = struct.pack(">IHB", addr + off, IO, 0) + struct.pack("<H", crc16(piece))
            cdb = cdb_of(WRITE_FLASH, args)
            # the same bytes again is safe: NOR flash only clears bits
            if not any(self.dev.command(cdb, data_out=piece) is not None for _ in range(3)):
                raise RomError("It didn't take what was written.")
        saved, self.chip.io = self.chip.io, IO
        try:
            back = self.read(addr, SECTOR)
        finally:
            self.chip.io = saved
        if back != data:
            raise RomError("It read back differently.")

    def keep_alive(self) -> None:
        self._exec(GET_ONLINE_DEVICE, tries=2, ms=1000)

    # the loader's screen on the FM-1 itself: best effort, never an error
    def screen_init(self, smk: bool = False) -> bool:
        resp = self._exec(SCREEN_INIT, bytes([2 if smk else 1]), tries=2, ms=3000)
        return resp is not None and resp[2] == 1

    def screen_row(self, row: int, text: str, fg: int = 0xFFFF, bg: int = 0x0000) -> None:
        raw = text.upper().encode("ascii", "replace")[:20]
        args = struct.pack(">BHHH", row, fg, bg, len(raw))
        self.dev.command(cdb_of(SCREEN_ROW, args), data_out=raw, timeout_ms=1000)

    def screen_map(self, states: bytes) -> None:
        self.dev.command(cdb_of(SCREEN_MAP, bytes([0, 0, 0, 0, 0, 1, 0])), data_out=bytes(states[:256]).ljust(256, b"\0"),
                         timeout_ms=1000)

    def leave(self) -> None:
        """The chip's own reset: it starts its firmware again. Its answer may be lost."""
        self._exec(RESET, tries=1, ms=500)


def _text(raw: bytes) -> str:
    out = "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw.split(b"\0")[0])
    return out.strip()
