"""What is done to an FM-1 once it is in its boot loader: identify, back up, install, restore.

The rules, the same as lilfm1buddy's and fm1-flip's writers:

- only whole 4 KiB sectors in [0x4000, 0xFF000) are ever written
- an SMK-37's calibration sector (0xC1000) is never written
- an FM-1's files never go on an SMK-37, nor the other way round
- a sector is read first and written only if it differs, then read back
- the sectors that must not change are checked again at the end
- everything that can be checked is checked before the first erase
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import pkg as pkgmod
from .rom import (FLASH_SIZE, MAP_DIFFERS, MAP_FAILED, MAP_NONE, MAP_READ, MAP_SAME, MAP_WRITTEN, NEW_CHIP, SECTOR,
                  SMK_CAL, WRITE_FROM, WRITE_TO, Chip, Rom, RomError, smk_mark, writable)

SECTORS = FLASH_SIZE // SECTOR


class JobError(RuntimeError):
    """The job stopped. wrote says whether anything was written before it did."""

    def __init__(self, message: str, wrote: bool = False, final: bool = False):
        super().__init__(message)
        self.wrote = wrote  # an erase or a write was at least tried: the flash may be part written
        self.final = final  # trying the same job again would hide what went wrong


@dataclass
class Progress:
    title: str = ""
    done: int = 0
    total: int = 0
    written: int = 0
    states: bytearray = field(default_factory=lambda: bytearray(SECTORS))


Report = Callable[[Progress], None]


def _nothing(_p: Progress) -> None:
    pass


def _require_supported(chip: Chip) -> None:
    if not chip.supported:
        raise JobError("This chip isn't one this tool knows (key %04X, flash %06X). Nothing was written."
                       % (chip.key, chip.flash_id))


def _protected(rom: Rom) -> list[int]:
    """A CRC of every sector that must not change."""
    out = []
    for addr in range(0, FLASH_SIZE, SECTOR):
        if not writable(addr):
            out.append(zlib.crc32(rom.read(addr, SECTOR)))
    return out


def _show(rom: Rom, p: Progress, report: Report, screen: bool) -> None:
    report(p)
    if screen:
        percent = 100 * p.done // p.total if p.total else 0
        rom.screen_row(0, "%s %d%%" % (p.title, percent))
        rom.screen_map(bytes(p.states))


def backup(rom: Rom, report: Report = _nothing) -> bytes:
    """The whole flash, read twice and compared."""
    chip = rom.chip
    _require_supported(chip)
    screen = rom.screen_init(False)
    if screen:
        rom.screen_row(1, "FM1-ZERO BACKUP")
    p = Progress("BACKUP", 0, 2 * SECTORS)
    passes = []
    for _ in range(2):
        data = bytearray()
        step = SECTOR
        for addr in range(0, FLASH_SIZE, step):
            data += rom.read(addr, step)
            for s in range(addr // SECTOR, (addr + step) // SECTOR):
                p.states[s] = MAP_READ if not passes else MAP_SAME
            p.done += step // SECTOR
            _show(rom, p, report, screen)
        passes.append(bytes(data))
    if passes[0] != passes[1]:
        raise JobError("The flash read differently twice. No backup was saved.")
    return passes[0]


def _write(rom: Rom, want: bytes, end: int, for_smk: bool, title: str, report: Report) -> int:
    """Make the flash from WRITE_FROM to end the same as want. Returns sectors written."""
    cal = rom.read(SMK_CAL, SECTOR)
    if smk_mark(cal) != for_smk:
        raise JobError("That's an FM-1's, and this is an SMK-37: nothing was written." if smk_mark(cal)
                       else "That's an SMK-37's, and this is an FM-1: nothing was written.")
    before = _protected(rom)
    screen = rom.screen_init(for_smk)
    if screen:
        rom.screen_row(1, ". SAME  + WRITE")
    addresses = [a for a in range(WRITE_FROM, end, SECTOR) if not (for_smk and a == SMK_CAL)]
    p = Progress(title, 0, len(addresses))
    _show(rom, p, report, screen)
    touched = False  # from the first erase tried, whether or not it worked: its answer may only have been lost
    addr = WRITE_FROM
    try:
        for addr in addresses:
            n = min(SECTOR, end - addr)
            now = rom.read(addr, SECTOR)
            if now[:n] != want[addr:addr + n]:
                p.states[addr // SECTOR] = MAP_DIFFERS
                sector = want[addr:addr + n] + now[n:]  # the end of a short image: left as it is
                touched = True
                last = None
                for _ in range(3):
                    try:
                        rom.write_sector(addr, sector)
                        last = None
                        break
                    except RomError as e:
                        last = e
                if last is not None:
                    raise last
                p.states[addr // SECTOR] = MAP_WRITTEN
                p.written += 1
            else:
                p.states[addr // SECTOR] = MAP_SAME
            p.done += 1
            if p.done % 8 == 0 or p.done == p.total:
                _show(rom, p, report, screen)
        after = _protected(rom)
    except BaseException as e:  # anything at all, Ctrl-C too: what matters is whether the flash was touched
        p.states[addr // SECTOR] = MAP_FAILED
        try:
            report(p)
        except BaseException:  # the callback may be what stopped it
            pass
        what = (str(e) or e.__class__.__name__).rstrip(".")
        raise JobError("%s, at sector %06X." % (what, addr), wrote=touched) from e
    if after != before:
        raise JobError("A sector that must not change has changed. Keep the FM-1 on and make a backup now.",
                       wrote=True, final=True)
    return p.written


def install(rom: Rom, package: pkgmod.Package, chip_rule: str | None = None, report: Report = _nothing) -> int:
    """A firmware package's flash image onto the FM-1. chip_rule is the catalogue's "chip" for it, if known."""
    chip = rom.chip
    _require_supported(chip)
    for_smk = pkgmod.is_smk(package.product)
    if package.product[:4].lower() == "ota-":
        raise JobError("That package is an update loader's, not a firmware. Nothing was written.")
    if not for_smk and not package.product.startswith("FM-1_"):
        raise JobError("That package is for %s, not an FM-1 or SMK-37." % pkgmod.model_of(package.product))
    if chip_rule == "old" and chip.flash_id == NEW_CHIP:
        raise JobError("That firmware is only for FM-1s with the older flash chip. Nothing was written.")
    end = package.flash_len
    if end <= WRITE_FROM or end > (SMK_CAL if for_smk else WRITE_TO):
        raise JobError("Its flash image is a size it can't be.")
    return _write(rom, package.flash, end, for_smk, "INSTALL", report)


def restore(rom: Rom, image: bytes, report: Report = _nothing, same_unit: bool = True) -> int:
    """A backup back onto the FM-1 it came from."""
    chip = rom.chip
    _require_supported(chip)
    if len(image) != FLASH_SIZE:
        raise JobError("That isn't a backup of an FM-1 or SMK-37 (1 MiB).")
    for_smk = smk_mark(image[SMK_CAL:SMK_CAL + SECTOR])
    if same_unit:
        for addr in range(0, FLASH_SIZE, SECTOR):
            if not writable(addr) and rom.read(addr, SECTOR) != image[addr:addr + SECTOR]:
                raise JobError("That backup is from a different unit (its boot or identity sectors differ). "
                               "Nothing was written.")
    return _write(rom, image, WRITE_TO, for_smk, "RESTORE", report)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["JobError", "Progress", "backup", "install", "restore", "sha256", "MAP_NONE"]
