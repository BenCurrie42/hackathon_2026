"""A stand-in for Ableton Live + RigLink, for building and testing without Live.

Speaks the same newline-delimited JSON protocol on the same port as
remote_script/RigLink, and keeps a small in-memory Live Set. Display strings
imitate Live's ("-6.0 dB", "25L", "C") closely enough for the UI, but this is
not a model of Live's behaviour -- anything learned here must be confirmed in
Live before it goes in CLAUDE.md.

    uv run python -m app.fake_live            # listens on 127.0.0.1:9877
"""

from __future__ import annotations

import json
import os
import random
import socketserver
import threading
import time

HOST = "127.0.0.1"
PORT = 9877

MIN_DB = -70.0
MAX_DB = 6.0

STOCK_DEVICES = {
    "audio_effects": [
        "Auto Filter", "Channel EQ", "Chorus-Ensemble", "Compressor", "Delay",
        "Drum Buss", "Echo", "EQ Eight", "EQ Three", "Gate", "Glue Compressor",
        "Hybrid Reverb", "Limiter", "Multiband Dynamics", "Overdrive", "Reverb",
        "Saturator", "Tuner", "Utility",
    ],
    "instruments": ["Drift", "Drum Rack", "Electric", "Operator", "Sampler", "Simpler", "Wavetable"],
    "midi_effects": ["Arpeggiator", "Chord", "Scale"],
}

PRESETS = {
    "Compressor": ["Gentle Squeeze", "Sustained Lead Vocal", "Vocal Leveler"],
    "Reverb": ["Large Hall", "Small Room", "Vocal Plate"],
    "Hybrid Reverb": ["Big Church", "Vocal Space"],
    "EQ Eight": ["Low Cut 80Hz", "Vocal Presence"],
    "Delay": ["Quarter Note", "Slapback"],
}

AUDIO_INPUTS = {
    "Ext. In": [str(n) for n in range(1, 9)] + ["1/2", "3/4", "5/6", "7/8"],
    "Resampling": [""],
    "No Input": [""],
}
MIDI_INPUTS = {"All Ins": ["All Channels"], "No Input": [""]}
OUTPUTS = {
    "Master": [""],
    "Ext. Out": ["1/2", "3/4", "5/6", "7/8"] + [str(n) for n in range(1, 9)],
    "Sends Only": [""],
}


# Live gives new tracks colours from its palette; these are close enough.
DEFAULT_COLORS = [0xFF94A6, 0xFFA529, 0xCC9927, 0xF7F47C, 0xBFFB00, 0x1AFF2F, 0x25FFA8, 0x5CFFE8, 0x8BC5FF, 0x5480E4]


def _gain(db):
    return 0.0 if db is None else 10 ** (db / 20)


def _db_text(db):
    return "-inf dB" if db is None else "%.1f dB" % db


def _pan_text(pan):
    amount = round(abs(pan) * 50)
    if amount == 0:
        return "C"
    return "%d%s" % (amount, "L" if pan < 0 else "R")


def _clamp_db(db):
    db = float(db)
    if db < MIN_DB:
        return None
    return min(db, MAX_DB)


def _match(options, wanted, noun):
    for option in options:
        if option.casefold() == str(wanted).casefold():
            return option
    names = ", ".join(o for o in options if o) or "none"
    raise LookupError("no %s called %r (options: %s)" % (noun, wanted, names))


