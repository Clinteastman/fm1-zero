"""The app on the CardputerZero's own screen and keyboard.

Screens are a stack: Enter goes in, Esc comes back. Anything that writes to the
FM-1 or deletes a file asks first, and wants Enter held for two seconds.

Keys in a list: F and X move (the arrows, with or without Fn), Enter opens, Esc goes back.
"""

from __future__ import annotations

import io
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from . import __version__, boot, catalogue, device, jobs, pkg as pkgmod
from .hw import H, W
from .rom import FM1_FLASHES
from .store import Saved, Store

BG, FG, DIM, ACCENT, BAR, BAD, GOOD = "#101014", "#e8e8e8", "#b4b4be", "#ffb000", "#26262e", "#ff5a4a", "#5ad26a"
MAP_COLOURS = ["#26262e", "#ffb000", "#5ad26a", "#4a9a8c", "#3fb6d6", "#ff5a4a"]
LABEL_W = 86  # where a "label<tab>value" line's value starts
HOLD_S = 2.0
FLASH_NAMES = {"%06X" % ident: name for ident, name in FM1_FLASHES.items()}
# Moving. The CardputerZero has no arrow keys of its own: they are Fn + F, X, Z and C. The same four
# keys move here without Fn too, so nothing needs a layer. (; and . are kept for a USB keyboard.)
UP, DOWN = ("up", "f", "F", ";"), ("down", "x", "X", ".")
LEFT, RIGHT = ("left", "pageup", "z", "Z"), ("right", "pagedown", "c", "C")
ROWS = 8
ROW_H = 16
TOP = 20

_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "arial.ttf")


