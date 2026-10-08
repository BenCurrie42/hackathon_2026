"""RigLink Control Surface.

Loaded by Live as a Control Surface (Preferences -> Link/MIDI). Opens a local
TCP socket and accepts newline-delimited JSON commands from live_connection.py,
executing them against the currently open Live Set.

Command vocabulary matches RigSpec, not Live's full API surface: tracks,
routing, mixer, stock devices and presets, returns and sends, tempo, scenes,
locators, transport, output meters. No raw device parameters: the one device
setting exposed is EQ Eight's bands, through named controls.

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
        self._meters = MeterWindow()
        # A second window for the UI's meters: each snapshot reads and clears
        # it, so drawing meters never resets a measurement in progress.
        self._display_meters = MeterWindow()
        # And one for the UI's fast meter feed, read and cleared many times a second.
        self._live_meters = MeterWindow()
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
        layout, levels = _read_levels(self.song())
        self._meters.add(layout, levels)
        self._display_meters.add(layout, levels)
        self._live_meters.add(layout, levels)
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


# -- Meters ----------------------------------------------------------------
# The Live API exposes each track's output meter (0.0-1.0, post-fader), not
# audio. Sampled once per update_display tick and folded into peak/average
# since the last reset, so a read never blocks Live's thread waiting for audio.
# How the 0-1 meter value maps to dBFS is unverified.


def _metered_tracks(song):
    yield "track", song.tracks
    yield "return", song.return_tracks
    yield "master", [song.master_track]


def _read_levels(song):
    """Every audio track's meter right now. Read once per tick: meters cost Live CPU."""
    levels = {}
    for kind, tracks in _metered_tracks(song):
        for i, track in enumerate(tracks):
            if track.has_audio_output:
                levels[(kind, i)] = max(track.output_meter_left, track.output_meter_right)
    return (len(song.tracks), len(song.return_tracks)), levels


class MeterWindow:
    def __init__(self):
        self.reset()

    def reset(self):
        self._stats = {}
        self._layout = None
        self.ticks = 0

    def add(self, layout, levels):
        if layout != self._layout:
            # Indices shifted under us; old stats would land on the wrong track.
            self.reset()
            self._layout = layout
        self.ticks += 1
        for key, level in levels.items():
            peak, total, count = self._stats.get(key, (0.0, 0.0, 0))
            self._stats[key] = (max(peak, level), total + level, count + 1)

    def stats(self, kind, i):
        """(peak, average) for one track since the last reset, or None if unheard."""
        peak, total, count = self._stats.get((kind, i), (0.0, 0.0, 0))
        return (peak, total / count) if count else None

    def rows(self, song):
        rows = []
        for kind, tracks in _metered_tracks(song):
            for i, track in enumerate(tracks):
                peak, total, count = self._stats.get((kind, i), (0.0, 0.0, 0))
                rows.append({
                    "kind": kind,
                    "index": i,
                    "name": track.name,
                    "has_audio_output": track.has_audio_output,
                    "peak": peak,
                    "average": total / count if count else 0.0,
                    "samples": count,
                })
        return rows


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
    """A new audio track with No Input.

    Live gives a new track input 1, so a playback track would pick up whatever is
    plugged in there as soon as it's armed or monitoring. Live sources get their
    input set explicitly afterwards.
    """
    song = rf.song()
    song.create_audio_track(-1)
    track = song.tracks[-1]
    if name:
        track.name = name
    try:
        track.input_routing_type = _by_display_name(track.available_input_routing_types, "No Input", "input")
    except LookupError:
        pass  # a Live without "No Input" keeps its default rather than failing the add
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


def _track_clips(track):
    session = [s.clip for s in track.clip_slots if s.has_clip]
    return session + list(getattr(track, "arrangement_clips", ()))


