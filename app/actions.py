"""The changes the assistant may propose, and how each one is carried out.

Same rule as spec.py: actions carry intent, never device parameters. A track is
named, a level is in dB, an effect is a stock device (optionally one of its
stock presets). Everything Ableton-specific happens in RigLink.

Tracks are referred to the way a volunteer would: by name, by the number Live
shows beside them (1-based), or by a return track's letter (A, B, ...).
Names are resolved against the live set when each action runs, so an action
can refer to a track an earlier action in the same batch created.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, Field

from app.live import LiveUnavailable
from rig import TRACK_COLORS
from riglink_client import RigLinkError
from spec import RigSpec, TrackSpec

ColorName = Literal[tuple(TRACK_COLORS)]

# How long to let a launched scene settle (launch quantisation) before measuring.
LISTEN_SETTLE_SECONDS = 1.0

TrackRef = Annotated[
    str,
    Field(description='A track name ("Lead Vocal"), its number in Live ("3"), or a return letter ("A").'),
]
SongRef = Annotated[str, Field(description="A song (scene) name, or its number in the list.")]
InputChannel = Annotated[
    str,
    Field(
        pattern=r"^\d{1,2}(/\d{1,2})?$",
        description='Hardware input as printed on the interface: "1" for mono, "3/4" for a stereo pair.',
    ),
]


class ActionFailed(Exception):
    """An action couldn't be carried out. The message is a sentence for the volunteer."""


class Device(BaseModel):
    device: str = Field(description='Stock Live device name as Live\'s browser shows it, e.g. "Compressor".')
    preset: str | None = Field(
        default=None,
        description="One of that device's stock presets. Only name a preset you are sure ships with Live.",
    )

    def label(self):
        return f"{self.device} ({self.preset})" if self.preset else self.device


class Output(BaseModel):
    destination: str = Field(description='Output type as Live shows it: "Master", "Ext. Out" or "Sends Only".')
    channel: str | None = Field(default=None, description='Output channel for Ext. Out, e.g. "3/4" or "5".')

    def label(self):
        if self.destination.casefold() == "ext. out" and self.channel:
            return f"outputs {self.channel}"
        return self.destination if not self.channel else f"{self.destination} {self.channel}"


# -- the actions -------------------------------------------------------------


class AddTrack(BaseModel):
    action: Literal["add_track"]
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["audio", "midi"] = "audio"
    input: InputChannel | None = Field(
        default=None,
        description="Hardware input for a live source (mic, DI). Null for playback tracks like click, pads or stems.",
    )
    output: Output | None = Field(default=None, description="Null to leave it going to the Master.")
    volume_db: float | None = Field(default=None, ge=-70, le=6)
    pan: float | None = Field(default=None, ge=-1, le=1, description="-1 hard left, 0 centre, 1 hard right.")
    devices: list[Device] = Field(default_factory=list, description="Effects in chain order.")
    color: ColorName | None = Field(default=None, description="Track colour, to group related tracks.")

    def describe(self):
        parts = [f"Add {'a MIDI' if self.kind == 'midi' else 'an audio'} track “{self.name}”"]
        if self.kind == "audio":
            parts.append(f"on input {self.input}" if self.input else "with no input")
        if self.output:
            parts.append(f"going to {self.output.label()}")
        text = " ".join(parts)
        extras = []
        if self.devices:
            extras.append("with " + ", ".join(d.label() for d in self.devices))
        if self.volume_db is not None:
            extras.append(f"at {_db(self.volume_db)}")
        if self.pan:
            extras.append(f"panned {_pan(self.pan)}")
        if self.color:
            extras.append(f"coloured {self.color}")
        return text + (", " + ", ".join(extras) if extras else "")

    def run(self, ex):
        create = "create_midi_track" if self.kind == "midi" else "create_audio_track"
        index = ex.call(create, name=self.name)["index"]
        ex.tracks_changed()
        target = {"track_index": index, "is_return": False}
        problems = []

        if self.kind == "audio":
            if self.input:
                ex.try_step(problems, f"set input {self.input}", "set_routing", **target,
                            direction="input", type_name="Ext. In", channel_name=self.input)
            else:
                ex.try_step(problems, "turn its input off", "set_routing", **target,
                            direction="input", type_name="No Input")
        if self.output:
            ex.try_step(problems, f"send it to {self.output.label()}", "set_routing", **target,
                        direction="output", type_name=self.output.destination,
                        channel_name=self.output.channel)
        if self.volume_db is not None:
            ex.try_step(problems, "set its volume", "set_volume", **target, db=self.volume_db)
        if self.pan:
            ex.try_step(problems, "pan it", "set_pan", **target, pan=self.pan)
        if self.color:
            ex.try_step(problems, f"colour it {self.color}", "set_track_color", **target,
                        rgb=TRACK_COLORS[self.color])
        for device in self.devices:
            note = ex.load_device(problems, index, False, device)
            if note:
                problems.append(note)

        done = f"Added “{self.name}” as track {index + 1}."
        if problems:
            return done + " But " + "; ".join(problems) + "."
        return done


