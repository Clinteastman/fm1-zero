import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from fm1zero import catalogue, pkg, store
from tests.fake import _noise, make_fwsc

FWSC = make_fwsc("FM-1_123", _noise(0x20000, 5))
SHA = hashlib.sha256(FWSC).hexdigest()
RELEASE = "https://github.com/example-maker/sample-fw/releases/tag/v1.2.3"
ASSET = "https://github.com/example-maker/sample-fw/releases/download/v1.2.3/sample-fw-1.2.3.fwsc"

FEED = {
    "license": {"license": "CC-BY-4.0"},
    "updated": "2026-11-14 21:01 UTC",
    "firmwares": [
        {"id": "sample-fw", "kind": "synth", "name": "Sample Synth", "author": "Example Maker",
         "repo": "example-maker/sample-fw", "what": "A test synth.", "version": "1.2.3",
         "pkg": "fw/sample-1.2.3.fwsc", "product": "FM-1_123", "size": len(FWSC), "sha256": SHA,
         "release": RELEASE, "tag": "v1.2.3", "assetPkg": "sample-fw-[0-9.]+\\.fwsc", "chip": "new",
         "installs": 123},
        {"id": "external-only", "kind": ["groove", "dev"], "name": "External Installer",
         "author": "Another Maker", "repo": "another-maker/installer", "what": "Link only."},
        {"id": "evil", "name": "Evil", "repo": "example-maker/sample-fw", "pkg": "x", "size": 10,
         "sha256": "a" * 64, "release": "https://evil.example/example-maker/sample-fw/releases/tag/v1"},
    ],
}


class Response(io.BytesIO):
    def __init__(self, data, url, headers=None):
        super().__init__(data)
        self._url, self.headers = url, headers or {}

    def geturl(self):
        return self._url


