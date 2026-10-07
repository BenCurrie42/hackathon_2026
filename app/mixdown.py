"""Mixing several stems into one file, so a part fits on one track.

Live allows one clip per track per song. When a part has several stems (Kick In,
Kick Out, Snare Top, OH L, OH R... all on Drums), they're summed here into one
WAV for that song, standard library only. The originals are never touched.

Stems are summed at the gain they arrive with, so the vendor's balance inside the
part is kept. The sum is then scaled to peak at -1 dBFS and written as 24-bit PCM;
the dB it was scaled by comes back so the caller can put it back as clip gain
and the part plays exactly as loud as its stems did together.
"""

from __future__ import annotations

import array
import math
import wave
from operator import add, mul
from pathlib import Path

from app import audio_files

PEAK_DBFS = -1.0
CHUNK_SECONDS = 2.0


class MixdownError(Exception):
    """The stems couldn't be mixed. The message is a sentence for the volunteer."""


def mix(sources, out_path):
    """Sum stems into one WAV.

    sources: [(path, gain_db, side)] with side -1 (left only), 0 (both) or 1 (right only).
    Returns {"path", "channels", "seconds", "gain_db"}: gain_db is how much the
    sum was turned down (negative) or up to peak at PEAK_DBFS; add its opposite
    as clip gain to play it at the stems' own level.
    """
    if not sources:
        raise MixdownError("There were no stems to mix.")
    opened = []
    try:
        for path, gain_db, side in sources:
            o = audio_files._opened(Path(path))
            if o is None:
                raise MixdownError(f"Couldn't read {Path(path).name} to mix it.")
            opened.append((o[0], o[1], 10 ** ((gain_db or 0) / 20), side))
        rates = {info["rate"] for _f, info, _g, _s in opened}
        if len(rates) > 1:
            raise MixdownError("Those stems have different sample rates, so they can't be mixed into one.")
        rate = rates.pop()
        stereo = any(info["channels"] > 1 or side for _f, info, _g, side in opened)
        out_channels = 2 if stereo else 1
        frames = max(audio_files._frames(f, info) for f, info, _g, _s in opened)
        total = array.array("f", bytes(4 * frames * out_channels))
        for f, info, gain, side in opened:
            _add_into(total, out_channels, f, info, gain, side)
    finally:
        for f, *_rest in opened:
            f.close()

    peak = max(max(total, default=0.0), -min(total, default=0.0))
    if peak <= 0:
        scale_db = 0.0
    else:
        scale_db = PEAK_DBFS - 20 * math.log10(peak)
    _write_24bit(out_path, total, out_channels, rate, 10 ** (scale_db / 20))
    return {"path": Path(out_path), "channels": out_channels, "seconds": frames / rate, "gain_db": scale_db}


def _add_into(total, out_channels, f, info, gain, side):
    decode, full_scale = audio_files._decoder(info)
    channels = info["channels"]
    frame_bytes = channels * info["bits"] // 8
    chunk = max(1, int(info["rate"] * CHUNK_SECONDS))
    f.seek(info["offset"])
    remaining = audio_files._frames(f, info)
    position = 0  # frame index into the output
    scale = gain / full_scale
    while remaining > 0:
        take = min(chunk, remaining)
        values = decode(f.read(take * frame_bytes))
        if not values:
            break
        got = len(values) // channels
        remaining -= take
        lanes = _lanes(values, channels, out_channels, side)
        for out_lane, samples in lanes:
            start = position * out_channels + out_lane
            stop = start + got * out_channels
            current = total[start:stop:out_channels]
            total[start:stop:out_channels] = array.array("f", map(add, current, map(mul, samples, [scale] * got)))
        position += got


def _lanes(values, channels, out_channels, side):
    """(output channel, samples) pairs for one decoded chunk."""
    if out_channels == 1:
        return [(0, values[0::channels] if channels > 1 else values)]
    if channels >= 2:
        return [(0, values[0::channels]), (1, values[1::channels])]
    if side < 0:
        return [(0, values)]
    if side > 0:
        return [(1, values)]
    return [(0, values), (1, values)]


def _write_24bit(path, total, channels, rate, scale):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    top = 2 ** 23 - 1
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(3)
        w.setframerate(rate)
        step = int(rate * CHUNK_SECONDS) * channels
        for start in range(0, len(total), step):
            block = total[start:start + step]
            ints = array.array("i", (max(-top, min(top, int(x * scale * top))) for x in block))
            if ints.itemsize != 4:
                raise MixdownError("This computer's Python can't write 24-bit audio.")
            raw = ints.tobytes()
            if array.array("i", [1]).tobytes()[0] != 1:  # big-endian machine: swap to WAV's little-endian
                ints.byteswap()
                raw = ints.tobytes()
            packed = bytearray(len(ints) * 3)
            packed[0::3], packed[1::3], packed[2::3] = raw[0::4], raw[1::4], raw[2::4]
            w.writeframes(bytes(packed))
    tmp.replace(path)
