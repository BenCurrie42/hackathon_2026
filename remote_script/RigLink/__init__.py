"""RigLink Control Surface.

Loaded by Live as a Control Surface (Preferences -> Link/MIDI). Opens a local
TCP socket and accepts newline-delimited JSON commands from runtime.py,
executing them against the currently open Live Set.

Command vocabulary matches RigSpec, not Live's full API surface: create a
track, name it, load a stock device onto it. Nothing here writes device
parameters or dB/pan values yet -- those need an empirical probe first (see
CLAUDE.md's "Live is the source of truth" rule) before we ship a possibly-wrong
conversion.
"""

import json
import socket

from _Framework.ControlSurface import ControlSurface

HOST = "127.0.0.1"
PORT = 9877


def create_instance(c_instance):
    return RigLink(c_instance)


class RigLink(ControlSurface):
    def __init__(self, c_instance):
        super().__init__(c_instance)
        self._clients = []
        self._buffers = {}
        self._server = None
        self._open_server()

    def _open_server(self):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((HOST, PORT))
            server.listen(4)
            server.setblocking(False)
            self._server = server
            self.log_message("RigLink: listening on %s:%d" % (HOST, PORT))
        except OSError as e:
            self.log_message("RigLink: failed to open socket: %s" % e)

    def disconnect(self):
        for client in self._clients:
            try:
                client.close()
            except OSError:
                pass
        self._clients = []
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        super().disconnect()

    def update_display(self):
        super().update_display()
        self._accept_new_clients()
        self._service_clients()

    def _accept_new_clients(self):
        if self._server is None:
            return
        try:
            while True:
                client, _addr = self._server.accept()
                client.setblocking(False)
                self._clients.append(client)
                self._buffers[client] = b""
        except BlockingIOError:
            pass
        except OSError as e:
            self.log_message("RigLink: accept failed: %s" % e)

    def _service_clients(self):
        dead = []
        for client in self._clients:
            try:
                chunk = client.recv(4096)
            except BlockingIOError:
                continue
            except OSError:
                dead.append(client)
                continue

            if not chunk:
                dead.append(client)
                continue

            self._buffers[client] += chunk
            while b"\n" in self._buffers[client]:
                line, self._buffers[client] = self._buffers[client].split(b"\n", 1)
                if line.strip():
                    self._handle_line(client, line)

        for client in dead:
            self._drop_client(client)

    def _drop_client(self, client):
        try:
            client.close()
        except OSError:
            pass
        if client in self._clients:
            self._clients.remove(client)
        self._buffers.pop(client, None)

    def _handle_line(self, client, line):
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._reply(client, {"ok": False, "error": "bad json: %s" % e})
            return

        cmd = request.get("cmd")
        args = request.get("args", {})
        handler = COMMANDS.get(cmd)
        if handler is None:
            self._reply(client, {"ok": False, "error": "unknown cmd: %s" % cmd})
            return

        try:
            result = handler(self, **args)
            self._reply(client, {"ok": True, "result": result})
        except Exception as e:  # noqa: BLE001 -- must never crash Live's event loop
            self._reply(client, {"ok": False, "error": str(e)})

    def _reply(self, client, payload):
        try:
            client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            self._drop_client(client)


# -- Commands --------------------------------------------------------------
# Each takes the RigLink instance plus keyword args from the request's
# "args" object, and returns a JSON-serializable result.


def _ping(rf):
    return "pong"


def _list_tracks(rf):
    song = rf.song()
    return [
        {"index": i, "name": t.name, "is_midi": t.has_midi_input}
        for i, t in enumerate(song.tracks)
    ]


def _create_audio_track(rf, name=None):
    song = rf.song()
    song.create_audio_track(-1)
    track = song.tracks[-1]
    if name:
        track.name = name
    return {"index": len(song.tracks) - 1, "name": track.name}


def _create_midi_track(rf, name=None):
    song = rf.song()
    song.create_midi_track(-1)
    track = song.tracks[-1]
    if name:
        track.name = name
    return {"index": len(song.tracks) - 1, "name": track.name}


def _set_track_name(rf, track_index, name):
    track = rf.song().tracks[track_index]
    track.name = name
    return {"index": track_index, "name": track.name}


def _find_browser_item(root, device_name):
    """Depth-first search of a browser tree for an item matching device_name."""
    stack = list(root.children) if root.children else []
    while stack:
        item = stack.pop()
        if item.name == device_name:
            return item
        if item.children:
            stack.extend(item.children)
    return None


def _load_device(rf, track_index, device_name):
    song = rf.song()
    track = song.tracks[track_index]
    song.view.selected_track = track

    browser = rf.application().browser
    for category in (
        browser.audio_effects,
        browser.midi_effects,
        browser.instruments,
        browser.drums,
        browser.sounds,
    ):
        item = _find_browser_item(category, device_name)
        if item is not None:
            browser.load_item(item)
            return {"track_index": track_index, "device": device_name}

    raise LookupError("no browser item named %r" % device_name)


COMMANDS = {
    "ping": _ping,
    "list_tracks": _list_tracks,
    "create_audio_track": _create_audio_track,
    "create_midi_track": _create_midi_track,
    "set_track_name": _set_track_name,
    "load_device": _load_device,
}
