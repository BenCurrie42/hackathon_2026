"""The changes the assistant may propose, and how each one is carried out.

Same rule as file_builder/rig_spec.py: actions carry intent, never device parameters. A track is
named, a level is in dB, an effect is a stock device (optionally one of its
stock presets). Everything Ableton-specific happens in RigLink.

Tracks are referred to the way a volunteer would: by name, by the number Live
shows beside them (1-based), or by a return track's letter (A, B, ...).
Names are resolved against the live set when each action runs, so an action
can refer to a track an earlier action in the same batch created.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, Field, model_validator

from app import mixdown, parts
from app.folders import FAMILIES, KEYS, classify
from app.live import LiveUnavailable
from live_control.starting_fader import starting_fader_db
from live_control.timecode import is_timecode
from rig import TRACK_COLORS
from live_control.live_connection import RigLinkError
from file_builder.rig_spec import RigSpec, TrackSpec

ColorName = Literal[tuple(TRACK_COLORS)]
FolderKey = Literal[tuple(KEYS)]

# How long to let a launched scene settle (launch quantisation) before measuring.
LISTEN_SETTLE_SECONDS = 1.0

TrackRef = Annotated[
    str,
    Field(description='A track name ("Lead Vocal"), its number in Live ("3"), or a return letter ("A").'),
]
SongRef = Annotated[str, Field(description="A song (scene) name, or its number in the list.")]
MixSong = Annotated[SongRef | None, Field(default=None, description=(
    "Whose mix this is: a song's name or number changes that song's saved mix. Null: the song "
    "the mixer is on, or every song when it's on none. For a song other than the one the mixer "
    "is on, the faders don't move now; the change comes in when that song is picked or starts."))]
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
    """A stock effect or instrument, and optionally one of its presets."""

    device: str = Field(description='Stock Live device name as Live\'s browser shows it, e.g. "Compressor".')
    preset: str | None = Field(
        default=None,
        description="One of that device's stock presets. Only name a preset you are sure ships with Live.",
    )

    def label(self):
        return f"{self.device} ({self.preset})" if self.preset else self.device


class Output(BaseModel):
    """Where a track's sound goes: Master (the main speakers) or an interface output."""

    destination: str = Field(description='Output type as Live shows it: "Master", "Ext. Out" or "Sends Only".')
    channel: str | None = Field(default=None, description='Output channel for Ext. Out, e.g. "3/4" or "5".')

    def label(self):
        if self.destination.casefold() == "ext. out" and self.channel:
            return f"outputs {self.channel}"
        return self.destination if not self.channel else f"{self.destination} {self.channel}"


# -- the actions -------------------------------------------------------------


class AddTrack(BaseModel):
    """Make a new track: a live mic or instrument on an input, or an empty playback track. Not for imports (import_part makes part tracks)."""

    action: Literal["add_track"]
    name: str = Field(min_length=1, max_length=64, description="Track name. Must differ from every other track.")
    kind: Literal["audio", "midi"] = Field(
        default="audio", description='"audio" for mics, instruments and stems; "midi" for software instruments.',
    )
    input: InputChannel | None = Field(
        default=None,
        description="Hardware input for a live source (mic, DI). Null for playback tracks like click, pads or stems.",
    )
    output: Output | None = Field(default=None, description="Null to leave it going to the Master.")
    volume_db: float | None = Field(
        default=None, ge=-70, le=6,
        description="Fader in dB, -70 to +6; 0 is unity. Null for a sensible start (low when importing stems).",
    )
    pan: float | None = Field(default=None, ge=-1, le=1, description="-1 hard left, 0 centre, 1 hard right.")
    devices: list[Device] = Field(default_factory=list, description="Effects in chain order.")
    color: ColorName | None = Field(default=None, description="Track colour, to group related tracks.")

    def describe(self):
        parts = [f"Add {'a MIDI' if self.kind == 'midi' else 'a'} track “{self.name}”"]
        if self.kind == "audio":
            parts.append(f"on input {self.input}" if self.input else "with no input")
        if self.output:
            parts.append(f"going to {self.output.label()}")
        text = " ".join(parts)
        extras = []
        if self.devices:
            extras.append("with " + ", ".join(d.label() for d in self.devices))
        if self.volume_db is not None:
            extras.append(f"at {_level(self.volume_db)}")
        if self.pan:
            extras.append(f"balance {_pan(self.pan)}")
        if self.color:
            extras.append(f"coloured {self.color}")
        return text + (", " + ", ".join(extras) if extras else "")

    def run(self, ex):
        create = "create_midi_track" if self.kind == "midi" else "create_audio_track"
        index = ex.call(create, name=self.name)["index"]
        ex.touch(self.name)
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
        volume_db = self.volume_db
        if volume_db is None and self.kind == "audio":
            volume_db = ex.new_track_fader_db
        if volume_db is not None:
            ex.try_step(problems, "set its volume", "set_volume", **target, db=volume_db)
        is_muted_timecode = ex.new_track_fader_db is not None and is_timecode(self.name)
        if is_muted_timecode:
            ex.try_step(problems, "mute it", "set_mute", **target, on=True)
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
        if self.volume_db is None and volume_db is not None:
            done = done[:-1] + f", fader at {_level(volume_db)} to leave room for every part together."
        if is_muted_timecode:
            done = done[:-1] + ", muted because it's timecode."
        if problems:
            return done + " But " + "; ".join(problems) + "."
        return done


