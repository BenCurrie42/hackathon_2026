"""Listening to a song's stems: what plays when, so the assistant understands the song.

The assistant can't hear, so this turns one song's stem files into text it can
read: when each part sounds, the song's sections (where parts come in and drop
out), the tempo from the click, and which vocal is probably the lead.

    Lead Vox: 0:16-1:02, 1:20-3:40
    0:48-1:02  + BGV High, BGV Low        <- backing vocals come in: likely a chorus

Standard library only, from the same WAV/AIFF reader as app/audio_files.py.
Nothing plays out loud; it reads the files.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path

from app import audio_files
from live_control.stereo_pairs import stereo_pairs

STEP_SECONDS = 1.0       # resolution of "when does this part sound"
# dB under a stem's own loud level that still counts as sounding. Tuned on a live
# multitrack: 15 dB keeps band bleed in a vocal mic from reading as singing, and
# barely moves instruments (checked against 10-30 dB on 50 real stems).
SOUNDING_BELOW_LOUD = 15
FILL_GAP_SECONDS = 4      # a pause this short (a breath, a rest) doesn't end a part
SHORTEST_PART_SECONDS = 2  # anything shorter is a blip, not the part playing
SHORTEST_SECTION_SECONDS = 6
MAX_SECTIONS = 30

CLICK = re.compile(r"\b(click|clk|metronome|met)\b", re.I)
CUES = re.compile(r"\b(guide|cues?|count|smpte|timecode|ltc)\b", re.I)
ROOM = re.compile(r"\b(crowds?|audience|foh|room|amb(ience)?)\b", re.I)
VOCAL = re.compile(r"\b(vox|vocals?|voc|bgvs?|bv|harmony|harm|choir|lead|ld|alto|tenor|sop(rano)?|gang)\b", re.I)
BACKING = re.compile(r"\b(bgvs?|bv|harmony|harm|choir|alto|tenor|sop(rano)?|gang|backing)\b", re.I)
# Everything a stem is usually called when it isn't a voice. Live recordings name
# vocal mics after the singer ("Chloe", "Corbin Phillips"), so a stem that matches
# none of these and no vocal word is probably a person singing.
INSTRUMENT = re.compile(
    r"\b(acc|acoustic|ag|eg|gtrs?|guitars?|b3|organ|bass|sub|keys?|piano|pno|rhodes|wurli|synths?|"
    r"pads?|arp|strings?|str|horns?|brass|sax|tpt|trumpet|cello|violin|fx|sfx|loops?|perc|drums?|"
    r"kick|snare|toms?|hats?|hh|trig|oh|overheads?|cymbals?|ride|shaker|tamb)\b", re.I)

_cache = {}


def listen(stems):
    """A text report on one song's stems. stems: [(name, path)]: a track or file name, and its file."""
    heard, unread = {}, []
    for name, path in stems:
        result = _study(Path(path))
        if result is None:
            unread.append(name)
        else:
            heard[name] = result
    if not heard:
        return "None of those files could be read (only WAV and AIFF can be listened to)."

    length = max(len(r["steps"]) for r in heard.values())
    steps = {name: r["steps"] for name, r in heard.items()}
    for left, right in stereo_pairs(list(steps)):  # one part, not two
        both = [a or b for a, b in zip(steps.pop(left), steps.pop(right))]
        steps[left[:-1].rstrip() + " L/R" if left[-1] in "lL" else left + "/R"] = both
    parts = {name: _runs(steps[name]) for name in sorted(steps, key=str.casefold)}
    lines = [f"Listened to {len(heard)} stems ({_time(length * STEP_SECONDS)} long)."]

    bpm = next((r["bpm"] for n, r in heard.items() if CLICK.search(n) and r.get("bpm")), None)
    if bpm:
        half = f" (or {bpm / 2:g} if the click plays eighth notes)" if bpm > 140 else ""
        lines.append(f"Tempo from the click: about {bpm:g} BPM{half}.")

    lines.append("\nWhen each part sounds:")
    whole = (0, length * STEP_SECONDS)
    for name, runs in parts.items():
        if not runs:
            text = "silent"
        elif len(runs) == 1 and runs[0][0] <= 5 and runs[0][1] >= whole[1] - 5:
            text = "the whole song"
        else:
            text = ", ".join(f"{_time(a)}-{_time(b)}" for a, b in runs)
        lines.append(f"  {name}: {text}")

    musical = {n: runs for n, runs in parts.items()
               if not (CLICK.search(n) or CUES.search(n) or ROOM.search(n))}
    sections = _sections(musical, length)
    if sections:
        lines.append("\nSections (where parts come in and drop out; click, cues and room mics left out):")
        before = set()
        for start, end, playing in sections:
            came = sorted(playing - before, key=str.casefold)
            went = sorted(before - playing, key=str.casefold)
            change = "; ".join(filter(None, [
                "in: " + ", ".join(came) if came else "",
                "out: " + ", ".join(went) if went else "",
            ])) or "no change"
            lines.append(f"  {_time(start)}-{_time(end)}  {change}")
            before = playing

    lead = _likely_lead({n: runs for n, runs in musical.items() if _is_voice(n)}, length * STEP_SECONDS)
    if lead:
        lines.append("\n" + lead)
    if unread:
        lines.append("\nCouldn't listen to: " + ", ".join(unread) + " (only WAV and AIFF).")
    return "\n".join(lines)