class FakeSet:
    """The in-memory Live Set. Every public method is a RigLink command."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tempo = 72.0
        self.numerator = 4
        self.denominator = 4
        self.is_playing = False
        self.tracks = []
        self.returns = []
        self.scenes = [{"name": "", "tempo": None}]
        self.cues = []
        self.playing_scene = None
        self.meters_since = time.monotonic()
        self.create_return_track("A-Reverb")
        self.create_return_track("B-Delay")

    # -- helpers --------------------------------------------------------

    def _new_track(self, name, is_midi=False, is_return=False):
        track = {
            "name": name,
            "color": DEFAULT_COLORS[(len(self.tracks) + len(self.returns)) % len(DEFAULT_COLORS)],
            "clips": {},  # scene index -> clip
            "is_midi": is_midi,
            "volume_db": 0.0,
            "pan": 0.0,
            "mute": False,
            "solo": False,
            "devices": [],
            "output": {"type": "Master", "channel": ""},
            "sends": [None for _ in self.returns],
        }
        if not is_return:
            track["input"] = (
                {"type": "All Ins", "channel": "All Channels"}
                if is_midi
                else {"type": "Ext. In", "channel": "1"}
            )
        return track

    def _track(self, track_index, is_return=False):
        tracks = self.returns if is_return else self.tracks
        if not 0 <= track_index < len(tracks):
            raise IndexError("list index out of range")
        return tracks[track_index]

    def _strip(self, i, t, is_return, since=True):
        row = {
            "index": i,
            "name": t["name"],
            "is_return": is_return,
            "color": t["color"],
            "meter": None if t["is_midi"] and not t["devices"] else self._meter(t, since),
            "volume": _db_text(t["volume_db"]),
            "volume_db": t["volume_db"],
            "pan": _pan_text(t["pan"]),
            "pan_value": t["pan"],
            "mute": t["mute"],
            "solo": t["solo"],
            "sends": [
                {"return": r["name"], "level": _db_text(s), "level_db": s}
                for r, s in zip(self.returns, t["sends"])
            ],
            "devices": list(t["devices"]),
            "output": dict(t["output"]),
        }
        if not is_return:
            row["is_midi"] = t["is_midi"]
            row["input"] = dict(t["input"])
            row["clips"] = [
                {
                    "scene_index": si,
                    "name": c["name"],
                    "is_audio": True,
                    "is_playing": self.is_playing and self.playing_scene == si,
                    "length": c["length"],
                    "gain": _db_text(c["gain_db"]),
                    "gain_db": c["gain_db"],
                }
                for si, c in sorted(t["clips"].items())
            ]
        return row

    def _level(self, t):
        """A pretend meter reading (0-1) for one track right now."""
        if not self.is_playing or t["mute"]:
            return 0.0
        clip = t["clips"].get(self.playing_scene)
        if clip is not None:
            source = clip["loudness"] * _gain(clip["gain_db"])
        elif not t.get("is_return") and t.get("input", {}).get("type") == "Ext. In":
            source = 0.25  # someone singing into a mic
        else:
            source = 0.0
        return min(1.0, source * _gain(t["volume_db"]) * random.uniform(0.85, 1.0))

    def _meter(self, t, since):
        level = self._level(t)
        return {"peak": min(1.0, level * 1.15), "average": level * 0.7} if since else {"peak": level, "average": level}

    def set_track_color(self, track_index, rgb, is_return=False):
        track = self._track(track_index, is_return)
        track["color"] = int(rgb)
        return {"index": track_index, "color": track["color"], "clips": len(track["clips"])}

    def track_contents(self, track_index, is_return=False):
        track = self._track(track_index, is_return)
        return {"devices": len(track["devices"]), "session_clips": len(track["clips"]), "arrangement_clips": 0}

    def reset_meters(self):
        self.meters_since = time.monotonic()
        return {"reset": True}

    def get_meters(self):
        ticks = int((time.monotonic() - self.meters_since) * 10)
        rows = []
        for kind, tracks in (("track", self.tracks), ("return", self.returns)):
            for i, t in enumerate(tracks):
                audio = not t["is_midi"] or bool(t["devices"])
                m = self._meter(t, ticks) if audio else {"peak": 0.0, "average": 0.0}
                rows.append({"kind": kind, "index": i, "name": t["name"], "has_audio_output": audio,
                             "peak": m["peak"], "average": m["average"], "samples": ticks})
        master = min(1.0, sum(r["peak"] for r in rows) * 0.6)
        rows.append({"kind": "master", "index": 0, "name": "Master", "has_audio_output": True,
                     "peak": master, "average": master * 0.7, "samples": ticks})
        return {"ticks": ticks, "tracks": rows}

    def import_audio(self, track_index, file_path, scene_index, name=None, gain_db=None):
        track = self._track(track_index)
        if track["is_midi"]:
            raise ValueError("audio clips go on audio tracks")
        if not 0 <= scene_index < len(self.scenes):
            raise IndexError("list index out of range")
        if scene_index in track["clips"]:
            raise ValueError("that slot already has a clip")
        if not os.path.isfile(file_path):
            raise OSError("no file at %s" % file_path)
        from app.audio_files import measure

        m = measure(file_path)
        loudness = 10 ** (m["loud_dbfs"] / 20) if m.get("loud_dbfs") is not None else 0.3
        seconds = m.get("seconds") or 180.0
        clip = {
            "name": name or os.path.splitext(os.path.basename(file_path))[0],
            "gain_db": float(gain_db) if gain_db is not None else 0.0,
            "length": seconds * self.tempo / 60.0,
            "loudness": loudness,
        }
        track["clips"][scene_index] = clip
        return {"name": clip["name"], "gain": _db_text(clip["gain_db"]), "length": clip["length"],
                "warping": False, "looping": False}

    def set_clip_gain(self, track_index, scene_index, db):
        clip = self._track(track_index)["clips"].get(scene_index)
        if clip is None:
            raise LookupError("there's no clip in that slot")
        clip["gain_db"] = max(-70.0, min(24.0, float(db)))
        return {"gain": _db_text(clip["gain_db"])}

    def _routing_options(self, track, direction):
        if direction == "output":
            return OUTPUTS
        return MIDI_INPUTS if track["is_midi"] else AUDIO_INPUTS

    def _routing_side(self, track, direction):
        options = self._routing_options(track, direction)
        current = track[direction]
        return {
            "type": current["type"],
            "channel": current["channel"],
            "types": list(options),
            "channels": list(options[current["type"]]),
        }

    def _scene_row(self, i):
        scene = self.scenes[i]
        return {"index": i, "name": scene["name"], "tempo": scene["tempo"]}

    # -- tracks ---------------------------------------------------------

    def ping(self):
        return "pong"

    def list_tracks(self):
        return [{"index": i, "name": t["name"], "is_midi": t["is_midi"]} for i, t in enumerate(self.tracks)]

    def list_returns(self):
        return [{"index": i, "name": t["name"]} for i, t in enumerate(self.returns)]

    def create_audio_track(self, name=None):
        self.tracks.append(self._new_track(name or "%d-Audio" % (len(self.tracks) + 1)))
        return {"index": len(self.tracks) - 1, "name": self.tracks[-1]["name"]}

    def create_midi_track(self, name=None):
        self.tracks.append(self._new_track(name or "%d-MIDI" % (len(self.tracks) + 1), is_midi=True))
        return {"index": len(self.tracks) - 1, "name": self.tracks[-1]["name"]}

    def create_return_track(self, name=None):
        letter = chr(ord("A") + len(self.returns))
        self.returns.append(self._new_track(name or "%s-Return" % letter, is_return=True))
        for t in self.tracks + self.returns:
            while len(t["sends"]) < len(self.returns):
                t["sends"].append(None)
        return {"index": len(self.returns) - 1, "name": self.returns[-1]["name"]}

    def set_track_name(self, track_index, name, is_return=False):
        track = self._track(track_index, is_return)
        track["name"] = name
        return {"index": track_index, "name": name}

    def delete_track(self, track_index, is_return=False):
        self._track(track_index, is_return)
        if is_return:
            del self.returns[track_index]
            for t in self.tracks + self.returns:
                del t["sends"][track_index]
        else:
            del self.tracks[track_index]
        return {"index": track_index}

    # -- routing --------------------------------------------------------

    def get_routing(self, track_index, is_return=False):
        track = self._track(track_index, is_return)
        routing = {"output": self._routing_side(track, "output")}
        if not is_return:
            routing["input"] = self._routing_side(track, "input")
        return routing

    def set_routing(self, track_index, direction, type_name, channel_name=None, is_return=False):
        if direction not in ("input", "output"):
            raise ValueError("direction must be input or output")
        track = self._track(track_index, is_return)
        options = self._routing_options(track, direction)
        kind = _match(options, type_name, direction)
        channel = options[kind][0]
        if channel_name:
            channel = _match(options[kind], channel_name, "%s channel" % direction)
        track[direction] = {"type": kind, "channel": channel}
        return self._routing_side(track, direction)

    # -- mixer ----------------------------------------------------------

    def get_mixer(self, track_index, is_return=False):
        strip = self._strip(track_index, self._track(track_index, is_return), is_return)
        return {k: strip[k] for k in ("volume", "pan", "mute", "solo")} | {
            "sends": [{"return": s["return"], "level": s["level"]} for s in strip["sends"]]
        }

    def set_volume(self, track_index, db, is_return=False):
        track = self._track(track_index, is_return)
        track["volume_db"] = _clamp_db(db)
        return {"volume": _db_text(track["volume_db"])}

    def set_pan(self, track_index, pan, is_return=False):
        track = self._track(track_index, is_return)
        track["pan"] = max(-1.0, min(1.0, float(pan)))
        return {"pan": _pan_text(track["pan"])}

    def set_mute(self, track_index, on, is_return=False):
        track = self._track(track_index, is_return)
        track["mute"] = bool(on)
        return {"mute": track["mute"]}

    def set_solo(self, track_index, on, is_return=False):
        track = self._track(track_index, is_return)
        track["solo"] = bool(on)
        return {"solo": track["solo"]}

    def set_send(self, track_index, return_index, db, is_return=False):
        track = self._track(track_index, is_return)
        track["sends"][return_index] = _clamp_db(db)
        return {"level": _db_text(track["sends"][return_index])}

    # -- devices --------------------------------------------------------

    def _device(self, device_name):
        for names in STOCK_DEVICES.values():
            for name in names:
                if name.casefold() == device_name.casefold():
                    return name
        raise LookupError("no stock device called %r" % device_name)

    def list_presets(self, device_name):
        return sorted(PRESETS.get(self._device(device_name), []))

    def load_device(self, track_index, device_name, preset=None, is_return=False):
        track = self._track(track_index, is_return)
        name = self._device(device_name)
        if preset:
            wanted = preset.casefold().removesuffix(".adv")
            if not any(p.casefold() == wanted for p in PRESETS.get(name, [])):
                raise LookupError("%s has no preset called %r" % (name, preset))
        time.sleep(0.3)  # Live walks its browser here; the real thing is slower.
        track["devices"].append(name)
        return {"track_index": track_index, "device": device_name, "preset": preset}

    def list_devices(self, track_index, is_return=False):
        track = self._track(track_index, is_return)
        return [
            {"index": i, "name": d, "class_name": d.replace(" ", ""), "is_rack": False}
            for i, d in enumerate(track["devices"])
        ]

    def delete_device(self, track_index, device_index, is_return=False):
        track = self._track(track_index, is_return)
        name = track["devices"].pop(device_index)
        return {"index": device_index, "name": name}

    def list_stock_devices(self):
        return {k: list(v) for k, v in STOCK_DEVICES.items()}

    # -- song -----------------------------------------------------------

    def get_song(self):
        return {
            "tempo": self.tempo,
            "is_playing": self.is_playing,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }

    def set_tempo(self, bpm):
        self.tempo = float(bpm)
        return {"tempo": self.tempo}

    def play(self):
        self.is_playing = True
        return {"is_playing": True}

    def stop(self):
        self.is_playing = False
        self.playing_scene = None
        return {"is_playing": False}

    def list_scenes(self):
        return [self._scene_row(i) for i in range(len(self.scenes))]

    def create_scene(self, name=None, bpm=None):
        self.scenes.append({"name": name or "", "tempo": float(bpm) if bpm is not None else None})
        return self._scene_row(len(self.scenes) - 1)

    def set_scene(self, scene_index, name=None, bpm=None):
        scene = self.scenes[scene_index]
        if name is not None:
            scene["name"] = name
        if bpm is not None:
            scene["tempo"] = float(bpm)
        return self._scene_row(scene_index)

    def count_scene_clips(self, scene_index):
        return sum(1 for t in self.tracks if scene_index in t["clips"])

    def delete_scene(self, scene_index):
        del self.scenes[scene_index]
        for t in self.tracks:
            t["clips"] = {(i - 1 if i > scene_index else i): c
                          for i, c in t["clips"].items() if i != scene_index}
        return {"index": scene_index}

    def fire_scene(self, scene_index):
        scene = self.scenes[scene_index]
        if scene["tempo"] is not None:
            self.tempo = scene["tempo"]
        self.is_playing = True
        self.playing_scene = scene_index
        return {"index": scene_index}

    def list_locators(self):
        cues = sorted(self.cues, key=lambda c: c["time"])
        return [{"index": i, "name": c["name"], "time": c["time"]} for i, c in enumerate(cues)]

    def add_locator(self, time, name=None):
        time = float(time)
        if any(abs(c["time"] - time) < 1e-6 for c in self.cues):
            raise ValueError("there's already a locator at that position")
        cue = {"name": name or "", "time": time}
        self.cues.append(cue)
        return dict(cue)

    def delete_locator(self, locator_index):
        cue = sorted(self.cues, key=lambda c: c["time"])[locator_index]
        self.cues.remove(cue)
        return {"name": cue["name"]}

    def jump_to_locator(self, locator_index):
        cue = sorted(self.cues, key=lambda c: c["time"])[locator_index]
        return dict(cue)

    def get_snapshot(self):
        tracks = [self._strip(i, t, False) for i, t in enumerate(self.tracks)]
        returns = [self._strip(i, t, True) for i, t in enumerate(self.returns)]
        master = min(1.0, sum((t["meter"] or {}).get("peak", 0) for t in tracks) * 0.6)
        return {
            "song": self.get_song(),
            "tracks": tracks,
            "returns": returns,
            "master": {"volume": _db_text(0.0), "volume_db": 0.0,
                       "meter": {"peak": master, "average": master * 0.7}},
            "scenes": self.list_scenes(),
            "locators": self.list_locators(),
        }

    # -- dispatch -------------------------------------------------------

    def handle(self, request):
        cmd = request.get("cmd")
        handler = getattr(self, cmd, None) if isinstance(cmd, str) and not cmd.startswith("_") else None
        if handler is None or cmd in ("handle", "lock", "tracks", "returns", "scenes", "cues"):
            return {"ok": False, "error": "unknown cmd: %s" % cmd}
        try:
            with self.lock:
                return {"ok": True, "result": handler(**request.get("args", {}))}
        except Exception as e:  # noqa: BLE001 -- mirror RigLink: errors become replies
            return {"ok": False, "error": str(e)}


def serve(host=HOST, port=PORT, latency=0.05):
    """Run a fake Live on a background thread. Returns (server, fake_set)."""
    fake = FakeSet()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                if not line.strip():
                    continue
                time.sleep(latency)  # Live answers from update_display, ~10 Hz.
                try:
                    reply = fake.handle(json.loads(line))
                except ValueError as e:
                    reply = {"ok": False, "error": "bad json: %s" % e}
                self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, fake


def main():
    server, _ = serve()
    print("Fake Live listening on %s:%d. Ctrl+C to stop." % server.server_address)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