def _font(size: int):
    for path in _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (W, H), BG)
        self.d = ImageDraw.Draw(self.img)
        self.font = _font(13)
        self.small = _font(11)

    def clear(self) -> None:
        self.d.rectangle([0, 0, W, H], fill=BG)

    def fit(self, text: str, width: int, font=None) -> str:
        font = font or self.font
        if font.getlength(text) <= width:
            return text
        while text and font.getlength(text + "...") > width:
            text = text[:-1]
        return text + "..."

    def wrap(self, text: str, width: int, font=None) -> list[str]:
        font = font or self.font
        out = []
        for para in text.split("\n"):
            line = ""
            for word in para.split(" "):
                trial = (line + " " + word).strip()
                if font.getlength(trial) <= width:
                    line = trial
                    continue
                if line:
                    out.append(line)
                line = word
                while font.getlength(line) > width:
                    cut = len(line) - 1
                    while cut > 1 and font.getlength(line[:cut]) > width:
                        cut -= 1
                    out.append(line[:cut])
                    line = line[cut:]
            out.append(line)
        return out

    def frame(self, title: str, hint: str, right: str = "", colour: str = ACCENT, hint2: str = "") -> None:
        self.clear()
        self.d.rectangle([0, 0, W, 17], fill=BAR)
        if colour != ACCENT:  # a result: its colour runs under the header too
            self.d.rectangle([0, 17, W, 18], fill=colour)
        self.d.text((6, 2), self.fit(title, W - 100), font=self.font, fill=colour)
        if right:
            self.d.text((W - 6 - self.small.getlength(right), 4), right, font=self.small, fill=DIM)
        if hint2:
            self.hints(hint, H - 28)
            self.hints(hint2)
        else:
            self.hints(hint)

    def hints(self, hint: str, y: int = H - 14) -> None:
        """A footer row: "Key what it does", two spaces between each. The key is bright, the rest grey.
        A key of more than one word is written with _ for its spaces: "Hold_Enter install"."""
        self.d.rectangle([0, y, W, y + 14], fill=BAR)
        x = 6
        for part in hint.split("  "):
            part = part.strip()
            if not part:
                continue
            key, _, what = part.partition(" ")
            key = key.replace("_", " ")
            if x + self.small.getlength(key + " " + what) > W - 4:
                break
            self.d.text((x, y + 1), key, font=self.small, fill=FG)
            x += self.small.getlength(key + " ")
            self.d.text((x, y + 1), what, font=self.small, fill=DIM)
            x += self.small.getlength(what) + 9

    def credit(self, entry: dict, y: int) -> None:
        """Whose picture it is: its maker's name and repository always go with it."""
        text = "%s by %s" % (entry["name"], entry["author"] or "?")
        text += "  github.com/" + entry["repo"] if entry["repo"] else ""
        self.d.text((6, y), self.fit(text, W - 12, self.small), font=self.small, fill=DIM)

    def scrollbar(self, top: int, shown: int, total: int, height: int) -> None:
        if total > shown:
            self.d.rectangle([W - 3, TOP, W - 1, TOP + height], fill=BAR)
            self.d.rectangle([W - 3, TOP + height * top // total, W - 1, TOP + height * (top + shown) // total],
                             fill=DIM)

    def sections(self, parts, top_line: int = 0, max_y: int = H - 16) -> tuple[int, int]:
        """Blocks of text, each in its own colour, a small gap between them. Returns (lines shown, lines in all)."""
        lines = []  # (label or None, text, colour, gap above)
        for n, (text, colour) in enumerate(parts):
            first = True
            for para in text.split("\n"):
                label, tab, value = para.partition("\t")
                if tab:  # "label<tab>value": the label grey at the left, the value in its column
                    for i, line in enumerate(self.wrap(value, W - 12 - LABEL_W)):
                        lines.append((label if i == 0 else "", line, colour, 5 if first and n else 0))
                        first = False
                else:
                    for line in self.wrap(para, W - 20):
                        lines.append((None, line, colour, 5 if first and n else 0))
                        first = False
        y, shown = TOP + 3, 0
        for label, line, colour, gap in lines[top_line:]:
            y += gap if shown else 0
            if y + 15 > max_y:
                break
            if label is None:
                self.d.text((8, y), line, font=self.font, fill=colour)
            else:
                self.d.text((8, y), label, font=self.font, fill=DIM)
                self.d.text((LABEL_W, y), line, font=self.font, fill=colour)
            y += 15
            shown += 1
        return shown, len(lines)


@dataclass
class Item:
    label: str
    right: str = ""
    action: Callable | None = None
    data: object = None
    colour: str = FG
    right_colour: str = DIM


class Screen:
    live = False  # redrawn all the time (progress, a held key)

    def draw(self, app, c: Canvas) -> None:
        pass

    def key(self, app, key: str) -> None:
        if key == "esc":
            app.pop()

    def release(self, app, key: str) -> None:
        pass

    def tick(self, app) -> None:
        pass


class ListScreen(Screen):
    hint = "F/X move  Enter open  Esc back"
    hint2 = ""  # a second row of keys, above the first, for the screens with many

    def __init__(self, title: str, items: list[Item] | None = None, empty: str = "Nothing here."):
        self.title, self._items, self.empty = title, items or [], empty
        self.sel = 0

    def items(self, app) -> list[Item]:
        return self._items

    def right(self, app) -> str:
        return ""

    def draw(self, app, c: Canvas) -> None:
        items = self.items(app)
        self.sel = max(0, min(self.sel, len(items) - 1))
        rows = ROWS - 1 if self.hint2 else ROWS
        c.frame(self.title, self.hint2 or self.hint, self.right(app), hint2=self.hint if self.hint2 else "")
        if not items:
            for i, line in enumerate(c.wrap(self.empty, W - 20)):
                c.d.text((10, TOP + 8 + i * ROW_H), line, font=c.font, fill=DIM)
            return
        top = max(0, min(self.sel - rows // 2, len(items) - rows))
        for row, item in enumerate(items[top:top + rows]):
            y = TOP + row * ROW_H
            chosen = top + row == self.sel
            if chosen:
                c.d.rectangle([0, y, W, y + ROW_H - 1], fill=ACCENT)
            room = W - 16
            if item.right:
                rw = c.small.getlength(item.right)
                c.d.text((W - 8 - rw, y + 2), item.right, font=c.small, fill=BG if chosen else item.right_colour)
                room -= rw + 8
            c.d.text((8, y + 1), c.fit(item.label, room), font=c.font, fill=BG if chosen else item.colour)
        c.scrollbar(top, rows, len(items), rows * ROW_H)  # where in the list

    def key(self, app, key: str) -> None:
        items = self.items(app)
        if key in UP:
            self.sel = (self.sel - 1) % max(1, len(items))
        elif key in DOWN:
            self.sel = (self.sel + 1) % max(1, len(items))
        elif key in LEFT:
            self.sel = max(0, self.sel - ROWS)
        elif key in RIGHT:
            self.sel = min(max(0, len(items) - 1), self.sel + ROWS)
        elif key == "enter" and items and items[self.sel].action:
            items[self.sel].action(app, items[self.sel])
        else:
            super().key(app, key)


def as_sections(text, colour: str = FG) -> list:
    return [(text, colour)] if isinstance(text, str) else list(text)


class TextScreen(Screen):
    """Text to read: one string, or blocks of (text, colour). keys adds a screen's own keys: {"p": do}."""

    def __init__(self, title: str, text, on_enter: Callable | None = None, hint: str = "", colour: str = FG,
                 title_colour: str = ACCENT, keys: dict | None = None, enter_leaves: bool = True):
        self.title, self.parts, self.on_enter = title, as_sections(text, colour), on_enter
        self.title_colour, self.keys, self.enter_leaves = title_colour, keys or {}, enter_leaves
        self.hint = hint or ("Enter options  Esc back" if on_enter else "Esc back")
        self.top = 0
        self.more = False

    @property
    def text(self) -> str:
        return " ".join(text for text, _colour in self.parts)

    def draw(self, app, c: Canvas) -> None:
        c.frame(self.title, ("F/X scroll  " if self.more else "") + self.hint, colour=self.title_colour)
        shown, total = c.sections(self.parts, self.top)
        self.more = total > shown or self.top > 0
        self.top = max(0, min(self.top, total - 1))
        c.scrollbar(self.top, shown, total, H - TOP - 16)

    def key(self, app, key: str) -> None:
        if key.lower() in self.keys:
            self.keys[key.lower()](app)
        elif key in UP:
            self.top = max(0, self.top - 1)
        elif key in DOWN and self.more:
            self.top += 1
        elif key == "enter" and self.on_enter:
            self.on_enter(app)
        elif key == "enter":
            if self.enter_leaves:
                app.pop()
        else:
            super().key(app, key)


class Confirm(Screen):
    """Enter held for two seconds does it. Esc (or any key that isn't one of this screen's own) goes back."""

    live = True

    def __init__(self, title: str, text, action: Callable, verb: str = "go ahead", keys: dict | None = None,
                 hint: str = ""):
        self.title, self.parts, self.action = title, as_sections(text), action
        self.verb, self.keys, self.extra = verb, keys or {}, hint
        self.since = None

    def draw(self, app, c: Canvas) -> None:
        c.frame(self.title, "Hold_Enter %s (2 s)  %sEsc cancel" % (self.verb, self.extra + "  " if self.extra else ""))
        c.sections(self.parts, max_y=H - 36)
        held = 0.0 if self.since is None else min(1.0, (app.now() - self.since) / HOLD_S)
        c.d.text((8, H - 37), "Keep holding..." if held else "Hold Enter to %s" % self.verb, font=c.small,
                 fill=ACCENT if held else DIM)
        c.d.rectangle([8, H - 23, W - 8, H - 18], fill=BAR)
        if held:
            c.d.rectangle([8, H - 23, 8 + int((W - 16) * held), H - 18], fill=ACCENT)

    def key(self, app, key: str) -> None:
        if key == "enter":
            if self.since is None:
                self.since = app.now()
        elif key.lower() in self.keys:
            app.pop()
            self.keys[key.lower()](app)
        else:
            app.pop()

    def release(self, app, key: str) -> None:
        if key == "enter":
            self.since = None

    def tick(self, app) -> None:
        if self.since is not None and app.now() - self.since >= HOLD_S:
            app.pop()
            self.action(app)


class Work(Screen):
    """Something that takes a while, on its own thread. Keys do nothing until it is done.

    The job returns what to say: a string, or blocks of (text, colour). again starts the same work
    afresh: it is what R does on the screen shown when a write to the FM-1 stopped part way.
    """

    live = True

    def __init__(self, title: str, job: Callable[["Work"], object], with_map: bool = False,
                 again: Callable | None = None, then: Callable | None = None):
        self.title, self.with_map, self.again, self.then = title, with_map, again, then
        self.status = "Starting"
        self.progress = jobs.Progress()
        self.result = None
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, args=(job,), daemon=True)

    def _run(self, job) -> None:
        try:
            self.result = job(self)
        except Exception as e:  # shown, never a crash
            self.error = e
            self.result = str(e) or e.__class__.__name__

    def say(self, text: str) -> None:
        self.status = text

    def report(self, p: jobs.Progress) -> None:
        self.progress = p

    def bytes_done(self, done: int, total: int) -> None:
        self.progress = jobs.Progress("", done, total)
        self.status = "%d of %d KiB" % (done // 1024, total // 1024)

    def draw(self, app, c: Canvas) -> None:
        c.frame(self.title, "Keys are off until this is done")
        p = self.progress
        width = W - 16 - (106 if self.with_map else 0)
        for i, line in enumerate(c.wrap(self.status, width)[:2]):
            c.d.text((8, TOP + 4 + i * 15), line, font=c.font, fill=FG)
        if p.total:
            c.d.rectangle([8, 58, 8 + width, 66], fill=BAR)
            c.d.rectangle([8, 58, 8 + width * p.done // p.total, 66], fill=ACCENT)
            text = "%d%%" % (100 * p.done // p.total)
            if self.with_map:
                text += "   %d of %d sectors" % (p.done, p.total)
            c.d.text((8, 70), text, font=c.font, fill=DIM)
        if self.with_map:
            # the one thing that matters while it writes, where it can't be missed
            for i, line in enumerate(c.wrap("Keep the FM-1 on and the cable in.", width)):
                c.d.text((8, 92 + i * 15), line, font=c.font, fill=ACCENT)
            failed = sum(1 for state in p.states if state == 5)
            if failed:
                c.d.text((8, 124), "%d sector%s retrying" % (failed, "" if failed == 1 else "s"),
                         font=c.font, fill=ACCENT)
            for s in range(256):  # the flash: 16 x 16 sectors of 4 KiB
                x, y = W - 104 + (s % 16) * 6, TOP + 4 + (s // 16) * 6
                c.d.rectangle([x, y, x + 4, y + 4], fill=MAP_COLOURS[min(p.states[s], 5)])
            x = W - 104
            for colour, word in ((MAP_COLOURS[2], "wrote"), (MAP_COLOURS[3], "same"), (MAP_COLOURS[5], "retry")):
                c.d.rectangle([x, 126, x + 4, 130], fill=colour)
                c.d.text((x + 7, 122), word, font=c.small, fill=DIM)
                x += 10 + c.small.getlength(word)

    def key(self, app, key: str) -> None:
        pass

    def tick(self, app) -> None:
        if not self.thread.is_alive() and self.thread.ident is None:
            self.thread.start()
        elif self.result is not None:
            app.pop()
            if self.then and self.error is None:
                app.push(self.then(app))  # nothing to say: on to what was asked for
            else:
                app.push(self.outcome(app))

    def outcome(self, app) -> TextScreen:
        """The screen that says how it went, and what to do now."""
        if self.error is None:
            parts = as_sections(self.result, GOOD)
            app.store.note(" ".join(t for t, _c in parts), history=self.with_map)
            return TextScreen("Done", parts, hint="Enter/Esc back", title_colour=GOOD)
        app.store.note("FAILED: " + self.result, history=self.with_map)
        if getattr(self.error, "wrote", False):
            # a write stopped part way. The one thing to do is on its own line, and has its own key;
            # Enter does nothing here, so a reflex can't leave a half-written FM-1 behind
            how = {boot.OPEN: "hold REC while switching it on, then install again.",
                   boot.STOCK: "it needs a USB_KEY dongle (README: Recovery)."}.get(
                       app.store.state["bootloader"], "see README: Recovery.")
            parts = [("Stopped part way: the FM-1 is half written.", BAD),
                     ("Keep it on and plugged in. Press R to run it again now.", FG),
                     ("If it gets switched off and won't start: " + how, DIM),
                     ("Detail: " + self.result, DIM)]
            keys = {"r": lambda app: (app.pop(), self.again(app))} if self.again else {}
            return TextScreen("Failed", parts,
                              hint=("R run it again  " if self.again else "") + "Esc leave it half written",
                              title_colour=BAD, keys=keys, enter_leaves=False)
        return TextScreen("Failed", [("Nothing was written to the FM-1." if self.with_map else "It didn't work.", BAD),
                                     (self.result, FG)], hint="Enter/Esc back", title_colour=BAD)


class Search(Screen):
    def __init__(self, on_done: Callable[[str], None], text: str = ""):
        self.on_done, self.text = on_done, text

    def draw(self, app, c: Canvas) -> None:
        c.frame("Search", "Enter search  Esc cancel  (empty shows all)")
        c.d.text((8, 60), "Find:", font=c.font, fill=DIM)
        c.d.rectangle([46, 56, W - 8, 78], outline=ACCENT)
        c.d.text((52, 60), c.fit(self.text + "_", W - 66), font=c.font, fill=FG)

    def key(self, app, key: str) -> None:
        if key == "enter":
            app.pop()
            self.on_done(self.text.strip())
        elif key == "esc":
            app.pop()
        elif key == "backspace":
            self.text = self.text[:-1]
        elif len(key) == 1 and len(self.text) < 40:
            self.text += key


TILE_W, TILE_H, COLS, GRID_ROWS = 100, 46, 3, 2


class Thumbs:
    """Small copies of the firmwares' pictures for the grid, got one at a time in the background."""

    def __init__(self, store: Store, fetch=None):
        self.store, self.fetch = store, fetch or catalogue.card
        self.done: dict[str, object] = {}  # id -> an image, or None if it couldn't be had
        self.asked: set[str] = set()
        self.todo: queue.Queue = queue.Queue()
        self.thread = None

    def get(self, entry: dict):
        """The picture if it is here already. If not, it is asked for and comes later."""
        ident = entry["id"]
        if ident not in self.asked and entry.get("card"):
            self.asked.add(ident)
            self.todo.put(entry)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, daemon=True)
                self.thread.start()
        return self.done.get(ident)

    def _work(self) -> None:
        while True:
            try:
                entry = self.todo.get(timeout=0.5)
            except queue.Empty:
                return
            try:
                img = Image.open(io.BytesIO(self.store.card(entry, self.fetch))).convert("RGB")
                img.thumbnail((TILE_W, TILE_H))
                self.done[entry["id"]] = img
            except Exception:
                self.done[entry["id"]] = None


class Picture(Screen):
    """A firmware's own picture, from the list. Always with its maker's name and repository under it."""

    live = True

    def __init__(self, entry: dict, fetch=catalogue.card):
        self.entry, self.fetch = entry, fetch
        self.image = None
        self.error = ""
        self.thread = None

    def _load(self, app) -> None:
        try:
            raw = app.store.card(self.entry, self.fetch)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((W, H - 28))
            self.image = img
        except Exception as e:
            self.error = str(e) or "The picture couldn't be shown."

    def tick(self, app) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._load, args=(app,), daemon=True)
            self.thread.start()

    def draw(self, app, c: Canvas) -> None:
        e = self.entry
        c.clear()
        if self.image is not None:
            c.img.paste(self.image, ((W - self.image.width) // 2, (H - 28 - self.image.height) // 2))
        else:
            for i, line in enumerate(c.wrap(self.error or "Getting the picture...", W - 20)[:6]):
                c.d.text((10, 50 + i * 15), line, font=c.font, fill=BAD if self.error else DIM)
        c.d.rectangle([0, H - 28, W, H - 14], fill=BG)
        c.credit(e, H - 27)
        c.hints("Esc/any_key back")

    def key(self, app, key: str) -> None:
        app.pop()


# ---------------------------------------------------------------------------------- the app's screens ---
class Browse(ListScreen):
    """The Hub's list (Discover), or only what is downloaded."""

    hint = "F/X move  Enter open  Esc back  Tab saved"
    hint2 = "S find  G grid  L fav  O sort  K kind  U unit"

    def __init__(self, downloaded: bool = False):
        super().__init__("")
        self.downloaded = downloaded

    def swap(self, app, other: "Browse") -> None:
        """Another view of the same list, at the same place in it."""
        other.sel = self.sel
        app.pop()
        app.push(other)

    def items(self, app) -> list[Item]:
        f = app.filters
        entries = catalogue.view(app.entries, f["search"], f["kind"], "", f["sort"], app.store.favourites, f["favs"])
        have = {(s.meta.get("id"), s.meta.get("sha256")) for s in app.store.packages()}
        out = []
        if self.downloaded:
            self.title = "Downloaded"
            self.hint, self.hint2 = "F/X move  Enter open  Esc back", "S find  L fav  Tab Discover"
            self.empty = "No packages yet. Download one from Discover, or copy .fwsc files into packages/."
            words = f["search"].lower().split()
            for saved in app.store.packages():
                label = saved.meta.get("name") or saved.name
                if all(w in (label + " " + saved.name).lower() for w in words):
                    out.append(Item(("* " if saved.meta.get("id") in app.store.favourites else "") + label,
                                    saved.meta.get("version") or "file", open_saved, saved))
            return out
        self.title = "Discover"
        self.empty = ("No firmwares match." if app.entries
                      else "The list hasn't been read yet. Go back and choose Refresh catalogue (needs internet).")
        newer = app.store.state.get("flash") == "B36014"
        for e in entries:
            if f["unit"] and newer and e["chip"] == "old":
                continue  # made for the older flash chip only: not for the FM-1 last seen here
            mark = "* " if e["id"] in app.store.favourites else ""
            got = (e["id"], e["sha256"]) in have
            right = ("saved  " if got else "") + (e["version"] if e["downloadable"] else "link only")
            out.append(Item(mark + e["name"], right, open_entry, e, FG if e["downloadable"] else DIM,
                            GOOD if got else DIM))
        return out

    def right(self, app) -> str:
        f = app.filters
        parts = ([] if self.downloaded else [f["sort"]]) + [x for x in (f["kind"], "favs" if f["favs"] else "", "this unit" if f["unit"] and not self.downloaded else "",
                                                                         f["search"] and '"%s"' % f["search"])
                               if x]
        return " ".join(parts)

    def key(self, app, key: str) -> None:
        f = app.filters
        items = self.items(app)
        if key in ("s", "S", "/"):
            app.push(Search(lambda text: f.update(search=text), f["search"]))
        elif key == "tab":
            app.pop()
            app.push(Browse(not self.downloaded))
        elif key in ("g", "G") and not self.downloaded:
            self.swap(app, Grid(False))
        elif key in ("o", "O") and not self.downloaded:
            f["sort"] = catalogue.SORTS[(catalogue.SORTS.index(f["sort"]) + 1) % len(catalogue.SORTS)]
        elif key in ("k", "K") and not self.downloaded:
            kinds = [""] + catalogue.kinds(app.entries)
            f["kind"] = kinds[(kinds.index(f["kind"]) + 1) % len(kinds)] if f["kind"] in kinds else ""
        elif key in ("l", "L") and items:
            data = items[self.sel].data
            ident = data.meta.get("id") if isinstance(data, Saved) else data["id"]
            if ident:
                app.store.toggle_favourite(ident)
        elif key in ("v", "V"):
            f["favs"] = not f["favs"]
        elif key in ("u", "U") and not self.downloaded:
            if app.store.state.get("flash"):
                f["unit"] = not f["unit"]
            else:
                app.push(TextScreen("This unit only", "Which flash chip your FM-1 has isn't known yet. Open FM-1 "
                                    "device on the first screen and press Enter to check it, then come back."))
        else:
            super().key(app, key)


class Grid(Browse):
    """Discover as pictures: three across, two down. The same list, filters and keys as the list view."""

    live = True
    hint = "F/X/Z/C move  Enter open  G list  Esc back"
    hint2 = ""

    def items(self, app) -> list[Item]:
        # the Hub has pictures for about half of its firmwares: those come first, in the list's own order
        return sorted(super().items(app), key=lambda item: not item.data.get("card"))

    def draw(self, app, c: Canvas) -> None:
        items = self.items(app)
        self.sel = max(0, min(self.sel, len(items) - 1))
        c.frame(self.title, self.hint, self.right(app))
        if not items:
            for i, line in enumerate(c.wrap(self.empty, W - 20)):
                c.d.text((10, TOP + 8 + i * ROW_H), line, font=c.font, fill=DIM)
            return
        per = COLS * GRID_ROWS
        top = self.sel // per * per
        gap = (W - COLS * TILE_W) // (COLS + 1)
        for n, item in enumerate(items[top:top + per]):
            x = gap + (n % COLS) * (TILE_W + gap)
            y = TOP + 2 + (n // COLS) * (TILE_H + 13)
            chosen = top + n == self.sel
            e = item.data
            c.d.rectangle([x, y, x + TILE_W - 1, y + TILE_H - 1], fill=BAR)
            img = app.thumbs.get(e)
            if img is not None:
                c.img.paste(img, (x + (TILE_W - img.width) // 2, y + (TILE_H - img.height) // 2))
            else:  # no picture (yet): its name, big, in the tile
                lines = c.wrap(e["name"], TILE_W - 8)[:2]
                top = y + (TILE_H - 16 * len(lines)) // 2
                for i, line in enumerate(lines):
                    line = c.fit(line, TILE_W - 8)
                    c.d.text((x + (TILE_W - c.font.getlength(line)) // 2, top + i * 16), line, font=c.font, fill=DIM)
            if chosen:
                c.d.rectangle([x - 2, y - 2, x + TILE_W + 1, y + TILE_H + 1], outline=ACCENT, width=2)
            label = c.fit(item.label, TILE_W, c.small)
            c.d.text((x, y + TILE_H + 1), label, font=c.small, fill=ACCENT if chosen else item.colour)
        c.credit(items[self.sel].data, H - 27)  # whose picture the chosen one is

    def key(self, app, key: str) -> None:
        count = max(1, len(self.items(app)))
        if key in ("left", "z", "Z"):
            self.sel = (self.sel - 1) % count
        elif key in ("right", "c", "C"):
            self.sel = (self.sel + 1) % count
        elif key in UP:
            self.sel = max(0, self.sel - COLS)
        elif key in DOWN:
            self.sel = min(count - 1, self.sel + COLS)
        elif key in ("pageup", "pagedown"):
            self.sel = max(0, min(count - 1, self.sel + (COLS * GRID_ROWS if key == "pagedown" else -COLS * GRID_ROWS)))
        elif key in ("g", "G", "tab"):
            self.swap(app, Browse(False))
        else:
            super().key(app, key)


def _size(n) -> str:
    return "%d KiB" % (n // 1024) if n else "?"


def fits(app, chip: str | None) -> tuple[str, str]:
    """Whether a firmware suits the FM-1 last seen here, by its flash chip: (text, colour)."""
    unit = app.store.state.get("flash", "")
    if not unit:
        return "open FM-1 device to check it against yours", DIM
    newer = unit == "B36014"
    if chip == "old" and newer:
        return "NOT for your FM-1: it has the newer flash chip", BAD
    if chip is None:
        return "not known for your FM-1", DIM
    return "fits your FM-1", GOOD


def open_entry(app, item: Item) -> None:
    e = item.data
    fit, fit_colour = fits(app, e["chip"])
    facts = [
        ("%s %s  by %s" % (e["name"], e["version"], e["author"] or "?"), FG),
        (fit[0].upper() + fit[1:], fit_colour),
        ("Chip\t%s\nFor\t%s\nSize\t%s\nKind\t%s" % (e["chip"] or "any", e["product"] or "?", _size(e["size"]),
                                                     ", ".join(e["kinds"]) or "?"), FG),
    ]
    if not e["downloadable"]:
        facts.append(("Link only: it can't be downloaded here. Use its maker's installer.", ACCENT))
    facts.append((e["what"], FG))
    facts.append(("Licence\t%s\nInstalls\t%s via the Hub%s" % (
        e["licence"] or "?", e["installs"] if e["installs"] is not None else "?",
        "\nSource\tgithub.com/" + e["repo"] if e["repo"] else ""), FG))
    facts.append((catalogue.ATTRIBUTION, DIM))
    card = bool(e.get("card"))
    app.push(TextScreen(e["name"], facts, lambda app: entry_options(app, e),
                        hint="Enter options%s  Esc back" % ("  P picture" if card else ""),
                        keys={"p": lambda app: app.push(Picture(e))} if card else {}))


def entry_options(app, e: dict) -> None:
    saved = app.store.package_for(e)
    items = []
    if saved:
        items.append(Item("Install on FM-1", "", lambda app, _i: ask_install(app, saved)))
    elif e["downloadable"]:
        items.append(Item("Download package", _size(e["size"]), lambda app, _i: start_download(app, e)))
    if e.get("card"):
        items.append(Item("Show picture", "", lambda app, _i: app.push(Picture(e))))
    fav = e["id"] in app.store.favourites
    items.append(Item("Unfavourite" if fav else "Favourite", "",
                      lambda app, _i: (app.store.toggle_favourite(e["id"]), app.pop())))
    if saved:
        items.append(Item("Delete download", "", lambda app, _i: ask_delete(app, saved)))
    app.push(ListScreen(e["name"], items))


def open_saved(app, item: Item) -> None:
    saved: Saved = item.data
    m = saved.meta
    try:
        package = pkgmod.load(saved.path)
        facts = "For: %s\nFlash image: %s\nSHA-256: %s..." % (package.product, _size(package.flash_len),
                                                           package.sha256[:16])
    except (OSError, pkgmod.PackageError) as e:
        facts = "This file can't be used: %s" % e
    head = "%s %s   by %s\n" % (m["name"], m.get("version") or "", m.get("author") or "?") if m.get("name") else ""
    note = "" if m else "\nCopied here by hand: not checked against the catalogue."
    app.push(TextScreen(m.get("name") or saved.name, "%sFile: %s\n%s\nChip: %s%s" % (
        head, saved.name, facts, m.get("chip") or "not known", note), lambda app: saved_options(app, saved)))


def saved_options(app, saved: Saved) -> None:
    app.push(ListScreen(saved.meta.get("name") or saved.name, [
        Item("Install on FM-1", "", lambda app, _i: ask_install(app, saved)),
        Item("Delete", "", lambda app, _i: ask_delete(app, saved)),
    ]))


def ask_delete(app, saved: Saved) -> None:
    def do(app):
        app.store.delete(saved)
        app.home()
        app.push(TextScreen("Deleted", "%s was deleted." % saved.name))

    app.push(Confirm("Delete?", "Delete %s from this CardputerZero? This can't be undone." % saved.name, do,
                     verb="delete"))


def start_download(app, e: dict) -> None:
    def job(work: Work) -> str:
        work.say("Asking the maker's GitHub release")
        raw = catalogue.download(e, progress=work.bytes_done)
        work.say("Checking the package")
        saved = app.store.add_package(e, raw)
        return [("%s %s is downloaded." % (e["name"], e["version"]), GOOD),
                ("Checked against the list's SHA-256. It is for %s." % saved.meta["product"], FG),
                ("Open it again to install it.", DIM)]

    app.pop()
    app.push(Work("Downloading " + e["name"], job))


def said(result) -> list:
    """What to tell the user once the FM-1 has restarted: blocks of (text, colour)."""
    if result.after:
        return [("It is safe to unplug.", FG), ("The FM-1 restarted. Its firmware answers as %s." % result.after, DIM)]
    return [("The FM-1 didn't answer after restarting: look at its own screen.", ACCENT)]


def ask_install(app, saved: Saved) -> None:
    """Install a package. An FM-1 without m-bootloader has no easy way back from a failed install, so
    that is put to the user first, on a screen of its own, before the install question (after
    M-Flasher +'s rule: m-bootloader before a firmware)."""
    kind = app.store.state["bootloader"]
    if kind == boot.OPEN:
        install_question(app, saved)
    elif kind in (boot.STOCK, boot.DAMAGED):
        app.push(Confirm("No easy way back", [
            ("This FM-1 has the stock bootloader." if kind == boot.STOCK else "This FM-1's boot area is damaged.", BAD),
            ("If an install fails and its firmware won't start, only a USB_KEY dongle recovers it.", FG),
            ("Safer: put m-bootloader on it first with M-Flasher. Then holding REC always gets it back.", DIM),
        ], lambda app: install_question(app, saved), verb="go on anyway"))
    else:
        app.push(TextScreen("Check the FM-1 first", [
            ("How this FM-1 recovers from a failed install isn't known yet.", ACCENT),
            ("Press Enter to check it now. That restarts the FM-1 and writes nothing. Then come back and install.",
             FG),
        ], lambda app: (app.pop(), show_device(app)), hint="Enter check it  I install anyway  Esc back",
            keys={"i": lambda app: (app.pop(), install_question(app, saved))}))


def install_question(app, saved: Saved) -> None:
    name = ("%s %s" % (saved.meta.get("name") or saved.name, saved.meta.get("version") or "")).strip()

    def job(work: Work):
        work.say("Checking the package")
        package = pkgmod.load(saved.path)
        want = saved.meta.get("sha256")
        if want and want != package.sha256:
            raise pkgmod.PackageError("The file has changed since it was downloaded. Delete it and download it again.")
        rule = saved.meta.get("chip") or catalogue.chip_rule(app.entries, package.sha256)
        work.say("Writing " + name)
        result = device.install(package, rule, work.report, work.say)
        app.saw(result, installed=name)
        return [("Installed %s" % name, GOOD),
                ("%d sectors written and read back." % result.written, FG)] + said(result)

    def start(app):
        app.push(Work("Installing", job, with_map=True, again=start))

    kind = app.store.state["bootloader"]
    risk = {boot.OPEN: [("Recovery: easy (m-bootloader)", GOOD),
                        ("If it fails: hold REC while switching the FM-1 on, then install again.", FG)],
            }.get(kind, [("Risk: no easy recovery", BAD), ("Only a USB_KEY dongle recovers a failed install.", FG)])
    now = app.firmware_name()
    parts = [("Install %s on the FM-1?" % name, FG),
             ("%sSongs and settings of another firmware are lost. No backup is made."
              % ("Replaces %s. " % now if now else ""), FG)] + risk
    app.push(Confirm("Install?", parts, start, verb="install", hint="B backup first",
                     keys={"b": lambda app: start_backup(app)}))


def start_backup(app, _item=None) -> None:
    def job(work: Work) -> str:
        result = device.backup(work.report, work.say)
        app.saw(result)
        work.say("Saving the backup")
        running = app.firmware_name() or result.before
        saved = app.store.add_backup(result.data, {
            "identity": running, "uid": result.chip.uid, "flash": "%06X" % result.chip.flash_id,
            "bootloader": result.bootloader_text,
            "sha256": jobs.sha256(result.data)}, label=running)
        return [("Backup saved.", GOOD), ("%s\n1 MiB, read twice and compared." % saved.name, FG)] + said(result)

    app.push(Work("Backing up", job, with_map=True))


class Backups(ListScreen):
    hint = "F/X move  Enter options  Esc back"

    def __init__(self):
        super().__init__("Backups", empty="No backups yet. Choose Create backup on the first screen.")

    def items(self, app) -> list[Item]:
        return [Item(s.meta.get("when") or s.name, s.meta.get("identity") or "", backup_options, s)
                for s in app.store.backups()]


def backup_options(app, item: Item) -> None:
    saved: Saved = item.data

    def job(work: Work) -> str:
        image = saved.path.read_bytes()
        want = saved.meta.get("sha256")
        if want and want != jobs.sha256(image):
            raise OSError("The backup file has changed since it was saved. Not restored.")
        result = device.restore(image, work.report, work.say)
        app.saw(result, installed=saved.meta.get("identity") or "")
        return [("Restored %s." % saved.name, GOOD),
                ("%d sectors written and read back." % result.written, FG)] + said(result)

    def start(app):
        app.push(Work("Restoring", job, with_map=True, again=start))

    text = [("Restore %s onto the FM-1?" % saved.name, FG),
            ("It replaces everything on it now with what it had then (%s)." % (saved.meta.get("identity") or "?"), DIM),
            ("Only for the same unit the backup came from.", ACCENT)]
    info = "File: %s\nWhen: %s\nWas running: %s\nFlash chip: %s\nChip ID: %s\nSHA-256: %s..." % (
        saved.path, saved.meta.get("when", "?"), saved.meta.get("identity") or "?", saved.meta.get("flash", "?"),
        saved.meta.get("uid") or "?", (saved.meta.get("sha256") or "?")[:16])
    app.push(ListScreen(saved.name, [
        Item("Restore on FM-1", "", lambda app, _i: app.push(
            Confirm("Restore?", text, start, verb="restore"))),
        Item("Details", "", lambda app, _i: app.push(TextScreen(saved.name, info))),
        Item("Delete", "", lambda app, _i: ask_delete(app, saved)),
    ]))


def show_device(app, _item=None) -> None:
    def job(work: Work):
        result = device.identify(work.say)
        chip = result.chip
        app.saw(result)
        return [("FM-1 checked. Nothing was written.", GOOD),
                ("Firmware\t%s\nAnswers as\t%s\nBootloader\t%s\nRecovery\t%s" % (
                    app.firmware_name() or "(no answer)", result.after or result.before or "(no answer)",
                    result.bootloader_text,
                    boot.RECOVERY[result.bootloader].replace("Recovery: ", "")), FG),
                ("Flash chip\t%s, %06X%s\nSize\t%d KiB\nChip key\t%04X\nChip ID\t%s\nBoot ROM\t%s\nLoader\t%s, "
                 "%d MHz\nWritable\t%s" % (
                     chip.flash_name, chip.flash_id, " (newer)" if chip.flash_id == 0xB36014 else "",
                     chip.size // 1024, chip.key, chip.uid or "?", chip.rom or "?", chip.loader or "?", chip.mhz,
                     "yes" if chip.supported else "NO: this tool won't write to it"), FG)]

    def go(app):
        app.push(Work("Asking the FM-1", job))

    try:
        state, detail = device.status()
    except Exception as e:
        state, detail = "error", str(e)
    st = app.store.state
    now = {"firmware": "plugged in", "rom": "plugged in, in download mode",
           "none": "not found on USB", "error": "USB couldn't be looked at: %s" % detail}[state]
    pairs = "Now\t%s" % now
    if app.firmware_name():
        pairs += "\nFirmware\t%s" % app.firmware_name()
    if state == "firmware" or st["identity"]:
        pairs += "\nAnswers as\t%s" % (detail if state == "firmware" else st["identity"])
    if st["bootloader"]:
        pairs += "\nBootloader\t%s\nRecovery\t%s" % (st["bootloader_text"],
                                                      boot.RECOVERY[st["bootloader"]].replace("Recovery: ", ""))
    if st.get("flash"):
        pairs += "\nFlash chip\t%s, %s%s" % (FLASH_NAMES.get(st["flash"], "unknown"), st["flash"],
                                             " (newer)" if st["flash"] == "B36014" else "")
    parts = [(pairs, FG),
             ("Enter asks its chip what it is. That restarts the FM-1 (unsaved work on it is lost) and writes "
              "nothing.", DIM)]
    if st["history"]:
        parts.append(("History\n" + "\n".join("%s  %s" % (h["when"], h["what"]) for h in st["history"][:10]), DIM))
    app.push(TextScreen("FM-1 device", parts, go, hint="Enter ask the chip  Esc back"))


def discover(app, _item=None) -> None:
    """Open the list. If it has never been read, or the last try was a day ago, read it first (the
    once-a-day rule in catalogue.refresh is what keeps the Hub from being asked too often)."""
    cache = catalogue.read_cache(app.store.catalogue_path)
    if cache and time.time() - cache.get("triedAt", 0) < catalogue.DAY:
        app.push(Browse(False))
        return

    def job(work: Work) -> str:
        work.say("Reading The Firmware Hub's list")
        new, message = catalogue.refresh(app.store.catalogue_path)
        app.entries = (new or {}).get("entries", [])
        return message

    app.push(Work("Discover", job, then=lambda app: Browse(False)))


def do_refresh(app, _item=None) -> None:
    def job(work: Work) -> str:
        work.say("Reading The Firmware Hub's list")
        cache, message = catalogue.refresh(app.store.catalogue_path)
        app.entries = (cache or {}).get("entries", [])
        return message + "\n" + catalogue.ATTRIBUTION

    app.push(Work("Refreshing", job))


def show_storage(app, _item=None) -> None:
    s = app.store
    app.push(TextScreen("Storage", "Free: %d MiB\nPackages: %d   Backups: %d\n\nFiles are in:\n%s\n\n"
                        "To copy them off or put .fwsc files on, use scp or the SD card:\n  packages/  backups/"
                        % (s.free_bytes() // (1024 * 1024), len(s.packages()), len(s.backups()), s.root)))


def home_items(app) -> list[Item]:
    return [
        Item("Discover", "%d firmwares" % len(app.entries), discover),
        Item("Downloaded", "%d" % len(app.store.packages()), lambda app, _i: app.push(Browse(True))),
        Item("FM-1 device", app.firmware_name(), show_device),
        Item("Create backup", "", start_backup),
        Item("Backups", "%d" % len(app.store.backups()), lambda app, _i: app.push(Backups())),
        Item("Refresh catalogue", "once a day", do_refresh),
        Item("Storage", "", show_storage),
        Item("Last operation", "", lambda app, _i: app.push(
            TextScreen("Last operation", app.store.state["last"] or "Nothing has been done yet."))),
        Item("Quit", "", lambda app, _i: app.quit()),
    ]


class Home(ListScreen):
    hint = "F/X move  Enter open"

    def __init__(self):
        super().__init__("FM-1 Zero")

    def items(self, app) -> list[Item]:
        return home_items(app)

    def right(self, app) -> str:
        return "v" + __version__

    def key(self, app, key: str) -> None:
        if key != "esc":
            super().key(app, key)


class App:
    def __init__(self, screen, keys, store: Store | None = None, now=time.monotonic):
        self.screen, self.keys, self.now = screen, keys, now
        self.store = store or Store()
        cache = catalogue.read_cache(self.store.catalogue_path)
        self.entries = (cache or {}).get("entries", [])
        self.filters = {"search": "", "kind": "", "sort": "popular", "favs": False, "unit": False}
        self.canvas = Canvas()
        self.thumbs = Thumbs(self.store)
        self.stack: list[Screen] = [Home()]
        self.running = True
        self._drawn = -1.0

    def push(self, screen: Screen) -> None:
        self.stack.append(screen)
        self._drawn = -1.0  # a new screen is drawn at once, key or no key

    def pop(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
        self._drawn = -1.0

    def home(self) -> None:
        del self.stack[1:]
        self._drawn = -1.0

    def quit(self) -> None:
        self.running = False

    def firmware_name(self) -> str:
        """What the FM-1 last seen here runs. A firmware built from another keeps that one's name
        ("FM-1_918"), so what was installed from here is shown by the name it was chosen by, for as
        long as the unit still answers the same (after M-Flasher +'s firmware names)."""
        st = self.store.state
        chosen = st.get("installed") or {}
        if chosen.get("name") and chosen.get("identity") == st["identity"]:
            return chosen["name"]
        return st["identity"]

    def saw(self, result, installed: str = "") -> None:
        """Keep what a session found out about the unit: what it runs, and how it would be recovered."""
        if installed:
            self.store.state["installed"] = {"name": installed, "identity": result.after, "uid": result.chip.uid}
        elif (self.store.state.get("installed") or {}).get("uid") not in ("", result.chip.uid):
            self.store.state["installed"] = {}  # a different FM-1
        self.store.state["identity"] = result.after or result.before or self.store.state["identity"]
        self.store.state["bootloader"] = result.bootloader
        self.store.state["bootloader_text"] = result.bootloader_text
        self.store.state["flash"] = "%06X" % result.chip.flash_id
        self.store.save()

    def step(self, timeout_s: float = 0.05) -> None:
        """One turn: keys in, the top screen's turn, a frame out."""
        events = self.keys.events(timeout_s)
        for key, what in events:
            top = self.stack[-1]
            (top.key if what == "down" else top.release)(self, key)
        self.stack[-1].tick(self)
        # a live screen (progress, a held key) is redrawn 5 times a second: the rest of the time is the job's
        due = self.stack[-1].live and self.now() - self._drawn >= 0.2
        if events or due or self._drawn < 0:
            self.stack[-1].draw(self, self.canvas)
            self.screen.show(self.canvas.img)
            self._drawn = self.now()

    def run(self) -> None:
        while self.running:
            self.step()


def main() -> int:
    from .hw import Framebuffer, Keyboard

    screen, keys = Framebuffer(), Keyboard()
    try:
        App(screen, keys).run()
    except KeyboardInterrupt:
        pass
    finally:
        keys.close()
        screen.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