class AddReturn(BaseModel):
    """Make a shared effect (return track), such as one reverb every vocal can send to."""

    action: Literal["add_return"]
    name: str = Field(min_length=1, max_length=64, description='Name of the shared effect, e.g. "Vocal Reverb".')
    devices: list[Device] = Field(default_factory=list, description="Usually one reverb or delay.")

    def describe(self):
        text = f"Add a shared effect “{self.name}”"
        if self.devices:
            text += " with " + ", ".join(d.label() for d in self.devices)
        return text

    def run(self, ex):
        made = ex.call("create_return_track", name=self.name)
        index = made["index"]
        ex.touch(made.get("name") or self.name)
        ex.tracks_changed()
        problems = []
        for device in self.devices:
            note = ex.load_device(problems, index, True, device)
            if note:
                problems.append(note)
        done = f"Added return {chr(ord('A') + index)}: “{self.name}”."
        return done + (" But " + "; ".join(problems) + "." if problems else "")


class RenameTrack(BaseModel):
    """Rename a track."""

    action: Literal["rename_track"]
    track: TrackRef
    new_name: str = Field(min_length=1, max_length=64, description="The new name. Must differ from every other track.")

    def describe(self):
        return f"Rename “{self.track}” to “{self.new_name}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_track_name", track_index=t.index, is_return=t.is_return, name=self.new_name)
        if ex.folders is not None:
            ex.folders.rename(t.name, self.new_name)
        ex.touch(self.new_name)
        ex.tracks_changed()
        return f"Renamed {t.name} to {self.new_name}."


class DeleteTrack(BaseModel):
    """Delete a track and everything on it, in every song."""

    action: Literal["delete_track"]
    track: TrackRef
    destructive: ClassVar[bool] = True

    def describe(self):
        return f"Delete the track “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("delete_track", track_index=t.index, is_return=t.is_return)
        ex.tracks_changed()
        return f"Deleted {t.name}. Undo in Ableton brings it back."


class SetVolume(BaseModel):
    """A track's fader: set a level (db) or move it (by_db). Give exactly one of the two."""

    action: Literal["set_volume"]
    track: TrackRef
    db: float | None = Field(default=None, ge=-70, le=6, description=(
        "The level to set, in dB: 0 is unity, -70 is off. Null when using by_db."))
    by_db: float | None = Field(default=None, ge=-24, le=24, description=(
        "How far to move it from where it is in that song now: -3 is 3 dB quieter, 2 is 2 dB louder. "
        "Use this for 'up', 'down', 'a bit'; the app works out the new level."))
    song: MixSong

    _one_level = model_validator(mode="after")(lambda self: _one_of(self, "db", "by_db"))

    def describe(self):
        return _in_song(self.song, f"{_level_change(self.track, self.db, self.by_db)}")

    def run(self, ex):
        t = ex.track(self.track)
        later = ex.later_song(self.song)
        db = _moved(self.db, self.by_db, lambda: ex.level_now(t, later))
        if later:
            ex.save_for_song(later, t, volume_db=db)
            return f"{t.name} will be at {_level(db)} in {later}."
        result = ex.call("set_volume", track_index=t.index, is_return=t.is_return, db=db)
        return f"{t.name} is now at {_level_text(result['volume'])}."


class SetPan(BaseModel):
    """Move a track left or right."""

    action: Literal["set_pan"]
    track: TrackRef
    pan: float = Field(ge=-1, le=1, description="-1 hard left, 0 centre, 1 hard right.")
    song: MixSong

    def describe(self):
        return _in_song(self.song, f"Set the balance of “{self.track}” to {_pan(self.pan)}")

    def run(self, ex):
        t = ex.track(self.track)
        later = ex.later_song(self.song)
        if later:
            ex.save_for_song(later, t, pan=self.pan)
            return f"{t.name} will be panned {_pan(self.pan)} in {later}."
        result = ex.call("set_pan", track_index=t.index, is_return=t.is_return, pan=self.pan)
        return f"{t.name} is balanced {_pan(self.pan)}."


class SetMute(BaseModel):
    """Mute or unmute a track, in the song on the mixer (or a named song). To leave a part out of one song, set_clip_active is better."""

    action: Literal["set_mute"]
    track: TrackRef
    on: bool = Field(description="True mutes the track, false unmutes it.")
    song: MixSong

    def describe(self):
        return _in_song(self.song, f"{'Mute' if self.on else 'Unmute'} “{self.track}”")

    def run(self, ex):
        t = ex.track(self.track)
        later = ex.later_song(self.song)
        if later:
            ex.save_for_song(later, t, mute=self.on)
            return f"{t.name} will be {'muted' if self.on else 'unmuted'} in {later}."
        ex.call("set_mute", track_index=t.index, is_return=t.is_return, on=self.on)
        return f"{t.name} is {'muted' if self.on else 'unmuted'}."


