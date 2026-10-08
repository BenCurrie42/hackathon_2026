"""Install RigLink so Holy Sound can talk to Ableton Live.

    uv run python scripts/install_riglink.py

Walks the volunteer through it in plain sentences:

  1. Links ableton_script/RigLink into Live's Remote Scripts folder.
  2. With Live closed, picks RigLink in the first free Control Surface slot by
     editing Live's Preferences.cfg, so nobody has to open Settings.
  3. Opens Live and waits until RigLink answers on localhost:9877.

Preferences.cfg is binary. Each Control Surface slot is three length-prefixed
UTF-16 strings (surface, input, output), and Live 12 has seven slots, so the
21 strings sit in one run near the end of the file. Diffing a never-picked
12.4.5 file against Live's own 12.4.6 save after picking RigLink showed the only
change is the first string: 4 x "None" -> 7 x "RigLink", same framing, and no
size or checksum header elsewhere. Live rewrites the file when it quits, so it
must be closed while we edit. Anything not in that verified shape falls back to
the click-through steps. A backup goes next to the file first.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SOURCE = ROOT / "ableton_script" / "RigLink"
REMOTE_SCRIPTS = Path.home() / "Music" / "Ableton" / "User Library" / "Remote Scripts"
PREFERENCES = Path.home() / "Library" / "Preferences" / "Ableton"
SURFACE_NAME = "RigLink"
PORT = 9877
SLOT_COUNT = 7  # Control Surface slots in Live 12's Link, Tempo & MIDI settings
TAIL_SLACK = 16  # bytes Live writes after the slots (4 in 12.4.x, 6 in 12.2.7)
CLICK_THROUGH = (
    "In Ableton, open Settings (Live menu > Settings), go to Link, Tempo & MIDI, "
    "and under Control Surface pick RigLink in any empty row. Then quit and reopen Ableton."
)


class PreferencesShapeError(Exception):
    """Preferences.cfg doesn't look like the layout we verified."""


def say(text: str = "") -> None:
    print(text, flush=True)


