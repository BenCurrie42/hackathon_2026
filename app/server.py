"""Holy Sound's web app: chat on one side, the live set on the other.

    uv run python -m app                 # this computer only
    uv run python -m app --lan           # also phones/tablets on the same Wi-Fi
    uv run python -m app --fake-live     # no Ableton? use a pretend Live Set
    uv run python -m app --list-models   # which OpenCode Go models can answer

Standard library HTTP only. The page is static files in app/static; everything
else is a small JSON API over Live (app/live.py) and the conversation
(app/assistant.py), plus /api/events, which streams the assistant's reply to
every open page as it's written.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import audio_files, parts, song_key, song_mixes, vendor_set
from app.actions import ActionFailed, Executor, Listen, run_all, to_rigspec
from app.assistant import AssistantUnavailable, Conversation, describe_proposal, session_notes
from app.assistant import _playing_scene as _playing_scene_of
from app.live import LiveLink, LiveUnavailable
from app.folders import FAMILIES, FolderMemory
from app.room import RoomMemory
from rig import TRACK_COLORS
from live_control.live_connection import RigLinkError

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
TEMPLATE = ROOT / "templates" / "test.als"
DEFAULT_PORT = 8765
KEEP_ALIVE = 15  # seconds between comments on a quiet event stream
AI_GLOW_SECONDS = 3.0  # how long a track stays lit after the assistant last changed it
MIX_SETTLE_SECONDS = 1.5  # after putting a song's mix back, don't save what Live reads mid-change

# What the mixer panel may do directly, without going through the assistant.
# Everything here is undoable in Live with Cmd+Z.
DIRECT_COMMANDS = {
    "set_volume", "set_pan", "set_mute", "set_solo", "set_send", "set_tempo",
    "play", "stop", "fire_scene", "load_device", "delete_device", "set_track_name",
    "create_scene", "set_scene", "set_routing", "create_audio_track", "create_midi_track",
    "create_return_track", "get_routing", "set_track_color", "set_clip_gain", "set_clip_active",
    "transpose_song",
}


class App:
    def __init__(self, live, conversation, key=None, folders=None, demo=False, imports_path=None,
                 parts_dir=None, mixes=None):
        self.room = conversation.room
        self.folders = folders or FolderMemory()
        self.mixes = mixes or song_mixes.SongMixMemory()
        self._mix_lock = threading.Lock()  # putting a mix back, so a poll doesn't save it half done
        self._mix_settled_at = 0.0
        self._last_playing = None  # the song Live was playing at the last look, to notice a new one
        self.parts_dir = parts_dir  # where mixed parts are written; None: ~/Music/Holy Sound/Parts
        self.demo = demo  # True when the "Live" behind this is the built-in pretend one
        self.clock = time.monotonic
        self._touched = {}  # track name (case-folded) -> (lit until, name)
        self.progress = None  # while Apply runs: {"id": proposal id, "states": [one per step]}
        self.live = live
        self.chat = conversation
        self.key = key  # None: only this computer can connect
        self._apply_lock = threading.Lock()  # a laptop and a phone may both press Apply
        # Imported file id -> absolute path; import_audio and listen_to_stems may use only these.
        self.files = conversation.files
        self.imports = {}  # imported folder name -> number of files
        # Where imports are kept between runs, so last week's folder needn't be imported again.
        self.imports_path = Path(imports_path) if imports_path else None
        self._load_imports()
        conversation.song_stems = self.song_stems

    # -- state ----------------------------------------------------------

    def touch(self, name):
        """The assistant just changed this track."""
        self._touched[name.casefold()] = (self.clock() + AI_GLOW_SECONDS, name)

    def applying(self):
        """The change being applied right now, with each step's state, or None."""
        progress = self.progress
        return {"id": progress["id"], "states": list(progress["states"])} if progress else None

    def activity(self):
        """Tracks the assistant is changing (or just changed), with how long they stay lit."""
        now = self.clock()
        lit = []
        for key, (until, name) in list(self._touched.items()):
            if until <= now:
                self._touched.pop(key, None)
            else:
                lit.append({"track": name, "ms": int((until - now) * 1000)})
        return lit

    def with_folders(self, snapshot):
        """The snapshot with each track's mixer folder (instrument family) added."""
        if not snapshot:
            return snapshot
        tracks = [dict(t, folder=self.folders.folder_for(t["name"]), keeps_key=self.folders.keeps_key(t["name"]))
                  for t in snapshot["tracks"]]
        return dict(snapshot, tracks=tracks)

    def live_state(self):
        try:
            snap = self.live.snapshot()
            self.follow_mix(snap)
            return {"connected": True, "snapshot": self.with_folders(snap), "message": None}
        except LiveUnavailable as e:
            return {"connected": False, "snapshot": None, "message": str(e)}
        except RigLinkError as e:
            return {"connected": False, "snapshot": None, "message": f"Ableton reported a problem: {e}"}

    def track_name(self, args):
        """The current name of the track a direct command points at, if it has one."""
        try:
            snap = self.live.snapshot()
            rows = snap["returns"] if args.get("is_return") else snap["tracks"]
            return rows[int(args.get("track_index"))]["name"]
        except (LiveUnavailable, RigLinkError, KeyError, IndexError, TypeError, ValueError):
            return None

    # -- song mixes -----------------------------------------------------

    def mix_state(self, snap=None):
        """Which song the mixer is on, for the page."""
        song = self.mixes.current
        scene = _scene_named(snap, song) if snap else None
        return {
            "song": song,
            "scene_index": scene["index"] if scene else None,
            "saved_at": self.mixes.saved_at(song) if song else None,
            "checkpoints": self.mixes.checkpoints(song) if song else [],
        }

    def pick_song_mix(self, scene_index):
        """Put the mixer on a song: its saved mix goes back on, or the current one becomes it.

        scene_index None takes the mixer off every song; faders are then shared again.
        """
        with self._mix_lock:
            snap = self.live.snapshot(max_age=0)
            if scene_index is None:
                self.mixes.pick(None)
                return
            scene = next((s for s in snap["scenes"] if s["index"] == int(scene_index)), None)
            if scene is None or not scene["name"]:
                raise UserError("Give that song a name first; its mix is kept by name.")
            saved = self.mixes.saved(scene["name"])
            if saved is None:
                self.mixes.record(scene["name"], song_mixes.mix_of(snap))
            else:
                self._put_back(saved, snap)
            self.mixes.pick(scene["name"])

    def _put_back(self, mix, snap):
        """Caller holds the mix lock."""
        try:
            for cmd, args in song_mixes.commands(mix, snap):
                self.live.call(cmd, **args)
        finally:
            self._mix_settled_at = self.clock() + MIX_SETTLE_SECONDS

    def checkpoint_song_mix(self, label=None, song=None):
        """Keep a named copy of a song's mix (the song on the mixer unless one is named)."""
        song = song or self._current_song()
        self.mixes.checkpoint(song, self._mix_for(song), label)

    def restore_song_mix(self, mark_id, song=None):
        """Go back to a checkpoint. The mix it replaces is checkpointed first, so this can be undone.

        For the song on the mixer the faders move now; for another song only its saved mix changes.
        """
        song = song or self._current_song()
        mark = self.mixes.checkpoint_mix(song, mark_id)
        if mark is None:
            raise UserError("That checkpoint isn't there any more.")
        with self._mix_lock:
            self.mixes.checkpoint(song, self._mix_for(song), f"Before going back to {mark['label']}")
            if self._is_current(song):
                self._put_back(mark["mix"], self.live.snapshot(max_age=0))
            self.mixes.record(song, mark["mix"])

    def _mix_for(self, song):
        """A song's mix now: the mixer for the song it's on, else the saved one (or the mixer)."""
        if not self._is_current(song):
            saved = self.mixes.saved(song)
            if saved is not None:
                return saved
        return song_mixes.mix_of(self.live.snapshot(max_age=0))

    def _is_current(self, song):
        return bool(self.mixes.current) and self.mixes.current.casefold() == song.casefold()

    def _current_song(self):
        if not self.mixes.current:
            raise UserError("Pick a song in Song mix first.")
        return self.mixes.current

    def follow_mix(self, snap):
        """On each look at Live: a song that just started gets its mix; otherwise save changes."""
        playing = _playing_scene(snap)
        started = playing is not None and playing != self._last_playing
        self._last_playing = playing
        try:
            if started:
                name = next((s["name"] for s in snap["scenes"] if s["index"] == playing), "")
                if name and name.casefold() != (self.mixes.current or "").casefold():
                    self.pick_song_mix(playing)
                    return
            if not self._mix_lock.acquire(blocking=False):
                return  # a mix is being put back right now
            try:
                song = self.mixes.current
                if song and self.clock() >= self._mix_settled_at and _scene_named(snap, song):
                    self.mixes.record(song, song_mixes.mix_of(snap))
            finally:
                self._mix_lock.release()
        except (LiveUnavailable, RigLinkError, UserError):
            pass  # the next look tries again

    def scene_name(self, args):
        try:
            return self.live.snapshot()["scenes"][int(args.get("scene_index"))]["name"] or None
        except (LiveUnavailable, RigLinkError, KeyError, IndexError, TypeError, ValueError):
            return None

    def ai_state(self):
        try:
            self.chat.client()
        except AssistantUnavailable as e:
            return {"ready": False, "message": str(e)}
        if self.chat.setup_error:
            return {"ready": False, "message": self.chat.setup_error}
        return {"ready": True, "message": None}

    def transcript(self):
        out = []
        for entry in self.chat.transcript:
            entry = dict(entry)
            pid = entry.pop("proposal_id", None)
            if pid:
                entry["proposal"] = describe_proposal(self.chat.proposal(pid))
            out.append(entry)
        return out

    def state(self):
        live = self.live_state()
        return {
            "live": live,
            "ai": self.ai_state(),
            "chat": self.transcript(),
            "busy": self.chat.busy,
            "usage": self.chat.usage,
            "colors": {name: f"#{rgb:06x}" for name, rgb in TRACK_COLORS.items()},
            "folders": [
                {"key": key, "label": label, "color": f"#{TRACK_COLORS[colour]:06x}"}
                for key, label, colour in FAMILIES
            ],
            "room": self.room.facts() if self.room else [],
            "activity": self.activity(),
            "demo": self.demo,
            "song_mix": self.mix_state(live["snapshot"]),
            "applying": self.applying(),
        }

    # -- actions --------------------------------------------------------

    def notes(self):
        live = self.live_state()
        stock = self.live.stock_devices() if live["connected"] else None
        saved = {song.casefold(): self.mixes.saved(song) for song in self.mixes.songs()}
        marks = {song.casefold(): self.mixes.checkpoints(song) for song in self.mixes.songs()}
        return session_notes(live["snapshot"], stock, live["message"], self.imports, self.room,
                             self.mixes.current, saved, marks)

    def send_message(self, text, attachment=None):
        self.chat.send(text, self.notes(), attachment)

    def song_stems(self, song):
        """[(track name, file path)] for each audio clip in a song of the open set."""
        try:
            ex = Executor(self.live)
            scene = ex.song(song)
            rows = self.live.call("song_files", scene_index=scene["index"])
        except ActionFailed as e:
            raise LookupError(str(e)) from e
        except LiveUnavailable as e:
            raise LookupError(f"{e} Songs in the set can only be heard with Ableton open.") from e
        except RigLinkError as e:
            if "unknown cmd" in str(e):
                raise LookupError("Live is running an older RigLink. Quit and reopen Live, then try again.") from e
            raise LookupError(f"Ableton couldn't say which files are in that song ({e}).") from e
        return [(r["track"], r["file_path"]) for r in rows if r.get("file_path")]

    def _load_imports(self):
        if self.imports_path is None:
            return
        try:
            data = json.loads(self.imports_path.read_text())
            self.files.update({str(k): str(v) for k, v in data.get("files", {}).items()})
            self.imports.update({str(k): int(v) for k, v in data.get("folders", {}).items()})
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # nothing saved yet, or unreadable: start fresh

    def _save_imports(self):
        if self.imports_path is None:
            return
        try:
            self.imports_path.parent.mkdir(parents=True, exist_ok=True)
            self.imports_path.write_text(json.dumps({"files": self.files, "folders": self.imports}, indent=1))
        except OSError:
            pass  # remembering imports is a convenience; the import itself worked

    def import_folder(self, path, note=""):
        """Measure every audio file in a folder and hand the list to the assistant."""
        try:
            folder, found = audio_files.scan(path)
        except audio_files.AudioFileError as e:
            raise UserError(str(e)) from e
        if not found:
            raise UserError(f"There's no audio in {Path(path).name}. Pick the folder with the WAV or AIFF files in it.")
        measured = audio_files.measure_many([p for _id, p in found])
        lines = [audio_files.describe(fid, m) for (fid, _p), m in zip(found, measured)]
        if len(found) >= audio_files.MAX_FILES:
            lines.append(f"(Only the first {audio_files.MAX_FILES} files are listed.)")
        self.files.update({fid: str(p) for fid, p in found})  # str: saved as JSON
        self.imports[folder.name] = len(found)
        self._save_imports()
        attachment = (
            f'<imported_folder name="{folder.name}" files="{len(found)}">\n'
            + "\n".join(lines) + "\n</imported_folder>"
        )
        songs = {}
        for fid, _p in found:
            songs.setdefault(str(Path(fid).parent), []).append(fid)
        attachment += "\n<parts>\n" + "\n".join(
            parts.describe(song, parts.plan(fids)) for song, fids in sorted(songs.items())) + "\n</parts>"
        keys = self.song_keys(found)
        if keys:
            attachment += "\n<song_keys>\n" + "\n".join(keys) + "\n</song_keys>"
        vendor = self.vendor_song(folder, found)
        if vendor:
            attachment += "\n" + vendor
        text = f"Import the audio in “{folder.name}” ({len(found)} files)."
        if note.strip():
            text += " " + note.strip()
        self.send_message(text, attachment)

    @staticmethod
    def vendor_song(folder, found):
        """What a vendor's set next to these stems says about the song, for the assistant.

        Only when the set's stems are mostly the ones imported, so a stray .als
        beside an unrelated folder isn't taken for this song's.
        """
        path = vendor_set.find(folder)
        if path is None:
            return None
        try:
            info = vendor_set.read(path)
        except vendor_set.VendorSetError as e:
            return f"<vendor_set_problem>{e}</vendor_set_problem>"
        ids = {str(Path(p).resolve()): fid for fid, p in found}
        matched = sum(1 for t in info["tracks"] if t["file"] and str(t["file"]) in ids)
        if not info["tracks"] or matched * 2 < len(info["tracks"]):
            return None
        return vendor_set.describe(info, ids)

    @staticmethod
    def song_keys(found):
        """A key guess for each song (subfolder) in an import: lines for the assistant."""
        songs = {}
        for fid, path in found:
            songs.setdefault(str(Path(fid).parent), []).append((Path(fid).stem, path))
        return [f'- "{song}": {song_key.describe(song_key.detect(stems))}'
                for song, stems in sorted(songs.items())]

    def apply(self, pid):
        with self._apply_lock:
            p = self._pending(pid)
            states = ["waiting"] * len(p["actions"])
            self.progress = {"id": pid, "states": states}

            def on_step(n, state):
                states[n] = state

            try:
                results = run_all(self.live, p["actions"], self.files, on_touch=self.touch,
                                  folders=self.folders, parts_dir=self.parts_dir, mixes=self.mixes,
                                  mix_control=self, on_step=on_step)
            finally:
                self.progress = None
            self.chat.record_outcome(pid, "applied", results)
        # A listen step's numbers are only useful once the assistant has read them.
        if any(isinstance(a, Listen) for a in p["actions"]):
            try:
                self.chat.follow_up(self.notes())
            except AssistantUnavailable:
                pass  # the results still show; the volunteer can ask about them
        return results

    def dismiss(self, pid):
        self._pending(pid)
        self.chat.record_outcome(pid, "dismissed")

    def export(self, pid):
        """Render a proposal's new tracks to .als bytes. Returns (bytes, notes)."""
        p = self.chat.proposal(pid)
        if p is None:
            raise UserError("That suggestion isn't available any more.")
        spec, notes = to_rigspec(p["actions"])
        if spec is None:
            raise UserError("There are no new tracks in that suggestion to put in a session file.")
        try:
            from file_builder.write_als import render

            data = render(spec, TEMPLATE)
        except LookupError as e:
            raise UserError(
                f"Couldn't build the session file: {e}. The session file needs Ableton Live 12 "
                "installed on this computer, because it copies Live's own effects."
            ) from e
        if p["status"] == "pending":
            self.chat.record_outcome(pid, "exported")
        return data, notes

    def _pending(self, pid):
        p = self.chat.proposal(pid)
        if p is None:
            raise UserError("That suggestion isn't available any more.")
        if p["status"] != "pending":
            raise UserError("That suggestion has already been dealt with.")
        return p