def _set_track_color(rf, track_index, rgb, is_return=False):
    """Colour a track and every clip on it.

    Clips carry their own colour, fixed when they're created, so recolouring
    only the track leaves older clips in whatever colour the track had then.
    Live snaps an RGB value to the nearest colour in its palette.
    """
    track = _track(rf, track_index, is_return)
    track.color = int(rgb)
    clips = [] if is_return else _track_clips(track)
    for clip in clips:
        clip.color = track.color
    return {"index": track_index, "color": track.color, "clips": len(clips)}


def _track_contents(rf, track_index, is_return=False):
    track = _track(rf, track_index, is_return)
    return {
        "devices": len(track.devices),
        "session_clips": sum(1 for s in track.clip_slots if s.has_clip),
        "arrangement_clips": len(getattr(track, "arrangement_clips", ())),
    }


def _api_names(rf):
    """Public attribute names on Song and Track, for checking what this Live exposes."""
    public = lambda obj: sorted(n for n in dir(obj) if not n.startswith("_"))
    song = rf.song()
    return {"song": public(song), "track": public(song.tracks[0]) if song.tracks else []}


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
    return _set_by_display(param, db, _parse_db)


def _set_by_display(param, wanted, parse):
    """Set a parameter whose display rises with its value, by searching its display strings."""
    lo, hi = param.min, param.max
    if wanted <= parse(param.str_for_value(lo)):
        param.value = lo
        return _display(param)
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if parse(param.str_for_value(mid)) < wanted:
            lo = mid
        else:
            hi = mid
    param.value = hi
    return _display(param)


def _parse_number(text):
    """A display like "1.20 kHz", "250 Hz", "0.71" or "-3.5 dB" as a number (kHz in Hz)."""
    text = text.replace("−", "-").strip()
    number = float(text.split()[0].rstrip("kK"))
    return number * 1000 if "k" in text.lower() else number


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


def _preset_key(name):
    name = name.casefold()
    return name[: -len(".adv")] if name.endswith(".adv") else name


def _list_presets(rf, device_name):
    device = _find_device_item(rf.application().browser, device_name)
    return sorted(p.name for p in _presets_under(device))


def _load_device(rf, track_index, device_name, preset=None, is_return=False):
    song = rf.song()
    song.view.selected_track = _track(rf, track_index, is_return)

    browser = rf.application().browser
    item = _find_device_item(browser, device_name)
    if preset:
        # Browser items are named with the file extension ("Gentle Squeeze.adv").
        wanted = _preset_key(preset)
        matches = [p for p in _presets_under(item) if _preset_key(p.name) == wanted]
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


def _device_parameters(rf, track_index, device_index, is_return=False):
    """Every parameter of one device as Live names and shows it. For probing, not for the app."""
    device = _track(rf, track_index, is_return).devices[device_index]
    return {
        "name": device.name,
        "class_name": device.class_name,
        "parameters": [
            {
                "name": p.name, "value": p.value, "min": p.min, "max": p.max, "display": _display(p),
                "is_quantized": p.is_quantized, "items": list(p.value_items) if p.is_quantized else [],
            }
            for p in device.parameters
        ],
    }


# -- EQ Eight ----------------------------------------------------------------
# The one device whose settings can be changed, and only through these named
# controls. Frequency, gain and Q are set by searching each parameter's own
# display text, like volume, so Live does every conversion. Filter types are
# Live's own names for them (value_items), passed back by index.

EQ_CLASS = "Eq8"
EQ_BANDS = 8


def _eq_device(track, device_index=None):
    if device_index is not None:
        device = track.devices[device_index]
        if device.class_name != EQ_CLASS:
            raise LookupError("%s isn't an EQ Eight" % device.name)
        return device_index, device
    for i, device in enumerate(track.devices):
        if device.class_name == EQ_CLASS:
            return i, device
    return None, None


def _eq_params(device):
    return {p.name: p for p in device.parameters}


def _eq_band_params(params, band):
    # Live 12.4.6 names them "1 Filter On A", "1 Filter Type A", "1 Frequency A",
    # "1 Gain A", "1 Q A"; A is the curve EQ Eight uses in Stereo mode (B is the
    # second curve in L/R and M/S modes).
    names = {"on": "Filter On", "type": "Filter Type", "freq": "Frequency", "gain": "Gain", "q": "Q"}
    found = {}
    for key, word in names.items():
        param = params.get("%d %s A" % (band, word))
        if param is None:
            raise LookupError("EQ Eight has no %r for band %d" % (word, band))
        found[key] = param
    return found


