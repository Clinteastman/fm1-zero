import unittest

from fm1zero import jobs, pkg, rom
from tests.fake import FakeFm1, _noise, make_flash, make_fwsc

NO_SLEEP = lambda _s: None  # noqa: E731


def attached(fake):
    r = rom.Rom(fake, sleep=NO_SLEEP)
    r.attach()
    return r


def protected(flash):
    return bytes(flash[:rom.WRITE_FROM]) + bytes(flash[rom.WRITE_TO:])


class PackageTests(unittest.TestCase):
    def test_round_trip(self):
        image = _noise(0x93000, 7)
        p = pkg.parse(make_fwsc("FM-1_918", image))
        self.assertEqual(p.product, "FM-1_918")
        self.assertEqual(p.flash, image)

    def test_corrupt_last_byte_is_refused(self):
        raw = bytearray(make_fwsc("FM-1_918", _noise(0x93000, 7)))
        raw[-1] ^= 1
        with self.assertRaises(pkg.PackageError):
            pkg.parse(bytes(raw))

    def test_truncated_is_refused(self):
        raw = make_fwsc("FM-1_918", _noise(0x93000, 7))
        with self.assertRaises(pkg.PackageError):
            pkg.parse(raw[:-100])
        with self.assertRaises(pkg.PackageError):
            pkg.parse(raw[:500])

    def test_families(self):
        self.assertTrue(pkg.is_fm1("FM-1_015"))
        self.assertTrue(pkg.is_fm1("ota-FM-1_900"))
        self.assertTrue(pkg.is_smk("SMK-37 Elite_016"))
        self.assertTrue(pkg.is_smk("STARRYKEY-37 PLAY_016"))
        self.assertFalse(pkg.same_hardware("FM-1_015", "SMK-37 Elite_016"))
        self.assertTrue(pkg.same_hardware("SMK-37 Pro_016", "SMK-37 Elite_016"))


class RomTests(unittest.TestCase):
    def test_cipher_is_its_own_inverse(self):
        data = _noise(1000, 3)
        self.assertEqual(rom.cipher(rom.cipher(data)), data)
        self.assertNotEqual(rom.cipher(data), data)

    def test_attach_reads_the_chip(self):
        fake = FakeFm1()
        chip = attached(fake).chip
        self.assertTrue(fake.running)
        self.assertEqual(chip.key, rom.FM1_KEY)
        self.assertEqual(chip.flash_id, 0x856014)
        self.assertEqual(chip.size, rom.FLASH_SIZE)
        self.assertEqual(chip.io, 0x1000)
        self.assertEqual(chip.loader, "1.3")
        self.assertEqual(chip.uid, "0102030405060708")
        self.assertEqual(chip.rom, "WL80 UBOOT1.00")
        self.assertTrue(chip.supported)

    def test_small_usb_buffer_falls_back(self):
        chip = attached(FakeFm1(usb_buffer=0x400)).chip
        self.assertEqual(chip.io, 0x400)

    def test_write_outside_the_firmware_is_refused(self):
        fake = FakeFm1()
        r = attached(fake)
        for addr in (0, 0x3000, rom.WRITE_TO, 0x4001):
            with self.assertRaises(rom.RomError):
                r.write_sector(addr, bytes(rom.SECTOR))
        self.assertEqual(fake.erased, [])