class AddReturn(BaseModel):
    action: Literal["add_return"]
    name: str = Field(min_length=1, max_length=64)
    devices: list[Device] = Field(default_factory=list, description="Usually one reverb or delay.")

    def describe(self):
        text = f"Add a shared effect (return track) “{self.name}”"
        if self.devices:
            text += " with " + ", ".join(d.label() for d in self.devices)
        return text

    def run(self, ex):
        index = ex.call("create_return_track", name=self.name)["index"]
        ex.tracks_changed()
        problems = []
        for device in self.devices:
            note = ex.load_device(problems, index, True, device)
            if note:
                problems.append(note)
        done = f"Added return {chr(ord('A') + index)}: “{self.name}”."
        return done + (" But " + "; ".join(problems) + "." if problems else "")


class RenameTrack(BaseModel):
    action: Literal["rename_track"]
    track: TrackRef
    new_name: str = Field(min_length=1, max_length=64)

    def describe(self):
        return f"Rename “{self.track}” to “{self.new_name}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_track_name", track_index=t.index, is_return=t.is_return, name=self.new_name)
        ex.tracks_changed()
        return f"Renamed {t.name} to {self.new_name}."


class DeleteTrack(BaseModel):
    action: Literal["delete_track"]
    track: TrackRef
    destructive: ClassVar[bool] = True

    def describe(self):
        return f"Delete the track “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("delete_track", track_index=t.index, is_return=t.is_return)
        ex.tracks_changed()
        return f"Deleted {t.name}. (Cmd+Z in Live brings it back.)"


class SetVolume(BaseModel):
    action: Literal["set_volume"]
    track: TrackRef
    db: float = Field(ge=-70, le=6, description="Fader level in dB. 0 is unity; -70 is effectively off.")

    def describe(self):
        return f"Set “{self.track}” to {_db(self.db)}"

    def run(self, ex):
        t = ex.track(self.track)
        result = ex.call("set_volume", track_index=t.index, is_return=t.is_return, db=self.db)
        return f"{t.name} is now at {result['volume']}."


class SetPan(BaseModel):
    action: Literal["set_pan"]
    track: TrackRef
    pan: float = Field(ge=-1, le=1, description="-1 hard left, 0 centre, 1 hard right.")

    def describe(self):
        return f"Pan “{self.track}” {_pan(self.pan)}"

    def run(self, ex):
        t = ex.track(self.track)
        result = ex.call("set_pan", track_index=t.index, is_return=t.is_return, pan=self.pan)
        return f"{t.name} is panned {result['pan']}."


class SetMute(BaseModel):
    action: Literal["set_mute"]
    track: TrackRef
    on: bool

    def describe(self):
        return f"{'Mute' if self.on else 'Unmute'} “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_mute", track_index=t.index, is_return=t.is_return, on=self.on)
        return f"{t.name} is {'muted' if self.on else 'unmuted'}."


class SetSolo(BaseModel):
    action: Literal["set_solo"]
    track: TrackRef
    on: bool

    def describe(self):
        return f"{'Solo' if self.on else 'Unsolo'} “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_solo", track_index=t.index, is_return=t.is_return, on=self.on)
        return f"{t.name} is {'soloed' if self.on else 'no longer soloed'}."


class SetInput(BaseModel):
    action: Literal["set_input"]
    track: TrackRef
    input: InputChannel | None = Field(description="Null to turn the track's input off.")

    def describe(self):
        if self.input is None:
            return f"Turn off the input on “{self.track}”"
        return f"Plug “{self.track}” into input {self.input}"

    def run(self, ex):
        t = ex.track(self.track, allow_return=False)
        if self.input is None:
            ex.call("set_routing", track_index=t.index, direction="input", type_name="No Input")
            return f"{t.name} has no input now."
        ex.call("set_routing", track_index=t.index, direction="input",
                type_name="Ext. In", channel_name=self.input)
        return f"{t.name} now listens to input {self.input}."


