"""The church's fixed list of part tracks, and which part each stem belongs to.

Every vendor names stems differently (ACC 1 / AG 1, GTR 1 L / EG 1, Kick In /
Drums (Live)), and a recording session adds a stem per mic. Making a track per
stem name grew one set to 93 tracks for three songs. Instead every song lands on
the same ~20 part tracks, in the same order, every week; where several stems belong
to one part they're mixed into one file for that song (app/mixdown.py).

A stem that fits no part keeps its own name (singers' names, oddities); the
assistant can map it by listening.
"""

from __future__ import annotations

import re
from pathlib import Path

from live_control.stereo_pairs import stereo_pairs

# (part name, pattern). Checked in this order, so "Synth Bass" is Synth Bass, not
# Bass, and "Drum Loop" is Loops. The order is also the order tracks are created in.
PARTS = [
    ("Click", r"click|metronome"),
    ("Guide", r"guide|\bcues?\b|count"),
    ("SMPTE", r"smpte|timecode|\bltc\b"),
    ("Loops", r"loop|sequence|\bseq\b"),
    ("Drums", r"drum|kick|snare|\btoms?\b|hi-?hats?|\bhats?\b|cymbal|overhead|\boh\b|\btrig|\bkit\b|\broom\b"),
    ("Perc", r"perc|shaker|tamb|conga|bongo|cajon|clap"),
    ("Synth Bass", r"synth ?bass|\bsub\b|808"),
    ("Bass", r"bass"),
    ("Acoustic", r"acoustic|\bacc?\b|\bag\b|\bacg\b"),
    ("Electric", r"electric|\beg\b|guitar|gtr"),
    ("Piano", r"piano"),
    ("Organ", r"organ|\bb3\b|hammond"),
    ("Keys", r"keys?\b|rhodes|wurli|\bkb\b|nord"),
    ("Strings", r"string|violin|viola|cello"),
    ("Horns", r"sax|horn|trumpet|trombone|brass"),
    ("Synths", r"synth|\bpads?\b|\barps?\b|\blead\b(?!.*vo)"),
    ("FX", r"\bfx\b|riser|swell|sweep|impact|ambien"),
    ("Lead Vocal", r"lead ?vo|\blv\b|lead vocal"),
    ("Choir", r"choir|gang"),
    ("BGVs", r"\bbgvs?\b|\bbvs?\b|backing|harmon|\bvox\b|vocal|\bvocs?\b|alto|tenor|soprano"),
    ("Crowd", r"crowd|audience|\bfoh\b"),
]
PART_NAMES = [name for name, _pattern in PARTS]
_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in PARTS]
_NUMBER = re.compile(r"\s*\d+$")


def part_for(stem):
    """The part a stem name belongs to, or None if it fits none."""
    for name, pattern in _COMPILED:
        if pattern.search(stem):
            return name
    return None


def plan(files):
    """Group one song's stems into parts: {part: [file id, ...]} in part order.

    files: [file id] such as "Washed/MultiTracks/EG 3.wav". A stem that fits no
    part gets its own entry under its name (without a trailing take number).
    """
    grouped = {}
    for fid in files:
        stem = Path(fid).stem
        part = part_for(stem) or _NUMBER.sub("", _side_less(stem)) or stem
        grouped.setdefault(part, []).append(fid)
    order = {name: i for i, name in enumerate(PART_NAMES)}
    return dict(sorted(grouped.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0].casefold())))


def pans(files):
    """{file id: -1, 0 or 1}: L/R stems go to their side when mixed, everything else centre."""
    names = {Path(f).stem: f for f in files}
    found = {}
    for left, right in stereo_pairs(list(names)):
        found[names[left]] = -1
        found[names[right]] = 1
    return {f: found.get(f, 0) for f in files}


def describe(song, grouped):
    """The plan as lines for the assistant."""
    lines = [f'Parts for "{song}":']
    for part, files in grouped.items():
        known = "" if part in PART_NAMES else " (fits no part: its own track, or map it to one)"
        lines.append(f"  {part}{known}: " + ", ".join(f'"{f}"' for f in files))
    return "\n".join(lines)


def _side_less(stem):
    return re.sub(r"[\s_-]+(l|r|left|right)$", "", stem, flags=re.IGNORECASE)