class SetSolo(BaseModel):
    """Solo a track so only soloed tracks are heard. For checking a sound, not part of a mix."""

    action: Literal["set_solo"]
    track: TrackRef
    on: bool = Field(description="True solos the track (only soloed tracks are heard), false unsolos it.")

    def describe(self):
        return f"{'Solo' if self.on else 'Unsolo'} “{self.track}”"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_solo", track_index=t.index, is_return=t.is_return, on=self.on)
        return f"{t.name} is {'soloed' if self.on else 'no longer soloed'}."


class SetInput(BaseModel):
    """Which interface input a track listens to; null for none (playback tracks)."""

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
    """Where a track's sound goes, e.g. the click to the in-ear output."""

    action: Literal["set_output"]
    track: TrackRef
    output: Output = Field(description="Where the track's sound goes.")

    def describe(self):
        return f"Send “{self.track}” to {self.output.label()}"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_routing", track_index=t.index, is_return=t.is_return, direction="output",
                type_name=self.output.destination, channel_name=self.output.channel)
        return f"{t.name} now goes to {self.output.label()}."


class SetSend(BaseModel):
    """How much of a track goes into a shared effect (reverb, delay): set a level (db) or move it (by_db). Give exactly one of the two."""

    action: Literal["set_send"]
    track: TrackRef
    to_return: TrackRef = Field(description="The return track (shared effect) to send to.")
    db: float | None = Field(default=None, ge=-70, le=6, description=(
        "The send level to set, in dB; -70 turns the send off. Null when using by_db."))
    by_db: float | None = Field(default=None, ge=-24, le=24, description=(
        "How far to move the send from where it is in that song now: 3 is more effect, -3 less. "
        "From off, it starts at -70."))
    song: MixSong

    _one_level = model_validator(mode="after")(lambda self: _one_of(self, "db", "by_db"))

    def describe(self):
        if self.by_db is not None:
            more = "more" if self.by_db > 0 else "less"
            return _in_song(self.song, f"{abs(self.by_db):g} dB {more} of “{self.to_return}” on “{self.track}”")
        return _in_song(self.song, f"Send “{self.track}” to “{self.to_return}” at {_level(self.db)}")

    def run(self, ex):
        t = ex.track(self.track)
        r = ex.track(self.to_return)
        if not r.is_return:
            raise ActionFailed(f"{r.name} isn't a shared effect, so nothing can be sent to it.")
        later = ex.later_song(self.song)
        db = _moved(self.db, self.by_db, lambda: ex.level_now(t, later, send_to=r.name))
        if later:
            ex.save_for_song(later, t, send=(r.name, db))
            return f"{t.name} will send to {r.name} at {_level(db)} in {later}."
        result = ex.call("set_send", track_index=t.index, is_return=t.is_return,
                         return_index=r.index, db=db)
        return f"{t.name} sends to {r.name} at {_level_text(result['level'])}."


class AddDevice(BaseModel):
    """Put a stock effect on a track, at the end of its chain."""

    action: Literal["add_device"]
    track: TrackRef
    device: Device = Field(description="The stock effect to add at the end of the track's chain.")

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
    """Take an effect off a track."""

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
    """The whole set's tempo now. A song's own tempo is update_song."""

    action: Literal["set_tempo"]
    bpm: float = Field(ge=20, le=999, description="The set's tempo in beats per minute, 20 to 999.")

    def describe(self):
        return f"Set the tempo to {self.bpm:g} BPM"

    def run(self, ex):
        result = ex.call("set_tempo", bpm=self.bpm)
        return f"Tempo is {result['tempo']:g} BPM."


class AddSong(BaseModel):
    """Add an empty song (a Live scene), at the end or at a slot."""

    action: Literal["add_song"]
    name: str = Field(min_length=1, max_length=64, description="The song title.")
    bpm: float | None = Field(default=None, ge=20, le=999, description="The song's tempo, if known.")
    position: int | None = Field(default=None, ge=1, description=(
        "Song slot number to put it in (1 = first), pushing later songs down. Null adds it at the end."))

    def describe(self):
        where = f" as song {self.position}" if self.position else ""
        return f"Add the song “{self.name}”" + (f" at {self.bpm:g} BPM" if self.bpm else "") + where

    def run(self, ex):
        index = self.position - 1 if self.position else None
        row = ex.call("create_scene", name=self.name, bpm=self.bpm, index=index)
        return f"Added song {row['index'] + 1}: {self.name}."


class MoveSong(BaseModel):
    """Move a song and its clips to another slot in the set's order."""

    action: Literal["move_song"]
    song: SongRef
    position: int = Field(ge=1, description="The song slot number to move it to (1 = first).")

    def describe(self):
        return f"Move “{self.song}” to song slot {self.position}"

    def run(self, ex):
        s = ex.song(self.song)
        slots = len(ex.call("list_scenes"))
        if self.position > slots:
            raise ActionFailed(f"There are only {slots} song slots. Add a song first to make room.")
        ex.call("move_scene", scene_index=s["index"], to_index=self.position - 1)
        return f"Moved {_song_name(s)} to slot {self.position}."


