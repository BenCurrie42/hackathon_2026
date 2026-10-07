"""The .als renderer against the read-only reference template.

    uv run python -m unittest discover tests

Device tests read Live's own .adv presets and skip when Live isn't installed;
everything else runs anywhere.
"""

import gzip
import hashlib
import re
import unittest
from pathlib import Path

from lxml import etree

from file_builder.rig_spec import RigSpec, TrackSpec
from file_builder.write_als import DEFAULTS, _Renderer, in_pointee_pool, render

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "test.als"


def parse(als: bytes) -> etree._Element:
    return etree.fromstring(gzip.decompress(als))


def click_pad_guide() -> RigSpec:
    return RigSpec(tracks=[
        TrackSpec(name="Click", input=None, volume_db=-6.0),
        TrackSpec(name="Pad", input=2, pan=-0.5),
        TrackSpec(name="Guide", input=3),
        TrackSpec(name="Keys", type="midi"),
    ])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.root = parse(render(click_pad_guide(), TEMPLATE))
        self.tracks = self.root.find("LiveSet/Tracks")

    def track(self, name):
        for t in self.tracks:
            if t.find("Name/EffectiveName").get("Value") == name:
                return t
        self.fail(f"no track named {name!r}")

    def test_container_and_formatting(self):
        als = render(click_pad_guide(), TEMPLATE)
        self.assertEqual(als[:2], b"\x1f\x8b")
        xml = gzip.decompress(als)
        self.assertTrue(xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n'))
        self.assertTrue(xml.endswith(b"\n"))
        self.assertNotRegex(xml, rb"[^ ]/>")

    def test_header_copied_verbatim(self):
        template = parse(TEMPLATE.read_bytes())
        self.assertEqual(dict(self.root.attrib), dict(template.attrib))

    def test_spec_tracks_replace_template_tracks(self):
        regular = [t for t in self.tracks if t.tag != "ReturnTrack"]
        names = [t.find("Name/EffectiveName").get("Value") for t in regular]
        self.assertEqual(names, ["Click", "Pad", "Guide", "Keys"])
        self.assertEqual([t.tag for t in regular], ["AudioTrack"] * 3 + ["MidiTrack"])
        for t in regular:
            name = t.find("Name")
            self.assertEqual(name.find("UserName").get("Value"), name.find("EffectiveName").get("Value"))

    def test_regular_tracks_before_returns(self):
        tags = [t.tag for t in self.tracks]
        first_return = tags.index("ReturnTrack")
        self.assertNotIn("AudioTrack", tags[first_return:])
        self.assertNotIn("MidiTrack", tags[first_return:])

    def test_track_ids_unique(self):
        ids = [t.get("Id") for t in self.tracks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_input_routing(self):
        def target(name):
            r = self.track(name).find("DeviceChain/AudioInputRouting")
            return (r.find("Target").get("Value"), r.find("UpperDisplayString").get("Value"),
                    r.find("LowerDisplayString").get("Value"))

        self.assertEqual(target("Click"), ("AudioIn/None", "No Input", ""))
        self.assertEqual(target("Pad"), ("AudioIn/External/M1", "Ext. In", "2"))
        self.assertEqual(target("Guide"), ("AudioIn/External/M2", "Ext. In", "3"))

    def test_volume_is_linear_gain_and_pan(self):
        mixer = self.track("Click").find("DeviceChain/Mixer")
        self.assertAlmostEqual(float(mixer.find("Volume/Manual").get("Value")), 10 ** (-6 / 20))
        self.assertEqual(float(self.track("Guide").find("DeviceChain/Mixer/Volume/Manual").get("Value")), 1.0)
        self.assertEqual(float(self.track("Pad").find("DeviceChain/Mixer/Pan/Manual").get("Value")), -0.5)

    def test_one_send_per_return(self):
        returns = len(self.tracks.findall("ReturnTrack"))
        for t in self.tracks:
            if t.tag == "ReturnTrack":
                continue
            ids = [h.get("Id") for h in t.findall(".//TrackSendHolder")]
            self.assertEqual(ids, [str(i) for i in range(returns)])

    def test_pointee_pool_sound(self):
        ids = [int(e.get("Id")) for e in self.root.iter() if in_pointee_pool(e)]
        self.assertEqual(len(ids), len(set(ids)))
        known = {str(i) for i in ids}
        for ref in self.root.iter("PointeeId"):
            self.assertIn(ref.get("Value"), known)
        self.assertGreater(int(self.root.find("LiveSet/NextPointeeId").get("Value")), max(ids))

    def test_no_track_refs_to_missing_tracks(self):
        ids = {t.get("Id") for t in self.tracks}
        for target in self.root.iter("Target"):
            for ref in re.findall(r"Track\.(\d+)", target.get("Value") or ""):
                self.assertIn(ref, ids)

    def test_template_untouched(self):
        before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        render(click_pad_guide(), TEMPLATE)
        self.assertEqual(hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(), before)


class DonorRoutingTest(unittest.TestCase):
    """A clone's routing strings may name tracks by ID inside plain text."""

    def renderer_with_donor_target(self, value):
        r = _Renderer(TEMPLATE)
        target = r.donors["AudioTrack"].find("DeviceChain/AudioOutputRouting/Target")
        target.set("Value", value)
        return r

    def test_donor_self_reference_follows_the_clone(self):
        donor_id = _Renderer(TEMPLATE).donors["AudioTrack"].get("Id")
        r = self.renderer_with_donor_target(f"AudioIn/Track.{donor_id}/TrackOut")
        track = r.add_track(TrackSpec(name="Pad"))
        value = track.find("DeviceChain/AudioOutputRouting/Target").get("Value")
        self.assertEqual(value, f"AudioIn/Track.{track.get('Id')}/TrackOut")

    def test_return_reference_kept(self):
        return_id = _Renderer(TEMPLATE).tracks.find("ReturnTrack").get("Id")
        r = self.renderer_with_donor_target(f"AudioIn/Track.{return_id}/TrackOut")
        track = r.add_track(TrackSpec(name="Pad"))
        value = track.find("DeviceChain/AudioOutputRouting/Target").get("Value")
        self.assertEqual(value, f"AudioIn/Track.{return_id}/TrackOut")

    def test_reference_to_removed_track_refused(self):
        r = self.renderer_with_donor_target("AudioIn/Track.9999/TrackOut")
        with self.assertRaisesRegex(ValueError, "isn't in the set"):
            r.add_track(TrackSpec(name="Pad"))


@unittest.skipUnless(DEFAULTS.is_dir(), "Ableton Live isn't installed")
class DeviceTest(unittest.TestCase):
    def test_devices_inserted_in_order_with_sound_pool(self):
        spec = RigSpec(tracks=[TrackSpec(name="Vocal", input=1, devices=["EQ Eight", "Compressor", "Reverb"])])
        root = parse(render(spec, TEMPLATE))
        track = root.find("LiveSet/Tracks/AudioTrack")
        devices = track.find("DeviceChain/DeviceChain/Devices")
        self.assertEqual([d.tag for d in devices], ["Eq8", "Compressor2", "Reverb"])
        self.assertEqual([d.get("Id") for d in devices], ["1", "2", "3"])
        ids = [e.get("Id") for e in root.iter() if in_pointee_pool(e)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_unknown_device_refused(self):
        spec = RigSpec(tracks=[TrackSpec(name="Vocal", devices=["Not A Device"])])
        with self.assertRaises(LookupError):
            render(spec, TEMPLATE)


if __name__ == "__main__":
    unittest.main()
