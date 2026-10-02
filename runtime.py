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

    def list_returns(self):
        return self.send("list_returns")

    def create_return_track(self, name=None):
        return self.send("create_return_track", name=name)

    def set_track_name(self, track_index, name, is_return=False):
        return self.send("set_track_name", track_index=track_index, name=name, is_return=is_return)

    def delete_track(self, track_index, is_return=False):
        return self.send("delete_track", track_index=track_index, is_return=is_return)

    def set_track_color(self, track_index, rgb, is_return=False):
        return self.send("set_track_color", track_index=track_index, rgb=rgb, is_return=is_return)

    def track_contents(self, track_index, is_return=False):
        return self.send("track_contents", track_index=track_index, is_return=is_return)

    def api_names(self):
        return self.send("api_names")

    def get_routing(self, track_index, is_return=False):
        return self.send("get_routing", track_index=track_index, is_return=is_return)

    def set_routing(self, track_index, direction, type_name, channel_name=None, is_return=False):
        return self.send(
            "set_routing",
            track_index=track_index,
            direction=direction,
            type_name=type_name,
            channel_name=channel_name,
            is_return=is_return,
        )

    def get_mixer(self, track_index, is_return=False):
        return self.send("get_mixer", track_index=track_index, is_return=is_return)

    def set_volume(self, track_index, db, is_return=False):
        return self.send("set_volume", track_index=track_index, db=db, is_return=is_return)

    def set_pan(self, track_index, pan, is_return=False):
        return self.send("set_pan", track_index=track_index, pan=pan, is_return=is_return)

    def set_mute(self, track_index, on, is_return=False):
        return self.send("set_mute", track_index=track_index, on=on, is_return=is_return)

    def set_solo(self, track_index, on, is_return=False):
        return self.send("set_solo", track_index=track_index, on=on, is_return=is_return)

    def set_send(self, track_index, return_index, db, is_return=False):
        return self.send(
            "set_send", track_index=track_index, return_index=return_index, db=db, is_return=is_return
        )

    def list_presets(self, device_name):
        return self.send("list_presets", device_name=device_name)

    def load_device(self, track_index, device_name, preset=None, is_return=False):
        return self.send(
            "load_device",
            track_index=track_index,
            device_name=device_name,
            preset=preset,
            is_return=is_return,
        )

    def list_devices(self, track_index, is_return=False):
        return self.send("list_devices", track_index=track_index, is_return=is_return)

    def delete_device(self, track_index, device_index, is_return=False):
        return self.send(
            "delete_device", track_index=track_index, device_index=device_index, is_return=is_return
        )

    def reset_meters(self):
        return self.send("reset_meters")

    def get_meters(self):
        return self.send("get_meters")

    def import_audio(self, track_index, file_path, scene_index, name=None, gain_db=None):
        return self.send(
            "import_audio",
            track_index=track_index,
            file_path=file_path,
            scene_index=scene_index,
            name=name,
            gain_db=gain_db,
        )

    def set_clip_gain(self, track_index, scene_index, db):
        return self.send("set_clip_gain", track_index=track_index, scene_index=scene_index, db=db)

    def get_song(self):
        return self.send("get_song")

    def set_tempo(self, bpm):
        return self.send("set_tempo", bpm=bpm)

    def play(self):
        return self.send("play")

    def stop(self):
        return self.send("stop")

    def list_scenes(self):
        return self.send("list_scenes")

    def create_scene(self, name=None, bpm=None):
        return self.send("create_scene", name=name, bpm=bpm)

    def set_scene(self, scene_index, name=None, bpm=None):
        return self.send("set_scene", scene_index=scene_index, name=name, bpm=bpm)

    def delete_scene(self, scene_index):
        return self.send("delete_scene", scene_index=scene_index)

    def fire_scene(self, scene_index):
        return self.send("fire_scene", scene_index=scene_index)

    def list_locators(self):
        return self.send("list_locators")

    def add_locator(self, time, name=None):
        return self.send("add_locator", time=time, name=name)

    def delete_locator(self, locator_index):
        return self.send("delete_locator", locator_index=locator_index)

    def jump_to_locator(self, locator_index):
        return self.send("jump_to_locator", locator_index=locator_index)

    def get_snapshot(self):
        return self.send("get_snapshot")

    def list_stock_devices(self):
        return self.send("list_stock_devices")