def _eq_state(track, device_index=None):
    """A track's EQ Eight (the first one, unless an index is given) as numbers and Live's words."""
    index, device = _eq_device(track, device_index)
    if device is None:
        return None
    params = _eq_params(device)
    bands = []
    types = []
    for band in range(1, EQ_BANDS + 1):
        p = _eq_band_params(params, band)
        types = list(p["type"].value_items)
        bands.append({
            "band": band,
            "on": bool(p["on"].value),
            "type_index": int(p["type"].value),
            "type": _display(p["type"]),
            "freq_hz": _parse_number(_display(p["freq"])),
            "gain_db": _parse_number(_display(p["gain"])),
            "q": _parse_number(_display(p["q"])),
        })
    on = params.get("Device On")
    return {"device_index": index, "on": bool(on.value) if on is not None else True,
            "types": types, "bands": bands}


def _snapshot_eq(track):
    """The EQ for the snapshot; a track whose EQ can't be read just shows none."""
    try:
        return _eq_state(track)
    except (LookupError, ValueError, IndexError):
        return None


def _get_eq(rf, track_index, is_return=False, device_index=None):
    state = _eq_state(_track(rf, track_index, is_return), device_index)
    if state is None:
        raise LookupError("that track has no EQ Eight")
    return state


def _set_eq_band(rf, track_index, band, is_return=False, device_index=None,
                 on=None, type_index=None, freq_hz=None, gain_db=None, q=None):
    track = _track(rf, track_index, is_return)
    index, device = _eq_device(track, device_index)
    if device is None:
        raise LookupError("that track has no EQ Eight")
    p = _eq_band_params(_eq_params(device), int(band))
    if type_index is not None:
        p["type"].value = max(p["type"].min, min(p["type"].max, int(type_index)))
    if freq_hz is not None:
        _set_by_display(p["freq"], float(freq_hz), _parse_number)
    if gain_db is not None:
        _set_by_display(p["gain"], float(gain_db), _parse_number)
    if q is not None:
        _set_by_display(p["q"], float(q), _parse_number)
    if on is not None:
        p["on"].value = 1 if on else 0
    return _eq_state(track, index)["bands"][int(band) - 1]


# -- Meters ----------------------------------------------------------------


def _reset_meters(rf):
    rf._meters.reset()
    return {"reset": True}


def _get_meters(rf):
    return {"ticks": rf._meters.ticks, "tracks": rf._meters.rows(rf.song())}


def _get_live_meters(rf):
    """Each meter's peak since the last call, for the UI. Kept small: polled several times a second.

    Live samples meters once per update_display tick, so a call between ticks
    has nothing new and says so rather than reporting silence.
    """
    window = rf._live_meters
    if not window.ticks:
        return {"fresh": False}
    song = rf.song()

    def peaks(kind, tracks):
        return [(window.stats(kind, i) or (0.0, 0.0))[0] if t.has_audio_output else None
                for i, t in enumerate(tracks)]

    meters = {
        "fresh": True,
        "tracks": peaks("track", song.tracks),
        "returns": peaks("return", song.return_tracks),
        "master": (window.stats("master", 0) or (0.0, 0.0))[0],
    }
    window.reset()
    return meters


# -- Audio clips -----------------------------------------------------------


