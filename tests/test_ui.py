import json
import tempfile
import time
import unittest
from pathlib import Path

from fm1zero import boot, catalogue, device, hw, jobs, rom, store, ui
from tests.fake import FakeFm1, _noise
from tests.test_catalogue import FEED, FWSC


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_app(d, keys=()):
    s = store.Store(Path(d))
    entries = catalogue.normalise(FEED)
    s.catalogue_path.write_text(json.dumps({"schema": 1, "entries": entries, "triedAt": time.time()}))
    # a unit already checked, with m-bootloader: installs go straight to the install question
    s.state.update(bootloader=boot.OPEN, bootloader_text="m-bootloader 2.4 (fm1)", flash="856014")
    clock = Clock()
    return ui.App(hw.PngScreen(), hw.ScriptKeys(keys), s, clock), clock


def press(app, *keys):
    app.keys.script.extend(keys)
    while app.keys.script:
        app.step(0)


def finish(app):
    """Let a Work screen's thread run to its end."""
    for _ in range(400):
        app.step(0)
        if not isinstance(app.stack[-1], ui.Work):
            return
        time.sleep(0.01)
    raise AssertionError("the work never finished")


class UiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        status = device.status
        self.addCleanup(lambda: setattr(device, "status", status))
        self.saved = (device.install, device.backup, device.restore)
        self.addCleanup(lambda: setattr(device, "install", self.saved[0]))
        self.addCleanup(lambda: setattr(device, "backup", self.saved[1]))
        self.addCleanup(lambda: setattr(device, "restore", self.saved[2]))

    def fake_device(self, fake):
        """device.py's jobs on a pretend FM-1, without USB."""
        def session(job):
            fake.running = fake.reset = False
            r = rom.Rom(fake, sleep=lambda _s: None)
            r.attach()
            written, data = job(r)
            return device.Result(r.chip, "FM-1_015", "FM-1_123", written, data)

        self.session = session
        device.install = lambda package, chip, report, say: session(
            lambda r: (jobs.install(r, package, chip, report), b""))
        device.backup = lambda report, say: session(lambda r: (0, jobs.backup(r, report)))
        device.restore = lambda image, report, say: session(lambda r: (jobs.restore(r, image, report), b""))

    def test_browse_and_search(self):
        app, _ = make_app(self.dir.name)
        press(app, "enter")
        self.assertIsInstance(app.stack[-1], ui.Browse)
        self.assertEqual([i.label for i in app.stack[-1].items(app)],
                         ["Sample Synth", "Evil", "External Installer"])
        press(app, "/", "g", "r", "o", "o", "v", "e", "enter")
        self.assertEqual([i.label for i in app.stack[-1].items(app)], ["External Installer"])
        press(app, "s", *["backspace"] * 6, "enter", "l")
        self.assertEqual(app.store.favourites, {"sample-fw"})
        press(app, "esc", "esc", "esc")
        self.assertIsInstance(app.stack[-1], ui.Home)

    def test_install_needs_enter_held_two_seconds(self):
        app, clock = make_app(self.dir.name)
        fake = FakeFm1()
        self.fake_device(fake)
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        press(app, "enter")  # a tap: pressed and let go
        clock.t += 5
        app.step(0)
        self.assertIsInstance(app.stack[-1], ui.Confirm)
        self.assertEqual(fake.erased, [])
        press(app, "hold:enter")
        clock.t += 1.0
        app.step(0)
        self.assertIsInstance(app.stack[-1], ui.Confirm)
        clock.t += 1.1
        app.step(0)
        self.assertIsInstance(app.stack[-1], ui.Work)
        during = app.screen.last.tobytes()
        finish(app)
        self.assertIn("Installed Sample Synth 1.2.3", app.stack[-1].text)
        self.assertNotEqual(app.screen.last.tobytes(), during)  # the result is on the screen without a key press
        self.assertTrue(fake.erased)
        self.assertIn("Installed", app.store.state["history"][0]["what"])

    def test_any_other_key_cancels(self):
        app, _ = make_app(self.dir.name)
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        press(app, "x")
        self.assertIsInstance(app.stack[-1], ui.Home)

    def test_changed_file_is_not_installed(self):
        app, clock = make_app(self.dir.name)
        fake = FakeFm1()
        self.fake_device(fake)
        saved = app.store.add_package(app.entries[0], FWSC)
        from tests.fake import make_fwsc
        saved.path.write_bytes(make_fwsc("FM-1_123", _noise(0x20000, 6)))
        ui.ask_install(app, saved)
        press(app, "hold:enter")
        clock.t += 2.1
        app.step(0)
        finish(app)
        self.assertEqual(app.stack[-1].title, "Failed")
        self.assertEqual(fake.erased, [])

    def test_backup_then_restore(self):
        app, clock = make_app(self.dir.name)
        fake = FakeFm1()
        before = bytes(fake.flash)
        self.fake_device(fake)
        ui.start_backup(app)
        finish(app)
        self.assertEqual(app.store.backups()[0].path.read_bytes(), before)
        fake.flash[0x10000:0x11000] = bytes(0x1000)
        press(app, "esc", "down", "right", "enter", "enter", "enter", "hold:enter")  # tiles: a row down, one across = Backups
        clock.t += 2.1
        app.step(0)
        finish(app)
        self.assertEqual(app.stack[-1].title, "Done")
        self.assertEqual(bytes(fake.flash), before)

    def test_every_screen_draws(self):
        app, _ = make_app(self.dir.name)
        app.store.add_package(app.entries[0], FWSC)
        for keys in (["enter"], ["enter", "enter"], ["enter", "enter", "enter"], ["down", "enter"],
                     ["down", "enter", "enter"], ["down", "down", "down", "down", "enter"],
                     ["down"] * 6 + ["enter"], ["down"] * 7 + ["enter"]):
            app.home()
            app.stack[0].sel = 0
            press(app, *keys)
            self.assertEqual(app.screen.last.size, (hw.W, hw.H))

    def test_picture_is_fetched_once_and_shown_with_its_maker(self):
        import io
        from PIL import Image
        app, _ = make_app(self.dir.name)
        entry = dict(app.entries[0], card="fw/cards/sample-fw-a1b2c3.w640.png")
        buf = io.BytesIO()
        Image.new("RGB", (640, 360), "#3060c0").save(buf, "PNG")
        asked = []

        def fetch(e):
            asked.append(e["id"])
            return buf.getvalue()

        for _ in range(2):
            screen = ui.Picture(entry, fetch)
            app.push(screen)
            app.step(0)
            screen.thread.join()
            app._drawn = -1.0
            app.step(0)
            self.assertEqual(app.screen.last.getpixel((160, 70)), (0x30, 0x60, 0xC0))
            self.assertEqual(app.screen.last.getpixel((2, hw.H - 2)), (0x26, 0x26, 0x2E))  # the credit bar
            press(app, "esc")
        self.assertEqual(asked, ["sample-fw"])

    def test_picture_that_fails_says_so(self):
        app, _ = make_app(self.dir.name)
        entry = dict(app.entries[0], card="fw/cards/x.png")

        def fetch(e):
            raise catalogue.CatalogueError("Couldn't get the picture (offline).")

        screen = ui.Picture(entry, fetch)
        app.push(screen)
        app.step(0)
        screen.thread.join()
        self.assertIn("offline", screen.error)
        press(app, "x")
        self.assertIsInstance(app.stack[-1], ui.Home)
    def test_grid_shows_pictures_and_shares_the_list(self):
        import io
        from PIL import Image
        app, clock = make_app(self.dir.name)
        app.entries[0]["card"] = "fw/cards/sample-fw.png"
        buf = io.BytesIO()
        Image.new("RGB", (640, 360), "#c03060").save(buf, "PNG")
        asked = []
        app.thumbs.fetch = lambda e: (asked.append(e["id"]), buf.getvalue())[1]
        press(app, "enter", "g")
        self.assertIsInstance(app.stack[-1], ui.Grid)
        app.thumbs.thread.join()
        clock.t += 1
        app.step(0)
        gap = (hw.W - ui.COLS * ui.TILE_W) // (ui.COLS + 1)
        self.assertEqual(app.screen.last.getpixel((gap + 50, ui.TOP + 30)), (0xC0, 0x30, 0x60))
        self.assertEqual(asked, ["sample-fw"])  # the two without a picture are never asked for
        press(app, "right", "right")
        self.assertEqual(app.stack[-1].sel, 2)
        press(app, "g")  # back to the list, at the same firmware
        self.assertNotIsInstance(app.stack[-1], ui.Grid)
        self.assertEqual(app.stack[-1].sel, 2)
        press(app, "g", "left", "left", "enter")
        self.assertEqual(app.stack[-1].title, "Sample Synth")
    def test_half_written_install_has_a_retry_key_and_enter_does_nothing(self):
        app, clock = make_app(self.dir.name)
        fake = FakeFm1()
        self.fake_device(fake)
        real = device.install
        calls = []

        def flaky(package, chip, report, say):
            calls.append(1)
            if len(calls) == 1:
                raise jobs.JobError("It read back differently, at sector 020000.", wrote=True)
            return real(package, chip, report, say)

        device.install = flaky
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        press(app, "hold:enter")
        clock.t += 2.1
        app.step(0)
        finish(app)
        failed = app.stack[-1]
        self.assertEqual(failed.title, "Failed")
        self.assertIn("half written", failed.text)
        self.assertIn("Press R", failed.text)
        press(app, "enter")
        self.assertIs(app.stack[-1], failed)  # a reflex Enter can't leave it
        press(app, "r")
        self.assertIsInstance(app.stack[-1], ui.Work)
        finish(app)
        self.assertEqual(app.stack[-1].title, "Done")
        self.assertEqual(len(calls), 2)

    def test_backup_first_from_the_install_question(self):
        app, _ = make_app(self.dir.name)
        self.fake_device(FakeFm1())
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        press(app, "b")
        self.assertEqual(app.stack[-1].title, "Backing up")
        finish(app)
        self.assertEqual(len(app.store.backups()), 1)

    def test_firmware_for_the_old_chip_is_flagged_on_a_new_chip_unit(self):
        app, _ = make_app(self.dir.name)
        app.store.state["flash"] = ""
        self.assertEqual(ui.fits(app, "old")[1], ui.DIM)  # not known yet: says to check
        app.store.state["flash"] = "B36014"
        self.assertEqual(ui.fits(app, "old")[1], ui.BAD)
        self.assertEqual(ui.fits(app, "new")[1], ui.GOOD)
        app.store.state["flash"] = "856014"
        self.assertEqual(ui.fits(app, "old")[1], ui.GOOD)
    def hold(self, app, clock):
        press(app, "hold:enter")
        clock.t += 2.1
        app.step(0)
        app.keys.script.append("enter")  # let go: a real key comes up before the next screen
        app.step(0)

    def test_stock_bootloader_is_put_to_the_user_before_the_install_question(self):
        app, clock = make_app(self.dir.name)
        fake = FakeFm1()
        self.fake_device(fake)
        app.store.state["bootloader"] = boot.STOCK
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        self.assertEqual(app.stack[-1].title, "No easy way back")
        press(app, "hold:enter")
        clock.t += 2.1
        app.step(0)
        self.assertEqual(app.stack[-1].title, "Install?")  # a second, separate hold is still needed
        self.assertEqual(fake.erased, [])
        clock.t += 5
        app.step(0)
        self.assertEqual(app.stack[-1].title, "Install?")  # the first hold doesn't carry over
        press(app, "esc")
        self.assertIsInstance(app.stack[-1], ui.Home)
        self.assertEqual(fake.erased, [])

    def test_first_install_checks_the_fm1_on_the_way_and_carries_on(self):
        app, _ = make_app(self.dir.name)
        fake = FakeFm1()
        self.fake_device(fake)
        keep = device.identify

        def identify(say):
            result = keep_session(lambda r: (0, b""))
            result.bootloader, result.bootloader_text = boot.OPEN, "m-bootloader 2.4"
            return result

        keep_session = self.session
        device.identify = identify
        self.addCleanup(lambda: setattr(device, "identify", keep))
        app.store.state["bootloader"] = ""
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        self.assertEqual(app.stack[-1].title, "Checking your FM-1")
        finish(app)
        self.assertEqual(app.stack[-1].title, "Install?")  # no "device" page to back out of
        self.assertEqual(app.store.state["bootloader"], boot.OPEN)  # remembered
        self.assertEqual(fake.erased, [])
        app.pop()
        ui.ask_install(app, saved)  # and the next time there is no check at all
        self.assertEqual(app.stack[-1].title, "Install?")

    def test_installed_firmware_keeps_the_name_it_was_chosen_by(self):
        app, clock = make_app(self.dir.name)
        self.fake_device(FakeFm1())
        saved = app.store.add_package(app.entries[0], FWSC)
        ui.ask_install(app, saved)
        press(app, "hold:enter")
        clock.t += 2.1
        app.step(0)
        finish(app)
        self.assertEqual(app.firmware_name(), "Sample Synth 1.2.3")
        app.home()
        ui.start_backup(app)
        finish(app)
        self.assertTrue(app.store.backups()[0].name.startswith("fm1-sample-synth-1.2.3-"))
        app.store.state["identity"] = "FM-1_900"  # something else was put on it since
        self.assertEqual(app.firmware_name(), "FM-1_900")

    def test_this_unit_only_hides_old_chip_firmware_on_a_new_chip_unit(self):
        app, _ = make_app(self.dir.name)
        app.entries[0]["chip"] = "old"
        press(app, "enter")
        browse = app.stack[-1]
        self.assertEqual(len(browse.items(app)), 3)
        press(app, "u")
        self.assertEqual(len(browse.items(app)), 3)  # an older-chip unit: everything fits
        app.store.state["flash"] = "B36014"
        self.assertEqual([i.label for i in browse.items(app)], ["Evil", "External Installer"])
        press(app, "u")
        self.assertEqual(len(browse.items(app)), 3)
        app.store.state["flash"] = ""
        press(app, "u")
        self.assertEqual(app.stack[-1].title, "This unit only")  # not known yet: says how to find out
    def test_everything_works_from_keys_the_cardputerzero_has_without_a_layer(self):
        """Letters, Enter, Esc and Tab only: no arrows, no ; . or /."""
        app, _ = make_app(self.dir.name)
        press(app, "x", "x", "f")
        self.assertEqual(app.stack[-1].sel, 3)  # the home tiles move by rows of three: down, down, up
        press(app, "f", "enter")
        self.assertIsInstance(app.stack[-1], ui.Browse)
        press(app, "x")
        self.assertEqual(app.stack[-1].sel, 1)
        press(app, "o")
        self.assertEqual(app.filters["sort"], "name")
        self.assertEqual(app.stack[-1].sel, 0)  # a new order starts from the top
        press(app, "g", "c", "z", "c")
        self.assertIsInstance(app.stack[-1], ui.Grid)
        self.assertEqual(app.stack[-1].sel, 1)
        press(app, "g", "tab")
        self.assertTrue(app.stack[-1].downloaded)
        press(app, "esc")
        self.assertIsInstance(app.stack[-1], ui.Home)
        for screen_hint in (ui.Home.hint, ui.Browse.hint, ui.Browse.hint2, ui.Grid.hint):
            self.assertFalse(set(";./`") & set(screen_hint.replace("F/X/Z/C", "").replace("F/X", "")), screen_hint)
    def test_discover_reads_the_list_itself_when_it_is_old(self):
        app, _ = make_app(self.dir.name)
        asked = []

        def fake(path, *a, **k):
            asked.append(path)
            return {"entries": app.entries}, "Read %d firmwares from The Firmware Hub." % len(app.entries)

        keep = catalogue.refresh
        catalogue.refresh = fake
        self.addCleanup(lambda: setattr(catalogue, "refresh", keep))
        ui.discover(app)  # fresh (just written): no read
        self.assertEqual(asked, [])
        self.assertIsInstance(app.stack[-1], ui.Browse)
        app.home()
        app.store.catalogue_path.write_text(json.dumps({"schema": 2, "entries": app.entries, "triedAt": 1}))
        ui.discover(app)
        finish(app)
        self.assertEqual(len(asked), 1)
        self.assertIsInstance(app.stack[-1], ui.Browse)  # straight on to the list, no "Done" screen in between

    def test_discover_with_no_list_reads_it(self):
        app, _ = make_app(self.dir.name)
        app.store.catalogue_path.unlink()
        keep = catalogue.refresh
        catalogue.refresh = lambda path, *a, **k: (None, "Couldn't read the list (offline). Kept the old one.")
        self.addCleanup(lambda: setattr(catalogue, "refresh", keep))
        app.entries = []
        ui.discover(app)
        finish(app)
        self.assertIsInstance(app.stack[-1], ui.Browse)
        self.assertEqual(app.entries, [])  # nothing to show, and the empty list says what to do

    def test_grid_puts_firmwares_with_pictures_first(self):
        app, _ = make_app(self.dir.name)
        app.entries[0]["card"] = ""
        app.entries[-1]["card"] = "fw/cards/x.w640.webp"
        grid = ui.Grid(False)
        shown = [i.data["id"] for i in grid.items(app)]
        self.assertEqual(shown[0], app.entries[-1]["id"])
        self.assertEqual(len(shown), len(app.entries))

    def test_every_footer_fits_the_screen_in_the_bigger_type(self):
        c = ui.Canvas()
        for text in (ui.Home.hint, ui.Browse.hint, ui.Browse.hint2, ui.Grid.hint, ui.Backups.hint,
                     "F/X move  Enter open  Esc back", "S find  L fav  O sort  Tab Discover", ui.Backups.hint,
                     "R run it again  Esc leave it half written"):
            width = 6
            for part in text.split("  "):
                key, _, what = part.strip().partition(" ")
                width += c.small.getlength(key.replace("_", " ") + " " + what) + 9
            self.assertLess(width - 9, ui.W - 4, text)
    def test_download_and_install_goes_on_to_the_install_question(self):
        app, _ = make_app(self.dir.name)
        fake = FakeFm1()
        self.fake_device(fake)
        e = app.entries[0]
        e["downloadable"] = True
        keep = catalogue.download
        catalogue.download = lambda entry, progress=None: FWSC
        self.addCleanup(lambda: setattr(catalogue, "download", keep))
        ui.entry_options(app, e)
        self.assertEqual([i.label for i in app.stack[-1].items(app)][:2], ["Download and install", "Download only"])
        press(app, "enter")  # the first one
        finish(app)
        self.assertEqual(app.stack[-1].title, "Install?")  # asked, not done: the hold still has to happen
        self.assertEqual(fake.erased, [])
        press(app, "esc")
        self.assertEqual(fake.erased, [])
        self.assertTrue(app.store.package_for(e))
    def test_home_is_tiles_moved_by_row_and_by_one(self):
        app, _ = make_app(self.dir.name)
        home = app.stack[-1]
        press(app, "x")
        self.assertEqual(home.sel, 3)  # down a row of three
        press(app, "c")
        self.assertEqual(home.sel, 4)
        press(app, "z", "f")
        self.assertEqual(home.sel, 0)
        press(app, "z")
        self.assertEqual(home.sel, len(home.items(app)) - 1)  # wraps round to Quit

    def test_every_icon_draws_and_every_header_has_one(self):
        from fm1zero import icons
        for name in icons.NAMES:
            im = icons.render(name, 22, "#ffb000")
            self.assertEqual(im.size, (22, 22))
            self.assertGreater(len(set(im.tobytes())), 2, name)  # something was drawn
        self.assertEqual(icons.icon_for("Felucca 1.8.2"), "chip")
        self.assertEqual(icons.icon_for("Installing"), "install")
        self.assertEqual(icons.icon_for("Failed"), "failed")
    def test_o_cycles_the_sort_on_every_list_and_starts_from_the_top(self):
        app, _ = make_app(self.dir.name)
        app.push(ui.Browse(False))
        seen = [app.filters["sort"]]
        for _ in catalogue.SORTS:
            press(app, "x", "o")
            self.assertEqual(app.stack[-1].sel, 0)
            seen.append(app.filters["sort"])
        self.assertEqual(seen[0], seen[-1])  # round the full circle
        self.assertEqual(set(seen), set(catalogue.SORTS))
        app.filters["sort"] = "maker"
        authors = [i.data["author"].lower() or "~" for i in app.stack[-1].items(app)]
        self.assertEqual(authors, sorted(authors))
        app.filters["sort"] = "ready"
        ready = [i.data["downloadable"] for i in app.stack[-1].items(app)]
        self.assertEqual(ready, sorted(ready, reverse=True))  # installable first
        app.pop()
        app.push(ui.Browse(True))
        press(app, "o")
        self.assertEqual(app.filters["saved_sort"], "newest")
        self.assertIn("newest", app.stack[-1].right(app))
        app.pop()
        app.push(ui.Backups())
        for want in ("oldest", "firmware", "newest"):
            press(app, "o")
            self.assertEqual(app.filters["backup_sort"], want)

if __name__ == "__main__":
    unittest.main()
