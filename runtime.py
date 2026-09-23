"""Client for the RigLink Control Surface (remote_script/RigLink).

Talks to a running Live instance over the local socket the control surface
opens. This is the live-edit path; write_als.py (closed-file .als generation)
remains the export/handoff path. Both take RigSpec-shaped intent -- this one
just applies it to a Live Set that's already open instead of writing a file.
"""

import json
import socket

HOST = "127.0.0.1"
PORT = 9877


class RigLinkError(RuntimeError):
    """Raised when Live rejects or fails to execute a command."""


class LiveConnection:
    """One TCP connection to the RigLink control surface.

    Not thread-safe; one command in flight at a time. Live only services the
    socket a few times a second (from update_display), so expect each
    round-trip to take on the order of 100ms.
    """

    def __init__(self, host=HOST, port=PORT, timeout=5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buffer = b""

    def close(self):
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def send(self, cmd, **args):
        request = json.dumps({"cmd": cmd, "args": args}) + "\n"
        self._sock.sendall(request.encode("utf-8"))
        return self._read_reply()

    def _read_reply(self):
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RigLinkError("connection closed before a reply arrived")
            self._buffer += chunk

        line, self._buffer = self._buffer.split(b"\n", 1)
        reply = json.loads(line.decode("utf-8"))
        if not reply.get("ok"):
            raise RigLinkError(reply.get("error", "unknown error"))
        return reply.get("result")

    # -- convenience wrappers, one per command in RigLink.COMMANDS --------

    def ping(self):
        return self.send("ping")

    def list_tracks(self):
        return self.send("list_tracks")

    def create_audio_track(self, name=None):
        return self.send("create_audio_track", name=name)

    def create_midi_track(self, name=None):
        return self.send("create_midi_track", name=name)

    def set_track_name(self, track_index, name):
        return self.send("set_track_name", track_index=track_index, name=name)

    def load_device(self, track_index, device_name):
        return self.send("load_device", track_index=track_index, device_name=device_name)
