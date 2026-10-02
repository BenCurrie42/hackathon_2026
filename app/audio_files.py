"""Audio files on this computer: browsing for a folder, and measuring what's in it.

The assistant can't hear, so before it decides levels it gets numbers: each
file's peak, how loud it is while it's actually sounding, and how much of the
time it sounds at all. Stems are mostly silence (a click that stops, a pad
that only plays in the bridge), so an average over the whole file would make
every quiet-ish stem look quieter than it is.

Standard library only. WAV and AIFF are measured; Live can import other
formats (MP3, FLAC, ...) and they're listed, just not measured.
"""

from __future__ import annotations

import array
import math
import os
import struct
import sys
from operator import mul
from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".wave", ".aif", ".aiff", ".aifc", ".mp3", ".m4a", ".flac", ".ogg"}
MEASURABLE = {".wav", ".wave", ".aif", ".aiff", ".aifc"}

MAX_FILES = 200
MAX_DEPTH = 3
WINDOW_SECONDS = 0.4
SAMPLES_PER_WINDOW = 2048  # RMS is estimated from this many samples per window
SILENT_DB = -60.0

_cache = {}


class AudioFileError(Exception):
    """A sentence about why a file or folder can't be used."""


# -- where we may look ----------------------------------------------------------


def roots():
    """Places the folder browser may show. Home, plus drives on a Mac."""
    found = [("Home", Path.home())]
    for name in ("Music", "Desktop", "Downloads", "Documents"):
        if (Path.home() / name).is_dir():
            found.append((name, Path.home() / name))
    volumes = Path("/Volumes")
    if volumes.is_dir():
        for drive in sorted(volumes.iterdir()):
            if drive.is_dir() and not drive.name.startswith("."):
                found.append((drive.name, drive))
    return found


def allowed(path):
    """Only the home folder and attached drives -- never the system."""
    path = Path(path).resolve()
    return any(path.is_relative_to(root.resolve()) for root in (Path.home(), Path("/Volumes")))


def _checked_folder(path):
    folder = Path(path).expanduser().resolve()
    if not allowed(folder):
        raise AudioFileError("Holy Sound can only look in your home folder and attached drives.")
    if not folder.is_dir():
        raise AudioFileError(f"There's no folder at {folder}.")
    return folder


def _is_audio(entry):
    return entry.is_file() and Path(entry.name).suffix.lower() in AUDIO_EXTENSIONS and not entry.name.startswith(".")


def browse(path=None):
    """One level of a folder: its subfolders and the audio files in it."""
    if path:
        folder = _checked_folder(path)
    else:
        music = Path.home() / "Music"
        folder = music if music.is_dir() else Path.home()

    folders, audio = [], []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.casefold())
    except PermissionError as e:
        raise AudioFileError(f"This computer won't let Holy Sound look inside {folder.name}.") from e
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            try:
                count = sum(1 for e in os.scandir(entry.path) if _is_audio(e))
            except OSError:
                count = 0
            folders.append({"name": entry.name, "path": entry.path, "audio": count})
        elif _is_audio(entry):
            audio.append(entry.name)

    parent = folder.parent if folder.parent != folder and allowed(folder.parent) else None
    home = Path.home().resolve()
    shown = "~/" + folder.relative_to(home).as_posix() if folder.is_relative_to(home) else str(folder)
    return {
        "path": str(folder),
        "display": shown.rstrip("/") if shown != "~/" else "~",
        "name": folder.name or str(folder),
        "parent": str(parent) if parent else None,
        "roots": [{"name": name, "path": str(p)} for name, p in roots()],
        "folders": folders,
        "audio": audio,
    }


def scan(path):
    """Every audio file under a folder (a few levels down), as (id, absolute path).

    The id is the folder's name plus the path inside it, e.g.
    "Sunday Stems/Way Maker/Click.wav" -- what the assistant sees and uses.
    """
    folder = _checked_folder(path)
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        depth = len(Path(dirpath).relative_to(folder).parts)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and depth < MAX_DEPTH)
        for name in sorted(filenames, key=str.casefold):
            if name.startswith(".") or Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            full = Path(dirpath) / name
            found.append((f"{folder.name}/{full.relative_to(folder).as_posix()}", full))
            if len(found) >= MAX_FILES:
                return folder, found
    return folder, found