class UpdateSong(BaseModel):
    """Rename a song or change its tempo."""

    action: Literal["update_song"]
    song: SongRef
    new_name: str | None = Field(default=None, description="The new song title. Null keeps the name.")
    bpm: float | None = Field(
        default=None, ge=20, le=999, description="The song's tempo in BPM, 20 to 999. Null keeps it.",
    )

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


class TransposeSong(BaseModel):
    """Change a song's key by semitones from its original key, without changing its speed."""

    action: Literal["transpose_song"]
    song: SongRef
    semitones: int = Field(ge=-12, le=12, description=(
        "Semitones from the original key: 2 is up a whole step, -3 down a minor third, 0 back to the original."))

    def describe(self):
        if self.semitones == 0:
            return f"Put “{self.song}” back in its original key"
        return f"Transpose “{self.song}” {_semitones(self.semitones)}"

    def run(self, ex):
        s = ex.song(self.song)
        tracks, _returns = ex._rows()
        kept = ex.folders.kept_tracks(tracks) if ex.folders is not None else []
        result = ex.call("transpose_song", scene_index=s["index"], semitones=self.semitones,
                         skip_tracks=kept)
        name = s["name"] or f"song {s['index'] + 1}"
        if not result.get("clips"):
            raise ActionFailed(f"{name} has no audio clips to transpose.")
        where = "back in its original key" if self.semitones == 0 else _semitones(self.semitones)
        n = result["clips"]
        done = f"{name} is {where} now: {n} clip{'' if n == 1 else 's'}"
        if result.get("kept"):
            done += f", {result['kept']} kept their key (like the click)"
        return done + "."


class DeleteSong(BaseModel):
    """Delete a song and its clips."""

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
    """Start playing a song in the room."""

    action: Literal["start_song"]
    song: SongRef
    audible: ClassVar[bool] = True

    def describe(self):
        return f"Start “{self.song}”"

    def run(self, ex):
        s = ex.song(self.song)
        ex.call("fire_scene", scene_index=s["index"])
        return f"Started {s['name'] or 'song ' + str(s['index'] + 1)}."


class PickSongMix(BaseModel):
    """Put the mixer on a song: that song's saved faders, pan, mute and sends come back now."""

    action: Literal["pick_song_mix"]
    song: SongRef | None = Field(description=(
        "The song whose mix goes on the mixer now: its saved faders, pan, mute and sends come back. "
        "Null takes the mixer off every song (faders shared again)."))

    def describe(self):
        return f"Put the mixer on “{self.song}”’s mix" if self.song else "Take the mixer off every song’s mix"

    def run(self, ex):
        control = ex.need_mix_control()
        if self.song is None:
            control.pick_song_mix(None)
            return "The mixer is on no song now: the faders are shared by every song."
        s = ex.song(self.song)
        control.pick_song_mix(s["index"])
        ex.tracks_changed()
        return f"The mixer is on {_song_name(s)}'s mix now."


class SaveCheckpoint(BaseModel):
    """Keep a named copy of a song's whole mix (faders, pan, mute, sends) to go back to later."""

    action: Literal["save_checkpoint"]
    label: str = Field(min_length=1, max_length=60, description="A short name, e.g. 'After soundcheck'.")
    song: MixSong

    def describe(self):
        return _in_song(self.song, f"Save a checkpoint “{self.label}”")

    def run(self, ex):
        song = ex.mix_song_name(self.song)
        ex.need_mix_control().checkpoint_song_mix(self.label, song)
        return f"Saved {song}'s mix as “{self.label}”."


class RestoreCheckpoint(BaseModel):
    """Put a song's mix back to a checkpoint the notes list. The mix it replaces is checkpointed first."""

    action: Literal["restore_checkpoint"]
    label: str = Field(description="The checkpoint's name as the notes list it.")
    song: MixSong

    def describe(self):
        return _in_song(self.song, f"Go back to the checkpoint “{self.label}”")

    def run(self, ex):
        song = ex.mix_song_name(self.song)
        mark = ex.mixes.find_checkpoint(song, self.label)
        if mark is None:
            names = ", ".join(m["label"] for m in ex.mixes.checkpoints(song)) or "none yet"
            raise ActionFailed(f"{song} has no checkpoint called {self.label}. Its checkpoints: {names}.")
        ex.need_mix_control().restore_song_mix(mark["id"], song)
        ex.tracks_changed()
        return (f"{song} is back to “{mark['label']}”. The mix before it is saved as a checkpoint, "
                "so this can be undone.")


class Transport(BaseModel):
    """Start or stop playback."""

    action: Literal["transport"]
    playing: bool = Field(description="True starts playback, false stops it.")

    @property
    def audible(self):
        return self.playing

    def describe(self):
        return "Start playback" if self.playing else "Stop playback"

    def run(self, ex):
        ex.call("play" if self.playing else "stop")
        return "Playing." if self.playing else "Stopped."


