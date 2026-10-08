import struct
import unittest

from scripts.install_riglink import PreferencesShapeError, _surface_slots, with_surface_picked


def encoded(text):
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def preferences(surfaces, tail=b"\x00\x00\x00\x00"):
    """Something shaped like the end of Live 12's Preferences.cfg."""
    head = b"\xab\x1eVx" + encoded("MidiOutDevicePreferences") + b"\x01\x00\x00\x00" * 3
    slots = b"".join(encoded(name) + encoded("None") + encoded("None") for name in surfaces)
    return head + slots + tail


class SurfacePickTest(unittest.TestCase):
    def test_fills_first_empty_slot(self):
        data = preferences(["Push3", "None", "None", "None", "None", "None", "None"])
        picked = with_surface_picked(data)
        names = [name for _, name in _surface_slots(picked)[1]]
        self.assertEqual(names, ["Push3", "RigLink", "None", "None", "None", "None", "None"])

    def test_matches_what_live_writes(self):
        before = preferences(["None"] * 7)
        after = preferences(["RigLink"] + ["None"] * 6)
        self.assertEqual(with_surface_picked(before), after)

    def test_already_picked_changes_nothing(self):
        self.assertIsNone(with_surface_picked(preferences(["None", "RigLink"] + ["None"] * 5)))

    def test_older_tail_still_found(self):
        data = preferences(["None"] * 7, tail=b"\x00\x00\x00\x00\x01\x00")
        self.assertTrue(with_surface_picked(data).endswith(b"\x00\x00\x00\x00\x01\x00"))

    def test_all_slots_full_refuses(self):
        with self.assertRaises(PreferencesShapeError):
            with_surface_picked(preferences([f"S{n}" for n in range(7)]))

    def test_unknown_layout_refuses(self):
        with self.assertRaises(PreferencesShapeError):
            with_surface_picked(preferences(["None"] * 6))


if __name__ == "__main__":
    unittest.main()