class SetOutput(BaseModel):
    action: Literal["set_output"]
    track: TrackRef
    output: Output

    def describe(self):
        return f"Send “{self.track}” to {self.output.label()}"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_routing", track_index=t.index, is_return=t.is_return, direction="output",
                type_name=self.output.destination, channel_name=self.output.channel)
        return f"{t.name} now goes to {self.output.label()}."


class SetSend(BaseModel):
    action: Literal["set_send"]
    track: TrackRef
    to_return: TrackRef = Field(description="The return track (shared effect) to send to.")
    db: float = Field(ge=-70, le=6, description="Send level in dB; -70 turns the send off.")

    def describe(self):
        return f"Send “{self.track}” into “{self.to_return}” at {_db(self.db)}"

    def run(self, ex):
        t = ex.track(self.track)
        r = ex.track(self.to_return)
        if not r.is_return:
            raise ActionFailed(f"{r.name} isn't a shared effect (return track), so nothing can be sent to it.")
        result = ex.call("set_send", track_index=t.index, is_return=t.is_return,
                         return_index=r.index, db=self.db)
        return f"{t.name} sends to {r.name} at {result['level']}."


class AddDevice(BaseModel):
    action: Literal["add_device"]
    track: TrackRef
    device: Device

    def describe(self):
        return f"Add {self.device.label()} to “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        problems = []
        note = ex.load_device(problems, t.index, t.is_return, self.device)
        if problems:
            raise ActionFailed(problems[0][0].upper() + problems[0][1:] + ".")
        return f"Added {self.device.label()} to {t.name}." + (f" ({note})" if note else "")


class RemoveDevice(BaseModel):
    action: Literal["remove_device"]
    track: TrackRef
    device: str = Field(description="The device's name as it appears on the track.")
    destructive: ClassVar[bool] = True

    def describe(self):
        return f"Remove {self.device} from “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        rows = ex.call("list_devices", track_index=t.index, is_return=t.is_return)
        matches = [d for d in rows if d["name"].casefold() == self.device.casefold()]
        if not matches:
            names = ", ".join(d["name"] for d in rows) or "no effects"
            raise ActionFailed(f"{t.name} has no {self.device}. It has {names}.")
        ex.call("delete_device", track_index=t.index, is_return=t.is_return, device_index=matches[-1]["index"])
        return f"Removed {matches[-1]['name']} from {t.name}."


class SetTempo(BaseModel):
    action: Literal["set_tempo"]
    bpm: float = Field(ge=20, le=999)

    def describe(self):
        return f"Set the tempo to {self.bpm:g} BPM"

    def run(self, ex):
        result = ex.call("set_tempo", bpm=self.bpm)
        return f"Tempo is {result['tempo']:g} BPM."


class AddSong(BaseModel):
    action: Literal["add_song"]
    name: str = Field(min_length=1, max_length=64, description="The song title.")
    bpm: float | None = Field(default=None, ge=20, le=999, description="The song's tempo, if known.")

    def describe(self):
        return f"Add the song “{self.name}”" + (f" at {self.bpm:g} BPM" if self.bpm else "")

    def run(self, ex):
        row = ex.call("create_scene", name=self.name, bpm=self.bpm)
        return f"Added song {row['index'] + 1}: {self.name}."


class UpdateSong(BaseModel):
    action: Literal["update_song"]
    song: SongRef
    new_name: str | None = None
    bpm: float | None = Field(default=None, ge=20, le=999)

    def describe(self):
        changes = []
        if self.new_name:
            changes.append(f"rename it “{self.new_name}”")
        if self.bpm:
            changes.append(f"set it to {self.bpm:g} BPM")
        return f"Song “{self.song}”: " + (" and ".join(changes) or "no change")

    def run(self, ex):
        s = ex.song(self.song)
        ex.call("set_scene", scene_index=s["index"], name=self.new_name, bpm=self.bpm)
        return f"Updated {self.new_name or s['name'] or 'song ' + str(s['index'] + 1)}."


class DeleteSong(BaseModel):
    action: Literal["delete_song"]
    song: SongRef
    destructive: ClassVar[bool] = True

    def describe(self):
        return f"Delete the song “{self.song}”"

    def run(self, ex):
        s = ex.song(self.song)
        ex.call("delete_scene", scene_index=s["index"])
        return f"Deleted {s['name'] or 'song ' + str(s['index'] + 1)}."