def _set_clip_gain_db(clip, db):
    """Clip gain is 0-1 with no public dB formula; search its own display text."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        clip.gain = (lo + hi) / 2.0
        if _parse_db(clip.gain_display_string) < db:
            lo = clip.gain
        else:
            hi = clip.gain
    clip.gain = hi
    return clip.gain_display_string


def _set_clip_gain(rf, track_index, scene_index, db):
    clip = _track(rf, track_index).clip_slots[scene_index].clip
    if clip is None:
        raise LookupError("there's no clip in that slot")
    return {"gain": _set_clip_gain_db(clip, float(db))}


def _play_from_file_start(clip):
    """Start every stem at sample 0.

    Auto-Warp moves a long sample's start to the first beat it detects, and that
    guess differs per stem, so turning warping off alone leaves stems offset.
    """
    clip.loop_start = 0.0
    clip.start_marker = 0.0


def _delete_clip(rf, track_index, scene_index):
    """Empty one song's slot on a track. Undo in Live with Cmd+Z."""
    slot = _track(rf, track_index).clip_slots[scene_index]
    if not slot.has_clip:
        raise LookupError("there's no clip in that slot")
    slot.delete_clip()
    return {"deleted": True}


def _set_clip_active(rf, track_index, scene_index, on):
    """Turn one song's clip on or off (Live's clip activator), saved in the set."""
    clip = _track(rf, track_index).clip_slots[scene_index].clip
    if clip is None:
        raise LookupError("there's no clip in that slot")
    clip.muted = not bool(on)
    return {"active": not clip.muted}


def _clip_markers(rf, scene_index):
    """Where each clip in a scene starts and ends, for checking stems line up."""
    rows = []
    for i, track in enumerate(rf.song().tracks):
        slots = track.clip_slots
        if scene_index >= len(slots) or not slots[scene_index].has_clip:
            continue
        clip = slots[scene_index].clip
        row = {"index": i, "name": track.name}
        for attr in ("start_marker", "end_marker", "loop_start", "loop_end", "length",
                     "warping", "looping", "sample_length", "sample_rate"):
            row[attr] = getattr(clip, attr, None)
        markers = getattr(clip, "warp_markers", None)
        if markers is not None:
            row["warp_markers"] = [[m.sample_time, m.beat_time] for m in markers][:4]
        rows.append(row)
    return rows


def _import_audio(rf, track_index, file_path, scene_index, name=None, gain_db=None):
    """Put an audio file in a Session slot, unwarped and playing once.

    Unwarped so stems from one session stay sample-locked to each other
    instead of each being stretched by Live's own tempo guess.
    """
    track = _track(rf, track_index)
    slot = track.clip_slots[scene_index]
    if slot.has_clip:
        raise ValueError("that slot already has a clip")
    if not hasattr(slot, "create_audio_clip"):
        raise NotImplementedError("this version of Live can't import audio from a script")
    slot.create_audio_clip(file_path)
    clip = slot.clip
    clip.warping = False
    clip.looping = False
    _play_from_file_start(clip)
    clip.color = track.color
    if name:
        clip.name = name
    if gain_db is not None:
        _set_clip_gain_db(clip, float(gain_db))
    return {
        "name": clip.name,
        "gain": clip.gain_display_string,
        "length": clip.length,
        "warping": clip.warping,
        "looping": clip.looping,
    }


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


def _audio_clips(scene):
    return [slot.clip for slot in scene.clip_slots if slot.has_clip and slot.clip.is_audio_clip]


def _song_transpose(scene):
    """Semitones the song is shifted: what every shifted clip agrees on, 0 if none are.

    Tracks that keep their key (click, guide) stay at 0, so they don't count.
    None when shifted clips disagree.
    """
    pitches = {clip.pitch_coarse for clip in _audio_clips(scene)} - {0}
    if not pitches:
        return 0
    return pitches.pop() if len(pitches) == 1 else None


def _scene_row(i, scene):
    row = {"index": i, "name": scene.name, "tempo": None, "transpose": _song_transpose(scene)}
    # Scene.tempo arrived in Live 11; tolerate its absence.
    if getattr(scene, "tempo_enabled", False):
        row["tempo"] = scene.tempo
    return row


def _list_scenes(rf):
    return [_scene_row(i, s) for i, s in enumerate(rf.song().scenes)]


