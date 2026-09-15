"""Turn a RigSpec into a .als.

Tracks are cloned from a donor track in the template rather than built from
scratch: Live's track XML carries a lot of structure we have no documentation
for, and copying a known-good one and rewriting the parts we understand is the
only honest way to do this. Everything cloned gets renumbered — see
AGENTS.md for the ID rules, which are not negotiable.
"""

from __future__ import annotations

import copy
import gzip
import math
import re
from pathlib import Path

from lxml import etree

from rigspec import RigSpec, TrackSpec

LIVE_APP = Path("/Applications/Ableton Live 12 Trial.app")
CORE_LIBRARY = LIVE_APP / "Contents/App-Resources/Core Library"
DEFAULTS = CORE_LIBRARY / "Defaults/Audio Effects"
DEVICES = CORE_LIBRARY / "Devices/Audio Effects"

DECLARATION = b'<?xml version="1.0" encoding="UTF-8"?>\n'

# One shared global pool, watermarked by <NextPointeeId>. Collisions make Live
# offer to repair the file, and it silently drops what it can't reconcile.
#
# The pool is not three tags. Alongside AutomationTarget and ModulationTarget
# there are eight specialised variants -- TranspositionModulationTarget,
# ComplexProEnvelopeModulationTarget and friends -- that live in clip and sample
# nodes and draw from the same numbering. Matching the suffix catches all of
# them; matching tag names by hand does not, and the ones it misses are exactly
# the ones a cloned track brings with it.
def in_pointee_pool(element: etree._Element) -> bool:
    return element.get("Id") is not None and (
        element.tag == "Pointee" or element.tag.endswith("Target")
    )

# Routing targets embed a track ID inside a plain string, e.g.
# "AudioIn/Track.14/TrackOut". Not an Id attribute, easy to miss.
TRACK_REF = re.compile(r"(Track\.)(\d+)")

# Devices without a factory default on disk need a preset chosen for them.
# Display name -> preset file under Devices/Audio Effects/<name>/.
PRESET_FALLBACKS = {
    "Compressor": "Sustained Lead Vocal.adv",
}


def gain_from_db(db: float) -> float:
    """Live stores fader position as a linear gain factor, not as dB.

    Derived from the fader's own range: its minimum 0.0003162277571 is
    10^(-70/20) and its maximum 1.99526238 is 10^(6/20).
    """
    return 10 ** (db / 20)


def device_preset(name: str) -> Path:
    """Locate the .adv holding a stock device, by its display name."""
    default = DEFAULTS / f"{name}.adv"
    if default.exists():
        return default

    folder = DEVICES / name
    if not folder.is_dir():
        raise LookupError(f"no stock device named {name!r}")

    fallback = PRESET_FALLBACKS.get(name)
    if fallback and (folder / fallback).exists():
        return folder / fallback

    presets = sorted(folder.glob("*.adv"))
    if not presets:
        raise LookupError(f"device {name!r} has no default and no presets")
    return presets[0]