class StartSong(BaseModel):
    action: Literal["start_song"]
    song: SongRef

    def describe(self):
        return f"Start “{self.song}”"

    def run(self, ex):
        s = ex.song(self.song)
        ex.call("fire_scene", scene_index=s["index"])
        return f"Started {s['name'] or 'song ' + str(s['index'] + 1)}."


class Transport(BaseModel):
    action: Literal["transport"]
    playing: bool

    def describe(self):
        return "Start playback" if self.playing else "Stop playback"

    def run(self, ex):
        ex.call("play" if self.playing else "stop")
        return "Playing." if self.playing else "Stopped."


class SetColor(BaseModel):
    action: Literal["set_color"]
    track: TrackRef
    color: ColorName

    def describe(self):
        return f"Colour “{self.track}” {self.color}"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_track_color", track_index=t.index, is_return=t.is_return, rgb=TRACK_COLORS[self.color])
        return f"{t.name} is {self.color} now."


class ImportAudio(BaseModel):
    action: Literal["import_audio"]
    track: TrackRef = Field(description="An audio track. Create it first in the same batch if needed.")
    file: str = Field(description='An imported file exactly as listed, e.g. "Sunday Stems/Way Maker/Click.wav".')
    song: SongRef = Field(description="The song (scene) the clip belongs to.")
    gain_db: float | None = Field(
        default=None, ge=-70, le=24,
        description="Clip gain in dB, to balance stems against each other. Null leaves it at 0 dB.",
    )

    def describe(self):
        text = f"Put {Path(self.file).name} on “{self.track}” in “{self.song}”"
        return text + (f", clip gain {_db(self.gain_db)}" if self.gain_db else "")

    def run(self, ex):
        path = ex.file(self.file)
        t = ex.track(self.track, allow_return=False)
        s = ex.song(self.song)
        result = ex.call("import_audio", track_index=t.index, file_path=str(path),
                         scene_index=s["index"], name=path.stem, gain_db=self.gain_db)
        gain = f" at {result['gain']}" if self.gain_db else ""
        return f"Put {result['name']} on {t.name} in {_song_name(s)}{gain}."


class SetClipGain(BaseModel):
    action: Literal["set_clip_gain"]
    track: TrackRef
    song: SongRef
    db: float = Field(ge=-70, le=24, description="Clip gain in dB. Changes the clip, not the fader.")

    def describe(self):
        return f"Set the clip on “{self.track}” in “{self.song}” to {_db(self.db)}"

    def run(self, ex):
        t = ex.track(self.track, allow_return=False)
        s = ex.song(self.song)
        result = ex.call("set_clip_gain", track_index=t.index, scene_index=s["index"], db=self.db)
        return f"The {t.name} clip in {_song_name(s)} is at {result['gain']}."


class Listen(BaseModel):
    action: Literal["listen"]
    song: SongRef | None = Field(
        default=None, description="A song to start and measure. Null to measure whatever is already playing.",
    )
    seconds: int = Field(default=10, ge=3, le=30)
    audible: ClassVar[bool] = True

    def describe(self):
        if self.song:
            return f"Play “{self.song}” for {self.seconds} seconds and measure how loud each track is"
        return f"Measure how loud each track is for {self.seconds} seconds"

    def run(self, ex):
        s = ex.song(self.song) if self.song else None
        was_playing = ex.call("get_song")["is_playing"]
        if s is None and not was_playing:
            raise ActionFailed("Nothing is playing, so there's nothing to measure. Start a song first.")
        if s is not None:
            ex.call("fire_scene", scene_index=s["index"])
            time.sleep(LISTEN_SETTLE_SECONDS)
        ex.call("reset_meters")
        time.sleep(self.seconds)
        meters = ex.call("get_meters")
        if s is not None and not was_playing:
            ex.call("stop")

        heard = [r for r in meters["tracks"] if r["has_audio_output"]]
        loud = [r for r in heard if r["peak"] > 0 and r["kind"] != "master"]
        what = _song_name(s) if s else "the set"
        if not loud:
            text = f"Listened to {what} for {self.seconds} seconds, but no track made a sound."
        else:
            loudest = max(loud, key=lambda r: r["peak"])
            quietest = min(loud, key=lambda r: r["peak"])
            text = f"Listened to {what} for {self.seconds} seconds. Loudest: {loudest['name']}."
            if quietest is not loudest:
                text = text[:-1] + f"; quietest: {quietest['name']}."
        ex.detail = _meter_report(heard, meters["ticks"])
        return text


