"""RigLink Control Surface.

Loaded by Live as a Control Surface (Preferences -> Link/MIDI). Opens a local
TCP socket and accepts newline-delimited JSON commands from runtime.py,
executing them against the currently open Live Set.

Command vocabulary matches RigSpec, not Live's full API surface: tracks,
routing, mixer, stock devices and presets, returns and sends, tempo, scenes,
locators, transport. Nothing here writes raw device parameters.

Volume and send levels are set in dB without a dB-to-value formula: we
binary-search the parameter's own str_for_value() display text, so Live's
fader law is the only source of truth for the conversion.

Tracks are addressed by index into song.tracks, or into song.return_tracks
when is_return is true.
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
        # Replies waiting to go out. The sockets are non-blocking, so a large
        # reply (a long preset list, a snapshot) may take several ticks to send.
        self._outgoing = {}
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
        for client in list(self._clients):
            self._drop_client(client)
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
        self._flush_clients()

    def _accept_new_clients(self):
        if self._server is None:
            return
        try:
            while True:
                client, _addr = self._server.accept()
                client.setblocking(False)
                self._clients.append(client)
                self._buffers[client] = b""
                self._outgoing[client] = b""
        except BlockingIOError:
            pass
        except OSError as e:
            self.log_message("RigLink: accept failed: %s" % e)

    def _service_clients(self):
        # Iterate over a copy: handling a line can drop the client.
        for client in list(self._clients):
            try:
                chunk = client.recv(4096)
            except BlockingIOError:
                continue
            except OSError:
                self._drop_client(client)
                continue

            if not chunk:
                self._drop_client(client)
                continue

            self._buffers[client] += chunk
            while client in self._buffers and b"\n" in self._buffers[client]:
                line, self._buffers[client] = self._buffers[client].split(b"\n", 1)
                if line.strip():
                    self._handle_line(client, line)

    def _flush_clients(self):
        for client in list(self._clients):
            self._flush(client)

    def _flush(self, client):
        pending = self._outgoing.get(client)
        if not pending:
            return
        try:
            sent = client.send(pending)
        except BlockingIOError:
            return
        except OSError:
            self._drop_client(client)
            return
        self._outgoing[client] = pending[sent:]

    def _drop_client(self, client):
        try:
            client.close()
        except OSError:
            pass
        if client in self._clients:
            self._clients.remove(client)
        self._buffers.pop(client, None)
        self._outgoing.pop(client, None)

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
        if client not in self._outgoing:
            return
        self._outgoing[client] += (json.dumps(payload) + "\n").encode("utf-8")
        self._flush(client)


# -- Commands --------------------------------------------------------------
# Each takes the RigLink instance plus keyword args from the request's
# "args" object, and returns a JSON-serializable result.


def _track(rf, track_index, is_return=False):
    song = rf.song()
    tracks = song.return_tracks if is_return else song.tracks
    return tracks[track_index]


def _display(param):
    return param.str_for_value(param.value)


def _by_display_name(options, wanted, noun):
    for option in options:
        if option.display_name.casefold() == wanted.casefold():
            return option
    names = ", ".join(o.display_name for o in options) or "none"
    raise LookupError("no %s called %r (options: %s)" % (noun, wanted, names))


# -- Tracks ----------------------------------------------------------------


def _ping(rf):
    return "pong"


def _list_tracks(rf):
    song = rf.song()
    return [
        {"index": i, "name": t.name, "is_midi": t.has_midi_input}
        for i, t in enumerate(song.tracks)
    ]


def _list_returns(rf):
    return [{"index": i, "name": t.name} for i, t in enumerate(rf.song().return_tracks)]


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


def _create_return_track(rf, name=None):
    song = rf.song()
    song.create_return_track()
    track = song.return_tracks[-1]
    if name:
        track.name = name
    return {"index": len(song.return_tracks) - 1, "name": track.name}


def _set_track_name(rf, track_index, name, is_return=False):
    track = _track(rf, track_index, is_return)
    track.name = name
    return {"index": track_index, "name": track.name}


def _delete_track(rf, track_index, is_return=False):
    song = rf.song()
    if is_return:
        song.delete_return_track(track_index)
    else:
        song.delete_track(track_index)
    return {"index": track_index}


# -- Routing ---------------------------------------------------------------
# Since Live 10, routing is set by assigning one of the objects from the
# available_*_routing_types / _channels vectors, never by string.


def _routing_side(track, direction):
    return {
        "type": getattr(track, "%s_routing_type" % direction).display_name,
        "channel": getattr(track, "%s_routing_channel" % direction).display_name,
        "types": [t.display_name for t in getattr(track, "available_%s_routing_types" % direction)],
        "channels": [c.display_name for c in getattr(track, "available_%s_routing_channels" % direction)],
    }


def _get_routing(rf, track_index, is_return=False):
    track = _track(rf, track_index, is_return)
    routing = {"output": _routing_side(track, "output")}
    if not is_return:
        routing["input"] = _routing_side(track, "input")
    return routing


def _set_routing(rf, track_index, direction, type_name, channel_name=None, is_return=False):
    if direction not in ("input", "output"):
        raise ValueError("direction must be input or output")
    track = _track(rf, track_index, is_return)
    type_attr = "%s_routing_type" % direction
    channel_attr = "%s_routing_channel" % direction
    types = getattr(track, "available_%s_routing_types" % direction)
    chosen_type = _by_display_name(types, type_name, direction)

    # The channel list depends on the type, so it can only be checked after
    # switching. Put the old routing back if the channel doesn't exist, so a
    # failed command changes nothing.
    previous_type = getattr(track, type_attr)
    previous_channel = getattr(track, channel_attr)
    setattr(track, type_attr, chosen_type)
    if channel_name:
        channels = getattr(track, "available_%s_routing_channels" % direction)
        try:
            chosen_channel = _by_display_name(channels, channel_name, "%s channel" % direction)
        except LookupError:
            setattr(track, type_attr, previous_type)
            setattr(track, channel_attr, previous_channel)
            raise
        setattr(track, channel_attr, chosen_channel)
    return _routing_side(track, direction)


# -- Mixer -----------------------------------------------------------------


def _parse_db(text):
    # Live may use a Unicode minus; "-inf dB" parses to float("-inf").
    return float(text.replace("−", "-").split()[0])


def _set_db(param, db):
    """Set a dB-displayed parameter by searching its own display strings."""
    lo, hi = param.min, param.max
    if db <= _parse_db(param.str_for_value(lo)):
        param.value = lo
        return _display(param)
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if _parse_db(param.str_for_value(mid)) < db:
            lo = mid
        else:
            hi = mid
    param.value = hi
    return _display(param)


def _get_mixer(rf, track_index, is_return=False):
    song = rf.song()
    track = _track(rf, track_index, is_return)
    mixer = track.mixer_device
    return {
        "volume": _display(mixer.volume),
        "pan": _display(mixer.panning),
        "mute": track.mute,
        "solo": track.solo,
        "sends": [
            {"return": r.name, "level": _display(s)}
            for r, s in zip(song.return_tracks, mixer.sends)
        ],
    }


def _set_volume(rf, track_index, db, is_return=False):
    track = _track(rf, track_index, is_return)
    return {"volume": _set_db(track.mixer_device.volume, float(db))}


def _set_pan(rf, track_index, pan, is_return=False):
    param = _track(rf, track_index, is_return).mixer_device.panning
    param.value = max(param.min, min(param.max, float(pan)))
    return {"pan": _display(param)}


def _set_mute(rf, track_index, on, is_return=False):
    track = _track(rf, track_index, is_return)
    track.mute = bool(on)
    return {"mute": track.mute}


def _set_solo(rf, track_index, on, is_return=False):
    track = _track(rf, track_index, is_return)
    track.solo = bool(on)
    return {"solo": track.solo}


def _set_send(rf, track_index, return_index, db, is_return=False):
    sends = _track(rf, track_index, is_return).mixer_device.sends
    return {"level": _set_db(sends[return_index], float(db))}


# -- Devices and presets ---------------------------------------------------
# Only effect and instrument folders are searched. Sounds and Drums are huge
# and hold presets that share names with devices ("Reverb"), so they're out.

DEVICE_CATEGORIES = ("audio_effects", "midi_effects", "instruments")
DEVICE_SEARCH_DEPTH = 2


def _find_device_item(browser, device_name):
    """Breadth-first, shallow search for the stock device itself (not a preset)."""
    wanted = device_name.casefold()
    for category in DEVICE_CATEGORIES:
        level = list(getattr(browser, category).children)
        for _ in range(DEVICE_SEARCH_DEPTH):
            for item in level:
                if item.is_device and item.name.casefold() == wanted:
                    return item
            level = [c for item in level if item.is_folder for c in item.children]
    raise LookupError("no stock device called %r" % device_name)


def _presets_under(item):
    stack = list(item.children)
    while stack:
        child = stack.pop()
        if child.is_loadable and not child.is_folder:
            yield child
        stack.extend(child.children)


def _list_presets(rf, device_name):
    device = _find_device_item(rf.application().browser, device_name)
    return sorted(p.name for p in _presets_under(device))


def _load_device(rf, track_index, device_name, preset=None, is_return=False):
    song = rf.song()
    song.view.selected_track = _track(rf, track_index, is_return)

    browser = rf.application().browser
    item = _find_device_item(browser, device_name)
    if preset:
        wanted = preset.casefold()
        if wanted.endswith(".adv"):
            wanted = wanted[: -len(".adv")]
        matches = [p for p in _presets_under(item) if p.name.casefold() == wanted]
        if not matches:
            raise LookupError("%s has no preset called %r" % (item.name, preset))
        item = matches[0]

    browser.load_item(item)
    return {"track_index": track_index, "device": device_name, "preset": preset}


def _list_devices(rf, track_index, is_return=False):
    track = _track(rf, track_index, is_return)
    return [
        {"index": i, "name": d.name, "class_name": d.class_name, "is_rack": d.can_have_chains}
        for i, d in enumerate(track.devices)
    ]


def _delete_device(rf, track_index, device_index, is_return=False):
    track = _track(rf, track_index, is_return)
    name = track.devices[device_index].name
    track.delete_device(device_index)
    return {"index": device_index, "name": name}


# -- Song: tempo, transport, scenes, locators ------------------------------


def _get_song(rf):
    song = rf.song()
    return {
        "tempo": song.tempo,
        "is_playing": song.is_playing,
        "numerator": song.signature_numerator,
        "denominator": song.signature_denominator,
    }


def _set_tempo(rf, bpm):
    song = rf.song()
    song.tempo = float(bpm)
    return {"tempo": song.tempo}


def _play(rf):
    rf.song().start_playing()
    return {"is_playing": True}


def _stop(rf):
    rf.song().stop_playing()
    return {"is_playing": False}


def _scene_row(i, scene):
    row = {"index": i, "name": scene.name, "tempo": None}
    # Scene.tempo arrived in Live 11; tolerate its absence.
    if getattr(scene, "tempo_enabled", False):
        row["tempo"] = scene.tempo
    return row


def _list_scenes(rf):
    return [_scene_row(i, s) for i, s in enumerate(rf.song().scenes)]


def _create_scene(rf, name=None, bpm=None):
    song = rf.song()
    song.create_scene(-1)
    index = len(song.scenes) - 1
    scene = song.scenes[index]
    if name:
        scene.name = name
    if bpm is not None:
        scene.tempo = float(bpm)
        scene.tempo_enabled = True
    return _scene_row(index, scene)


def _set_scene(rf, scene_index, name=None, bpm=None):
    scene = rf.song().scenes[scene_index]
    if name is not None:
        scene.name = name
    if bpm is not None:
        scene.tempo = float(bpm)
        scene.tempo_enabled = True
    return _scene_row(scene_index, scene)


def _delete_scene(rf, scene_index):
    rf.song().delete_scene(scene_index)
    return {"index": scene_index}


def _fire_scene(rf, scene_index):
    rf.song().scenes[scene_index].fire()
    return {"index": scene_index}


def _cues_by_time(song):
    return sorted(song.cue_points, key=lambda c: c.time)


def _list_locators(rf):
    return [
        {"index": i, "name": c.name, "time": c.time}
        for i, c in enumerate(_cues_by_time(rf.song()))
    ]


def _add_locator(rf, time, name=None):
    """Add a locator at `time` beats. set_or_delete_cue toggles at the playhead."""
    song = rf.song()
    time = float(time)
    if any(abs(c.time - time) < 1e-6 for c in song.cue_points):
        raise ValueError("there's already a locator at that position")
    song.current_song_time = time
    song.set_or_delete_cue()
    for c in song.cue_points:
        if abs(c.time - time) < 1e-6:
            if name:
                c.name = name
            return {"name": c.name, "time": c.time}
    raise LookupError("Live didn't create the locator")


def _delete_locator(rf, locator_index):
    song = rf.song()
    cue = _cues_by_time(song)[locator_index]
    name = cue.name
    song.current_song_time = cue.time
    song.set_or_delete_cue()
    return {"name": name}


def _jump_to_locator(rf, locator_index):
    cue = _cues_by_time(rf.song())[locator_index]
    cue.jump()
    return {"name": cue.name, "time": cue.time}


# -- Snapshot --------------------------------------------------------------
# Everything a UI needs to draw the set, in one round trip. Live only services
# the socket from update_display, so a dozen small calls cost a second or more.


def _db_value(param):
    """A dB display as a number, or None for -inf (JSON has no infinity)."""
    try:
        db = _parse_db(_display(param))
    except ValueError:
        return None
    return None if db == float("-inf") else db


def _routing_now(track, direction):
    kind = getattr(track, "%s_routing_type" % direction).display_name
    channel = getattr(track, "%s_routing_channel" % direction).display_name
    return {"type": kind, "channel": channel}


def _strip(song, i, track, is_return):
    mixer = track.mixer_device
    row = {
        "index": i,
        "name": track.name,
        "is_return": is_return,
        "volume": _display(mixer.volume),
        "volume_db": _db_value(mixer.volume),
        "pan": _display(mixer.panning),
        "pan_value": mixer.panning.value,
        "mute": track.mute,
        "solo": track.solo,
        "sends": [
            {"return": r.name, "level": _display(s), "level_db": _db_value(s)}
            for r, s in zip(song.return_tracks, mixer.sends)
        ],
        "devices": [d.name for d in track.devices],
        "output": _routing_now(track, "output"),
    }
    if not is_return:
        row["is_midi"] = track.has_midi_input
        row["input"] = _routing_now(track, "input")
    return row


def _get_snapshot(rf):
    song = rf.song()
    return {
        "song": _get_song(rf),
        "tracks": [_strip(song, i, t, False) for i, t in enumerate(song.tracks)],
        "returns": [_strip(song, i, t, True) for i, t in enumerate(song.return_tracks)],
        "master": {
            "volume": _display(song.master_track.mixer_device.volume),
            "volume_db": _db_value(song.master_track.mixer_device.volume),
        },
        "scenes": _list_scenes(rf),
        "locators": _list_locators(rf),
    }


def _list_stock_devices(rf):
    """Stock device names by browser category, as this copy of Live has them."""
    browser = rf.application().browser
    found = {}
    for category in DEVICE_CATEGORIES:
        names = []
        level = list(getattr(browser, category).children)
        for _ in range(DEVICE_SEARCH_DEPTH):
            names.extend(item.name for item in level if item.is_device)
            level = [c for item in level if item.is_folder for c in item.children]
        found[category] = sorted(set(names))
    return found


COMMANDS = {
    "ping": _ping,
    "list_tracks": _list_tracks,
    "list_returns": _list_returns,
    "create_audio_track": _create_audio_track,
    "create_midi_track": _create_midi_track,
    "create_return_track": _create_return_track,
    "set_track_name": _set_track_name,
    "delete_track": _delete_track,
    "get_routing": _get_routing,
    "set_routing": _set_routing,
    "get_mixer": _get_mixer,
    "set_volume": _set_volume,
    "set_pan": _set_pan,
    "set_mute": _set_mute,
    "set_solo": _set_solo,
    "set_send": _set_send,
    "list_presets": _list_presets,
    "load_device": _load_device,
    "list_devices": _list_devices,
    "delete_device": _delete_device,
    "get_song": _get_song,
    "set_tempo": _set_tempo,
    "play": _play,
    "stop": _stop,
    "list_scenes": _list_scenes,
    "create_scene": _create_scene,
    "set_scene": _set_scene,
    "delete_scene": _delete_scene,
    "fire_scene": _fire_scene,
    "list_locators": _list_locators,
    "add_locator": _add_locator,
    "delete_locator": _delete_locator,
    "jump_to_locator": _jump_to_locator,
    "get_snapshot": _get_snapshot,
    "list_stock_devices": _list_stock_devices,
}
