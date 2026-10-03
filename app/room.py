"""What Holy Sound remembers about a church's room, gear and team.

The second Sunday should take one sentence (docs/idea.md): which interface,
who sings on which input, which outputs feed the in-ears. The assistant saves
facts like these with its remember tool; the volunteer sees them in the Room
tab and can add or delete any of them.

Stored as plain JSON in ~/.holysound/room.json (or $HOLYSOUND_HOME), so a
person can read or fix it by hand.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

MAX_FACTS = 60
MAX_LENGTH = 200


def default_path():
    home = os.environ.get("HOLYSOUND_HOME")
    return (Path(home) if home else Path.home() / ".holysound") / "room.json"


class RoomMemory:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._facts = []
        self._next_id = 1
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self._facts = [f for f in data.get("facts", []) if isinstance(f, dict) and f.get("text")]
        self._next_id = max((f["id"] for f in self._facts), default=0) + 1

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"facts": self._facts}, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def facts(self):
        with self._lock:
            return [dict(f) for f in self._facts]

    def add(self, texts):
        """Save new facts (skipping exact repeats). Returns the ones saved."""
        added = []
        with self._lock:
            known = {f["text"].casefold() for f in self._facts}
            for text in texts:
                text = " ".join(str(text).split())[:MAX_LENGTH]
                if not text or text.casefold() in known:
                    continue
                if len(self._facts) >= MAX_FACTS:
                    break
                fact = {"id": self._next_id, "text": text, "added": date.today().isoformat()}
                self._next_id += 1
                self._facts.append(fact)
                known.add(text.casefold())
                added.append(dict(fact))
            if added:
                self._save()
        return added

    def remove(self, ids):
        """Forget facts by id. Returns the ones removed."""
        ids = set(ids)
        with self._lock:
            removed = [dict(f) for f in self._facts if f["id"] in ids]
            if removed:
                self._facts = [f for f in self._facts if f["id"] not in ids]
                self._save()
        return removed

    def notes(self):
        """The facts as the assistant sees them in each message."""
        facts = self.facts()
        if not facts:
            return "What you remember about this church: nothing yet."
        lines = ["What you remember about this church (from earlier weeks; trust it unless told otherwise):"]
        lines += [f"  #{f['id']}. {f['text']}" for f in facts]
        return "\n".join(lines)
