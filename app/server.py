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
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import audio_files
from app.actions import Listen, run_all, to_rigspec
from app.assistant import AssistantUnavailable, Conversation, describe_proposal, session_notes
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

# What the mixer panel may do directly, without going through the assistant.
# Everything here is undoable in Live with Cmd+Z.
DIRECT_COMMANDS = {
    "set_volume", "set_pan", "set_mute", "set_solo", "set_send", "set_tempo",
    "play", "stop", "fire_scene", "load_device", "delete_device", "set_track_name",
    "create_scene", "set_scene", "set_routing", "create_audio_track", "create_midi_track",
    "create_return_track", "get_routing", "set_track_color", "set_clip_gain",
}


class App:
    def __init__(self, live, conversation, key=None, folders=None):
        self.room = conversation.room
        self.folders = folders or FolderMemory()
        self.clock = time.monotonic
        self._touched = {}  # track name (case-folded) -> (lit until, name)
        self.live = live
        self.chat = conversation
        self.key = key  # None: only this computer can connect
        self._apply_lock = threading.Lock()  # a laptop and a phone may both press Apply
        self.files = {}    # imported file id -> absolute path; import_audio may use only these
        self.imports = {}  # imported folder name -> number of files

    # -- state ----------------------------------------------------------

    def touch(self, name):
        """The assistant just changed this track."""
        self._touched[name.casefold()] = (self.clock() + AI_GLOW_SECONDS, name)

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
        tracks = [dict(t, folder=self.folders.folder_for(t["name"])) for t in snapshot["tracks"]]
        return dict(snapshot, tracks=tracks)

    def live_state(self):
        try:
            return {"connected": True, "snapshot": self.with_folders(self.live.snapshot()), "message": None}
        except LiveUnavailable as e:
            return {"connected": False, "snapshot": None, "message": str(e)}
        except RigLinkError as e:
            return {"connected": False, "snapshot": None, "message": f"Live reported a problem: {e}"}

    def track_name(self, args):
        """The current name of the track a direct command points at, if it has one."""
        try:
            snap = self.live.snapshot()
            rows = snap["returns"] if args.get("is_return") else snap["tracks"]
            return rows[int(args.get("track_index"))]["name"]
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
        return {
            "live": self.live_state(),
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
        }

    # -- actions --------------------------------------------------------

    def notes(self):
        live = self.live_state()
        stock = self.live.stock_devices() if live["connected"] else None
        return session_notes(live["snapshot"], stock, live["message"], self.imports, self.room)

    def send_message(self, text, attachment=None):
        self.chat.send(text, self.notes(), attachment)

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
        self.files.update(dict(found))
        self.imports[folder.name] = len(found)
        attachment = (
            f'<imported_folder name="{folder.name}" files="{len(found)}">\n'
            + "\n".join(lines) + "\n</imported_folder>"
        )
        text = f"Import the audio in “{folder.name}” ({len(found)} files)."
        if note.strip():
            text += " " + note.strip()
        self.send_message(text, attachment)

    def apply(self, pid):
        with self._apply_lock:
            p = self._pending(pid)
            results = run_all(self.live, p["actions"], self.files, on_touch=self.touch)
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
                return self._error(f"Live couldn't do that: {e}")

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
                        raise UserError("Still working on the last message — one moment.")
                    app.send_message(text)
                    return self._json(app.state())
                if path == "/api/import":
                    if app.chat.busy:
                        raise UserError("Still working on the last message — one moment.")
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
                    result = app.live.call(cmd, **args)
                    if old_name and args.get("name"):
                        app.folders.rename(old_name, str(args["name"]))
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
                return self._error(f"The page asked Live for something it didn't understand ({e}).")

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
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") or kind.endswith("javascript") else ""))
            self.send_header("Cache-Control", "no-cache")
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
    parser = argparse.ArgumentParser(description="Holy Sound — describe your Sunday, get a working Ableton set.")
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
    app = App(LiveLink(port=live_port), Conversation(room=RoomMemory()), key=key)
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