class SetColor(BaseModel):
    """Change a track's colour."""

    action: Literal["set_color"]
    track: TrackRef
    color: ColorName = Field(description="Track colour, to group related tracks.")

    def describe(self):
        return f"Colour “{self.track}” {self.color}"

    def run(self, ex):
        t = ex.track(self.track)
        ex.call("set_track_color", track_index=t.index, is_return=t.is_return, rgb=TRACK_COLORS[self.color])
        return f"{t.name} is {self.color} now."


class MoveToFolder(BaseModel):
    """Move a track to another mixer folder (it also takes the folder's colour in Live)."""

    action: Literal["move_to_folder"]
    track: TrackRef = Field(description="A track (not a return).")
    folder: FolderKey = Field(description=(
        'Mixer folder: "vocals", "instruments" (drums, bass, guitars, keys, pads), '
        '"playback" (click, guide, stems, timecode) or "other".'))

    def describe(self):
        return f"Move “{self.track}” to the {_folder_label(self.folder)} folder"

    def run(self, ex):
        if ex.folders is None:
            raise ActionFailed("Folders aren't available here.")
        t = ex.track(self.track, allow_return=False)
        ex.folders.move(t.name, self.folder)
        # Live can't show folders; the folder's colour is how it shows up there.
        colour = next(c for key, _label, c in FAMILIES if key == self.folder)
        problems = []
        ex.try_step(problems, f"colour it {colour} in Ableton", "set_track_color",
                    track_index=t.index, is_return=False, rgb=TRACK_COLORS[colour])
        done = f"Moved {t.name} to {_folder_label(self.folder)}, {colour} in Ableton."
        if problems:
            return done + " But " + "; ".join(problems) + "."
        return done


class ImportPart(BaseModel):
    """One part of one song onto its part track: one stem, or several mixed into one file.

    The track is the church's part track (Drums, Keys, BGVs...), shared by every song,
    and is created if the set doesn't have it yet. Never a track per stem.
    """

    action: Literal["import_part"]
    part: str = Field(min_length=1, max_length=64, description=(
        'The part track, from the parts list ("Drums", "Keys", "BGVs"...). A stem that fits no part '
        "may use its own name. Created if the set doesn't have it."))
    files: list[str] = Field(min_length=1, max_length=40, description=(
        'Imported files exactly as listed, e.g. "Sunday Stems/Way Maker/Kick In.wav". Several are '
        "mixed into one clip, keeping their balance; L/R stems keep their sides."))
    song: SongRef = Field(description="The song (scene) the clip belongs to.")
    gain_db: float | None = Field(
        default=None, ge=-70, le=24,
        description="Clip gain in dB, to balance this part in this song. Null plays it as the stems are.",
    )

    def describe(self):
        what = Path(self.files[0]).stem if len(self.files) == 1 else f"{len(self.files)} stems mixed into one"
        text = f"Put {what} on “{self.part}” in “{self.song}”"
        return text + (f", clip gain {_db(self.gain_db)}" if self.gain_db else "")

    def run(self, ex):
        paths = [ex.file(f) for f in self.files]
        s = ex.song(self.song)
        t, created = ex.part_track(self.part)
        gain = self.gain_db or 0.0
        if len(paths) == 1:
            source, name = paths[0], paths[0].stem
        else:
            sides = parts.pans(self.files)
            out = ex.parts_dir / _file_safe(_song_name(s)) / f"{_file_safe(self.part)}.wav"
            try:
                mixed = mixdown.mix([(p, 0.0, sides[f]) for p, f in zip(paths, self.files)], out)
            except mixdown.MixdownError as e:
                raise ActionFailed(str(e)) from e
            # The mix is scaled to peak at -1 dBFS; clip gain puts its level back.
            source, name, gain = mixed["path"], self.part, gain - mixed["gain_db"]
        gain = max(-70.0, min(24.0, gain))
        result = ex.call("import_audio", track_index=t.index, file_path=str(source),
                         scene_index=s["index"], name=name, gain_db=gain if abs(gain) > 0.05 else None)
        what = f"{len(paths)} stems mixed" if len(paths) > 1 else result["name"]
        new = " (new track)" if created else ""
        return f"Put {what} on {t.name}{new} in {_song_name(s)}."


class SetClipGain(BaseModel):
    """A clip's own level in one song, for evening out stems at import. For 'louder in this song', use set_volume."""

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


class SetClipActive(BaseModel):
    """Leave a track's clip out of one song (on false), or bring it back (on true). Live applies it when the song starts, even without the app."""

    action: Literal["set_clip_active"]
    track: TrackRef
    song: SongRef
    on: bool = Field(description="False to leave this track out of this song only; true to bring it back.")

    def describe(self):
        return f"{'Bring back' if self.on else 'Leave out'} “{self.track}” in “{self.song}”"

    def run(self, ex):
        t = ex.track(self.track, allow_return=False)
        s = ex.song(self.song)
        ex.call("set_clip_active", track_index=t.index, scene_index=s["index"], on=self.on)
        return f"{t.name} is {'on' if self.on else 'off'} in {_song_name(s)}."


