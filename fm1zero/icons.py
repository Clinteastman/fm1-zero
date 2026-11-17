"""Small icons, drawn with shapes (no image files, no icon font), so the app has nothing to install for them.

Each is drawn on a 16 x 16 grid at six times the size and shrunk, which smooths the edges.
render(name, size, colour) gives an RGBA image; unknown names give the chip.
"""
import math

from PIL import Image, ImageDraw

_UNIT = 6
_CACHE: dict = {}


class _Pen:
    def __init__(self, d: ImageDraw.ImageDraw, colour: str):
        self.d, self.colour = d, colour

    def _p(self, pts):
        return [(x * _UNIT, y * _UNIT) for x, y in pts]

    def line(self, pts, w=1.6):
        self.d.line(self._p(pts), fill=self.colour, width=int(w * _UNIT), joint="curve")
        for x, y in self._p(pts):  # round ends
            r = w * _UNIT / 2
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=self.colour)

    def box(self, x0, y0, x1, y1, w=1.6, fill=False):
        self.d.rounded_rectangle([x0 * _UNIT, y0 * _UNIT, x1 * _UNIT, y1 * _UNIT], radius=1.2 * _UNIT,
                                 outline=self.colour, width=int(w * _UNIT), fill=self.colour if fill else None)

    def circle(self, cx, cy, r, w=1.6, fill=False):
        self.d.ellipse([(cx - r) * _UNIT, (cy - r) * _UNIT, (cx + r) * _UNIT, (cy + r) * _UNIT],
                       outline=self.colour, width=int(w * _UNIT), fill=self.colour if fill else None)

    def arc(self, x0, y0, x1, y1, start, end, w=1.8):
        self.d.arc([x0 * _UNIT, y0 * _UNIT, x1 * _UNIT, y1 * _UNIT], start, end, fill=self.colour, width=int(w * _UNIT))

    def poly(self, pts, fill=True, w=1.6):
        if fill:
            self.d.polygon(self._p(pts), fill=self.colour)
        else:
            self.line(pts + [pts[0]], w)

    def head(self, x, y, dx, dy, size=3.2):
        """An arrow head with its tip ahead of (x, y) along (dx, dy)."""
        n = math.hypot(dx, dy)
        dx, dy = dx / n, dy / n
        tip = (x + dx * size, y + dy * size)
        a = (x - dy * size * .8, y + dx * size * .8)
        b = (x + dy * size * .8, y - dx * size * .8)
        self.poly([tip, a, b])


def _discover(p):
    p.circle(6.5, 6.5, 4.6)
    p.line([(10, 10), (14, 14)], 2.2)


def _download(p):
    p.line([(8, 1.5), (8, 9.5)], 1.8)
    p.head(8, 8.2, 0, 1, 3.4)
    p.line([(2.5, 10.5), (2.5, 14), (13.5, 14), (13.5, 10.5)], 1.6)


def _device(p):
    p.box(1.2, 3.5, 14.8, 13, 1.5)
    for x in (4.6, 8, 11.4):
        p.line([(x, 8), (x, 13)], 1.2)
    p.line([(3.5, 6), (5.5, 6)], 1.4)
    p.line([(10.5, 6), (12.5, 6)], 1.4)


def _backup(p):
    p.poly([(8, 1.2), (14, 3.6), (14, 8), (8, 14.8), (2, 8), (2, 3.6)], fill=False, w=1.6)
    p.line([(5.2, 8), (7.2, 10), (11, 5.8)], 1.8)


def _backups(p):
    for y in (1.8, 6.4, 11):
        p.box(1.8, y, 14.2, y + 3.4, 1.4)
        p.circle(11.6, y + 1.7, .7, 1, True)


def _refresh(p):
    p.arc(2, 2, 14, 14, 30, 300, 1.9)
    x, y = 8 + 6 * math.cos(math.radians(300)), 8 + 6 * math.sin(math.radians(300))
    p.head(x, y, -math.sin(math.radians(300)), math.cos(math.radians(300)), 3.6)


def _storage(p):
    p.box(1.2, 4.5, 14.8, 12.5, 1.5)
    p.line([(3.5, 8.5), (8, 8.5)], 1.4)
    p.circle(12, 8.5, .9, 1, True)


def _history(p):
    p.circle(8, 8, 6.4, 1.6)
    p.line([(8, 8), (8, 4.2)], 1.6)
    p.line([(8, 8), (11, 9.6)], 1.6)