# -- measuring ------------------------------------------------------------------


def measure(path):
    """Duration, format and loudness of one file. Cached by size and mtime."""
    path = Path(path)
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime)
    if key not in _cache:
        _cache[key] = _measure(path)
    return _cache[key]


def measure_many(paths):
    """Measure several files, in parallel processes when there are enough to matter."""
    paths = [Path(p) for p in paths]
    todo = [p for p in paths if (str(p), p.stat().st_size, p.stat().st_mtime) not in _cache]
    if len(todo) >= 4:
        from concurrent.futures import ProcessPoolExecutor

        workers = min(8, os.cpu_count() or 2, len(todo))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for path, result in zip(todo, pool.map(_measure, todo)):
                stat = path.stat()
                _cache[(str(path), stat.st_size, stat.st_mtime)] = result
    return [measure(p) for p in paths]


def _measure(path):
    if path.suffix.lower() not in MEASURABLE:
        return {"format": path.suffix.lstrip(".").upper(), "measured": False}
    try:
        with open(path, "rb") as f:
            info = _wav_header(f) if path.suffix.lower() in (".wav", ".wave") else _aiff_header(f)
            return _loudness(f, info)
    except (AudioFileError, struct.error, ValueError, OSError) as e:
        return {"format": path.suffix.lstrip(".").upper(), "measured": False, "problem": str(e) or "unreadable"}


def _wav_header(f):
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise AudioFileError("not a WAV file")
    fmt = None
    while True:
        chunk = f.read(8)
        if len(chunk) < 8:
            raise AudioFileError("no audio data in the file")
        cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
        if cid == b"fmt ":
            body = f.read(size + (size & 1))
            tag, channels, rate, _rate_bytes, _align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE
                tag = struct.unpack("<H", body[24:26])[0]
            kind = {1: "int", 3: "float"}.get(tag)
            if kind is None:
                raise AudioFileError("compressed WAV")
            fmt = {"container": "WAV", "kind": kind, "channels": channels, "rate": rate,
                   "bits": bits, "big_endian": False}
        elif cid == b"data":
            if fmt is None:
                raise AudioFileError("audio data before its format")
            fmt["offset"] = f.tell()
            fmt["length"] = size
            return fmt
        else:
            f.seek(size + (size & 1), 1)


def _extended(b):
    """An 80-bit IEEE extended float, as AIFF stores its sample rate."""
    exponent = ((b[0] & 0x7F) << 8) | b[1]
    mantissa = int.from_bytes(b[2:10], "big")
    if exponent == 0 and mantissa == 0:
        return 0.0
    return mantissa * 2.0 ** (exponent - 16383 - 63)


def _aiff_header(f):
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"FORM" or head[8:12] not in (b"AIFF", b"AIFC"):
        raise AudioFileError("not an AIFF file")
    fmt = {"container": "AIFF", "kind": "int", "big_endian": True}
    while True:
        chunk = f.read(8)
        if len(chunk) < 8:
            raise AudioFileError("no audio data in the file")
        cid, size = chunk[:4], struct.unpack(">I", chunk[4:])[0]
        if cid == b"COMM":
            body = f.read(size + (size & 1))
            fmt["channels"], _frames, fmt["bits"] = struct.unpack(">hIh", body[:8])
            fmt["rate"] = _extended(body[8:18])
            compression = body[18:22] if head[8:12] == b"AIFC" and len(body) >= 22 else b"NONE"
            if compression == b"sowt":
                fmt["big_endian"] = False
            elif compression in (b"fl32", b"FL32"):
                fmt["kind"], fmt["bits"] = "float", 32
            elif compression != b"NONE":
                raise AudioFileError("compressed AIFF")
        elif cid == b"SSND":
            if "rate" not in fmt:
                raise AudioFileError("audio data before its format")
            data_offset, _block = struct.unpack(">II", f.read(8))
            f.seek(data_offset, 1)
            fmt["offset"] = f.tell()
            fmt["length"] = size - 8 - data_offset
            return fmt
        else:
            f.seek(size + (size & 1), 1)