class PartChoice(BaseModel):
    """Which part a track's stems go to, in one song or all of them."""

    song: str | None = Field(default=None, description="A song name, or null for every song.")
    track: str = Field(description="A current track whose name doesn't say its part, e.g. a singer's name.")
    part: str = Field(description='The part it belongs to in that song, e.g. "Lead Vocal" or "BGVs".')


class TidyIntoParts(BaseModel):
    """Rebuild every song on the church's part tracks and remove the per-stem tracks."""

    action: Literal["tidy_into_parts"]
    assign: list[PartChoice] = Field(default_factory=list, max_length=100, description=(
        "Parts for tracks named after people or anything else that fits no part. The lead "
        "singer often changes per song: listen_to_stems each song first and assign per song."))
    destructive: ClassVar[bool] = True

    def describe(self):
        return ("Rebuild every song on the part tracks (Drums, Keys, BGVs...) and delete the "
                "per-stem tracks they replace. Takes a minute or two per song.")

    def run(self, ex):
        from app.tidy import tidy  # tidy builds on this module

        summary, *notes = tidy(ex, [(c.song, c.track, c.part) for c in self.assign])
        return summary + (" But " + _lower_first(" ".join(notes)) if notes else "")


class Listen(BaseModel):
    """Play a song briefly and read the meters. Makes sound in the room."""

    action: Literal["listen"]
    song: SongRef | None = Field(
        default=None, description="A song to start and measure. Null to measure whatever is already playing.",
    )
    seconds: int = Field(default=10, ge=3, le=30, description="How long to play and measure, 3 to 30 seconds.")
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
    MoveSong, TransposeSong, DeleteSong, StartSong, PickSongMix, SaveCheckpoint, RestoreCheckpoint,
    Transport, SetColor, MoveToFolder, ImportPart, SetClipGain, SetClipActive, TidyIntoParts, Listen,
]


class Proposal(BaseModel):
    """What the propose_changes tool takes."""

    actions: list[Annotated[Action, Field(discriminator="action")]] = Field(min_length=1, max_length=150)


# -- running them -------------------------------------------------------------


class Target:
    def __init__(self, index, is_return, name):
        self.index = index
        self.is_return = is_return
        self.name = name


