"""Reading a song from a set a tracks vendor made (Washed, MultiTracks, Loop Community).

Vendors ship one song per set: every stem laid out in Arrangement view, sections as
locators. Holy Sound keeps the church's whole Sunday in one set, one scene per song,
so instead of changing the vendor's file we read it and bring the song in:
its tempo, its sections, and which stem sits on which track.

Read only. Their file is never written. Handles Live 8 sets (sample paths as
RelativePathElement dirs plus a Name, tempo as an automation event) and newer ones
(a RelativePath and Path value, tempo as a Manual value).
"""

from __future__ import annotations

import gzip
from pathlib import Path

from lxml import etree

MAX_SET_BYTES = 50_000_000  # decompressed; a stems set is a few MB


class VendorSetError(Exception):
    """The set couldn't be read. The message is a sentence for the volunteer."""


def find(folder):
    """The vendor set for a stems folder: an .als in it, or in its parent. None if there isn't one.

    Picking either the song folder or its stems subfolder should work. Live's
    Backup folder and more than one candidate (no way to tell which) give None.
    """
    folder = Path(folder)
    for place in (folder, folder.parent):
        sets = [p for p in place.glob("*.als") if not p.name.startswith(".")]
        if len(sets) == 1:
            return sets[0]
        if len(sets) > 1:
            return None
    return None


def read(path):
    """What a vendor set says about its song.

    {"file", "made_with", "tempo", "tempo_changes", "sections": [(name, beat)],
     "tracks": [{"name", "file", "start", "warped", "pitch", "clips"}], "layout_notes": [...]}
    """
    path = Path(path)
    try:
        with gzip.open(path) as f:
            raw = f.read(MAX_SET_BYTES + 1)
    except OSError:
        try:
            raw = path.read_bytes()  # Live also opens plain XML
        except OSError as e:
            raise VendorSetError(f"Couldn't open {path.name}.") from e
    if len(raw) > MAX_SET_BYTES:
        raise VendorSetError(f"{path.name} is too big to be a stems set.")
    try:
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as e:
        raise VendorSetError(f"{path.name} isn't an Ableton set Holy Sound can read.") from e
    if root.tag != "Ableton" or root.find("LiveSet") is None:
        raise VendorSetError(f"{path.name} isn't an Ableton set.")
    live_set = root.find("LiveSet")

    tempos = _tempos(live_set)
    sections = []
    for loc in live_set.findall("Locators/Locators/Locator"):
        name = (_value(loc, "Name") or "").strip()
        if name:
            sections.append((name, float(_value(loc, "Time") or 0)))
    sections.sort(key=lambda s: s[1])

    tracks, notes = [], []
    for track in live_set.findall("Tracks/AudioTrack"):
        name = _value(track, "Name/EffectiveName") or _value(track, "Name/UserName") or ""
        clips = track.findall("DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip")
        if not clips:
            continue
        first = min(clips, key=lambda c: float(c.get("Time", 0)))
        tracks.append({
            "name": name,
            "file": _sample_path(first, path.parent),
            "start": float(first.get("Time", 0)),
            "warped": _value(first, "IsWarped") == "true",
            "pitch": int(float(_value(first, "PitchCoarse") or 0)),
            "clips": len(clips),
        })
    if any(t["clips"] > 1 for t in tracks):
        notes.append("some tracks are cut into several clips; only the first is used")
    if any(t["start"] for t in tracks):
        notes.append("not every stem starts at the top of the song")
    if any(t["warped"] for t in tracks):
        notes.append("some stems are warped in their set")
    return {
        "file": path.name,
        "made_with": root.get("Creator", ""),
        "tempo": tempos[0] if tempos else None,
        "tempo_changes": len(set(tempos)) > 1,
        "sections": sections,
        "tracks": tracks,
        "layout_notes": notes,
    }


def describe(info, file_ids):
    """The set, for the assistant. file_ids: {absolute path: imported file id}."""
    tempo = info["tempo"]
    lines = [f'<vendor_set file="{info["file"]}" made_with="{info["made_with"]}">']
    lines.append(
        f"Tempo {tempo:g} BPM" + (" (it changes during the song)" if info["tempo_changes"] else "") + "."
        if tempo else "Tempo not set.")
    lines.append(f"{len(info['tracks'])} stem tracks, laid out in Arrangement view"
                 + (f"; {'; '.join(info['layout_notes'])}" if info["layout_notes"] else
                    ", all starting together at the top, unwarped") + ".")
    if info["sections"] and tempo:
        lines.append("Sections: " + ", ".join(
            f"{name} {_clock(beat * 60 / tempo)}" for name, beat in info["sections"]))
    for t in info["tracks"]:
        fid = file_ids.get(str(t["file"])) if t["file"] else None
        lines.append(f'- "{t["name"]}": ' + (f'"{fid}"' if fid else "its file isn't in this folder"))
    lines.append("</vendor_set>")
    return "\n".join(lines)


def _tempos(live_set):
    tempo = live_set.find("MasterTrack/MasterChain/Mixer/Tempo")
    if tempo is None:
        tempo = live_set.find("MasterTrack/DeviceChain/Mixer/Tempo")
    if tempo is None:
        return []
    events = [float(e.get("Value")) for e in tempo.findall("ArrangerAutomation/Events/FloatEvent")]
    manual = tempo.find("Manual")
    if manual is not None and not events:
        return [float(manual.get("Value"))]
    if not events:
        # Newer sets keep tempo automation in the track's AutomationEnvelopes.
        return [float(manual.get("Value"))] if manual is not None else []
    return events


def _sample_path(clip, base):
    ref = clip.find("SampleRef/FileRef")
    if ref is None:
        return None
    absolute = ref.find("Path")
    if absolute is not None and absolute.get("Value") and Path(absolute.get("Value")).is_file():
        return Path(absolute.get("Value")).resolve()
    relative = ref.find("RelativePath")
    if relative is not None and relative.get("Value"):
        candidate = base / relative.get("Value")
    elif relative is not None:
        dirs = [e.get("Dir", "") for e in relative.findall("RelativePathElement")]
        candidate = base.joinpath(*dirs, _value(ref, "Name") or "")
    else:
        return None
    return candidate.resolve() if candidate.is_file() else None


def _value(node, path):
    found = node.find(path)
    return found.get("Value") if found is not None else None


def _clock(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
