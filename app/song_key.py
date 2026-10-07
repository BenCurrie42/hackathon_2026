"""Guessing a song's key from its stems, so the volunteer only has to confirm it.

Standard library only. For a few dozen short stretches of each pitched stem (keys,
pads, guitars, bass) it measures how strongly each of the 48 notes from E2 to D#6
sounds, folds them into the 12 note names, and matches that against the
Krumhansl-Kessler major and minor key profiles.

It's a guess. The usual miss is the relative key (E major vs C# minor share every
note), so the runner-up is always reported alongside.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from app import audio_files

MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MINOR_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]
# Krumhansl & Kessler (1982): how well each scale degree fits a major or minor key.
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

PITCHED = re.compile(
    r"bass|key|piano|organ|\bb3\b|hammond|synth|pad|rhodes|wurli|string|guitar|gtr|acoustic|\bacc\b|\bag\b|\beg\b|nord|moog",
    re.IGNORECASE)
UNPITCHED = re.compile(
    r"click|guide|\bcue|count|smpte|timecode|drum|kick|snare|\btoms?\b|hat|cymbal|overhead|perc|shaker"
    r"|tamb|loop|crowd|room|noise",
    re.IGNORECASE)
VOCAL = re.compile(r"vox|vocal|\bvocs?\b|bgv|choir|harmon", re.IGNORECASE)

ANALYSIS_RATE = 5500     # Hz after decimation: room for every note up to D#6
SEGMENT_SAMPLES = 2048   # per stretch, after decimation (~0.37 s)
SEGMENTS = 40            # stretches per stem, spread over the song
QUIET_DBFS = -45         # stretches quieter than this are skipped
LOWEST_NOTE, HIGHEST_NOTE = 40, 87  # MIDI E2 to D#6
CLEAR_MARGIN = 0.08      # correlation lead over the runner-up that counts as clear


def key_name(tonic, mode):
    """'E major' / 'C# minor' from a pitch class (0 = C) and 'major' or 'minor'."""
    return f"{(MAJOR_NAMES if mode == 'major' else MINOR_NAMES)[tonic]} {mode}"


def pitched_stems(stems):
    """The stems worth listening to for the key: [(name, path)].

    Instruments that carry harmony first; vocals only if there's nothing else.
    """
    usable = [(n, p) for n, p in stems if not UNPITCHED.search(n)]
    harmony = [(n, p) for n, p in usable if PITCHED.search(n) and not VOCAL.search(n)]
    return harmony or usable


def detect(stems):
    """Guess one song's key from its stems: [(name, path)].

    Returns {"key", "tonic", "mode", "runner_up", "clear", "stems"} or None when
    no stem had readable, pitched audio.
    """
    chroma = [0.0] * 12
    used = []
    for name, path in pitched_stems(stems):
        c = _stem_chroma(Path(path))
        if c:
            used.append(name)
            chroma = [a + b for a, b in zip(chroma, c)]
    if not used or not any(chroma):
        return None
    scores = []
    for tonic in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = profile[-tonic:] + profile[:-tonic] if tonic else profile
            scores.append((_correlation(chroma, rotated), tonic, mode))
    scores.sort(reverse=True)
    (best, tonic, mode), (second, t2, m2) = scores[0], scores[1]
    return {
        "key": key_name(tonic, mode),
        "tonic": tonic,
        "mode": mode,
        "runner_up": key_name(t2, m2),
        "clear": best - second >= CLEAR_MARGIN,
        "stems": used,
    }


def describe(guess):
    """One line for the assistant."""
    if guess is None:
        return "Key: couldn't tell (no pitched stems to listen to)."
    sure = "fairly clear" if guess["clear"] else "not clear"
    return (f"Key: probably {guess['key']} ({sure}; next closest {guess['runner_up']}), "
            f"from {', '.join(guess['stems'])}.")


def _stem_chroma(path):
    """Summed per-stretch note-name strengths for one file, or None if unreadable."""
    opened = audio_files._opened(path)
    if opened is None:
        return None
    f, info = opened
    with f:
        try:
            return _read_chroma(f, info)
        except (OSError, ValueError, ZeroDivisionError):
            return None


def _read_chroma(f, info):
    decode, full_scale = audio_files._decoder(info)
    channels, rate = info["channels"], info["rate"]
    frame_bytes = channels * info["bits"] // 8
    total = audio_files._frames(f, info)
    step = max(1, rate // ANALYSIS_RATE)
    span = SEGMENT_SAMPLES * step
    if total < span:
        return None
    analysis_rate = rate / step
    coeffs = [(n % 12, 2 * math.cos(2 * math.pi * 440 * 2 ** ((n - 69) / 12) / analysis_rate))
              for n in range(LOWEST_NOTE, HIGHEST_NOTE + 1)]
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / (SEGMENT_SAMPLES - 1)) for i in range(SEGMENT_SAMPLES)]
    quiet = 10 ** (QUIET_DBFS / 20)

    chroma = [0.0] * 12
    heard = False
    for k in range(SEGMENTS):
        start = int((total - span) * (k + 0.5) / SEGMENTS)
        f.seek(info["offset"] + start * frame_bytes)
        values = decode(f.read(span * frame_bytes))
        mono = values[::channels]
        # Average each run of `step` samples: decimation with a crude low-pass.
        down = [sum(run) / (step * full_scale) for run in zip(*(mono[i::step] for i in range(step)))]
        if len(down) < SEGMENT_SAMPLES:
            continue
        down = down[:SEGMENT_SAMPLES]
        if math.sqrt(sum(x * x for x in down) / SEGMENT_SAMPLES) < quiet:
            continue
        x = [a * w for a, w in zip(down, hann)]
        segment = [0.0] * 12
        for pitch_class, coeff in coeffs:
            s1 = s2 = 0.0
            for sample in x:
                s1, s2 = sample + coeff * s1 - s2, s1
            segment[pitch_class] += math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2))
        total_strength = sum(segment)
        if total_strength:
            # Each stretch counts the same, so one loud chorus doesn't decide the key.
            chroma = [c + s / total_strength for c, s in zip(chroma, segment)]
            heard = True
    return chroma if heard else None


def _correlation(a, b):
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    da = [x - mean_a for x in a]
    db = [y - mean_b for y in b]
    denom = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
    return sum(x * y for x, y in zip(da, db)) / denom if denom else 0.0