Action = Union[
    AddTrack, AddReturn, RenameTrack, DeleteTrack, SetVolume, SetPan, SetMute, SetSolo,
    SetInput, SetOutput, SetSend, AddDevice, RemoveDevice, SetTempo, AddSong, UpdateSong,
    DeleteSong, StartSong, Transport, SetColor, ImportAudio, SetClipGain, Listen,
]


class Proposal(BaseModel):
    """What the propose_changes tool takes."""

    actions: list[Annotated[Action, Field(discriminator="action")]] = Field(min_length=1, max_length=60)


# -- running them -------------------------------------------------------------


class Target:
    def __init__(self, index, is_return, name):
        self.index = index
        self.is_return = is_return
        self.name = name


class Executor:
    """Runs actions against Live, resolving names as it goes."""

    def __init__(self, live, files=None):
        self._live = live
        self._files = files or {}
        self._tracks = None
        self.detail = None  # extra facts for the assistant from the last action

    def file(self, file_id):
        """An imported file by the id the assistant saw. Only those, never any path."""
        wanted = file_id.strip().casefold()
        for known, path in self._files.items():
            if known.casefold() == wanted:
                return path
        raise ActionFailed(f"I don't have a file called {file_id}. Import its folder first.")

    def call(self, cmd, **args):
        try:
            return self._live.call(cmd, **args)
        except RigLinkError as e:
            raise ActionFailed(_sentence(e)) from e

    def tracks_changed(self):
        self._tracks = None

    def try_step(self, problems, what, cmd, **args):
        try:
            self.call(cmd, **args)
        except ActionFailed as e:
            problems.append(f"couldn't {what} ({_lower_first(str(e)).rstrip('.')})")

    def load_device(self, problems, index, is_return, device):
        """Load a device, falling back to its default if the preset isn't there.

        Returns a note for the volunteer when it had to fall back.
        """
        target = {"track_index": index, "is_return": is_return}
        try:
            self._live.call("load_device", device_name=device.device, preset=device.preset, **target)
            return None
        except RigLinkError as e:
            if device.preset and "no preset called" in str(e):
                try:
                    self._live.call("load_device", device_name=device.device, **target)
                    return f"{device.device} is on its default settings — Live has no preset called “{device.preset}”"
                except RigLinkError as e2:
                    e = e2
            problems.append(f"couldn't add {device.label()} ({_lower_first(_sentence(e)).rstrip('.')})")
            return None

    def _rows(self):
        if self._tracks is None:
            tracks = self.call("list_tracks")
            returns = self.call("list_returns")
            self._tracks = (tracks, returns)
        return self._tracks

    def track(self, ref, allow_return=True):
        tracks, returns = self._rows()
        ref = ref.strip()
        everything = [Target(t["index"], False, t["name"]) for t in tracks]
        if allow_return:
            everything += [Target(r["index"], True, r["name"]) for r in returns]

        named = [t for t in everything if t.name.casefold() == ref.casefold()]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise ActionFailed(f"More than one track is called {ref}. Rename one so I can tell them apart.")
        if ref.isdigit() and 1 <= int(ref) <= len(tracks):
            row = tracks[int(ref) - 1]
            return Target(row["index"], False, row["name"])
        if allow_return and len(ref) == 1 and ref.isalpha():
            i = ord(ref.upper()) - ord("A")
            if 0 <= i < len(returns):
                return Target(i, True, returns[i]["name"])
        # Live names returns "A-Reverb"; people say "Reverb".
        loose = [t for t in everything if _loose(t.name) == _loose(ref)]
        if len(loose) == 1:
            return loose[0]
        names = ", ".join(t.name for t in everything) or "none yet"
        raise ActionFailed(f"There's no track called {ref}. The tracks are: {names}.")

    def song(self, ref):
        rows = self.call("list_scenes")
        ref = ref.strip()
        named = [s for s in rows if s["name"].casefold() == ref.casefold()]
        if len(named) == 1:
            return named[0]
        if ref.isdigit() and 1 <= int(ref) <= len(rows):
            return rows[int(ref) - 1]
        names = ", ".join(s["name"] or str(s["index"] + 1) for s in rows) or "none yet"
        raise ActionFailed(f"There's no song called {ref}. The songs are: {names}.")


