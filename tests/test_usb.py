import unittest

from fm1zero import usb


def identity_packet(text: str) -> bytes:
    body = bytes([0, 0x59, 0x11, 0, 0, 0]) + text.encode().ljust(28, b"\0")
    return b"\xF0" + usb.pack7(body) + b"\xF7"


class MidiTests(unittest.TestCase):
    def test_seven_bit_packing_round_trips(self):
        data = bytes(range(256)) * 3
        packed = usb.pack7(data)
        self.assertTrue(all(b < 0x80 for b in packed))
        self.assertEqual(usb.unpack7(packed)[:len(data)], data)

    def test_identity(self):
        identity = usb.parse_identity(identity_packet("FM-1_918"))
        self.assertEqual((identity.text, identity.model, identity.version), ("FM-1_918", "FM-1", 918))
        self.assertFalse(identity.ota)
        self.assertTrue(usb.parse_identity(identity_packet("ota-FM-1_900")).ota)
        self.assertEqual(usb.parse_identity(identity_packet("SMK-37 Elite_016")).model, "SMK-37 Elite")

    def test_not_an_identity(self):
        self.assertIsNone(usb.parse_identity(identity_packet("FM-1")))
        self.assertIsNone(usb.parse_identity(identity_packet("FM-1_9x")))
        self.assertIsNone(usb.parse_identity(b"\xF0\x7E\x00\x06\x02\xF7"))
        self.assertIsNone(usb.parse_identity(b""))

    def test_sysex_is_cut_out_of_a_stream(self):
        buffer = bytearray(b"\x90\x40\x7F\xF0\x01\x02\xF7\xF8\xF0\x03")
        self.assertEqual(usb.sysex_packets(buffer), [b"\xF0\x01\x02\xF7"])
        buffer += b"\x04\xF7"
        self.assertEqual(usb.sysex_packets(buffer), [b"\xF0\x03\x04\xF7"])
        self.assertEqual(buffer, bytearray())

    def test_only_fm1_names_are_candidates(self):
        self.assertTrue(usb.MidiPort("p", "FM-1", 0x1234, 1).candidate)
        self.assertTrue(usb.MidiPort("p", "Something", usb.FELUCCA_VID, 1).candidate)
        self.assertFalse(usb.MidiPort("p", "Launchkey Mini", 0x1235, 1).candidate)


if __name__ == "__main__":
    unittest.main()