def _quit(p):
    p.arc(3, 3.4, 13, 13.4, -50, 230, 1.9)
    p.line([(8, 1.5), (8, 7.5)], 1.9)


def _chip(p, tick=False):
    p.box(4, 4, 12, 12, 1.5)
    for v in (6, 8, 10):
        p.line([(v, 1.5), (v, 4)], 1.1)
        p.line([(v, 12), (v, 14.5)], 1.1)
        p.line([(1.5, v), (4, v)], 1.1)
        p.line([(12, v), (14.5, v)], 1.1)
    if tick:
        p.line([(6.2, 8.2), (7.6, 9.6), (10, 6.6)], 1.5)


def _install(p):
    p.line([(8, 1.2), (8, 7)], 1.8)
    p.head(8, 6, 0, 1, 3.2)
    p.box(2.5, 10, 13.5, 14.8, 1.4)
    p.line([(5, 12.4), (11, 12.4)], 1.2)


def _done(p):
    p.circle(8, 8, 6.6, 1.7)
    p.line([(4.8, 8.2), (7, 10.4), (11.3, 5.6)], 1.9)


def _failed(p):
    p.circle(8, 8, 6.6, 1.7)
    p.line([(5.4, 5.4), (10.6, 10.6)], 1.8)
    p.line([(10.6, 5.4), (5.4, 10.6)], 1.8)


def _warning(p):
    p.poly([(8, 1.6), (15, 14), (1, 14)], fill=False, w=1.6)
    p.line([(8, 6), (8, 10)], 1.6)
    p.circle(8, 12.1, .8, 1, True)


def _picture(p):
    p.box(1.2, 2.8, 14.8, 13.2, 1.5)
    p.line([(2.5, 12), (6, 7.5), (9, 10.5), (11, 8.5), (13.5, 11.5)], 1.4)
    p.circle(11.6, 5.6, 1, 1, True)


def _trash(p):
    p.line([(2.5, 4), (13.5, 4)], 1.6)
    p.line([(6, 4), (6, 2.2), (10, 2.2), (10, 4)], 1.4)
    p.poly([(3.8, 5.4), (4.8, 14.2), (11.2, 14.2), (12.2, 5.4)], fill=False, w=1.5)


def _info(p):
    p.circle(8, 8, 6.6, 1.7)
    p.circle(8, 5, .9, 1, True)
    p.line([(8, 7.4), (8, 11.4)], 1.7)


_ICONS = {
    "discover": _discover, "search": _discover, "saved": _download, "download": _download, "device": _device,
    "backup": _backup, "backups": _backups, "refresh": _refresh, "storage": _storage, "history": _history,
    "quit": _quit, "chip": _chip, "check": lambda p: _chip(p, True), "install": _install, "done": _done,
    "failed": _failed, "warning": _warning, "picture": _picture, "trash": _trash, "info": _info,
}
NAMES = tuple(_ICONS)

# a title starts with ... -> its icon. Anything else (a firmware's own name) gets the chip
_BY_TITLE = (
    ("discover", "discover"), ("downloaded", "saved"), ("downloading", "download"), ("checking", "check"),
    ("check the", "check"), ("installing", "install"), ("install", "install"), ("backups", "backups"),
    ("backup", "backup"), ("saving", "backup"), ("fm-1 device", "device"), ("fm-1 zero", "device"),
    ("asking", "device"), ("refresh", "refresh"), ("reading", "refresh"), ("storage", "storage"),
    ("last operation", "history"), ("done", "done"), ("failed", "failed"), ("search", "search"),
    ("delete", "trash"), ("no easy", "warning"), ("restor", "backups"), ("this unit", "info"),
)


def icon_for(title: str) -> str:
    low = title.lower()
    for start, name in _BY_TITLE:
        if low.startswith(start):
            return name
    return "chip"


def render(name: str, size: int, colour: str) -> Image.Image:
    key = (name, size, colour)
    if key not in _CACHE:
        big = Image.new("RGBA", (16 * _UNIT, 16 * _UNIT), (0, 0, 0, 0))
        _ICONS.get(name, _chip)(_Pen(ImageDraw.Draw(big), colour))
        _CACHE[key] = big.resize((size, size), Image.LANCZOS)
    return _CACHE[key]


def paste(img: Image.Image, name: str, x: int, y: int, size: int, colour: str) -> None:
    tile = render(name, size, colour)
    img.paste(tile, (x, y), tile)
