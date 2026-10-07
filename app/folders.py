"""Which folder (instrument family) each track belongs in, for the mixer.

The mixer groups tracks by what they are -- Vocals, Instruments, Click &
playback -- not by colour. A track's folder comes from its name, unless
the volunteer has moved it by hand, which is remembered by track name in
~/.holysound/folders.json (or $HOLYSOUND_HOME) so next week's set sorts the
same way.

Live has no way to move tracks or make group tracks from a Control Surface, so
moving a track also gives it the folder's colour in Live (the page does that
with set_track_color). Colour is the one folder signal Live can show.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

# (key, label, colour name from rig.TRACK_COLORS). The order is the order the
# folders appear in the mixer.
FAMILIES = [
    ("vocals", "Vocals", "red"),
    ("instruments", "Instruments", "blue"),
    ("playback", "Click & playback", "teal"),
    ("other", "Other", "grey"),
]
KEYS = [key for key, _label, _colour in FAMILIES]

# Checked in this order, so "Backing Track" lands in playback, not vocals.
_RULES = [
    ("playback", r"click|guide|\bcue|loops?\b|playback|\bstems?\b|smpte|timecode|backing ?tracks?|\btracks?\b|sequence|metronome"),
    ("vocals", r"vox|vocal|\bvocs?\b|\bbgvs?\b|\bbv\d*\b|choir|singer|harmon|lead v|\bmic\b|speech|pastor|preacher|soprano|alto\b|tenor|announce"),
    # Drums, bass, guitars and keys all go in the one Instruments folder.
    ("instruments", "|".join([
        r"drum|kick|snare|\btoms?\b|hi-?hats?|\bhats?\b|cymbal|overhead|\boh\b|perc|cajon|shaker|tambourine|conga|bongo|\bkit\b",
        r"bass",
        r"guitar|gtr|acoustic|\bag\b|\beg\b|electric|banjo|\buke|mandolin|dobro",
        r"key|piano|organ|synth|\bpads?\b|rhodes|wurli|string|nord|\bkb\b|moog",
    ])),
]
_COMPILED = [(key, re.compile(pattern, re.IGNORECASE)) for key, pattern in _RULES]

# Tracks that keep their key when a song is transposed, unless the volunteer says
# otherwise: a click or spoken cue shouldn't change pitch, and SMPTE breaks if it does.
_KEEPS_KEY = re.compile(r"click|guide|\bcue|count|smpte|timecode|ltc|metronome", re.IGNORECASE)


def classify(name):
    """The instrument family a track name suggests ("other" if it suggests none)."""
    for key, pattern in _COMPILED:
        if pattern.search(name or ""):
            return key
    return "other"


def default_path():
    home = os.environ.get("HOLYSOUND_HOME")
    return (Path(home) if home else Path.home() / ".holysound") / "folders.json"


class FolderMemory:
    """Folders the volunteer picked by hand, by track name."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._moved = {}  # track name (case-folded) -> family key
        self._follows = {}  # track name (case-folded) -> follows the song key, where chosen by hand
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        moved = data.get("moved", {}) if isinstance(data, dict) else {}
        self._moved = {str(k): v for k, v in moved.items() if v in KEYS}
        follows = data.get("follows_key", {}) if isinstance(data, dict) else {}
        self._follows = {str(k): v for k, v in follows.items() if isinstance(v, bool)}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"moved": self._moved, "follows_key": self._follows}
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def folder_for(self, name):
        with self._lock:
            return self._moved.get((name or "").casefold()) or classify(name)

    def move(self, name, folder):
        """Put a track in a folder. A folder of None goes back to sorting by name."""
        if folder is not None and folder not in KEYS:
            raise ValueError(f"There's no folder called {folder}.")
        with self._lock:
            key = (name or "").casefold()
            if folder is None:
                self._moved.pop(key, None)
            else:
                self._moved[key] = folder
            self._save()

    def keeps_key(self, name):
        """True if transposing a song leaves this track alone."""
        with self._lock:
            chosen = self._follows.get((name or "").casefold())
        return not chosen if chosen is not None else bool(_KEEPS_KEY.search(name or ""))

    def set_follows_key(self, name, follows):
        with self._lock:
            self._follows[(name or "").casefold()] = bool(follows)
            self._save()

    def kept_tracks(self, tracks):
        """Indexes of the tracks (rows with "index" and "name") a transpose leaves alone."""
        return [t["index"] for t in tracks if self.keeps_key(t["name"])]

    def rename(self, old, new):
        """A renamed track keeps the folder it was moved to, and its key choice."""
        with self._lock:
            old, new = (old or "").casefold(), (new or "").casefold()
            folder = self._moved.pop(old, None)
            if folder:
                self._moved[new] = folder
            follows = self._follows.pop(old, None)
            if follows is not None:
                self._follows[new] = follows
            if folder or follows is not None:
                self._save()
