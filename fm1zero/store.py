"""What fm1-zero keeps on the CardputerZero: packages, backups, favourites, history.

    ~/.local/share/fm1zero/        (or $FM1ZERO_HOME)
        catalogue.json             the Hub's list, as last read
        packages/NAME.fwsc         downloaded, or copied here by hand
        packages/NAME.fwsc.json    what the catalogue said of it (downloads only)
        backups/NAME.bin           whole 1 MiB flashes
        backups/NAME.bin.json      when, which chip, what it was running
        state.json                 favourites, history, the last operation
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import pkg as pkgmod
from .catalogue import _write_atomic
from .rom import FLASH_SIZE


def home() -> Path:
    env = os.environ.get("FM1ZERO_HOME")
    return Path(env) if env else Path.home() / ".local" / "share" / "fm1zero"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "file"


@dataclass
class Saved:
    path: Path
    meta: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else home()
        self.packages_dir = self.root / "packages"
        self.backups_dir = self.root / "backups"
        self.cards_dir = self.root / "cards"
        self.catalogue_path = self.root / "catalogue.json"
        self.state_path = self.root / "state.json"
        for d in (self.packages_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.state = {"favourites": [], "history": [], "last": "", "identity": "", "bootloader": "",
                      "bootloader_text": "", "flash": "", "installed": {}}
        try:
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    # ---- state
    def save(self) -> None:
        _write_atomic(self.state_path, json.dumps(self.state, indent=1).encode("utf-8"))

    @property
    def favourites(self) -> set:
        return set(self.state["favourites"])

    def toggle_favourite(self, ident: str) -> bool:
        favourites = self.favourites
        favourites.symmetric_difference_update({ident})
        self.state["favourites"] = sorted(favourites)
        self.save()
        return ident in favourites

    def note(self, text: str, history: bool = False) -> None:
        """The last operation's result; with history, also kept in the list of what was done."""
        stamp = time.strftime("%Y-%m-%d %H:%M")
        self.state["last"] = "%s  %s" % (stamp, text)
        if history:
            self.state["history"] = ([{"when": stamp, "what": text}] + self.state["history"])[:50]
        self.save()

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    # ---- packages
    def packages(self) -> list[Saved]:
        out = []
        for path in sorted(self.packages_dir.glob("*.fwsc"), key=lambda p: p.name.lower()):
            out.append(Saved(path, _read_json(path.with_name(path.name + ".json"))))
        return out

    def package_for(self, entry: dict) -> Saved | None:
        for saved in self.packages():
            if saved.meta.get("id") == entry["id"] and saved.meta.get("sha256") == entry.get("sha256"):
                return saved
        return None

    def add_package(self, entry: dict, raw: bytes) -> Saved:
        package = pkgmod.parse(raw)  # refused here if it isn't a whole package
        want = entry.get("product") or ""
        if want and not pkgmod.same_hardware(want, package.product):
            raise pkgmod.PackageError("The package is for %s, but the catalogue says %s. Not kept."
                                      % (package.product, want))
        path = self.packages_dir / ("%s-%s.fwsc" % (_safe(entry["id"]), _safe(entry.get("version") or "x")))
        _write_atomic(path, raw)
        meta = {k: entry.get(k) for k in ("id", "name", "author", "version", "chip", "sha256", "repo", "licence")}
        meta["product"] = package.product
        _write_atomic(path.with_name(path.name + ".json"), json.dumps(meta, indent=1).encode("utf-8"))
        return Saved(path, meta)

    # ---- pictures
    def card_path(self, entry: dict) -> Path:
        return self.cards_dir / _safe("%s-%s" % (entry["id"], (entry.get("card") or "x").rsplit("/", 1)[-1]))

    def card(self, entry: dict, fetch) -> bytes:
        """The firmware's picture: kept once fetched, so it is asked for once."""
        path = self.card_path(entry)
        try:
            return path.read_bytes()
        except OSError:
            raw = fetch(entry)
            _write_atomic(path, raw)
            return raw

    # ---- backups
    def backups(self) -> list[Saved]:
        out = []
        for path in sorted(self.backups_dir.glob("*.bin"), key=lambda p: p.name, reverse=True):
            if path.stat().st_size == FLASH_SIZE:
                out.append(Saved(path, _read_json(path.with_name(path.name + ".json"))))
        return out

    def add_backup(self, data: bytes, meta: dict, label: str = "") -> Saved:
        if len(data) != FLASH_SIZE:
            raise ValueError("A backup is 1 MiB.")
        if self.free_bytes() < 2 * FLASH_SIZE:
            raise OSError("Not enough room to save a backup.")
        # named by what it was running: fm1-felucca-1.8.2-20261116-165332.bin
        what = _safe(label.split(" (")[0]).lower()[:32] + "-" if label else ""
        stem = "fm1-" + what + time.strftime("%Y%m%d-%H%M%S")
        path = self.backups_dir / (stem + ".bin")
        _write_atomic(path, data)
        if path.read_bytes() != data:
            path.unlink(missing_ok=True)
            raise OSError("The backup read back differently from the card. Not kept.")
        meta = dict(meta, when=time.strftime("%Y-%m-%d %H:%M"))
        _write_atomic(path.with_name(path.name + ".json"), json.dumps(meta, indent=1).encode("utf-8"))
        return Saved(path, meta)

    def delete(self, saved: Saved) -> None:
        saved.path.unlink(missing_ok=True)
        saved.path.with_name(saved.path.name + ".json").unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
