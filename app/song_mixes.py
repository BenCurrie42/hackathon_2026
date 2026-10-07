"""Each song's own mixer settings, put back when the song is picked.

A song's clips already carry its part levels and on/off (clip gain, the clip
activator), and Live applies those itself. Faders, pan, mute, sends and EQ belong to
the track, so they're shared by every song. This keeps them per song: the app
stores every track's and shared effect's mixer state under the song picked in the
mixer, saves changes as they happen (from the volunteer, the assistant or Live
itself), and sets them back when that song is picked again or starts playing.

Checkpoints are named copies of a song's mix to go back to. Restoring one first
checkpoints the mix it replaces, so a restore can be undone.

Kept by song name and track name in ~/.holysound/song_mixes.json (or
$HOLYSOUND_HOME), so moving songs or adding tracks doesn't lose anything. Solo
isn't kept: it's for listening, not part of a mix. Neither is the Master fader.

EQ is a track's first EQ Eight, band by band (app/eq.py). Adding or removing the
device isn't per song; a song saved before a track had one leaves it as it is.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from app import eq

MAX_CHECKPOINTS = 20
DB_STEP = 0.05  # Live reads faders back to about 0.01 dB; anything smaller isn't a change
PAN_STEP = 0.01
SILENT_DB = -70.0  # a fader all the way down reads as None (-inf)


def default_path():
    home = os.environ.get("HOLYSOUND_HOME")
    return (Path(home) if home else Path.home() / ".holysound") / "song_mixes.json"


def mix_of(snapshot):
    """The mixer state of every track and shared effect in a snapshot, by name."""
    def strip(row):
        state = {"volume_db": row.get("volume_db"), "mute": bool(row.get("mute"))}
        if row.get("pan_value") is not None:
            state["pan"] = row["pan_value"]
        state["sends"] = {s["return"]: s.get("level_db") for s in row.get("sends", [])}
        if row.get("eq"):
            state["eq"] = eq.bands_of(row["eq"])
        return state

    return {
        "tracks": {t["name"].casefold(): strip(t) for t in snapshot["tracks"]},
        "returns": {r["name"].casefold(): strip(r) for r in snapshot["returns"]},
    }


def commands(saved, snapshot):
    """The RigLink calls that turn the snapshot's mixer into the saved mix.

    Only what differs. Tracks the saved mix doesn't know (added since) are left alone.
    """
    calls = []
    for kind, is_return in (("tracks", False), ("returns", True)):
        known = saved.get(kind, {})
        for row in snapshot[kind]:
            want = known.get(row["name"].casefold())
            if want is None:
                continue
            where = {"track_index": row["index"], "is_return": is_return}
            if _db_differs(want.get("volume_db"), row.get("volume_db")):
                calls.append(("set_volume", dict(where, db=_db(want.get("volume_db")))))
            if "pan" in want and row.get("pan_value") is not None and abs(want["pan"] - row["pan_value"]) > PAN_STEP:
                calls.append(("set_pan", dict(where, pan=want["pan"])))
            if "mute" in want and bool(want["mute"]) != bool(row.get("mute")):
                calls.append(("set_mute", dict(where, on=bool(want["mute"]))))
            sends = want.get("sends", {})
            for i, send in enumerate(row.get("sends", [])):
                if send["return"] in sends and _db_differs(sends[send["return"]], send.get("level_db")):
                    calls.append(("set_send", dict(where, return_index=i, db=_db(sends[send["return"]]))))
            if want.get("eq") and row.get("eq"):
                calls.extend(eq.commands(where, row["eq"], want["eq"]))
    return calls


def same(a, b):
    """True if two mixes (from mix_of) sound the same: no fader, pan, mute or send apart."""
    for kind in ("tracks", "returns"):
        if set(a.get(kind, {})) != set(b.get(kind, {})):
            return False
        for name, x in a[kind].items():
            y = b[kind][name]
            if _db_differs(x.get("volume_db"), y.get("volume_db")) or bool(x.get("mute")) != bool(y.get("mute")):
                return False
            if ("pan" in x) != ("pan" in y) or ("pan" in x and abs(x["pan"] - y["pan"]) > PAN_STEP):
                return False
            xs, ys = x.get("sends", {}), y.get("sends", {})
            if set(xs) != set(ys) or any(_db_differs(xs[r], ys[r]) for r in xs):
                return False
            if ("eq" in x) != ("eq" in y) or ("eq" in x and not eq.same(x["eq"], y["eq"])):
                return False
    return True


def describe_song(mix, snapshot, scene_index):
    """One song's mix as the assistant reads it: every track in the song, with its level.

    mix: the song's saved mix, or mix_of(snapshot) for the song on the mixer. Tracks with no
    clip in this song but clips in others aren't in it and are left out; live inputs (tracks
    with no clips at all) are in every song.
    """
    items = []
    for row in snapshot["tracks"]:
        clips = row.get("clips", [])
        clip = next((c for c in clips if c["scene_index"] == scene_index), None)
        if clips and clip is None:
            continue
        strip = mix.get("tracks", {}).get(row["name"].casefold())
        if strip is None:
            strip = mix_of({"tracks": [row], "returns": []})["tracks"][row["name"].casefold()]
        items.append(row["name"] + " " + _strip_words(strip, clip_off=clip is not None and clip.get("active") is False))
    for row in snapshot["returns"]:
        strip = mix.get("returns", {}).get(row["name"].casefold())
        if strip is not None:
            items.append(f"{row['name']} (shared effect) " + _strip_words(strip))
    return ", ".join(items)


def _strip_words(strip, clip_off=False):
    db = strip.get("volume_db")
    words = ["off" if db is None or db <= SILENT_DB else f"{db:g} dB"]
    if clip_off:
        words.append("OFF in this song")
    if strip.get("mute"):
        words.append("MUTED")
    if strip.get("pan") and abs(strip["pan"]) > PAN_STEP:
        words.append(f"pan {strip['pan']:+.2f}")
    sends = [f"{r} {v:g} dB" for r, v in strip.get("sends", {}).items() if v is not None and v > SILENT_DB]
    if sends:
        words.append("sends " + ", ".join(sends))
    if strip.get("eq"):
        words.append("(EQ " + eq.describe(strip["eq"]) + ")")
    return " ".join(words)


def _db(value):
    return SILENT_DB if value is None else float(value)


def _db_differs(a, b):
    return abs(_db(a) - _db(b)) > DB_STEP


class SongMixMemory:
    """Per-song mixes and their checkpoints, and which song the mixer is on."""

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else default_path()
        self.clock = clock
        self._lock = threading.Lock()
        self._songs = {}  # song name (case-folded) -> {"name", "mix", "saved_at", "checkpoints"}
        self.current = None  # song name the mixer is on, or None for no song (faders shared)
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        songs = data.get("songs", {}) if isinstance(data, dict) else {}
        self._songs = {str(k): v for k, v in songs.items() if isinstance(v, dict) and isinstance(v.get("mix"), dict)}
        current = data.get("current") if isinstance(data, dict) else None
        self.current = current if isinstance(current, str) else None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"current": self.current, "songs": self._songs}
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def _entry(self, song):
        return self._songs.get((song or "").casefold())

    def saved(self, song):
        """The song's saved mix, or None."""
        with self._lock:
            entry = self._entry(song)
            return entry["mix"] if entry else None

    def pick(self, song):
        """Put the mixer on a song (None: no song). Doesn't touch Live."""
        with self._lock:
            self.current = song
            self._save()

    def record(self, song, mix):
        """Save a song's mix if it changed. Returns True if it did."""
        with self._lock:
            entry = self._entry(song)
            if entry and same(entry["mix"], mix):
                return False
            if entry is None:
                entry = self._songs[song.casefold()] = {"name": song, "checkpoints": []}
            entry.update(name=song, mix=mix, saved_at=self.clock())
            self._save()
            return True

    def edit(self, song, seed, is_return, track, volume_db=None, pan=None, mute=None, send=None, eq_bands=None):
        """Change one track in a song's saved mix. seed() gives a mix to start from if it has none.

        send: (return name, dB). eq_bands: the track's whole EQ, every band (app/eq.py).
        """
        with self._lock:
            entry = self._entry(song)
            if entry is None:
                entry = self._songs[song.casefold()] = {"name": song, "mix": seed(), "checkpoints": []}
            strips = entry["mix"].setdefault("returns" if is_return else "tracks", {})
            strip = strips.setdefault(track.casefold(), {"volume_db": 0.0, "mute": False, "sends": {}})
            if volume_db is not None:
                strip["volume_db"] = float(volume_db)
            if pan is not None:
                strip["pan"] = float(pan)
            if mute is not None:
                strip["mute"] = bool(mute)
            if send is not None:
                strip.setdefault("sends", {})[send[0]] = float(send[1])
            if eq_bands is not None:
                strip["eq"] = [dict(b) for b in eq_bands]
            entry["saved_at"] = self.clock()
            self._save()

    def songs(self):
        """Names of the songs with a saved mix."""
        with self._lock:
            return [e["name"] for e in self._songs.values()]

    def checkpoint(self, song, mix, label=None):
        """A named copy of the song's mix. Returns its id."""
        with self._lock:
            entry = self._entry(song)
            if entry is None:
                entry = self._songs[song.casefold()] = {"name": song, "mix": mix, "saved_at": self.clock(),
                                                        "checkpoints": []}
            marks = entry.setdefault("checkpoints", [])
            mark_id = secrets.token_hex(4)
            label = (label or "").strip()[:60] or f"Checkpoint {len(marks) + 1}"
            marks.append({"id": mark_id, "label": label, "at": self.clock(), "mix": mix})
            del marks[:-MAX_CHECKPOINTS]
            self._save()
            return mark_id

    def checkpoint_mix(self, song, mark_id):
        with self._lock:
            entry = self._entry(song)
            for mark in (entry or {}).get("checkpoints", []):
                if mark["id"] == mark_id:
                    return mark
            return None

    def find_checkpoint(self, song, label):
        """The newest checkpoint of a song whose name matches label (any case), or None."""
        wanted = (label or "").strip().casefold()
        marks = self.checkpoints(song)
        return (next((m for m in marks if m["label"].casefold() == wanted), None)
                or next((m for m in marks if wanted and wanted in m["label"].casefold()), None))

    def delete_checkpoint(self, song, mark_id):
        with self._lock:
            entry = self._entry(song)
            if entry:
                entry["checkpoints"] = [m for m in entry.get("checkpoints", []) if m["id"] != mark_id]
                self._save()

    def checkpoints(self, song):
        """[{"id", "label", "at"}], newest first."""
        with self._lock:
            entry = self._entry(song)
            marks = (entry or {}).get("checkpoints", [])
            return [{"id": m["id"], "label": m["label"], "at": m["at"]} for m in reversed(marks)]

    def saved_at(self, song):
        with self._lock:
            entry = self._entry(song)
            return entry.get("saved_at") if entry else None

    def rename(self, old, new):
        """A renamed song keeps its mix."""
        with self._lock:
            entry = self._songs.pop((old or "").casefold(), None)
            if entry is None:
                return
            entry["name"] = new
            self._songs[new.casefold()] = entry
            if self.current and self.current.casefold() == old.casefold():
                self.current = new
            self._save()