class JobTests(unittest.TestCase):
    def test_backup_is_the_flash(self):
        fake = FakeFm1()
        self.assertEqual(jobs.backup(attached(fake)), bytes(fake.flash))
        self.assertEqual(fake.erased, [])

    def test_install_writes_only_the_image(self):
        fake = FakeFm1()
        before = bytes(fake.flash)
        image = _noise(0x93000, 9)
        written = jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", image)))
        self.assertEqual(written, (0x93000 - rom.WRITE_FROM) // rom.SECTOR)
        self.assertEqual(bytes(fake.flash[rom.WRITE_FROM:0x93000]), image[rom.WRITE_FROM:])
        self.assertEqual(bytes(fake.flash[:rom.WRITE_FROM]), before[:rom.WRITE_FROM])
        self.assertEqual(bytes(fake.flash[0x93000:]), before[0x93000:])
        self.assertTrue(all(rom.WRITE_FROM <= a < 0x93000 for a in fake.erased))

    def test_install_again_writes_nothing(self):
        fake = FakeFm1()
        package = pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9)))
        jobs.install(attached(fake), package)
        fake.running, fake.erased = False, []
        self.assertEqual(jobs.install(attached(fake), package), 0)
        self.assertEqual(fake.erased, [])

    def test_short_last_sector_keeps_its_tail(self):
        fake = FakeFm1()
        before = bytes(fake.flash)
        end = 0x50800
        image = _noise(end, 11)
        jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", image)))
        self.assertEqual(bytes(fake.flash[rom.WRITE_FROM:end]), image[rom.WRITE_FROM:])
        self.assertEqual(bytes(fake.flash[end:]), before[end:])

    def test_fm1_package_on_smk_is_refused(self):
        fake = FakeFm1(make_flash(smk=True))
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
        self.assertEqual(fake.erased, [])

    def test_smk_package_on_fm1_is_refused(self):
        fake = FakeFm1()
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), pkg.parse(make_fwsc("SMK-37 Elite_961", _noise(0x93000, 9))))
        self.assertEqual(fake.erased, [])

    def test_other_product_is_refused(self):
        fake = FakeFm1()
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), pkg.parse(make_fwsc("TOASTER_001", _noise(0x93000, 9))))
        self.assertEqual(fake.erased, [])

    def test_unknown_chip_is_refused(self):
        for fake in (FakeFm1(key=0x1234), FakeFm1(flash_id=0x856015)):
            with self.assertRaises(jobs.JobError):
                jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
            self.assertEqual(fake.erased, [])

    def test_old_chip_firmware_on_new_chip_is_refused(self):
        fake = FakeFm1(flash_id=rom.NEW_CHIP)
        package = pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9)))
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), package, chip_rule="old")
        self.assertEqual(fake.erased, [])
        fake.running = False
        jobs.install(attached(fake), package, chip_rule="new")

    def test_oversize_image_is_refused(self):
        fake = FakeFm1()
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(rom.WRITE_TO + 1, 9))))
        self.assertEqual(fake.erased, [])

    def test_bad_write_is_caught_by_read_back(self):
        fake = FakeFm1()
        fake.fail_write_at = 0x20000
        with self.assertRaises(jobs.JobError) as caught:
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
        self.assertTrue(caught.exception.wrote)

    def test_device_gone_mid_write_reports_wrote(self):
        fake = FakeFm1()
        fake.die_after_writes = 5
        with self.assertRaises(jobs.JobError) as caught:
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
        self.assertTrue(caught.exception.wrote)

    def test_attach_again_with_the_loader_running(self):
        fake = FakeFm1()
        attached(fake)
        chip = attached(fake).chip  # as after an install that stopped part way
        self.assertTrue(chip.supported)

    def test_stopped_install_carries_on(self):
        fake = FakeFm1()
        image = _noise(0x93000, 9)
        package = pkg.parse(make_fwsc("FM-1_918", image))
        fake.fail_write_at = 0x20000
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), package)
        fake.fail_write_at = None
        jobs.install(attached(fake), package)
        self.assertEqual(bytes(fake.flash[rom.WRITE_FROM:0x93000]), image[rom.WRITE_FROM:])


    def test_restore_brings_a_backup_back(self):
        fake = FakeFm1()
        saved = jobs.backup(attached(fake))
        fake.running = False
        jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
        self.assertNotEqual(bytes(fake.flash), saved)
        fake.running = False
        jobs.restore(attached(fake), saved)
        self.assertEqual(bytes(fake.flash), saved)

    def test_restore_from_another_unit_is_refused(self):
        fake = FakeFm1()
        other = bytearray(make_flash(seed=1))
        other[0x100] ^= 0xFF
        with self.assertRaises(jobs.JobError):
            jobs.restore(attached(fake), bytes(other))
        self.assertEqual(fake.erased, [])

    def test_restore_wrong_size_is_refused(self):
        fake = FakeFm1()
        with self.assertRaises(jobs.JobError):
            jobs.restore(attached(fake), bytes(rom.FLASH_SIZE - 1))
        self.assertEqual(fake.erased, [])

    def test_smk_calibration_is_never_written(self):
        fake = FakeFm1(make_flash(smk=True))
        saved = bytes(fake.flash)
        changed = bytearray(saved)
        for a in range(rom.WRITE_FROM, rom.WRITE_TO, rom.SECTOR):
            changed[a + 5] ^= 0x55
        changed[rom.SMK_MARK:rom.SMK_MARK + 7] = b"trimdat"
        jobs.restore(attached(fake), bytes(changed))
        self.assertNotIn(rom.SMK_CAL, fake.erased)
        self.assertEqual(bytes(fake.flash[rom.SMK_CAL:rom.SMK_CAL + rom.SECTOR]),
                         saved[rom.SMK_CAL:rom.SMK_CAL + rom.SECTOR])
        self.assertEqual(protected(fake.flash), protected(saved))

    def test_progress_and_fm1_screen(self):
        fake = FakeFm1()
        seen = []
        jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))),
                     report=lambda p: seen.append((p.done, p.total)))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual(fake.rows[0], b"INSTALL 100%")
        self.assertEqual(len(fake.map), 256)

    def test_lost_erase_answer_counts_as_written(self):
        fake = FakeFm1()
        fake.lose_erase_answer_at = rom.WRITE_FROM  # the very first sector: erased, and nothing after it worked
        fake.die_after_writes = 0
        with self.assertRaises(jobs.JobError) as caught:
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
        self.assertTrue(caught.exception.wrote)

    def test_interrupt_mid_write_counts_as_written(self):
        fake = FakeFm1()

        def stop(p):
            if p.done >= 16:
                raise KeyboardInterrupt

        with self.assertRaises(jobs.JobError) as caught:
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))), report=stop)
        self.assertTrue(caught.exception.wrote)

    def test_changed_boot_sector_is_final(self):
        fake = FakeFm1()
        fake.corrupt_protected = True
        with self.assertRaises(jobs.JobError) as caught:
            jobs.install(attached(fake), pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9))))
        self.assertTrue(caught.exception.final)

    def test_update_loader_package_is_refused(self):
        fake = FakeFm1()
        with self.assertRaises(jobs.JobError):
            jobs.install(attached(fake), pkg.parse(make_fwsc("ota-FM-1_900", _noise(0x93000, 9))))
        self.assertEqual(fake.erased, [])