def _create_scene(rf, name=None, bpm=None, index=None):
    """A new song at the end, or at a slot (0-based) pushing the rest down."""
    song = rf.song()
    if index is None or not 0 <= int(index) < len(song.scenes):
        song.create_scene(-1)
        index = len(song.scenes) - 1
    else:
        index = int(index)
        song.create_scene(index)
    scene = song.scenes[index]
    if name:
        scene.name = name
    if bpm is not None:
        scene.tempo = float(bpm)
        scene.tempo_enabled = True
    return _scene_row(index, scene)


def _move_scene(rf, scene_index, to_index):
    """Move a song (scene) and its clips to another slot (0-based).

    Live has no call to move a scene, so: insert an empty scene where it should
    end up, copy each clip into it, copy the name, tempo and colour, then delete
    the original. Undoable in Live, one step per clip.
    """
    song = rf.song()
    count = len(song.scenes)
    scene_index, to_index = int(scene_index), int(to_index)
    if not (0 <= scene_index < count and 0 <= to_index < count):
        raise IndexError("there are %d song slots" % count)
    if scene_index == to_index:
        return dict(_scene_row(to_index, song.scenes[to_index]), clips=0)
    # Insert so that, once the original is deleted, the copy sits at to_index.
    insert_at = to_index + 1 if to_index > scene_index else to_index
    song.create_scene(insert_at)
    old = scene_index + 1 if insert_at <= scene_index else scene_index
    source, target = song.scenes[old], song.scenes[insert_at]
    target.name = source.name
    if getattr(source, "tempo_enabled", False):
        target.tempo = source.tempo
        target.tempo_enabled = True
    target.color = source.color
    copied = 0
    for track in song.tracks:
        slot = track.clip_slots[old]
        if slot.has_clip:
            slot.duplicate_clip_to(track.clip_slots[insert_at])
            copied += 1
    song.delete_scene(old)
    return dict(_scene_row(to_index, song.scenes[to_index]), clips=copied)


def _set_scene(rf, scene_index, name=None, bpm=None):
    scene = rf.song().scenes[scene_index]
    if name is not None:
        scene.name = name
    if bpm is not None:
        scene.tempo = float(bpm)
        scene.tempo_enabled = True
    return _scene_row(scene_index, scene)


# Live's Clip.warp_mode numbering; 6 is Complex Pro. Read back after setting, so a
# different numbering shows up in the result instead of passing silently.
WARP_COMPLEX_PRO = 6


def _new_warp_marker(sample_time, beat_time):
    """The ways Live has been known to take a new warp marker, most likely first."""
    import Live

    kind = Live.Clip.WarpMarker
    yield "WarpMarker(sample_time=, beat_time=)", lambda: kind(sample_time=sample_time, beat_time=beat_time)
    yield "WarpMarker(beat_time, sample_time)", lambda: kind(beat_time, sample_time)
    yield "dict", lambda: {"sample_time": sample_time, "beat_time": beat_time}


def _lock_to_tempo(clip, bpm):
    """Warp a clip so it still plays at its own speed: file second s at beat s * bpm / 60.

    Live warps a long sample at its own tempo guess, set by a "shadow" marker that
    can't be moved. So keep the first marker, pin it to the file start, and add one
    real marker on that line; the tempo between them carries on to the end.
    Returns which form of new marker Live accepted.
    """
    beats_per_second = bpm / 60.0
    markers = list(clip.warp_markers)
    for m in reversed(markers[1:]):
        try:
            clip.remove_warp_marker(m.beat_time)
        except Exception:
            pass  # the shadow marker isn't a real one and can't be removed either
    first = list(clip.warp_markers)[0]
    if abs(first.beat_time - first.sample_time * beats_per_second) > 1e-6:
        clip.move_warp_marker(first.beat_time, first.sample_time * beats_per_second - first.beat_time)
    seconds = clip.sample_length / float(clip.sample_rate)
    sample_time = seconds / 2.0
    tried = []
    for label, build in _new_warp_marker(sample_time, sample_time * beats_per_second):
        try:
            clip.add_warp_marker(build())
            return label
        except Exception as e:
            tried.append("%s: %s" % (label, e))
    raise ValueError("couldn't add a warp marker (" + "; ".join(tried) + ")")