def run_all(live, actions, files=None):
    """Run actions in order. One failure doesn't stop the rest."""
    ex = Executor(live, files)
    results = []
    for n, action in enumerate(actions):
        try:
            ex.detail = None
            text = action.run(ex)
            results.append({"ok": True, "partial": " But " in text, "text": text})
            if ex.detail:
                results[-1]["detail"] = ex.detail
        except ActionFailed as e:
            results.append({"ok": False, "text": f"{action.describe()}: {e}"})
            ex.tracks_changed()
        except LiveUnavailable as e:
            results.append({"ok": False, "text": f"{action.describe()}: {e}"})
            results.extend(
                {"ok": False, "text": f"{a.describe()}: not done — lost touch with Live."}
                for a in actions[n + 1:]
            )
            break
    return results


def is_destructive(action):
    return getattr(type(action), "destructive", False)


def is_audible(action):
    """Plays sound through the speakers when applied."""
    return getattr(type(action), "audible", False)


# -- exporting to a .als ------------------------------------------------------


def to_rigspec(actions):
    """The add_track actions as a RigSpec, plus notes on what a file can't hold.

    The file renderer only knows tracks so far (see plan.md). Everything else
    in a proposal only works against a running Live.
    """
    tracks, notes = [], []
    for action in actions:
        if not isinstance(action, AddTrack):
            notes.append(f"“{action.describe()}” only works with Live open.")
            continue
        spec_input = None
        if action.input and action.input.isdigit():
            spec_input = int(action.input)
        elif action.input:
            notes.append(f"{action.name}: stereo inputs can't go in a session file yet, so it has no input.")
        if action.output and action.output.destination.casefold() != "master":
            notes.append(f"{action.name}: only the Master output can go in a session file yet.")
        if action.color:
            notes.append(f"{action.name}: colours only apply in Live, not in a session file.")
        for d in action.devices:
            if d.preset:
                notes.append(f"{action.name}: {d.device} uses its default settings in a session file, not “{d.preset}”.")
        tracks.append(
            TrackSpec(
                name=action.name,
                type=action.kind,
                input=spec_input,
                devices=[d.device for d in action.devices],
                volume_db=action.volume_db if action.volume_db is not None else 0.0,
                pan=action.pan or 0.0,
            )
        )
    if not tracks:
        return None, notes
    return RigSpec(tracks=tracks), notes


# -- wording ------------------------------------------------------------------


def _db(db):
    if db <= -70:
        return "off"
    return f"{db:+g} dB" if db else "0 dB"


def _pan(pan):
    amount = round(abs(pan) * 50)
    if amount == 0:
        return "centre"
    return f"{amount}{'L' if pan < 0 else 'R'}"


def _song_name(s):
    return s["name"] or f"song {s['index'] + 1}"


def _meter_report(rows, ticks):
    """Meter readings for the assistant. Values are Live's own meter scale."""
    lines = [
        f"Meter readings over {ticks} samples (Live's output meters, 0-1, after the fader; "
        "compare tracks with each other -- how 0-1 maps to dB isn't verified):"
    ]
    for r in rows:
        label = {"track": str(r["index"] + 1), "return": chr(ord("A") + r["index"]), "master": "Master"}[r["kind"]]
        if r["peak"] == 0:
            lines.append(f"  {label}. {r['name']}: silent")
        else:
            lines.append(f"  {label}. {r['name']}: peak {r['peak']:.2f}, average {r['average']:.2f}")
    return "\n".join(lines)


def _loose(name):
    return re.sub(r"^[a-z]-", "", name.casefold()).strip()


def _lower_first(text):
    return text[:1].lower() + text[1:]


def _sentence(error):
    """RigLink's errors are terse; make the common ones read like sentences."""
    text = str(error)
    match = re.match(r"no (.+?) called '(.+?)' \(options: (.+)\)", text)
    if match:
        noun, wanted, options = match.groups()
        noun = {"input": "input type", "output": "output type"}.get(noun, noun)
        return f"Live has no {noun} called “{wanted}”. It has: {options}."
    match = re.match(r"no stock device called '(.+?)'", text)
    if match:
        return f"Live has no stock device called “{match.group(1)}”."
    if "index out of range" in text:
        return "That item doesn't exist any more — the set may have changed."
    return "Live said: " + text
