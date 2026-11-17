"""The FM-1 on Linux USB: its boot loader as a storage device, and its firmware as a MIDI device.

The boot loader (4C4A:8057) is a USB mass storage device that takes JieLi's own
16-byte commands. They are sent here with pyusb, as Bulk-Only Transport, with
the kernel's usb-storage driver taken off the device first.

A running firmware is a USB-MIDI device. The kernel's snd-usb-audio has it, so
it is used through ALSA's raw MIDI files (/dev/snd/midiC*D*): no library needed.
"""

from __future__ import annotations

import glob
import os
import re
import select
import struct
import time
from dataclasses import dataclass

from . import rom

# what an FM-1 or SMK-37 can be called on USB. Only these are ever sent anything
NAMES = ("fm-1", "felucca", "ota", "composite", "sinco", "usb-midi", "smk-37", "mke-p37", "starrykey")
FELUCCA_VID = 0x1209
HS_QUERY = bytes([0xF0, 0x00, 0x32, 0x45, 0x00, 0x00, 0x00, 0x40, 0x7F, 0xF7])
BOOT_KEY = bytes([0xF0, 0x22, 0x24, 0x35, 0x7D, 0xF7])


# ------------------------------------------------------------------ the boot loader: USB storage ---
class Storage:
    """rom.py's Storage on a real FM-1 in its boot loader."""

    def __init__(self, dev):
        import usb.util

        self.dev = dev
        self.tag = 0
        self.interface = 0
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, Exception):  # not there, or not Linux
            pass
        try:
            dev.set_configuration()
        except Exception:
            pass  # already configured
        intf = dev.get_active_configuration()[(0, 0)]
        self.interface = intf.bInterfaceNumber
        usb.util.claim_interface(dev, self.interface)
        ends = list(intf)
        is_bulk = lambda e: usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK  # noqa: E731
        is_in = lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN  # noqa: E731
        self.ep_in = next(e for e in ends if is_bulk(e) and is_in(e))
        self.ep_out = next(e for e in ends if is_bulk(e) and not is_in(e))

    def command(self, cdb: bytes, data_in: int = 0, data_out: bytes = b"", timeout_ms: int = 2000):
        import usb.core

        self.tag = (self.tag + 1) & 0xFFFFFFFF
        length = data_in or len(data_out)
        cbw = struct.pack("<IIIBBB", 0x43425355, self.tag, length, 0x80 if data_in else 0, 0, len(cdb))
        cbw += bytes(cdb).ljust(16, b"\0")
        try:
            self.ep_out.write(cbw, timeout_ms)
            data = b""
            if data_in:
                try:
                    data = bytes(self.ep_in.read(data_in, timeout_ms))
                except usb.core.USBError as e:
                    if getattr(e, "errno", None) != 32:  # not a stall
                        raise
                    self.dev.clear_halt(self.ep_in.bEndpointAddress)
            elif data_out:
                self.ep_out.write(data_out, timeout_ms)
            try:
                csw = bytes(self.ep_in.read(13, timeout_ms))
            except usb.core.USBError as e:
                if getattr(e, "errno", None) != 32:
                    raise
                self.dev.clear_halt(self.ep_in.bEndpointAddress)
                csw = bytes(self.ep_in.read(13, timeout_ms))
            if len(csw) != 13:
                self._recover()
                return None
            sig, tag, _residue, status = struct.unpack("<IIIB", csw)
            if sig != 0x53425355 or tag != self.tag or status != 0:
                return None
            return data
        except usb.core.USBError:
            self._recover()
            return None

    def _recover(self) -> None:
        """Both endpoints' halts cleared, as fm1buddy does, so the next command starts clean."""
        for ep in (self.ep_in, self.ep_out):
            try:
                self.dev.clear_halt(ep.bEndpointAddress)
            except Exception:
                pass

    def close(self) -> None:
        import usb.util

        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass


def rom_present() -> bool:
    import usb.core

    return usb.core.find(idVendor=rom.VID, idProduct=rom.PID) is not None


def open_rom(wait_s: float = 10.0) -> Storage:
    """The boot loader, once it is on USB."""
    import usb.core

    end = time.monotonic() + wait_s
    last = "Its boot loader didn't show up on USB."
    while True:
        dev = usb.core.find(idVendor=rom.VID, idProduct=rom.PID)
        if dev is not None:
            try:
                return Storage(dev)
            except Exception as e:  # permissions, or the kernel still has it
                if getattr(e, "errno", None) == 13:  # no point waiting: only a rule or root fixes this
                    raise rom.RomError(
                        "This user is not allowed to open the FM-1's boot loader. Run 'sudo sh "
                        "packaging/postinst', replug it, and try again (or run this once with sudo).")
                last = "Its boot loader came, but it couldn't be opened (%s)." % e
        if time.monotonic() > end:
            raise rom.RomError(last)
        time.sleep(0.05)