def ask_yes(question: str) -> bool:
    answer = input(f"{question} [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def wait_for_enter(prompt: str) -> None:
    input(f"{prompt} Press Return when that's done. ")


# --- Preferences.cfg ----------------------------------------------------------


def _encoded(text: str) -> bytes:
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def _read_string(data: bytes, at: int) -> tuple[str, int]:
    """One length-prefixed UTF-16 string at `at`; returns it and where the next starts."""
    if at + 4 > len(data):
        raise PreferencesShapeError("ran off the end of the file")
    (length,) = struct.unpack_from("<I", data, at)
    end = at + 4 + 2 * length
    if length > 256 or end > len(data):
        raise PreferencesShapeError("string length out of range")
    return data[at + 4 : end].decode("utf-16-le"), end


def _surface_slots(data: bytes) -> tuple[int, list[tuple[int, str]]]:
    """Where the slot run starts, and each slot's surface name with its offset.

    The run is the last thing in the file: 12.4.5 and 12.4.6 follow it with 4
    zero bytes, 12.2.7 with 6. So look for exactly one start near the end where
    21 non-empty, printable strings parse back to back and stop just short of EOF.
    Starting inside a string reads UTF-16 text as a length, which is huge and
    rejected, so misaligned starts don't parse.
    """
    strings = SLOT_COUNT * 3
    found = []
    for start in range(max(0, len(data) - strings * (4 + 2 * 256) - TAIL_SLACK), len(data)):
        try:
            at, slots = start, []
            for index in range(strings):
                text, next_at = _read_string(data, at)
                if not text or not text.isprintable():
                    raise PreferencesShapeError("not a slot string")
                if index % 3 == 0:
                    slots.append((at, text))
                at = next_at
        except (PreferencesShapeError, UnicodeDecodeError):
            continue
        if len(data) - at <= TAIL_SLACK:
            found.append((start, slots))
    if len(found) != 1:
        raise PreferencesShapeError("couldn't find the Control Surface slots")
    return found[0]


def with_surface_picked(data: bytes, name: str = SURFACE_NAME) -> bytes | None:
    """Preferences bytes with `name` in the first empty slot; None if it's already picked."""
    _, slots = _surface_slots(data)
    if any(text == name for _, text in slots):
        return None
    for at, text in slots:
        if text == "None":
            return data[:at] + _encoded(name) + data[at + len(_encoded("None")) :]
    raise PreferencesShapeError("every Control Surface slot is already in use")


# --- Live itself --------------------------------------------------------------


def installed_lives() -> list[tuple[Path, str]]:
    """Each Ableton Live 12 app and its version, newest first."""
    found = []
    for app in Path("/Applications").glob("Ableton Live 12*.app"):
        try:
            with open(app / "Contents" / "Info.plist", "rb") as f:
                version = plistlib.load(f)["CFBundleShortVersionString"].split()[0]
        except (OSError, KeyError, plistlib.InvalidFileException):
            continue
        found.append((app, version))
    return sorted(found, key=lambda pair: [int(n) for n in re.findall(r"\d+", pair[1])], reverse=True)


def is_live_running() -> bool:
    result = subprocess.run(["pgrep", "-f", "Ableton Live 12"], capture_output=True)
    return result.returncode == 0


def is_riglink_answering() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


# --- Steps --------------------------------------------------------------------


def link_script() -> bool:
    target = REMOTE_SCRIPTS / SURFACE_NAME
    if target.is_symlink() and target.resolve() == SCRIPT_SOURCE.resolve():
        say("RigLink is already in Ableton's Remote Scripts folder.")
        return True
    if target.exists() or target.is_symlink():
        say(f"There's already something called RigLink in {REMOTE_SCRIPTS}.")
        if not ask_yes("Replace it with this copy of RigLink?"):
            say("Leaving it alone. Ableton will keep using the old one.")
            return False
        backup = target.with_name(f"{SURFACE_NAME}.old")
        if backup.exists() or backup.is_symlink():
            say(f"Move or delete {backup} first, then run this again.")
            return False
        target.rename(backup)
        say(f"Moved the old one to {backup}.")
    REMOTE_SCRIPTS.mkdir(parents=True, exist_ok=True)
    target.symlink_to(SCRIPT_SOURCE)
    say("Put RigLink in Ableton's Remote Scripts folder.")
    return True


def pick_control_surface(version: str) -> bool:
    """Choose RigLink in Live's settings without opening them. False means do it by hand."""
    prefs = PREFERENCES / f"Live {version}" / "Preferences.cfg"
    if not prefs.exists():
        say("Ableton hasn't been opened on this Mac yet, so it has no settings to change.")
        say("Open Ableton once, quit it, then run this again.")
        return False
    while is_live_running():
        wait_for_enter("Ableton is open. Please save your set and quit Ableton.")
    data = prefs.read_bytes()
    try:
        updated = with_surface_picked(data)
    except PreferencesShapeError as e:
        say(f"Couldn't change Ableton's settings automatically ({e}).")
        return False
    if updated is None:
        say("RigLink is already picked as a Control Surface.")
        return True
    backup = prefs.with_name("Preferences.cfg.before-riglink")
    if not backup.exists():
        shutil.copy2(prefs, backup)
    tmp = prefs.with_name("Preferences.cfg.tmp")
    tmp.write_bytes(updated)
    os.replace(tmp, prefs)
    say(f"Picked RigLink as a Control Surface. (Old settings saved as {backup.name}.)")
    return True


def open_and_check(app: Path) -> bool:
    say(f"Opening {app.stem}. This can take a minute.")
    subprocess.run(["open", "-a", str(app)], check=False)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if is_riglink_answering():
            return True
        time.sleep(2)
    return False


def main() -> int:
    say("Setting up RigLink, the piece that lets Holy Sound talk to Ableton.\n")
    if sys.platform != "darwin":
        say("This setup only works on a Mac.")
        return 1
    lives = installed_lives()
    if not lives:
        say("Couldn't find Ableton Live 12 in your Applications folder. Install it, then run this again.")
        return 1
    app, version = lives[0]
    say(f"Found {app.stem} ({version}).")

    if not link_script():
        return 1
    picked = pick_control_surface(version)
    if not picked:
        say("\nOne step to do by hand:")
        say(CLICK_THROUGH)
        wait_for_enter("\nAfter that,")
        if is_riglink_answering():
            say("\nAll set. Holy Sound can reach Ableton now.")
            return 0
    elif is_live_running():
        say("Ableton is already open again. Quit and reopen it so it picks up RigLink.")
        return 0

    if is_riglink_answering() or open_and_check(app):
        say("\nAll set. Holy Sound can reach Ableton now.")
        return 0
    say("\nAbleton opened, but RigLink didn't answer.")
    say(CLICK_THROUGH)
    say(f"If RigLink is already picked there, Ableton's log may say why: {PREFERENCES / f'Live {version}' / 'Log.txt'}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nStopped. Nothing else was changed.")
        sys.exit(130)
