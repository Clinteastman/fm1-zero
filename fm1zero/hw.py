"""The CardputerZero's screen (/dev/fb0, 320x170 RGB565) and keyboard (evdev).

As M5Stack's Python_FrameBuffer_HelloWorld example does it. Both have a
stand-in so the app runs, and is tested, on any computer: frames go to PNG
files and keys come from a list.
"""

from __future__ import annotations

import os
import select
import struct
import time

W, H = 320, 170

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


def rgb565(img) -> bytes:
    try:
        import numpy as np

        a = np.asarray(img, dtype=np.uint16)
        out = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
        return out.astype("<u2").tobytes()
    except ImportError:
        raw = img.tobytes()
        out = bytearray(len(raw) // 3 * 2)
        for i in range(0, len(raw), 3):
            v = ((raw[i] >> 3) << 11) | ((raw[i + 1] >> 2) << 5) | (raw[i + 2] >> 3)
            struct.pack_into("<H", out, i // 3 * 2, v)
        return bytes(out)


class Framebuffer:
    def __init__(self, device: str | None = None):
        import fcntl
        import mmap

        device = device or os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0")
        self.fd = os.open(device, os.O_RDWR)
        var = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var)
        self.w, self.h, _, _, _, _, bpp, _ = struct.unpack_from("8I", var, 0)
        fix_fmt = "16sL" + "I" * 6 + "HHHI"
        fix = bytearray(struct.calcsize(fix_fmt))
        fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix)
        fields = struct.unpack(fix_fmt, fix)
        self.line = fields[8]
        if bpp != 16:
            raise OSError("The screen isn't 16 bits a pixel (it says %d)." % bpp)
        self.mm = mmap.mmap(self.fd, fields[2], mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def show(self, img) -> None:
        if img.size != (self.w, self.h):
            img = img.resize((self.w, self.h))
        frame = rgb565(img)
        row = self.w * 2
        if self.line == row:
            self.mm.seek(0)
            self.mm.write(frame)
        else:
            for y in range(self.h):
                self.mm.seek(y * self.line)
                self.mm.write(frame[y * row:(y + 1) * row])

    def close(self) -> None:
        self.mm.close()
        os.close(self.fd)


class PngScreen:
    """Each frame shown is kept; save() writes one."""

    def __init__(self):
        self.last = None

    def show(self, img) -> None:
        self.last = img.copy()

    def close(self) -> None:
        pass


# evdev key code names -> what the app calls them. Letters and digits come through as themselves
_NAMED = {
    "KEY_UP": "up", "KEY_DOWN": "down", "KEY_LEFT": "left", "KEY_RIGHT": "right", "KEY_ENTER": "enter",
    "KEY_KPENTER": "enter", "KEY_ESC": "esc", "KEY_BACKSPACE": "backspace", "KEY_DELETE": "backspace",
    "KEY_TAB": "tab", "KEY_SPACE": " ", "KEY_SEMICOLON": ";", "KEY_DOT": ".", "KEY_COMMA": ",", "KEY_SLASH": "/",
    "KEY_MINUS": "-", "KEY_EQUAL": "=", "KEY_GRAVE": "`", "KEY_APOSTROPHE": "'", "KEY_LEFTBRACE": "[",
    "KEY_RIGHTBRACE": "]", "KEY_BACKSLASH": "\\", "KEY_PAGEUP": "pageup", "KEY_PAGEDOWN": "pagedown",
    "KEY_HOME": "home", "KEY_END": "end",
}
_SHIFTED = {"-": "_", "1": "!", "/": "?", ";": ":", "'": '"', "=": "+"}


class Keyboard:
    """Key presses from every keyboard evdev has. events() gives (key, "down" | "up")."""

    def __init__(self):
        from evdev import InputDevice, ecodes, list_devices

        self.ecodes = ecodes
        self.devices = []
        self.shift = False
        for path in list_devices():
            try:
                d = InputDevice(path)
                if ecodes.KEY_ENTER in d.capabilities().get(ecodes.EV_KEY, []):
                    self.devices.append(d)
                else:
                    d.close()
            except OSError:
                pass

    def events(self, timeout_s: float) -> list[tuple[str, str]]:
        out = []
        if not self.devices:
            time.sleep(timeout_s)
            return out
        ready, _, _ = select.select([d.fd for d in self.devices], [], [], timeout_s)
        for d in self.devices:
            if d.fd not in ready:
                continue
            try:
                for ev in d.read():
                    if ev.type != self.ecodes.EV_KEY or ev.value == 2:
                        continue
                    names = self.ecodes.KEY.get(ev.code, "")
                    name = names if isinstance(names, str) else names[0]
                    if name in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
                        self.shift = ev.value == 1
                        continue
                    key = _NAMED.get(name)
                    if key is None and name.startswith("KEY_") and len(name) == 5:
                        key = name[4].lower()
                    if key is None:
                        continue
                    if self.shift:
                        key = _SHIFTED.get(key, key.upper() if len(key) == 1 else key)
                    out.append((key, "down" if ev.value == 1 else "up"))
            except OSError:
                pass
        return out

    def close(self) -> None:
        for d in self.devices:
            try:
                d.close()
            except OSError:
                pass


class ScriptKeys:
    """Keys from a list, one batch a call. "hold:enter" presses and keeps it down; "wait" does nothing."""

    def __init__(self, script):
        self.script = list(script)

    def events(self, timeout_s: float) -> list[tuple[str, str]]:
        if not self.script:
            return []
        key = self.script.pop(0)
        if key == "wait":
            return []
        if key.startswith("hold:"):
            return [(key[5:], "down")]
        return [(key, "down"), (key, "up")]

    def close(self) -> None:
        pass