class Web:
    """A pretend internet: url -> bytes, counting what was asked for."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, request, timeout=0):
        url = request.full_url
        self.asked.append((url, dict(request.header_items())))
        if url not in self.pages:
            raise urllib.error.URLError("no such page")
        return Response(self.pages[url], url, {"ETag": '"e1"'})


def web(asset=FWSC, assets=None):
    api = "https://api.github.com/repos/example-maker/sample-fw/releases/tags/v1.2.3"
    listing = {"tag_name": "v1.2.3", "html_url": RELEASE, "assets": assets if assets is not None else [
        {"name": "sample-fw-1.2.3.fwsc", "browser_download_url": ASSET},
        {"name": "notes.txt", "browser_download_url": ASSET.replace(".fwsc", ".txt")}]}
    return Web({catalogue.URL: json.dumps(FEED).encode(), api: json.dumps(listing).encode(), ASSET: asset})


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.entries = catalogue.normalise(FEED)

    def test_normalise(self):
        sample, external, evil = self.entries
        self.assertTrue(sample["downloadable"])
        self.assertEqual(sample["kinds"], ["synth"])
        self.assertFalse(external["downloadable"])
        self.assertEqual(external["kinds"], ["groove", "dev"])
        self.assertFalse(evil["downloadable"])  # its release isn't on github.com
        self.assertIsNone(evil["release"])

    def test_wrong_licence_is_refused(self):
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.normalise({"license": {"license": "other"}, "firmwares": []})

    def test_refresh_once_a_day(self):
        with tempfile.TemporaryDirectory() as d:
            path, pages, clock = Path(d) / "c.json", web(), [1000.0]
            cache, _ = catalogue.refresh(path, pages, lambda: clock[0])
            self.assertEqual(len(cache["entries"]), 3)
            self.assertIn("fm1-zero", pages.asked[0][1]["User-agent"])
            clock[0] += 3600
            catalogue.refresh(path, pages, lambda: clock[0])
            self.assertEqual(len(pages.asked), 1)  # not asked again
            clock[0] += catalogue.DAY
            catalogue.refresh(path, pages, lambda: clock[0])
            self.assertEqual(len(pages.asked), 2)
            self.assertEqual(pages.asked[1][1]["If-none-match"], '"e1"')

    def test_failed_refresh_keeps_the_list_and_still_waits_a_day(self):
        with tempfile.TemporaryDirectory() as d:
            path, clock = Path(d) / "c.json", [1000.0]
            catalogue.refresh(path, web(), lambda: clock[0])
            clock[0] += catalogue.DAY + 1
            down = Web({})
            cache, message = catalogue.refresh(path, down, lambda: clock[0])
            self.assertEqual(len(cache["entries"]), 3)
            self.assertIn("Kept", message)
            clock[0] += 60
            catalogue.refresh(path, down, lambda: clock[0])
            self.assertEqual(len(down.asked), 1)

    def test_download_comes_from_the_maker_and_matches(self):
        pages = web()
        self.assertEqual(catalogue.download(self.entries[0], pages), FWSC)
        self.assertTrue(all("thefirmwarehub" not in url for url, _ in pages.asked))

    def test_download_with_wrong_bytes_is_refused(self):
        bad = bytearray(FWSC)
        bad[100] ^= 1
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.download(self.entries[0], web(bytes(bad)))
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.download(self.entries[0], web(FWSC + b"x"))

    def test_asset_elsewhere_is_refused(self):
        elsewhere = [{"name": "sample-fw-1.2.3.fwsc",
                      "browser_download_url": "https://evil.example/sample-fw-1.2.3.fwsc"}]
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.asset_url(self.entries[0], web(assets=elsewhere))

    def test_external_only_is_not_downloadable(self):
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.download(self.entries[1], web())

    def test_view(self):
        names = [e["id"] for e in catalogue.view(self.entries)]
        self.assertEqual(names[0], "sample-fw")  # the most installs first
        self.assertEqual([e["id"] for e in catalogue.view(self.entries, search="groove")], ["external-only"])
        self.assertEqual([e["id"] for e in catalogue.view(self.entries, favourites={"evil"})][0], "evil")
        self.assertEqual([e["id"] for e in catalogue.view(self.entries, kind="synth")], ["sample-fw"])

    def test_card_path_is_kept_only_if_it_is_a_hub_card(self):
        feed = json.loads(json.dumps(FEED))
        feed["firmwares"][0]["card"] = "fw/cards/sample-fw-a1b2c3.w640.webp"
        feed["firmwares"][1]["card"] = "https://evil.example/x.png"
        feed["firmwares"][2]["card"] = "../../etc/passwd"
        self.assertEqual([e["card"] for e in catalogue.normalise(feed)],
                         ["fw/cards/sample-fw-a1b2c3.w640.webp", None, None])

    def test_card_comes_from_the_hub_only(self):
        entry = dict(self.entries[0], card="fw/cards/a.png")
        pages = Web({"https://thefirmwarehub.com/fw/cards/a.png": b"PNGDATA"})
        self.assertEqual(catalogue.card(entry, pages), b"PNGDATA")
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.card(dict(entry, card="//evil.example/a.png"), pages)
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.card(dict(entry, card=None), pages)

    def test_old_cache_without_pictures_is_read_in_full_again(self):
        with tempfile.TemporaryDirectory() as d:
            path, clock = Path(d) / "c.json", [catalogue.DAY * 3]
            path.write_text(json.dumps({"schema": 1, "entries": self.entries, "triedAt": 0, "etag": '"old"'}))
            pages = web()
            cache, _ = catalogue.refresh(path, pages, lambda: clock[0])
            self.assertNotIn("If-none-match", pages.asked[0][1])
            self.assertEqual(cache["schema"], catalogue.SCHEMA)


class StoreTests(unittest.TestCase):
    def test_packages_backups_and_state(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.Store(Path(d))
            entry = catalogue.normalise(FEED)[0]
            saved = s.add_package(entry, FWSC)
            self.assertEqual(saved.meta["product"], "FM-1_123")
            self.assertEqual(s.package_for(entry).path, saved.path)
            self.assertEqual(pkg.load(saved.path).sha256, SHA)
            with self.assertRaises(pkg.PackageError):
                s.add_package(dict(entry, product="SMK-37 Elite_016"), FWSC)
            with self.assertRaises(pkg.PackageError):
                s.add_package(entry, FWSC[:-10])
            backup = s.add_backup(_noise(0x100000, 2), {"uid": "AB"})
            self.assertEqual([b.name for b in s.backups()], [backup.name])
            with self.assertRaises(ValueError):
                s.add_backup(b"short", {})
            self.assertTrue(s.toggle_favourite("sample-fw"))
            s.note("Installed Sample Synth", history=True)
            again = store.Store(Path(d))
            self.assertEqual(again.favourites, {"sample-fw"})
            self.assertEqual(again.state["history"][0]["what"], "Installed Sample Synth")
            s.delete(backup)
            self.assertEqual(s.backups(), [])


if __name__ == "__main__":
    unittest.main()
