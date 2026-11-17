"""One whole operation on the FM-1 plugged into the CardputerZero: get it into its boot loader,
do the job, send it back to its firmware, and see what it then says it is.

How it gets into its boot loader:

- it is already there (4C4A:8057 on USB): used as it is
- its firmware answers the identity question (stock V15, Felucca-based firmwares): it is sent the
  boot key over MIDI and goes there itself
- anything else: this tool can't. The ESP32 tools bit-bang a key on the USB data lines as the FM-1
  is switched on; a Linux USB host port can't do that
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import boot, jobs, pkg as pkgmod, usb
from .rom import Chip, Rom, RomError

USE_KEYS = ("Put the FM-1 in download mode from its keys: with m-bootloader, switch it off, then on "
            "holding REC. In Felucca, SLOOP or Snoop37, hold OCT- and OCT+ for 5 s. Waiting for it...")
NO_ENTRY = ("No FM-1 in download mode showed up. Check the cable is a data cable and the FM-1 is on. "
            "With the stock bootloader and a firmware that doesn't answer, only a USB_KEY dongle gets it there.")
KEY_WAIT_S = 60.0


@dataclass
class Result:
    chip: Chip
    before: str = ""  # what it said it was before ("FM-1_918"), if it said
    after: str = ""  # and after it restarted
    written: int = 0
    data: bytes = b""  # a backup
    bootloader: str = boot.UNKNOWN  # boot.OPEN, STOCK, DAMAGED or UNKNOWN
    bootloader_text: str = ""


def status() -> tuple[str, str]:
    """What is plugged in right now: ("rom" | "firmware" | "none", detail)."""
    if usb.rom_present():
        return "rom", "in its boot loader"
    found = usb.find_fm1()
    if found:
        return "firmware", found[1].text
    return "none", ""


def _enter(say: Callable[[str], None]) -> tuple[Rom, str]:
    before = ""
    if not usb.rom_present():
        say("Looking for the FM-1")
        found = usb.find_fm1()
        if not found:
            # its firmware doesn't know the message (or nothing runs): say which keys do it, and carry
            # on when it shows up, either in download mode or as a firmware that does answer
            say(USE_KEYS)
            end = time.monotonic() + KEY_WAIT_S
            tries = 0
            while not found and not usb.rom_present():
                if time.monotonic() > end:
                    raise RomError(NO_ENTRY)
                time.sleep(0.5)
                tries += 1
                if tries % 6 == 0:
                    found = usb.find_fm1()
        if found:
            port, identity = found
            before = identity.text
            say("Sending %s to download mode" % identity.model)
            usb.send_boot_key(port)
    try:
        storage = usb.open_rom(10.0)
    except RomError as e:
        if not before:
            raise
        raise RomError("%s If other USB devices are plugged into the CardputerZero, unplug them: the FM-1 "
                       "doesn't take the message with others on the hub." % e) from e
    say("Starting the loader")
    rom = Rom(storage)
    try:
        rom.attach()
    except Exception:
        storage.close()
        raise
    return rom, before


def _leave(rom: Rom, say: Callable[[str], None]) -> str:
    """Back to its firmware, and what it then says it is ("" if it doesn't say)."""
    rom.leave()
    rom.dev.close()
    say("Waiting for the FM-1 to restart")
    end = time.monotonic() + 12.0
    while time.monotonic() < end:
        time.sleep(0.5)
        try:
            if usb.rom_present():
                continue
            found = usb.find_fm1()
        except Exception:
            found = None
        if found:
            return found[1].text
    return ""


HALF_WRITTEN = ("The install stopped part way: the FM-1 is half written. KEEP IT ON AND PLUGGED IN and run the "
                "same install again now. If it does get switched off and its firmware won't start: with "
                "m-bootloader hold REC while switching it on, then install again. With the stock bootloader it "
                "needs a USB_KEY dongle (see the README: recovery).")


def run(job: Callable[[Rom], tuple[int, bytes]], say: Callable[[str], None] = lambda _s: None) -> Result:
    rom, before = _enter(say)
    wrote = False
    try:
        for attempt in range(3):
            try:
                written, data = job(rom)
                break
            except BaseException as e:
                # a job writes only what differs, so the same job again carries on where it stopped. Not when
                # a protected sector changed (again would hide it), nor when it was stopped on purpose
                wrote = wrote or bool(getattr(e, "wrote", False))
                stopped = not isinstance(e, Exception) or isinstance(e.__cause__, KeyboardInterrupt)
                if not wrote or attempt == 2 or getattr(e, "final", False) or stopped:
                    raise
                say("It stopped part way: trying again")
                rom.keep_alive()
    except BaseException as e:
        if wrote:
            # not reset: it stays in its loader, where the same install can be run again
            rom.dev.close()
            raise jobs.JobError(str(e) or e.__class__.__name__, wrote=True) from e
        try:
            rom.leave()  # nothing was written: back to the firmware it had
            rom.dev.close()
        except Exception:
            pass
        raise
    try:
        kind, text = boot.identify(rom.read(0, boot.BOOT_AREA))
    except Exception:
        kind, text = boot.UNKNOWN, "unreadable"
    after = _leave(rom, say)
    return Result(rom.chip, before, after, written, data, kind, text)


def identify(say=lambda _s: None) -> Result:
    return run(lambda rom: (0, b""), say)


def backup(report=lambda _p: None, say=lambda _s: None) -> Result:
    return run(lambda rom: (0, jobs.backup(rom, report)), say)


def install(package: pkgmod.Package, chip_rule=None, report=lambda _p: None, say=lambda _s: None) -> Result:
    return run(lambda rom: (jobs.install(rom, package, chip_rule, report), b""), say)


def restore(image: bytes, report=lambda _p: None, say=lambda _s: None) -> Result:
    return run(lambda rom: (jobs.restore(rom, image, report), b""), say)
