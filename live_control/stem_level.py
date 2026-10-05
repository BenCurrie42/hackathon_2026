"""How loud a stem is while it's playing, and how high it peaks, from a WAV file.

Stems are mostly silence between entries, so a whole-file average says more
about the arrangement than the level. This measures RMS over 400 ms windows
and keeps only the windows with sound in them.

Standard library only: integer PCM WAV, 16/24/32-bit. Anything else returns
None and the caller leaves the stem's level alone.
"""

import math
import wave
from typing import NamedTuple

WINDOW_SECONDS = 0.4
SILENCE_DB = -50.0
# Reading every 6th sample is plenty for RMS and keeps pure Python fast.
SAMPLE_STEP = 6


class StemLevel(NamedTuple):
    active_rms_db: float
    peak_db: float


def stem_level(path):
    """Active RMS and peak in dBFS, or None if the file is silent or unreadable.

    The peak comes from the same sparse read as the RMS, so a short transient
    can sit up to a dB or so above it.
    """
    try:
        w = wave.open(str(path))
    except (wave.Error, EOFError):
        return None
    with w:
        width = w.getsampwidth()
        if width not in (2, 3, 4):
            return None
        full_scale = 2 ** (8 * width - 1)
        frame_bytes = width * w.getnchannels()
        window_frames = int(w.getframerate() * WINDOW_SECONDS)
        silence = full_scale * 10 ** (SILENCE_DB / 20)

        active_sum, active_windows, peak = 0.0, 0, 0
        while raw := w.readframes(window_frames):
            samples = [
                int.from_bytes(raw[i : i + width], "little", signed=True)
                for i in range(0, len(raw) - width + 1, frame_bytes * SAMPLE_STEP)
            ]
            if not samples:
                break
            peak = max(peak, max(map(abs, samples)))
            mean_square = sum(s * s for s in samples) / len(samples)
            if math.sqrt(mean_square) > silence:
                active_sum += mean_square
                active_windows += 1

    if not active_windows:
        return None
    rms = math.sqrt(active_sum / active_windows)
    return StemLevel(20 * math.log10(rms / full_scale), 20 * math.log10(peak / full_scale))