class _Renderer:
    """Mutable rendering state: one document, one ID watermark."""

    DONOR_TAGS = ("AudioTrack", "MidiTrack", "GroupTrack")

    def __init__(self, template: Path):
        self.root = etree.fromstring(gzip.decompress(template.read_bytes()))
        self.tracks = self.root.find("LiveSet/Tracks")
        self.watermark = self.root.find("LiveSet/NextPointeeId")
        self.next_pointee = int(self.watermark.get("Value"))
        self.return_count = len(self.tracks.findall("ReturnTrack"))

        # A spec describes the whole session, so the template's own tracks are
        # scaffolding, not content. Keep one of each kind to clone from, then
        # clear them out. Return tracks stay -- every track we add needs a send
        # holder per return, so removing them would change that contract.
        self.donors = {
            tag: copy.deepcopy(found[-1])
            for tag in self.DONOR_TAGS
            if (found := self.tracks.findall(tag))
        }
        for tag in self.DONOR_TAGS:
            for track in self.tracks.findall(tag):
                self.tracks.remove(track)

    def renumber(self, subtree: etree._Element) -> None:
        """Reassign every pointee-pool ID in a subtree and remap references."""
        remapped: dict[str, str] = {}
        for element in subtree.iter():
            if in_pointee_pool(element):
                remapped[element.get("Id")] = str(self.next_pointee)
                element.set("Id", str(self.next_pointee))
                self.next_pointee += 1

        for element in subtree.iter("PointeeId"):
            old = element.get("Value")
            if old in remapped:
                element.set("Value", remapped[old])

    def next_track_id(self) -> int:
        ids = [int(t.get("Id")) for t in self.tracks if t.get("Id") is not None]
        return max(ids, default=0) + 1

    def donor(self, kind: str) -> etree._Element:
        tag = "AudioTrack" if kind == "audio" else "MidiTrack"
        if tag not in self.donors:
            raise LookupError(f"template has no {tag} to clone")
        return self.donors[tag]

    def add_device(self, track: etree._Element, name: str) -> None:
        preset = device_preset(name)
        root = etree.fromstring(gzip.decompress(preset.read_bytes()))
        candidates = [c for c in root if c.tag != "OverwriteProtectionNumber"]
        if len(candidates) != 1:
            raise ValueError(f"expected one device element in {preset.name}")
        device = candidates[0]

        self.renumber(device)
        chain = track.find("DeviceChain/DeviceChain/Devices")
        device.set("Id", str(len(chain) + 1))
        chain.append(device)

    def add_track(self, spec: TrackSpec) -> etree._Element:
        track = copy.deepcopy(self.donor(spec.type))
        track_id = self.next_track_id()
        track.set("Id", str(track_id))
        self.renumber(track)

        name = track.find("Name")
        name.find("EffectiveName").set("Value", spec.name)
        name.find("UserName").set("Value", spec.name)

        mixer = track.find("DeviceChain/Mixer")
        mixer.find("Volume/Manual").set("Value", repr(gain_from_db(spec.volume_db)))
        mixer.find("Pan/Manual").set("Value", repr(spec.pan))

        chain = track.find("DeviceChain")
        if spec.type == "audio":
            routing = chain.find("AudioInputRouting")
            if spec.input is None:
                target, upper, lower = "AudioIn/None", "No Input", ""
            else:
                target = f"AudioIn/External/M{spec.input - 1}"
                upper, lower = "Ext. In", str(spec.input)
            routing.find("Target").set("Value", target)
            routing.find("UpperDisplayString").set("Value", upper)
            routing.find("LowerDisplayString").set("Value", lower)

        # A cloned track carries the donor's routing strings, which may name the
        # donor by ID. Nothing we generate routes track-to-track yet, but a stale
        # reference here is silent and would be miserable to find later.
        for target in chain.iter("Target"):
            value = target.get("Value") or ""
            if TRACK_REF.search(value):
                target.set("Value", TRACK_REF.sub(rf"\g<1>{track_id}", value))

        # Empty the device chain the donor came with before adding our own.
        devices = track.find("DeviceChain/DeviceChain/Devices")
        for existing in list(devices):
            devices.remove(existing)
        for device_name in spec.devices:
            self.add_device(track, device_name)

        self.check_sends(track)

        # Regular tracks live before the return tracks in document order.
        returns = self.tracks.find("ReturnTrack")
        if returns is None:
            self.tracks.append(track)
        else:
            returns.addprevious(track)
        return track

    def check_sends(self, track: etree._Element) -> None:
        """One TrackSendHolder per return track, IDs 0..N-1.

        A mismatch crashes Live on load with 'invalid vector subscript'.
        """
        holders = track.findall(".//TrackSendHolder")
        expected = [str(i) for i in range(self.return_count)]
        if [h.get("Id") for h in holders] != expected:
            raise ValueError(
                f"track {track.find('Name/EffectiveName').get('Value')!r} has "
                f"{len(holders)} send holders, expected {self.return_count}"
            )

    def check_pointee_pool(self) -> None:
        """No duplicates, no dangling references, watermark ahead of the max.

        Live responds to a violation by offering to repair the file rather than
        refusing it, so this will not show up as a crash -- it shows up as a
        dialog and silently dropped state. Check it here; a file we cannot prove
        is sound should never reach disk.
        """
        ids = [e.get("Id") for e in self.root.iter() if in_pointee_pool(e)]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate pointee IDs: {sorted(duplicates)[:5]}")

        known = set(ids)
        dangling = {
            p.get("Value") for p in self.root.iter("PointeeId")
            if p.get("Value") not in known
        }
        if dangling:
            raise ValueError(f"dangling PointeeId references: {sorted(dangling)[:5]}")

        if self.next_pointee <= max(int(i) for i in ids):
            raise ValueError("NextPointeeId is not ahead of the highest ID in use")

    def finish(self) -> bytes:
        self.check_pointee_pool()
        self.watermark.set("Value", str(self.next_pointee))
        body = etree.tostring(
            self.root.getroottree(), xml_declaration=False, encoding="UTF-8"
        )
        body = body.replace(b"/>", b" />")
        if not body.endswith(b"\n"):
            body += b"\n"
        return gzip.compress(DECLARATION + body)


def render(spec: RigSpec, template_path: Path) -> bytes:
    """Render a spec to the bytes of a .als. Never mutates the template."""
    renderer = _Renderer(template_path)
    for track in spec.tracks:
        renderer.add_track(track)
    return renderer.finish()