def _playing_scene(snap):
    """The song (scene index) Live is playing, from its playing clips, or None."""
    return _playing_scene_of(snap) if snap else None


def _scene_named(snap, name):
    if not name:
        return None
    return next((s for s in snap["scenes"] if (s["name"] or "").casefold() == name.casefold()), None)


class UserError(Exception):
    """A problem to show the volunteer as-is."""


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HolySound"

        def log_message(self, fmt, *args):  # quieter than the default
            if not self.path.startswith(("/api/state", "/api/events")):
                super().log_message(fmt, *args)

        # -- access ------------------------------------------------------

        def _local(self):
            return ipaddress.ip_address(self.client_address[0]).is_loopback

        def _authorised(self):
            if self._local():
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return app.key is not None and cookie.get("hs_key") is not None and \
                secrets.compare_digest(cookie["hs_key"].value, app.key)

        def _same_origin(self):
            # Browsers send Origin on cross-site POSTs; refuse other sites.
            origin = self.headers.get("Origin")
            return origin is None or urlparse(origin).netloc == self.headers.get("Host")

        # -- responses ---------------------------------------------------

        def _json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page reloaded or closed mid-reply; nothing to do

        def _events(self):
            """Server-sent events from the conversation's ReplyFeed, until the page goes away."""
            feed = app.chat.feed
            seq = feed.cursor()  # before the headers, so nothing published after them is missed
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    events = feed.since(seq, KEEP_ALIVE)
                    if not events:
                        self.wfile.write(b": still here\n\n")
                    for seq, kind, data in events:
                        self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page closed or reloaded

        def _error(self, message, status=HTTPStatus.BAD_REQUEST):
            self._json({"error": message}, status)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64_000:
                raise UserError("That message is too long.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw)
            except ValueError as e:
                raise UserError("The page sent something the app couldn't read.") from e
            if not isinstance(data, dict):
                raise UserError("The page sent something the app couldn't read.")
            return data

        # -- routes ------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/" and not self._authorised():
                key = parse_qs(url.query).get("key", [None])[0]
                if app.key and key and secrets.compare_digest(key, app.key):
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Set-Cookie", f"hs_key={app.key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                return self._text(HTTPStatus.FORBIDDEN, "Open the link Holy Sound printed on the computer running it.")
            if not self._authorised():
                return self._error("Not allowed.", HTTPStatus.FORBIDDEN)

            try:
                if url.path == "/api/state":
                    return self._json(app.state())
                if url.path == "/api/events":
                    return self._events()
                if url.path == "/api/presets":
                    device = parse_qs(url.query).get("device", [""])[0]
                    return self._json({"presets": app.live.presets(device)})
                if url.path == "/api/devices":
                    return self._json({"devices": app.live.stock_devices()})
                if url.path == "/api/folders":
                    path = parse_qs(url.query).get("path", [None])[0]
                    try:
                        return self._json(audio_files.browse(path))
                    except audio_files.AudioFileError as e:
                        raise UserError(str(e)) from e
                match = re.fullmatch(r"/api/proposals/(\d+)/export", url.path)
                if match:
                    data, _notes = app.export(match.group(1))
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", 'attachment; filename="Holy Sound.als"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if url.path == "/api/proposals/export-notes":
                    pid = parse_qs(url.query).get("id", [""])[0]
                    p = app.chat.proposal(pid)
                    notes = to_rigspec(p["actions"])[1] if p else []
                    return self._json({"notes": notes})
                return self._static(url.path)
            except (UserError, LiveUnavailable) as e:
                return self._error(str(e))
            except RigLinkError as e:
                return self._error(f"Ableton couldn't do that: {e}")
            except ConnectionError:
                raise  # the browser went away; nothing to answer
            except Exception:
                return self._unexpected()

        def do_POST(self):
            if not self._authorised() or not self._same_origin():
                return self._error("Not allowed.", HTTPStatus.FORBIDDEN)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._error("Expected JSON.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/chat":
                    text = str(body.get("message", "")).strip()
                    if not text:
                        raise UserError("Type a message first.")
                    if app.chat.busy:
                        raise UserError("Still working on the last message. One moment.")
                    app.send_message(text)
                    return self._json(app.state())
                if path == "/api/import":
                    if app.chat.busy:
                        raise UserError("Still working on the last message. One moment.")
                    folder = str(body.get("folder", "")).strip()
                    if not folder:
                        raise UserError("Pick a folder first.")
                    app.import_folder(folder, str(body.get("note", "")))
                    return self._json(app.state())
                if path == "/api/room":
                    if app.room is None:
                        raise UserError("Memory isn't available.")
                    if body.get("add"):
                        app.room.add([str(body["add"])])
                    if body.get("remove") is not None:
                        app.room.remove([int(body["remove"])])
                    return self._json(app.state())
                if path == "/api/track-folder":
                    track = str(body.get("track", "")).strip()
                    if not track:
                        raise UserError("Say which track to move.")
                    try:
                        app.folders.move(track, body.get("folder"))
                    except ValueError as e:
                        raise UserError(str(e))
                    return self._json(app.state())
                if path == "/api/track-key":
                    track = str(body.get("track", "")).strip()
                    if not track:
                        raise UserError("Say which track.")
                    app.folders.set_follows_key(track, bool(body.get("follows")))
                    return self._json(app.state())
                if path == "/api/song-mix":
                    action = body.get("action")
                    if action == "pick":
                        index = body.get("scene_index")
                        app.pick_song_mix(None if index is None or index == "" else int(index))
                    elif action == "checkpoint":
                        app.checkpoint_song_mix(str(body.get("label") or ""))
                    elif action == "restore":
                        app.restore_song_mix(str(body.get("id", "")))
                    elif action == "delete":
                        app.mixes.delete_checkpoint(app._current_song(), str(body.get("id", "")))
                    else:
                        raise UserError("The page asked for something the song mix can't do.")
                    return self._json(app.state())
                if path == "/api/reset":
                    app.chat.reset()
                    return self._json(app.state())
                match = re.fullmatch(r"/api/proposals/(\d+)/(apply|dismiss)", path)
                if match:
                    pid, verb = match.groups()
                    if verb == "apply":
                        app.apply(pid)
                    else:
                        app.dismiss(pid)
                    return self._json(app.state())
                if path == "/api/live":
                    cmd = body.get("cmd")
                    if cmd not in DIRECT_COMMANDS:
                        raise UserError("The app can't do that from here.")
                    args = body.get("args") or {}
                    if not isinstance(args, dict):
                        raise UserError("The page sent something the app couldn't read.")
                    old_name = app.track_name(args) if cmd == "set_track_name" else None
                    old_song = app.scene_name(args) if cmd == "set_scene" and args.get("name") else None
                    if cmd == "transpose_song":
                        # Click, guide and anything the volunteer opted out keep their key.
                        args["skip_tracks"] = app.folders.kept_tracks(app.live.snapshot()["tracks"])
                    result = app.live.call(cmd, **args)
                    if old_name and args.get("name"):
                        app.folders.rename(old_name, str(args["name"]))
                    if old_song:
                        app.mixes.rename(old_song, str(args["name"]))
                    if cmd == "fire_scene" and app.scene_name(args):
                        app.pick_song_mix(int(args["scene_index"]))  # starting a song here puts its mix on
                    return self._json({"result": result, "live": app.live_state()})
                return self._error("Not found.", HTTPStatus.NOT_FOUND)
            except UserError as e:
                return self._error(str(e))
            except (LiveUnavailable, AssistantUnavailable) as e:
                return self._error(str(e), HTTPStatus.SERVICE_UNAVAILABLE)
            except RigLinkError as e:
                from app.actions import _sentence

                return self._error(_sentence(e))
            except TypeError as e:
                if path != "/api/live":
                    raise  # a bug here, not a bad request from the page; don't blame Live
                return self._error(f"The page asked Ableton for something it didn't understand ({e}).")
            except ConnectionError:
                raise  # the browser went away; nothing to answer
            except Exception:
                return self._unexpected()

        def _unexpected(self):
            """A bug, not a user mistake: log it, but still answer the page in a sentence."""
            traceback.print_exc()
            return self._error(
                "Something went wrong inside Holy Sound. Try again; if it keeps happening, restart the app.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        def _text(self, status, text):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path):
            if path == "/":
                path = "/index.html"
            target = (STATIC / path.lstrip("/")).resolve()
            if STATIC not in target.parents or not target.is_file():
                return self._text(HTTPStatus.NOT_FOUND, "Not found.")
            body = target.read_bytes()
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            font = target.suffix == ".woff2"
            if font:
                kind = "font/woff2"  # the OS's idea of this type varies; fonts are vendored in static/fonts
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") or kind.endswith("javascript") else ""))
            self.send_header("Cache-Control", "public, max-age=86400" if font else "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def load_dotenv(path=ROOT / ".env"):
    """Read KEY=value lines from .env into the environment (no dependency)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def lan_address():
    """This computer's address on the local network (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Holy Sound: describe your Sunday, get a working Ableton set.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--lan", action="store_true", help="Let phones and tablets on the same Wi-Fi connect.")
    parser.add_argument("--fake-live", action="store_true", help="Use a pretend Live Set instead of Ableton.")
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser window.")
    parser.add_argument("--list-models", action="store_true",
                        help="List the OpenCode Go models and which ones Holy Sound can use, then quit.")
    args = parser.parse_args(argv)

    load_dotenv()
    if args.list_models:
        from app.providers import list_models_text

        print(list_models_text())
        return
    live_port = 9877
    if args.fake_live:
        from app import fake_live

        fake_server, _ = fake_live.serve(port=0)
        live_port = fake_server.server_address[1]

    key = secrets.token_urlsafe(9) if args.lan else None
    room = RoomMemory()
    app = App(LiveLink(port=live_port), Conversation(room=room), key=key, demo=args.fake_live,
              imports_path=room.path.with_name("imports.json"))
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), make_handler(app))
    server.daemon_threads = True

    local_url = f"http://127.0.0.1:{args.port}/"
    print(f"Holy Sound is running: {local_url}")
    if args.fake_live:
        print("Using a pretend Live Set (--fake-live). Nothing here touches Ableton.")
    if args.lan:
        print(f"On a phone or tablet on the same Wi-Fi, open: http://{lan_address()}:{args.port}/?key={key}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (local_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