class SessionTests(unittest.TestCase):
    """device.run: what happens to the FM-1 when a job goes wrong."""

    def setUp(self):
        from fm1zero import device
        self.device = device
        self.fake = FakeFm1()
        self.saved = (device._enter, device._leave)
        device._enter = lambda say: (self._rom(), "FM-1_015")
        device._leave = lambda r, say: (r.leave(), "FM-1_918")[1]
        self.fake.close = lambda: None
        self.addCleanup(lambda: setattr(device, "_enter", self.saved[0]))
        self.addCleanup(lambda: setattr(device, "_leave", self.saved[1]))
        self.package = pkg.parse(make_fwsc("FM-1_918", _noise(0x93000, 9)))

    def _rom(self):
        self.fake.running = False
        return attached(self.fake)

    def test_good_install_resets_into_the_firmware(self):
        result = self.device.install(self.package)
        self.assertTrue(self.fake.reset)
        self.assertEqual(result.after, "FM-1_918")

    def test_one_bad_write_is_tried_again_and_finishes(self):
        fake = self.fake
        fake.fail_write_at = 0x20000
        real = fake.command

        def flaky(cdb, **kw):  # the fault clears once it has been hit three times (one sector's tries)
            out = real(cdb, **kw)
            if fake.programmed.count(0x20000) >= 3:
                fake.fail_write_at = None
            return out

        fake.command = flaky
        result = self.device.install(self.package)
        self.assertEqual(bytes(fake.flash[rom.WRITE_FROM:0x93000]), self.package.flash[rom.WRITE_FROM:])
        self.assertTrue(fake.reset)
        self.assertGreater(result.written, 0)

    def test_half_written_is_not_reset_and_says_so(self):
        self.fake.fail_write_at = 0x20000
        with self.assertRaises(jobs.JobError) as caught:
            self.device.install(self.package)
        self.assertFalse(self.fake.reset)  # left in its loader, to run the install again
        self.assertTrue(caught.exception.wrote)

    def test_refused_before_writing_goes_back_to_its_firmware(self):
        with self.assertRaises(jobs.JobError) as caught:
            self.device.install(pkg.parse(make_fwsc("TOASTER_001", _noise(0x93000, 9))))
        self.assertTrue(self.fake.reset)
        self.assertFalse(caught.exception.wrote)
        self.assertEqual(self.fake.erased, [])

    def test_changed_boot_sector_is_not_hidden_by_a_retry(self):
        self.fake.corrupt_protected = True
        with self.assertRaises(jobs.JobError) as caught:
            self.device.install(self.package)
        self.assertIn("must not change", str(caught.exception))
        self.assertFalse(self.fake.reset)

    def test_ctrl_c_mid_write_is_not_reset(self):
        def stop(p):
            if p.done >= 16:
                raise KeyboardInterrupt

        with self.assertRaises(jobs.JobError) as caught:
            self.device.install(self.package, report=stop)
        self.assertFalse(self.fake.reset)
        self.assertTrue(caught.exception.wrote)

