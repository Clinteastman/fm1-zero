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
    s.catalogue_path.write_text(json.dumps({"schema": 1, "entries": entries, "triedAt": 0}))
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
        press(app, "/", *["backspace"] * 6, "enter", "f")
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
        press(app, "esc", "down", "down", "down", "down", "enter", "enter", "enter", "hold:enter")
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

    def test_unknown_bootloader_sends_you_to_check_first(self):
        app, _ = make_app(self.dir.name)
        app.store.state["bootloader"] = ""
        saved = app.store.add_package(app.entries[0], FWSC)
        device.status = lambda: ("firmware", "FM-1_015")
        ui.ask_install(app, saved)
        self.assertEqual(app.stack[-1].title, "Check the FM-1 first")
        press(app, "enter")
        self.assertEqual(app.stack[-1].title, "FM-1 device")
        app.home()
        ui.ask_install(app, saved)
        press(app, "i")
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
        self.assertEqual(app.firmware_name(), "Sample Synth 1.2.3 (FM-1_123)")
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

if __name__ == "__main__":
    unittest.main()