def _warp_report(clip, bpm):
    markers = [[round(m.sample_time, 4), round(m.beat_time, 4)] for m in clip.warp_markers]
    return {
        "warping": clip.warping,
        "warp_mode": getattr(clip, "warp_mode", None),
        "pitch": clip.pitch_coarse,
        "length": clip.length,
        "seconds": clip.sample_length / float(clip.sample_rate),
        "expected_beats": clip.sample_length / float(clip.sample_rate) * bpm / 60.0,
        "markers": markers[:2] + markers[-2:] if len(markers) > 4 else markers,
    }


def _transpose_song(rf, scene_index, semitones, skip_tracks=None):
    """Shift every audio clip in a song to a new key (-12 to 12) without changing its speed.

    Transposing an unwarped clip speeds it up like tape, so a transposed clip is warped
    (Complex Pro) and pinned to play at its own speed at the song's tempo. Back at the
    original key it goes back to unwarped and sample-locked. Tracks in skip_tracks
    (by index: click, guide) are put back at their original key.
    """
    semitones = int(semitones)
    if not -12 <= semitones <= 12:
        raise ValueError("transpose is -12 to 12 semitones")
    song = rf.song()
    scene = song.scenes[scene_index]
    bpm = scene.tempo if getattr(scene, "tempo_enabled", False) else song.tempo
    skip = set(skip_tracks or [])
    clips, report = [], []
    for i, track in enumerate(song.tracks):
        slot = track.clip_slots[scene_index]
        if not (slot.has_clip and slot.clip.is_audio_clip):
            continue
        clip = slot.clip
        shift = 0 if i in skip else semitones
        if i not in skip:
            clips.append(clip)
        problems = []
        if shift:
            clip.warping = True
            try:
                clip.warp_mode = WARP_COMPLEX_PRO
            except Exception as e:  # Live raises its own types; report rather than stop
                problems.append("warp mode: %s" % e)
            try:
                row_lock = _lock_to_tempo(clip, bpm)
                end = clip.sample_length / float(clip.sample_rate) * bpm / 60.0
                clip.end_marker = end
                clip.loop_end = end
            except Exception as e:
                row_lock = None
                problems.append("warp markers: %s" % e)
        elif clip.warping:
            clip.warping = False
            # Unwarped markers are in seconds; don't keep the warped end in beats.
            seconds = clip.sample_length / float(clip.sample_rate)
            clip.end_marker = seconds
            clip.loop_end = seconds
        clip.pitch_coarse = shift
        _play_from_file_start(clip)
        row = _warp_report(clip, bpm)
        row["track"] = track.name
        row["problems"] = problems
        if shift:
            row["marker_form"] = row_lock
        report.append(row)
    return dict(_scene_row(scene_index, scene), clips=len(clips), kept=len(report) - len(clips),
                bpm=bpm, report=report)


def _count_scene_clips(rf, scene_index):
    return sum(1 for t in rf.song().tracks if t.clip_slots[scene_index].has_clip)


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
    return _db_from_text(_display(param))


def _db_from_text(text):
    try:
        db = _parse_db(text)
    except (ValueError, IndexError):
        return None
    return None if db == float("-inf") else db


def _clip_rows(track):
    """Session clips on a track, by the scene (song) they sit in."""
    rows = []
    for i, slot in enumerate(track.clip_slots):
        if not slot.has_clip:
            continue
        clip = slot.clip
        row = {
            "scene_index": i,
            "name": clip.name,
            "is_audio": clip.is_audio_clip,
            "is_playing": clip.is_playing,
            "length": clip.length,
        }
        if clip.is_audio_clip:
            row["gain"] = clip.gain_display_string
            row["gain_db"] = _db_from_text(clip.gain_display_string)
        # Live's clip activator: an off clip stays silent when its song starts.
        row["active"] = not clip.muted
        rows.append(row)
    return rows


