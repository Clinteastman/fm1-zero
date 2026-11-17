"""The Firmware Hub's list of FM-1 firmwares, and downloading a package from its maker.

Adapted from fm1-flip's tools/sync_catalogue.py (GPL-3.0). The Hub's terms
(https://thefirmwarehub.com/kb/#catalog), which this keeps to:

- catalog.json is read at most once a day, with a User-Agent that says who is asking
- packages come from the maker's own GitHub release, never from the Hub's mirror
- a package is only kept if its SHA-256 is the catalogue's
- the list is CC BY 4.0: "Firmware list: The Firmware Hub - https://thefirmwarehub.com"
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__

URL = "https://thefirmwarehub.com/catalog.json"
ATTRIBUTION = "Firmware list: The Firmware Hub - https://thefirmwarehub.com (CC BY 4.0)"
USER_AGENT = "fm1-zero/%s (CardputerZero FM-1 firmware manager; catalogue client)" % __version__
DAY = 24 * 60 * 60
MAX_CATALOGUE = 8 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_API = 1024 * 1024
MAX_PACKAGE = 16 * 1024 * 1024
MAX_CARD = 2 * 1024 * 1024
CARD_PATH = re.compile(r"fw/cards/[A-Za-z0-9_.-]{1,180}\.(?:webp|png|jpe?g)", re.IGNORECASE)
SCHEMA = 2  # 2: entries carry "card", the firmware's picture


class CatalogueError(ValueError):
    pass


def _text(value, limit=4096):
    return value if isinstance(value, str) and len(value) <= limit else None


def release_url_ok(url, repo, tag=None) -> bool:
    """A GitHub release page of exactly this repository (and tag)."""
    if not isinstance(url, str):
        return False
    p = urllib.parse.urlsplit(url)
    want = "/%s/releases/tag/" % repo
    if (p.scheme != "https" or p.hostname != "github.com" or p.port is not None or p.username or p.password
            or p.query or p.fragment or not p.path.startswith(want)):
        return False
    url_tag = urllib.parse.unquote(p.path[len(want):])
    return bool(url_tag) and "/" not in url_tag and (tag is None or url_tag == tag)


def normalise(document) -> list[dict]:
    """The feed, checked, as the entries this tool uses. Raises CatalogueError."""
    if not isinstance(document, dict):
        raise CatalogueError("The catalogue isn't what was expected.")
    lic = document.get("license")
    if not isinstance(lic, dict) or lic.get("license") != "CC-BY-4.0":
        raise CatalogueError("The catalogue's licence isn't the one expected.")
    entries = document.get("firmwares")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise CatalogueError("The catalogue's list isn't what was expected.")
    out, seen = [], set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        ident, name = _text(e.get("id"), 128), _text(e.get("name"), 256)
        if not ident or not name or ident in seen:
            continue
        seen.add(ident)
        repo = _text(e.get("repo"), 256)
        if repo is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            repo = None
        sha = e.get("sha256")
        sha = sha.lower() if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha) else None
        size = e.get("size")
        size = size if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_PACKAGE else None
        tag = _text(e.get("tag"), 256)
        release = e.get("release") if repo and release_url_ok(e.get("release"), repo, tag) else None
        kind = e.get("kind")
        installs = e.get("installs")
        out.append({
            "id": ident,
            "name": name,
            "author": _text(e.get("author"), 256) or "",
            "what": _text(e.get("what")) or "",
            "kinds": [kind] if isinstance(kind, str) else [k for k in kind if isinstance(k, str)]
            if isinstance(kind, list) else [],
            "version": _text(e.get("version"), 128) or "",
            "licence": _text(e.get("lic"), 128) or "",
            "repo": repo,
            "release": release,
            "tag": tag,
            "asset": _text(e.get("assetPkg"), 256),
            "product": _text(e.get("product"), 128) or "",
            "size": size,
            "sha256": sha,
            "chip": e.get("chip") if e.get("chip") in ("old", "new") else None,
            "beta": bool(e.get("beta", False)),
            # the Hub's copy of the maker's own picture of the firmware. The maker's work, not CC BY: it is only
            # ever shown with the maker's name and repository beside it
            "card": e["card"] if isinstance(e.get("card"), str) and CARD_PATH.fullmatch(e["card"]) else None,
            "installs": installs if isinstance(installs, int) and not isinstance(installs, bool)
            and installs >= 0 else None,
            # only these can be downloaded here. The Hub's own "pkg" mirror is never used
            "downloadable": bool(e.get("pkg")) and bool(sha) and size is not None and release is not None,
        })
    return out


def read_cache(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("schema") in (1, SCHEMA) else None
    except (OSError, ValueError):
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def refresh(path: Path, opener=urllib.request.urlopen, now=time.time) -> tuple[dict | None, str]:
    """Read the Hub's list if a day has passed since the last try. Returns (cache, what happened).

    There is no way round the once-a-day rule: the last try's time is kept even when it failed.
    """
    cache = read_cache(path)
    last = (cache or {}).get("triedAt", 0)
    if cache and now() - last < DAY:
        hours = int((DAY - (now() - last)) / 3600) + 1
        return cache, "The list was read less than a day ago. Next read in about %d h." % hours
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if cache and cache.get("etag") and cache.get("schema") == SCHEMA:
        headers["If-None-Match"] = cache["etag"]
    base = dict(cache or {"schema": SCHEMA, "entries": [], "fetchedAt": 0})
    base["triedAt"] = now()
    try:
        with opener(urllib.request.Request(URL, headers=headers), timeout=30) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "thefirmwarehub.com":
                raise CatalogueError("The catalogue came from somewhere unexpected.")
            raw = response.read(MAX_CATALOGUE + 1)
            etag = response.headers.get("ETag")
        if len(raw) > MAX_CATALOGUE:
            raise CatalogueError("The catalogue is too big.")
        document = json.loads(raw)
        entries = normalise(document)
        base.update(schema=SCHEMA, entries=entries, fetchedAt=now(), etag=etag, updated=_text(document.get("updated"), 64) or "",
                    attribution=ATTRIBUTION)
        message = "Read %d firmwares from The Firmware Hub." % len(entries)
    except urllib.error.HTTPError as e:
        message = "The list hasn't changed." if e.code == 304 else "The Hub answered %d. Kept the old list." % e.code
    except (OSError, ValueError) as e:
        message = "Couldn't read the list (%s). Kept the old one." % e
    _write_atomic(path, json.dumps(base).encode("utf-8"))
    return base, message


def card(entry: dict, opener=urllib.request.urlopen) -> bytes:
    """The firmware's picture, from the Hub (which says the pictures come from each maker's repository)."""
    path = entry.get("card")
    if not path or not CARD_PATH.fullmatch(path):
        raise CatalogueError("This firmware has no picture in the list.")
    request = urllib.request.Request("https://thefirmwarehub.com/" + path, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=20) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "thefirmwarehub.com":
                raise CatalogueError("The picture came from somewhere unexpected.")
            raw = response.read(MAX_CARD + 1)
    except OSError as e:
        raise CatalogueError("Couldn't get the picture (%s)." % e) from e
    if not raw or len(raw) > MAX_CARD:
        raise CatalogueError("The picture is missing or too big.")
    return raw


def asset_url(entry: dict, opener=urllib.request.urlopen) -> str:
    """The one .fwsc of the maker's release, from GitHub's API. Never the Hub's mirror."""
    repo, release = entry.get("repo"), entry.get("release")
    if not repo or not release_url_ok(release, repo, entry.get("tag")):
        raise CatalogueError("This firmware has no maker release to download from.")
    tag = urllib.parse.unquote(urllib.parse.urlsplit(release).path.rsplit("/", 1)[1])
    api = "https://api.github.com/repos/%s/releases/tags/%s" % (repo, urllib.parse.quote(tag, safe=""))
    request = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=20) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "api.github.com":
                raise CatalogueError("GitHub's answer came from somewhere unexpected.")
            payload = response.read(MAX_API + 1)
    except OSError as e:
        raise CatalogueError("Couldn't ask GitHub for the release (%s)." % e) from e
    try:
        data = json.loads(payload)
    except ValueError as e:
        raise CatalogueError("GitHub's answer wasn't what was expected.") from e
    if len(payload) > MAX_API or not isinstance(data, dict) or data.get("tag_name") != tag:
        raise CatalogueError("GitHub's answer wasn't the release asked for.")
    try:
        pattern = re.compile(entry["asset"]) if entry.get("asset") else None
    except re.error as e:
        raise CatalogueError("The catalogue's file name pattern is broken.") from e
    found = []
    prefix = "/%s/releases/download/%s/" % (repo, urllib.parse.quote(tag, safe=""))
    for asset in data.get("assets") or []:
        name, url = (asset or {}).get("name"), (asset or {}).get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str) or not name.endswith(".fwsc"):
            continue
        p = urllib.parse.urlsplit(url)
        if (p.scheme != "https" or p.hostname != "github.com" or p.query or p.fragment
                or not p.path.startswith(prefix) or urllib.parse.unquote(p.path.rsplit("/", 1)[1]) != name):
            continue
        if pattern is None or pattern.fullmatch(name):
            found.append(url)
    if len(found) != 1:
        raise CatalogueError("Expected one .fwsc in the maker's release, found %d." % len(found))
    return found[0]