class Executor:
    """Runs actions against Live, resolving names as it goes."""

    def __init__(self, live, files=None, on_touch=None, folders=None, parts_dir=None, mixes=None,
                 mix_control=None):
        self._live = live
        self.mixes = mixes  # the app's SongMixMemory, or None where there are no song mixes
        # The app, which puts a song's mix on the mixer and keeps checkpoints:
        # pick_song_mix(scene index), checkpoint_song_mix(label, song), restore_song_mix(id, song).
        self.mix_control = mix_control
        self._files = files or {}
        self.folders = folders  # the mixer's FolderMemory, or None where there is no mixer
        self.parts_dir = Path(parts_dir) if parts_dir else default_parts_dir()  # mixed parts go here
        self._on_touch = on_touch  # called with a track's name whenever an action works on it
        self._tracks = None
        self.detail = None  # extra facts for the assistant from the last action
        self.new_track_fader_db = None  # set when a batch imports audio; see run_all

    def file(self, file_id):
        """An imported file by the id the assistant saw. Only those, never any path."""
        wanted = file_id.strip().casefold()
        for known, path in self._files.items():
            if known.casefold() == wanted:
                return Path(path)  # saved imports come back from JSON as strings
        raise ActionFailed(f"I don't have a file called {file_id}. Import its folder first.")

    def call(self, cmd, **args):
        try:
            return self._live.call(cmd, **args)
        except RigLinkError as e:
            raise ActionFailed(_sentence(e)) from e

    def tracks_changed(self):
        self._tracks = None

    def touch(self, name):
        """Say which track an action is working on, so the page can light it up."""
        if self._on_touch and name:
            self._on_touch(name)

    def part_track(self, part):
        """The track for a part, by exact name (any case), made if the set lacks it.

        Returns (Target, created). A new one has No Input, a fader low enough for a whole
        song together, its folder's colour, and starts muted if it's timecode.
        """
        tracks, _returns = self._rows()
        for row in tracks:
            if row["name"].casefold() == part.strip().casefold():
                target = Target(row["index"], False, row["name"])
                self.touch(target.name)
                return target, False
        index = self.call("create_audio_track", name=part.strip())["index"]
        self.tracks_changed()
        self.touch(part)
        target = {"track_index": index, "is_return": False}
        problems = []
        if self.new_track_fader_db is not None:
            self.try_step(problems, "set its volume", "set_volume", **target, db=self.new_track_fader_db)
        if is_timecode(part):
            self.try_step(problems, "mute it", "set_mute", **target, on=True)
        colour = next(c for key, _label, c in FAMILIES if key == classify(part))
        self.try_step(problems, "colour it", "set_track_color", **target, rgb=TRACK_COLORS[colour])
        return Target(index, False, part.strip()), True

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
                    return f"{device.device} is on its default settings, because Ableton has no preset called “{device.preset}”"
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
        target = self._find_track(ref, allow_return)
        self.touch(target.name)
        return target

    def _find_track(self, ref, allow_return=True):
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
        # People (and smaller models) paraphrase: "lead vox", "backing vocals", "the keys".
        guess = _one_named(everything, ref, lambda t: t.name)
        if guess is not None:
            return guess
        part = parts.part_for(ref)
        if part:
            named = [t for t in everything if t.name.casefold() == part.casefold()]
            if len(named) == 1:
                return named[0]
        names = ", ".join(t.name for t in everything) or "none yet"
        raise ActionFailed(f"There's no track called {ref}. The tracks are: {names}.")

    def need_mix_control(self):
        if self.mix_control is None or self.mixes is None:
            raise ActionFailed("Song mixes need the Holy Sound app.")
        return self.mix_control

    def mix_song_name(self, ref):
        """The song a mix action is for: the named one, else the one on the mixer."""
        if ref is not None:
            name = self.song(ref)["name"]
            if not name:
                raise ActionFailed("Give that song a name first; its mix is kept by name.")
            return name
        if self.mixes is None or not self.mixes.current:
            raise ActionFailed("The mixer isn't on a song. Say which song, or pick one first.")
        return self.mixes.current

    def later_song(self, ref):
        """The song a mixer change is for, if it isn't the one the mixer is on now. Else None."""
        if ref is None:
            return None
        name = self.song(ref)["name"]
        if not name:
            raise ActionFailed("Give that song a name first; its mix is kept by name.")
        if self.mixes is None:
            raise ActionFailed("Song mixes need the Holy Sound app.")
        current = self.mixes.current
        return None if current and current.casefold() == name.casefold() else name

    def level_now(self, target, song=None, send_to=None):
        """A track's fader (or its send to a return) in dB: in a song's saved mix, else in Live now.

        Off reads as -70.
        """
        from app import song_mixes

        snap = self._live.snapshot(max_age=0)
        mix = (self.mixes.saved(song) if song and self.mixes else None) or song_mixes.mix_of(snap)
        strip = mix["returns" if target.is_return else "tracks"].get(target.name.casefold())
        if strip is None:
            strip = song_mixes.mix_of(snap)["returns" if target.is_return else "tracks"].get(target.name.casefold(), {})
        value = strip.get("sends", {}).get(send_to) if send_to else strip.get("volume_db")
        return song_mixes.SILENT_DB if value is None else float(value)

    def save_for_song(self, song, target, volume_db=None, pan=None, mute=None, send=None):
        """Change one track in a song's saved mix, without touching Live now."""
        from app import song_mixes

        def seed():
            return song_mixes.mix_of(self._live.snapshot(max_age=0))

        self.mixes.edit(song, seed, target.is_return, target.name,
                        volume_db=volume_db, pan=pan, mute=mute, send=send)

    def song(self, ref):
        rows = self.call("list_scenes")
        ref = ref.strip()
        named = [s for s in rows if s["name"].casefold() == ref.casefold()]
        if len(named) == 1:
            return named[0]
        if ref.isdigit() and 1 <= int(ref) <= len(rows):
            return rows[int(ref) - 1]
        guess = _one_named([s for s in rows if s["name"]], ref, lambda s: s["name"])
        if guess is not None:
            return guess
        names = ", ".join(s["name"] or str(s["index"] + 1) for s in rows) or "none yet"
        raise ActionFailed(f"There's no song called {ref}. The songs are: {names}.")


def _in_song(song, text):
    return f"{text} in “{song}”" if song else text


def default_parts_dir():
    """Where mixed parts are written: a folder people can find, not the vendor's."""
    home = os.environ.get("HOLYSOUND_HOME")
    return (Path(home) / "Parts") if home else Path.home() / "Music" / "Holy Sound" / "Parts"


def run_all(live, actions, files=None, on_touch=None, folders=None, parts_dir=None, mixes=None,
            mix_control=None, on_step=None):
    """Run actions in order. One failure doesn't stop the rest.

    on_touch(name) is called for each track an action works on. folders is the
    mixer's FolderMemory, for move_to_folder and renames. mixes and mix_control are
    the app's song mixes, for mixer changes meant for one song and checkpoints.
    on_step(n, state) is called as step n starts ("working") and ends ("done",
    "check" if it only partly worked, "failed"); steps skipped because Live went
    away are "failed".
    """
    step = on_step or (lambda n, state: None)
    ex = Executor(live, files, on_touch, folders, parts_dir, mixes, mix_control)
    # New audio tracks in a batch that imports a song start low enough that all of
    # its stems together don't clip, unless the assistant chose a volume itself.
    per_song = {}
    for action in actions:
        if isinstance(action, ImportPart):
            per_song[action.song.casefold()] = per_song.get(action.song.casefold(), 0) + 1
    if per_song:
        ex.new_track_fader_db = starting_fader_db(max(per_song.values()))
    results = []
    for n, action in enumerate(actions):
        step(n, "working")
        try:
            ex.detail = None
            text = action.run(ex)
            results.append({"ok": True, "partial": " But " in text, "text": text})
            if ex.detail:
                results[-1]["detail"] = ex.detail
            step(n, "check" if results[-1]["partial"] else "done")
        except ActionFailed as e:
            results.append({"ok": False, "text": f"{action.describe()}: {e}"})
            ex.tracks_changed()
            step(n, "failed")
        except LiveUnavailable as e:
            results.append({"ok": False, "text": f"{action.describe()}: {e}"})
            results.extend(
                {"ok": False, "text": f"{a.describe()}: not done. Lost touch with Ableton."}
                for a in actions[n + 1:]
            )
            for lost in range(n, len(actions)):
                step(lost, "failed")
            break
    return results