def _song_files(rf, scene_index):
    """The audio file behind each audio clip in one scene (song), by track.

    For listening to a song's stems from disk. Kept out of the snapshot: paths
    are long and the page doesn't need them.
    """
    rows = []
    for i, track in enumerate(rf.song().tracks):
        slot = track.clip_slots[scene_index]
        if slot.has_clip and slot.clip.is_audio_clip:
            rows.append({"track_index": i, "track": track.name, "file_path": slot.clip.file_path})
    return rows


def _meter_row(window, kind, i, track):
    if not track.has_audio_output:
        return None
    heard = window.stats(kind, i)
    if heard is None:
        return {"peak": 0.0, "average": 0.0}
    return {"peak": heard[0], "average": heard[1]}


def _routing_now(track, direction):
    kind = getattr(track, "%s_routing_type" % direction).display_name
    channel = getattr(track, "%s_routing_channel" % direction).display_name
    return {"type": kind, "channel": channel}


def _strip(song, i, track, is_return, meters):
    mixer = track.mixer_device
    row = {
        "index": i,
        "name": track.name,
        "is_return": is_return,
        "color": track.color,
        "meter": _meter_row(meters, "return" if is_return else "track", i, track),
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
        "eq": _snapshot_eq(track),
        "output": _routing_now(track, "output"),
    }
    if not is_return:
        row["is_midi"] = track.has_midi_input
        row["input"] = _routing_now(track, "input")
        row["clips"] = _clip_rows(track)
    return row


def _ext_outputs(master):
    """The interface's output channels ("1/2", "3/4", ...), as the Master sees them.

    A track only lists the channels of its current output type, so a track on
    Master can't say which Ext. Out channels exist. The Master always goes to
    Ext. Out, so its list is the whole set, without touching any routing.
    """
    try:
        if master.output_routing_type.display_name != "Ext. Out":
            return []
        return [c.display_name for c in master.available_output_routing_channels]
    except AttributeError:
        return []


def _get_snapshot(rf):
    """The whole set. Meters are the peak/average since the previous snapshot."""
    song = rf.song()
    meters = rf._display_meters
    master = song.master_track
    snapshot = {
        "song": _get_song(rf),
        "tracks": [_strip(song, i, t, False, meters) for i, t in enumerate(song.tracks)],
        "returns": [_strip(song, i, t, True, meters) for i, t in enumerate(song.return_tracks)],
        "master": {
            "volume": _display(master.mixer_device.volume),
            "volume_db": _db_value(master.mixer_device.volume),
            "meter": _meter_row(meters, "master", 0, master),
        },
        "scenes": _list_scenes(rf),
        "locators": _list_locators(rf),
        "ext_outputs": _ext_outputs(master),
    }
    meters.reset()
    return snapshot


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
    "set_track_color": _set_track_color,
    "track_contents": _track_contents,
    "api_names": _api_names,
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
    "device_parameters": _device_parameters,
    "get_eq": _get_eq,
    "set_eq_band": _set_eq_band,
    "reset_meters": _reset_meters,
    "get_meters": _get_meters,
    "get_live_meters": _get_live_meters,
    "import_audio": _import_audio,
    "set_clip_gain": _set_clip_gain,
    "set_clip_active": _set_clip_active,
    "delete_clip": _delete_clip,
    "clip_markers": _clip_markers,
    "song_files": _song_files,
    "get_song": _get_song,
    "set_tempo": _set_tempo,
    "play": _play,
    "stop": _stop,
    "list_scenes": _list_scenes,
    "create_scene": _create_scene,
    "set_scene": _set_scene,
    "transpose_song": _transpose_song,
    "count_scene_clips": _count_scene_clips,
    "delete_scene": _delete_scene,
    "move_scene": _move_scene,
    "fire_scene": _fire_scene,
    "list_locators": _list_locators,
    "add_locator": _add_locator,
    "delete_locator": _delete_locator,
    "jump_to_locator": _jump_to_locator,
    "get_snapshot": _get_snapshot,
    "list_stock_devices": _list_stock_devices,
}