def _decoder(info):
    """A function from raw bytes to an array of samples, and the full-scale value."""
    bits, kind, big = info["bits"], info["kind"], info["big_endian"]
    swap = big != (sys.byteorder == "big")

    def typed(code):
        def decode(raw):
            values = array.array(code)
            values.frombytes(raw[: len(raw) - len(raw) % values.itemsize])
            if swap:
                values.byteswap()
            return values
        return decode

    if kind == "float" and bits == 32:
        return typed("f"), 1.0
    if kind == "float" and bits == 64:
        return typed("d"), 1.0
    if kind == "int" and bits == 16:
        return typed("h"), 32768.0
    if kind == "int" and bits == 32:
        return typed("i"), 2.0 ** 31
    if kind == "int" and bits == 24:
        # Widen each 3-byte sample to 4 bytes with slicing (fast, no Python loop):
        # the sample lands in the top 24 bits of a native int, sign intact.
        order = (2, 1, 0) if big else (0, 1, 2)

        def decode24(raw):
            n = len(raw) // 3
            wide = bytearray(n * 4)
            lanes = (1, 2, 3) if sys.byteorder == "little" else (2, 1, 0)
            for lane, src in zip(lanes, order):
                wide[lane::4] = raw[src: n * 3: 3]
            values = array.array("i")
            values.frombytes(bytes(wide))
            return values
        return decode24, 2.0 ** 31
    raise AudioFileError(f"{bits}-bit {kind} audio")


def _db(linear):
    return 20 * math.log10(linear) if linear > 0 else float("-inf")


def _loudness(f, info):
    channels, rate = info["channels"], info["rate"]
    if not channels or not rate:
        raise AudioFileError("the file doesn't say its format")
    decode, full_scale = _decoder(info)
    frame_bytes = channels * info["bits"] // 8
    available = os.fstat(f.fileno()).st_size - info["offset"]
    length = min(info["length"], available) if info["length"] else available
    frames = length // frame_bytes
    window_frames = max(1, int(rate * WINDOW_SECONDS))

    peak = 0.0
    window_db = []
    f.seek(info["offset"])
    remaining = frames
    while remaining > 0:
        take = min(window_frames, remaining)
        raw = f.read(take * frame_bytes)
        if not raw:
            break
        remaining -= take
        values = decode(raw)
        if not values:
            continue
        loudest = max(max(values), -min(values))
        peak = max(peak, loudest)
        step = max(1, len(values) // SAMPLES_PER_WINDOW)
        sample = values[::step]
        rms = math.sqrt(sum(map(mul, sample, sample)) / len(sample)) / full_scale
        window_db.append(_db(rms))

    peak_db = _db(peak / full_scale)
    sounding = sorted(d for d in window_db if d > SILENT_DB)
    result = {
        "format": f"{info['container']} {info['bits']}-bit{' float' if info['kind'] == 'float' else ''}",
        "measured": True,
        "seconds": round(frames / rate, 1),
        "channels": channels,
        "sample_rate": int(round(rate)),
        "silent": not sounding,
        "peak_dbfs": round(peak_db, 1) if peak_db != float("-inf") else None,
    }
    if sounding:
        # The level of its loudest stretches: the 90th percentile of 0.4 s windows.
        result["loud_dbfs"] = round(sounding[min(len(sounding) - 1, int(len(sounding) * 0.9))], 1)
        result["sounding_pct"] = round(100 * len(sounding) / max(1, len(window_db)))
    return result


# -- for the assistant ------------------------------------------------------------


def _duration(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def describe(file_id, m):
    """One line about a file, for the assistant."""
    if not m.get("measured"):
        why = f" ({m['problem']})" if m.get("problem") else ""
        return f'- "{file_id}" — {m["format"]}; not measured{why}'
    shape = {1: "mono", 2: "stereo"}.get(m["channels"], f"{m['channels']} channels")
    parts = [f"{_duration(m['seconds'])}", shape, f"{m['sample_rate'] / 1000:g} kHz {m['format']}"]
    if m["silent"]:
        parts.append("SILENT (nothing above -60 dBFS)")
    else:
        parts.append(f"peak {m['peak_dbfs']} dBFS")
        parts.append(f"loud parts {m['loud_dbfs']} dBFS")
        parts.append(f"sounding {m['sounding_pct']}% of the time")
        if m["peak_dbfs"] is not None and m["peak_dbfs"] >= -0.1:
            parts.append("CLIPS")
    return f'- "{file_id}" — ' + ", ".join(parts)