# ----------------------------------------------------------------- a running firmware: USB-MIDI ---
@dataclass
class MidiPort:
    path: str  # /dev/snd/midiC1D0
    name: str
    vid: int
    pid: int

    @property
    def candidate(self) -> bool:
        lower = self.name.lower()
        return any(n in lower for n in NAMES) or self.vid == FELUCCA_VID


@dataclass
class Identity:
    text: str  # "FM-1_918"
    model: str  # "FM-1"
    version: int

    @property
    def ota(self) -> bool:
        return self.model.lower().startswith("ota-")


def midi_ports() -> list[MidiPort]:
    out = []
    for path in sorted(glob.glob("/dev/snd/midiC*D*")):
        m = re.search(r"midiC(\d+)D(\d+)$", path)
        if not m:
            continue
        card = m.group(1)
        name, vid, pid = "", 0, 0
        try:
            with open("/proc/asound/card%s/usbid" % card) as f:
                v, p = f.read().strip().split(":")
                vid, pid = int(v, 16), int(p, 16)
        except (OSError, ValueError):
            continue  # not a USB device
        try:
            with open("/proc/asound/cards") as f:
                for line in f:
                    lm = re.match(r"\s*%s\s+\[.*?\]:\s*\S+\s+-\s+(.*)" % card, line)
                    if lm:
                        name = lm.group(1).strip()
        except OSError:
            pass
        out.append(MidiPort(path, name, vid, pid))
    return out


def pack7(data: bytes) -> bytes:
    acc = bits = 0
    out = bytearray()
    for byte in data:
        acc |= byte << bits
        bits += 8
        while bits >= 7:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    if bits:
        out.append(acc & 0x7F)
    return bytes(out)


def unpack7(data: bytes) -> bytes:
    acc = bits = 0
    out = bytearray()
    for byte in data:
        acc |= (byte & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    return bytes(out)


def parse_identity(packet: bytes) -> Identity | None:
    """An answer to HS_QUERY: who the device says it is."""
    if len(packet) < 2 or packet[0] != 0xF0 or packet[-1] != 0xF7:
        return None
    d = unpack7(packet[1:-1])
    if len(d) != 34 or d[0] != 0 or d[1] != 0x59 or d[2] != 0x11:
        return None
    text = d[6:33].split(b"\0")[0].decode("ascii", "replace")
    model, sep, version = text.rpartition("_")
    if not sep or not model or not version.isdigit() or len(model) > 23:
        return None
    return Identity(text, model, int(version))


def sysex_packets(buffer: bytearray) -> list[bytes]:
    """Whole F0..F7 messages taken out of buffer."""
    out = []
    while True:
        start = buffer.find(0xF0)
        if start < 0:
            buffer.clear()
            return out
        end = buffer.find(0xF7, start)
        if end < 0:
            del buffer[:start]
            return out
        out.append(bytes(buffer[start:end + 1]))
        del buffer[:end + 1]


class Midi:
    def __init__(self, port: MidiPort):
        self.port = port
        self.fd = os.open(port.path, os.O_RDWR | os.O_NONBLOCK)
        self.buffer = bytearray()

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def drain(self) -> None:
        try:
            while os.read(self.fd, 4096):
                pass
        except (BlockingIOError, OSError):
            pass
        self.buffer.clear()

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read_sysex(self, timeout_s: float) -> list[bytes]:
        ready, _, _ = select.select([self.fd], [], [], timeout_s)
        if ready:
            try:
                # real-time bytes (clock, active sensing) may come in the middle of a SysEx: not part of it
                self.buffer += bytes(b for b in os.read(self.fd, 4096) if b < 0xF8)
            except BlockingIOError:
                pass
        return sysex_packets(self.buffer)

    def identify(self, tries: int = 2) -> Identity | None:
        self.drain()
        for _ in range(tries):
            self.send(HS_QUERY)
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                for packet in self.read_sysex(0.05):
                    identity = parse_identity(packet)
                    if identity:
                        return identity
        return None


def find_fm1() -> tuple[MidiPort, Identity] | None:
    """The first MIDI device that says it is an FM-1 or SMK-37. Other gear is never asked."""
    for port in midi_ports():
        if not port.candidate:
            continue
        try:
            midi = Midi(port)
        except OSError:
            continue
        try:
            identity = midi.identify()
        except OSError:
            identity = None
        finally:
            midi.close()
        if identity:
            return port, identity
    return None


def send_boot_key(port: MidiPort) -> None:
    midi = Midi(port)
    try:
        midi.send(BOOT_KEY)
        time.sleep(0.2)
    except OSError:
        pass  # it may already be restarting
    finally:
        midi.close()
