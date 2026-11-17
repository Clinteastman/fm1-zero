"""Which bootloader an FM-1 has, read from the first 16 KiB of its flash. Read only: nothing here writes.

It matters for recovery. With m-bootloader (jvitkauskas, MIT) on the unit, holding REC while switching
it on puts it in download mode whatever its firmware does, so a firmware install that went wrong can
always be done again. With M-VAVE's stock bootloader, a unit whose firmware doesn't start needs a
USB_KEY dongle to get there.

The format is as M-Flasher's src/formats.rs reads it (jvitkauskas, MIT).
"""

from __future__ import annotations

import struct

from .pkg import crc16, enc

BOOT_AREA = 0x4000
BOOT_AT = 0xA0

OPEN, STOCK, DAMAGED, UNKNOWN = "m-bootloader", "stock", "damaged", "unknown"


def _text(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("ascii", "replace").strip()


def container_identity(boot: bytes) -> tuple[str, str] | None:
    """(board, version) of an m-bootloader container (plain BANKCB). None for stock or anything else."""
    if len(boot) < 16:
        return None
    bank, size = struct.unpack_from("<HH", boot, 0)
    offset, pcrc, hcrc = struct.unpack_from("<IHH", boot, 8)
    if bank != 1 or crc16(boot[:14]) != hcrc or offset + size > len(boot):
        return None
    payload = boot[offset:offset + size]
    if crc16(payload) != pcrc or len(payload) < 0x70 or payload[0x40:0x48] != b"AC79BLID":
        return None
    return _text(payload[0x48:0x50]), _text(payload[0x50:0x70])


def _table(head: bytes) -> list[tuple[int, int, int, str]]:
    """The boot area's file table: (data crc, address, length, name). Empty if the flash header is damaged."""
    if len(head) < BOOT_AREA:
        return []
    header = enc(head[:32])
    if crc16(header[2:]) != struct.unpack_from("<H", header, 0)[0]:
        return []
    out = []
    for at in range(0x20, BOOT_AT, 0x20):
        r = enc(head[at:at + 32])
        if crc16(r[2:]) != struct.unpack_from("<H", r, 0)[0]:
            break
        out.append((struct.unpack_from("<H", r, 2)[0], struct.unpack_from("<I", r, 4)[0],
                    struct.unpack_from("<I", r, 8)[0], _text(r[16:32])))
    return out


def identify(head: bytes) -> tuple[str, str]:
    """(kind, text to show) for the first 16 KiB of a flash."""
    table = _table(head)
    if not table:
        return DAMAGED, "damaged or missing"
    data_crc, addr, length, name = table[0]
    if name != "uboot.boot" or addr != BOOT_AT or length < 16 or BOOT_AT + length > len(head):
        return UNKNOWN, "unreadable"
    stored = head[BOOT_AT:BOOT_AT + length]
    container = enc(stored[:16]) + enc(stored[16:])
    if crc16(container) != data_crc:
        return UNKNOWN, "unreadable"
    found = container_identity(container)
    if found is None:
        return STOCK, "stock (M-VAVE)"
    board, version = found
    return OPEN, "m-bootloader %s (%s)" % (version, board)


RECOVERY = {
    OPEN: "Recovery: hold REC while switching the FM-1 on. That works whatever firmware is on it.",
    STOCK: ("Recovery: only while its firmware runs. If an install goes wrong and the firmware won't start, "
            "you need a USB_KEY dongle. Installing m-bootloader with M-Flasher first removes that risk."),
    DAMAGED: "Its boot area is damaged. Don't switch it off: restore a backup with M-Flasher.",
    UNKNOWN: "Its bootloader couldn't be read, so how to recover it isn't known.",
}