class BootTests(unittest.TestCase):
    """Which bootloader a flash has, against M-Flasher's own m-bootloader file when it is there."""

    REAL = None

    @staticmethod
    def area(container: bytes) -> bytes:
        import struct
        from fm1zero import boot
        head = bytearray(b"\xFF" * boot.BOOT_AREA)

        def record(data_crc, addr, length, name):
            r = bytearray(32)
            struct.pack_into("<HII", r, 2, data_crc, addr, length)
            r[16:16 + len(name)] = name
            struct.pack_into("<H", r, 0, pkg.crc16(bytes(r[2:])))
            return pkg.enc(bytes(r))

        head[0:32] = record(0, 0, 0, b"flash")
        head[0x20:0x40] = record(pkg.crc16(container), boot.BOOT_AT, len(container), b"uboot.boot")
        head[0x40:0x60] = bytes(32)  # not a valid record: the table ends
        head[boot.BOOT_AT:boot.BOOT_AT + len(container)] = pkg.enc(container[:16]) + pkg.enc(container[16:])
        return bytes(head)

    @staticmethod
    def container(payload: bytes) -> bytes:
        import struct
        head = bytearray(16)
        struct.pack_into("<HH", head, 0, 1, len(payload))
        struct.pack_into("<IH", head, 8, 16, pkg.crc16(payload))
        struct.pack_into("<H", head, 14, pkg.crc16(bytes(head[:14])))
        return bytes(head) + payload

    def test_m_bootloader_is_recognised(self):
        from fm1zero import boot
        payload = bytearray(_noise(0x200, 4))
        payload[0x40:0x48] = b"AC79BLID"
        payload[0x48:0x50] = b"fm1\0\0\0\0\0"
        payload[0x50:0x70] = b"2.4-395e6cc".ljust(32, b"\0")
        kind, text = boot.identify(self.area(self.container(bytes(payload))))
        self.assertEqual((kind, text), (boot.OPEN, "m-bootloader 2.4-395e6cc (fm1)"))

    def test_stock_and_damaged(self):
        from fm1zero import boot
        self.assertEqual(boot.identify(self.area(self.container(_noise(0x200, 4))))[0], boot.STOCK)
        self.assertEqual(boot.identify(_noise(boot.BOOT_AREA, 3))[0], boot.DAMAGED)
        broken = bytearray(self.area(self.container(_noise(0x200, 4))))
        broken[boot.BOOT_AT + 40] ^= 1
        self.assertEqual(boot.identify(bytes(broken))[0], boot.UNKNOWN)

    def test_real_m_bootloader_file(self):
        import os
        from fm1zero import boot
        path = os.environ.get("FM1ZERO_REAL_BOOT")
        if not path:
            self.skipTest("set FM1ZERO_REAL_BOOT to m-flasher's data/bootloader-fm1.boot")
        with open(path, "rb") as f:
            real = f.read()
        board, version = boot.container_identity(real)
        self.assertEqual(board, "fm1")
        self.assertEqual(boot.identify(self.area(real))[0], boot.OPEN)
        print("real m-bootloader:", board, version)

class EnterTests(unittest.TestCase):
    """Getting into download mode when the firmware doesn't know the message: wait for the keys."""

    def setUp(self):
        from fm1zero import device, usb
        self.device, self.usb = device, usb
        keep = (usb.rom_present, usb.find_fm1, usb.open_rom, usb.send_boot_key, device.time.sleep,
                device.KEY_WAIT_S)
        self.addCleanup(lambda: (setattr(usb, "rom_present", keep[0]), setattr(usb, "find_fm1", keep[1]),
                                 setattr(usb, "open_rom", keep[2]), setattr(usb, "send_boot_key", keep[3]),
                                 setattr(device.time, "sleep", keep[4]), setattr(device, "KEY_WAIT_S", keep[5])))
        device.time.sleep = lambda _s: None
        usb.find_fm1 = lambda: None
        usb.send_boot_key = lambda port: self.fail("nothing answered: no message may be sent")

    def test_carries_on_when_the_keys_put_it_in_download_mode(self):
        fake = FakeFm1()
        fake.close = lambda: None
        polls = []
        self.usb.rom_present = lambda: (polls.append(1), len(polls) > 4)[1]
        self.usb.open_rom = lambda wait: fake
        said = []
        r, before = self.device._enter(said.append)
        self.assertIn("hold OCT- and OCT+", said[1])
        self.assertEqual(before, "")
        self.assertTrue(fake.running)

    def test_gives_up_after_a_while_with_nothing_written(self):
        self.device.KEY_WAIT_S = 0.0
        self.usb.rom_present = lambda: False
        with self.assertRaises(rom.RomError):
            self.device._enter(lambda _s: None)

if __name__ == "__main__":
    unittest.main()