def download(entry: dict, opener=urllib.request.urlopen, progress=lambda done, total: None) -> bytes:
    """The package's bytes, from the maker, checked against the catalogue's size and SHA-256."""
    if not entry.get("downloadable"):
        raise CatalogueError("This firmware can't be downloaded here: use its maker's own installer.")
    url = asset_url(entry, opener)
    size = entry["size"]
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    chunks, got = [], 0
    try:
        with opener(request, timeout=30) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https":
                raise CatalogueError("The download left HTTPS.")
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if got > size:
                    raise CatalogueError("The download is bigger than the catalogue says.")
                chunks.append(chunk)
                progress(got, size)
    except OSError as e:
        raise CatalogueError("The download failed (%s)." % e) from e
    raw = b"".join(chunks)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise CatalogueError("The download doesn't match the catalogue (size or SHA-256). Not kept.")
    return raw


SORTS = ("popular", "name", "maker", "ready")


def view(entries: list[dict], search: str = "", kind: str = "", chip: str = "", sort: str = "popular",
         favourites: set | None = None, only_favourites: bool = False, show_beta: bool = True) -> list[dict]:
    """The entries to show: filtered and sorted. Favourites first."""
    favourites = favourites or set()
    words = search.lower().split()
    out = []
    for e in entries:
        hay = " ".join([e["name"], e["author"], e["what"], " ".join(e["kinds"])]).lower()
        if any(w not in hay for w in words):
            continue
        if kind and kind not in e["kinds"]:
            continue
        if chip and e["chip"] not in (None, chip):
            continue
        if only_favourites and e["id"] not in favourites:
            continue
        if not show_beta and e["beta"]:
            continue
        out.append(e)
    if sort == "name":
        out.sort(key=lambda e: e["name"].lower())
    elif sort == "maker":  # by who made it, then by name
        out.sort(key=lambda e: (e["author"].lower() or "~", e["name"].lower()))
    elif sort == "ready":  # what can be installed here first, then by how popular
        out.sort(key=lambda e: (not e["downloadable"], e["installs"] is None, -(e["installs"] or 0), e["name"].lower()))
    else:
        out.sort(key=lambda e: (e["installs"] is None, -(e["installs"] or 0), e["name"].lower()))
    out.sort(key=lambda e: e["id"] not in favourites)
    return out


def chip_rule(entries: list[dict], sha256: str) -> str | None:
    """What the list says of the package with this SHA-256: "old", "new", or None if it isn't in the list."""
    for e in entries:
        if e.get("sha256") == sha256:
            return e.get("chip")
    return None


def kinds(entries: list[dict]) -> list[str]:
    return sorted({k for e in entries for k in e["kinds"]})