def is_destructive(action):
    return getattr(type(action), "destructive", False)


def is_audible(action):
    """Plays sound through the speakers when applied."""
    return getattr(action, "audible", False)


# -- exporting to a .als ------------------------------------------------------


def to_rigspec(actions):
    """The add_track actions as a RigSpec, plus notes on what a file can't hold.

    The file renderer only knows tracks so far (see docs/td_next.md). Everything else
    in a proposal only works against a running Live.
    """
    tracks, notes = [], []
    for action in actions:
        if not isinstance(action, AddTrack):
            notes.append(f"“{action.describe()}” only works with Ableton open.")
            continue
        spec_input = None
        if action.input and action.input.isdigit():
            spec_input = int(action.input)
        elif action.input:
            notes.append(f"{action.name}: stereo inputs can't go in a session file yet, so it has no input.")
        if action.output and action.output.destination.casefold() != "master":
            notes.append(f"{action.name}: only the Master output can go in a session file yet.")
        if action.color:
            notes.append(f"{action.name}: colours only apply in Ableton, not in a saved file.")
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


def _file_safe(name):
    """A song or part name as a file name: no slashes or other trouble."""
    cleaned = re.sub(r"[^\w .()&'-]+", "_", name).strip(" .")
    return cleaned or "Untitled"


def _semitones(n):
    return f"{'up' if n > 0 else 'down'} {abs(n)} semitone{'' if abs(n) == 1 else 's'}"


def _folder_label(key):
    return next(label for k, label, _colour in FAMILIES if k == key)


_FILLER = re.compile(r"^(the|our|my)\s+|\s+(song|track|tracks|part)$", re.IGNORECASE)


def _words(text):
    """Lower-case words with a trailing plural s dropped, so 'Vocals' and 'vocal' match."""
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w
            for w in re.findall(r"[a-z0-9]+", text.casefold().replace("’", "'").replace("'", ""))]


def _one_named(rows, ref, name_of):
    """The one row whose name matches a loose reference, or None if none or several do.

    Tried in turn: same words ignoring "the" and plurals; every word of the reference in the
    name ("Glad" for "I'm So Glad I Met Jesus").
    """
    want = _words(_FILLER.sub("", ref.strip()))
    if not want:
        return None
    for test in (lambda have: have == want, lambda have: all(w in have for w in want)):
        found = [r for r in rows if test(_words(name_of(r)))]
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def _one_of(model, *fields):
    given = [f for f in fields if getattr(model, f) is not None]
    if len(given) != 1:
        raise ValueError(f"give exactly one of {' or '.join(fields)}")
    return model


def _moved(level, by, now):
    """A level from either an absolute dB or a change from now(), kept to the fader's range."""
    if level is not None:
        return level
    return max(-70.0, min(6.0, max(-70.0, now()) + by))


def _level_change(track, db, by_db):
    if by_db is not None:
        return f"Turn “{track}” {'up' if by_db > 0 else 'down'} {abs(by_db):g} dB"
    return f"Set “{track}” to {_level(db)}"


# Where a fader sits for a level, as the page draws it: 0 dB is 80% of the travel.
_FADER_LAW = [(-70, 0.0), (-40, 0.16), (-30, 0.28), (-20, 0.42), (-10, 0.6), (0, 0.8), (6, 1.0)]


def _level(db):
    """A fader level in a volunteer's words: "off", or how far up the fader is."""
    if db is None or db <= -70:
        return "off"
    if db >= 6:
        return "100%"
    for (d0, p0), (d1, p1) in zip(_FADER_LAW, _FADER_LAW[1:]):
        if db <= d1:
            return f"{round((p0 + (db - d0) / (d1 - d0) * (p1 - p0)) * 100)}%"
    return "100%"


def _level_text(shown):
    """Live's own level string ("-4.0 dB", "-inf dB") in the same words."""
    match = re.search(r"-?\d+(?:\.\d+)?", shown or "")
    if "inf" in (shown or "") or not match:
        return "off"
    return _level(float(match.group()))


def _db(db):
    """A relative change in dB (clip levels only; faders use _level)."""
    if db <= -70:
        return "off"
    return f"{db:+g} dB" if db else "0 dB"


def _pan(pan):
    amount = round(abs(pan) * 100)
    if amount == 0:
        return "centre"
    return f"{amount}% {'left' if pan < 0 else 'right'}"


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
        return f"Ableton has no {noun} called “{wanted}”. It has: {options}."
    match = re.match(r"no stock device called '(.+?)'", text)
    if match:
        return f"Ableton has no effect called “{match.group(1)}”."
    if "index out of range" in text:
        return "That item doesn't exist any more. The set may have changed."
    return "Ableton said: " + text