def _is_voice(name):
    """Named as a vocal, or named after a person (nothing an instrument is called)."""
    return not INSTRUMENT.search(name)


# -- one stem ----------------------------------------------------------------


def _study(path):
    """{"steps": [bool per second], "bpm": float|None} for one file, or None if unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime)
    if key not in _cache:
        levels = audio_files.envelope(path, STEP_SECONDS)
        if levels is None:
            return None
        loud = [d for d in levels if d > audio_files.SILENT_DB]
        if loud:
            floor = max(audio_files.SILENT_DB, sorted(loud)[int(len(loud) * 0.9)] - SOUNDING_BELOW_LOUD)
        else:
            floor = 0.0
        result = {"steps": [d > floor for d in levels], "bpm": None}
        if CLICK.search(path.stem):
            result["bpm"] = _tempo(path)
        _cache[key] = result
    return _cache[key]


def _runs(steps):
    """Sounding stretches as (start, end) seconds, short gaps filled and blips dropped."""
    runs, start = [], None
    for i, on in enumerate(steps + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append([start, i])
            start = None
    merged = []
    for run in runs:
        if merged and (run[0] - merged[-1][1]) * STEP_SECONDS <= FILL_GAP_SECONDS:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [(a * STEP_SECONDS, b * STEP_SECONDS) for a, b in merged
            if (b - a) * STEP_SECONDS >= SHORTEST_PART_SECONDS]


def _tempo(path):
    """BPM from a click stem: the typical gap between clicks."""
    onsets = audio_files.onsets(path)
    if onsets is None or len(onsets) < 8:
        return None
    gaps = [b - a for a, b in zip(onsets, onsets[1:]) if 0.2 <= b - a <= 2.0]  # 30-300 BPM
    if len(gaps) < 4:
        return None
    return round(60 / statistics.median(gaps), 1)


# -- the whole song --------------------------------------------------------------


def _sections(parts, length):
    """(start, end, {parts playing}) wherever the set of playing parts changes."""
    if not parts:
        return []
    playing_at = []
    for second in range(int(length * STEP_SECONDS)):
        playing_at.append(frozenset(n for n, runs in parts.items() if any(a <= second < b for a, b in runs)))
    sections = []
    for second, playing in enumerate(playing_at):
        if sections and sections[-1][2] == playing:
            sections[-1][1] = second + 1
        else:
            sections.append([second, second + 1, playing])
    # Fold short sections into the one before: a fill or a late entry isn't a new section.
    folded = []
    for section in sections:
        if folded and section[1] - section[0] < SHORTEST_SECTION_SECONDS:
            folded[-1][1] = section[1]
        else:
            folded.append(section)
    # Folding can leave neighbours playing the same parts; they're one section.
    merged = []
    for section in folded:
        if merged and merged[-1][2] == section[2]:
            merged[-1][1] = section[1]
        else:
            merged.append(section)
    folded = merged
    while len(folded) > MAX_SECTIONS:
        shortest = min(range(1, len(folded)), key=lambda i: folded[i][1] - folded[i][0])
        folded[shortest - 1][1] = folded[shortest][1]
        del folded[shortest]
    return [(a, b, set(p)) for a, b, p in folded if p]


def _likely_lead(vocals, length):
    """Sentences on which voice is probably the lead, or "" when there are no voices."""
    sung = {n: sum(b - a for a, b in runs) for n, runs in vocals.items()}
    sung = {n: t for n, t in sung.items() if t}
    if not sung:
        return ""
    ranked = sorted(sung, key=sung.get, reverse=True)
    leads = [n for n in ranked if not BACKING.search(n)]
    lead = leads[0] if leads else ranked[0]
    share = lambda n: f"{n} {round(100 * sung[n] / length)}%"  # noqa: E731
    named = [n for n in ranked if not VOCAL.search(n)]
    text = ("Voices (sounding share of the song): " + ", ".join(share(n) for n in ranked) + "."
            f"\nProbably the lead vocal: {lead}, who sings the most.")
    if named:
        text += (" Taken to be " + ("singers" if len(named) > 1 else "a singer") +
                 " from the name alone: " + ", ".join(named) + ".")
    return text


# -- wording ------------------------------------------------------------------


def _time(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
