"""Command line for editing a running Live Set through RigLink.

    uv run rig.py status
    uv run rig.py track list
    uv run rig.py track add "Click"
    uv run rig.py route out Click "Ext. Out" 1
    uv run rig.py mix volume Click -6
    uv run rig.py effect add "Lead Vocal" Compressor --preset "Gentle Squeeze"
    uv run rig.py song import ~/Downloads/"Let's Have Church" --bpm 170
    uv run rig.py song play "Let's Have Church"

Commands are grouped by what they act on: track, mix, route, effect, song,
marker. A song is one scene in Live's Session view.

Tracks are picked by name, by the number Live shows beside them (1-based), or
for return tracks by their letter (A, B, ...). Songs, markers and effects are
picked by name or number.
"""

import fnmatch
import re
import socket
import time
from pathlib import Path

import typer

from live_control.live_connection import LiveConnection, RigLinkError
from live_control.starting_fader import starting_fader_db
from live_control.stem_level import pair_level, stem_level
from live_control.stereo_pairs import stereo_pairs
from live_control.timecode import is_timecode

# Loading a device walks Live's browser tree, which can take a while.
TIMEOUT_SECONDS = 30.0

# Lets negative numbers like -6 through as arguments instead of options.
NEGATIVE_NUMBERS = {"ignore_unknown_options": True}

# File types Live imports as audio clips.
AUDIO_SUFFIXES = {".wav", ".aif", ".aiff", ".flac", ".mp3"}

# Loudness every imported stem is brought to, as active RMS in dBFS. The sum
# of many stems is far louder; starting_fader_db makes room for it.
STEM_LEVEL_DB = -20.0
# Never boost a stem past this peak, or spiky stems like click would clip.
STEM_PEAK_CEILING_DB = -1.0
CLIP_GAIN_RANGE_DB = (-24.0, 24.0)

app = typer.Typer(no_args_is_help=True, add_completion=False)
track_app = typer.Typer(no_args_is_help=True, help="Add, rename, colour and delete tracks.")
mix_app = typer.Typer(no_args_is_help=True, help="Volume, pan, mute, solo, sends and levels.")
route_app = typer.Typer(no_args_is_help=True, help="Where a track's audio comes from and goes to.")
effect_app = typer.Typer(no_args_is_help=True, help="Stock Live effects and their presets.")
song_app = typer.Typer(no_args_is_help=True, help="Songs in the set. Each song is one scene.")
marker_app = typer.Typer(no_args_is_help=True, help="Named markers on the Arrangement timeline.")
app.add_typer(track_app, name="track")
app.add_typer(mix_app, name="mix")
app.add_typer(route_app, name="route")
app.add_typer(effect_app, name="effect")
app.add_typer(song_app, name="song")
app.add_typer(marker_app, name="marker")


def _connect():
    try:
        return LiveConnection(timeout=TIMEOUT_SECONDS)
    except (ConnectionRefusedError, socket.timeout):
        _fail(
            "Couldn't reach Ableton Live. Make sure Live is open and RigLink is "
            "selected as a Control Surface in Preferences → Link, Tempo & MIDI."
        )


def _fail(message):
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _run(call):
    """Run one RigLink call, turning its failures into a sentence."""
    try:
        return call()
    except RigLinkError as e:
        _fail(f"Live couldn't do that: {e}")
    except socket.timeout:
        _fail("Live stopped responding before it finished.")


def _return_letter(index):
    return chr(ord("A") + index)


def _pick(rows, choice, noun):
    """Pick a row by 1-based number or case-insensitive name."""
    if choice.isdigit():
        number = int(choice)
        if 1 <= number <= len(rows):
            return rows[number - 1]
        _fail(f"There's no {noun} {number}. There are {len(rows)}.")

    matches = [r for r in rows if r["name"].casefold() == choice.casefold()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        _fail(f"More than one {noun} is called {choice!r}. Use its number instead.")
    names = ", ".join(r["name"] for r in rows) or "none"
    _fail(f"No {noun} called {choice!r}. Options: {names}.")


def _resolve_track(live, choice):
    """Turn a track name, number, or return letter into a target dict.

    The dict has index, is_return, and label (how Live shows it: "3" or "A").
    """
    tracks = _run(live.list_tracks)
    returns = _run(live.list_returns)
    if choice.isdigit():
        row = _pick(tracks, choice, "track")
        return {"index": row["index"], "is_return": False, "label": str(row["index"] + 1)}

    everything = [dict(t, is_return=False) for t in tracks] + [dict(r, is_return=True) for r in returns]
    matches = [t for t in everything if t["name"].casefold() == choice.casefold()]
    if len(matches) > 1:
        _fail(f"More than one track is called {choice!r}. Use its number or letter instead.")
    if matches:
        row = matches[0]
    elif len(choice) == 1 and choice.isalpha():
        index = ord(choice.upper()) - ord("A")
        if not 0 <= index < len(returns):
            _fail(f"There's no return track {choice.upper()}.")
        row = dict(returns[index], is_return=True)
    else:
        names = ", ".join(t["name"] for t in everything) or "none"
        _fail(f"No track called {choice!r}. Tracks in this set: {names}.")

    label = _return_letter(row["index"]) if row["is_return"] else str(row["index"] + 1)
    return {"index": row["index"], "is_return": row["is_return"], "label": label}


def _parse_pan(text):
    """'C', '25L', 'L25', '50R' -> -1..1. Live shows pan as 50L..C..50R."""
    text = text.strip().upper()
    if text in ("C", "CENTER", "CENTRE", "0"):
        return 0.0
    match = re.fullmatch(r"(\d+)([LR])|([LR])(\d+)", text)
    if not match:
        _fail(f"Pan should look like C, 25L, or 25R — got {text!r}.")
    amount = int(match.group(1) or match.group(4))
    side = match.group(2) or match.group(3)
    if amount > 50:
        _fail("Pan goes from 50L to 50R.")
    return (amount / 50.0) * (-1 if side == "L" else 1)


def _beats_per_bar(song):
    return song["numerator"] * 4 / song["denominator"]


# -- Top level: connection and transport -----------------------------------


@app.command()
def status():
    """Check the connection to Live and show tempo and playback."""
    with _connect() as live:
        s = _run(live.get_song)
    state = "playing" if s["is_playing"] else "stopped"
    typer.echo(f"Connected to Live. {s['tempo']:g} BPM, {s['numerator']}/{s['denominator']}, {state}.")


@app.command()
def play():
    """Start playback."""
    with _connect() as live:
        _run(live.play)
    typer.echo("Playing.")


@app.command()
def stop():
    """Stop playback."""
    with _connect() as live:
        _run(live.stop)
    typer.echo("Stopped.")


@app.command()
def tempo(bpm: float = typer.Argument(..., help="Beats per minute, e.g. 72.")):
    """Set the tempo right now. For a song's own tempo, use `song tempo`."""
    with _connect() as live:
        result = _run(lambda: live.set_tempo(bpm))
    typer.echo(f"Tempo: {result['tempo']:g} BPM")


# -- track -----------------------------------------------------------------


@track_app.command("list")
def track_list():
    """List the tracks and return tracks in the open set."""
    with _connect() as live:
        rows = _run(live.list_tracks)
        returns = _run(live.list_returns)
    if not rows and not returns:
        typer.echo("This set has no tracks.")
    for t in rows:
        kind = "MIDI" if t["is_midi"] else "Audio"
        typer.echo(f"{t['index'] + 1:>3}  {t['name']}  ({kind})")
    for r in returns:
        typer.echo(f"{_return_letter(r['index']):>3}  {r['name']}  (Return)")


@track_app.command("add")
def track_add(
    name: str = typer.Argument(None, help="Name for the new track."),
    midi: bool = typer.Option(False, "--midi", help="Make a MIDI track instead of audio."),
    is_return: bool = typer.Option(False, "--return", help="Make a return track (for shared reverb or delay)."),
):
    """Add a track to the end of the set."""
    if midi and is_return:
        _fail("A track can be MIDI or a return, not both.")
    with _connect() as live:
        if is_return:
            result = _run(lambda: live.create_return_track(name=name))
            typer.echo(f"Added return {_return_letter(result['index'])}: {result['name']}")
            return
        create = live.create_midi_track if midi else live.create_audio_track
        result = _run(lambda: create(name=name))
    typer.echo(f"Added track {result['index'] + 1}: {result['name']}")


@track_app.command("rename")
def track_rename(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    name: str = typer.Argument(..., help="New name."),
):
    """Rename a track."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_track_name(target["index"], name, target["is_return"]))
    typer.echo(f"Track {target['label']} is now {result['name']}.")


TRACK_COLORS = {
    "red": 0xFF3636,
    "orange": 0xFF9A00,
    "yellow": 0xFFE140,
    "green": 0x3DC300,
    "teal": 0x00BFAF,
    "blue": 0x3C82F0,
    "purple": 0xA67CF0,
    "pink": 0xFF5FBE,
    "grey": 0x7A7A7A,
}


@track_app.command("color")
def track_color(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    name: str = typer.Argument(..., help=f"One of: {', '.join(TRACK_COLORS)}."),
):
    """Colour a track and its clips. Live picks the nearest colour from its own palette."""
    rgb = TRACK_COLORS.get(name.casefold())
    if rgb is None:
        _fail(f"Pick one of these colours: {', '.join(TRACK_COLORS)}.")
    with _connect() as live:
        target = _resolve_track(live, track)
        _run(lambda: live.set_track_color(target["index"], rgb, target["is_return"]))
    typer.echo(f"Track {target['label']} is now {name.casefold()}.")


@track_app.command("delete")
def track_delete(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """Delete a track or return track. Undo in Live with Cmd+Z."""
    with _connect() as live:
        target = _resolve_track(live, track)
        _run(lambda: live.delete_track(target["index"], target["is_return"]))
    typer.echo(f"Deleted track {target['label']}.")


# -- route -----------------------------------------------------------------


def _routing_text(side):
    return f"{side['type']} / {side['channel']}" if side["channel"] else side["type"]


def _echo_routing_side(label, options_label, side):
    typer.echo(f"{label:<7} {_routing_text(side)}")
    typer.echo(f"        {options_label}: {', '.join(side['types'])}")
    channels = [c for c in side["channels"] if c]  # "No Input" lists one blank channel
    if channels:
        typer.echo(f"        channels: {', '.join(channels)}")


@route_app.command("show")
def route_show(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """Show a track's input and output, and what they can be set to."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.get_routing(target["index"], target["is_return"]))
    if "input" in result:
        _echo_routing_side("Input:", "from", result["input"])
    _echo_routing_side("Output:", "to", result["output"])


def _set_routing(track, direction, source, channel):
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(
            lambda: live.set_routing(target["index"], direction, source, channel, target["is_return"])
        )
    typer.echo(f"Track {target['label']} {direction}: {_routing_text(result)}")


@route_app.command("in")
def route_in(
    track: str = typer.Argument(..., help="Track name or number."),
    source: str = typer.Argument(..., help='Input type as Live shows it, e.g. "Ext. In" or "No Input".'),
    channel: str = typer.Argument(None, help='Channel as Live shows it, e.g. "1" or "1/2".'),
):
    """Set where a track gets its audio or MIDI from."""
    _set_routing(track, "input", source, channel)


@route_app.command("out")
def route_out(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    destination: str = typer.Argument(..., help='Output type as Live shows it, e.g. "Ext. Out" or "Main".'),
    channel: str = typer.Argument(None, help='Channel as Live shows it, e.g. "1" or "3/4".'),
):
    """Set where a track sends its audio, e.g. click to the drummer's outputs."""
    _set_routing(track, "output", destination, channel)


# -- mix -------------------------------------------------------------------


@mix_app.command("show")
def mix_show(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """Show a track's volume, pan, mute, solo and sends."""
    with _connect() as live:
        target = _resolve_track(live, track)
        m = _run(lambda: live.get_mixer(target["index"], target["is_return"]))
    flags = [f for f, on in (("muted", m["mute"]), ("soloed", m["solo"])) if on]
    typer.echo(f"Volume {m['volume']}, pan {m['pan']}" + (f", {' and '.join(flags)}" if flags else ""))
    for i, s in enumerate(m["sends"]):
        typer.echo(f"  Send {_return_letter(i)} ({s['return']}): {s['level']}")


@mix_app.command("volume", context_settings=NEGATIVE_NUMBERS)
def mix_volume(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    db: float = typer.Argument(..., help="Level in dB, e.g. -6 or 0. Use -inf for silent."),
):
    """Set a track's fader in dB."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_volume(target["index"], db, target["is_return"]))
    typer.echo(f"Track {target['label']} volume: {result['volume']}")


@mix_app.command("pan")
def mix_pan(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    position: str = typer.Argument(..., help="C for center, or 25L / 25R (up to 50)."),
):
    """Pan a track left or right."""
    value = _parse_pan(position)
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_pan(target["index"], value, target["is_return"]))
    typer.echo(f"Track {target['label']} pan: {result['pan']}")


@mix_app.command("mute")
def mix_mute(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    off: bool = typer.Option(False, "--off", help="Unmute instead."),
):
    """Mute a track (or unmute it with --off)."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_mute(target["index"], not off, target["is_return"]))
    typer.echo(f"Track {target['label']} is {'muted' if result['mute'] else 'unmuted'}.")


@mix_app.command("solo")
def mix_solo(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    off: bool = typer.Option(False, "--off", help="Unsolo instead."),
):
    """Solo a track (or unsolo it with --off)."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_solo(target["index"], not off, target["is_return"]))
    typer.echo(f"Track {target['label']} is {'soloed' if result['solo'] else 'not soloed'}.")


@mix_app.command("send", context_settings=NEGATIVE_NUMBERS)
def mix_send(
    track: str = typer.Argument(..., help="Track sending the audio."),
    to: str = typer.Argument(..., help="Return track name or letter."),
    db: float = typer.Argument(..., help="Send level in dB, e.g. -12. Use -inf for none."),
):
    """Set how much of a track goes to a return, e.g. vocal into reverb."""
    with _connect() as live:
        target = _resolve_track(live, track)
        dest = _resolve_track(live, to)
        if not dest["is_return"]:
            _fail(f"{to!r} isn't a return track. Sends only go to returns.")
        result = _run(lambda: live.set_send(target["index"], dest["index"], db, target["is_return"]))
    typer.echo(f"Track {target['label']} send {dest['label']}: {result['level']}")


def _meter_label(row):
    if row["kind"] == "return":
        return _return_letter(row["index"])
    if row["kind"] == "master":
        return "M"
    return str(row["index"] + 1)


@mix_app.command("levels")
def mix_levels(seconds: float = typer.Option(5.0, "--seconds", help="How long to listen.")):
    """Listen for a few seconds and report how loud each track got.

    Meter values run 0 to 1, as Live's track meters show them (after the
    fader, before mute). Their mapping to dB hasn't been checked yet.
    """
    with _connect() as live:
        _run(live.reset_meters)
        time.sleep(seconds)
        result = _run(live.get_meters)
    if result["ticks"] == 0:
        _fail("Live didn't report any meter readings. Is the set open and not frozen?")
    for row in result["tracks"]:
        label = f"{_meter_label(row):>3}  {row['name']}"
        if not row["has_audio_output"]:
            typer.echo(f"{label}  (no audio output)")
        elif row["peak"] == 0:
            typer.echo(f"{label}  silent")
        else:
            typer.echo(f"{label}  peak {row['peak']:.2f}, average {row['average']:.2f}")


# -- effect ----------------------------------------------------------------


@effect_app.command("list")
def effect_list(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """List the effects on a track, in order."""
    with _connect() as live:
        target = _resolve_track(live, track)
        rows = _run(lambda: live.list_devices(target["index"], target["is_return"]))
    if not rows:
        typer.echo(f"Track {target['label']} has no effects.")
    for d in rows:
        rack = ", rack" if d["is_rack"] else ""
        typer.echo(f"{d['index'] + 1:>3}  {d['name']}  ({d['class_name']}{rack})")


@effect_app.command("add")
def effect_add(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    device: str = typer.Argument(..., help="Stock effect name as Live's browser shows it, e.g. Reverb."),
    preset: str = typer.Option(None, "--preset", help="A stock preset of that effect, e.g. \"Gentle Squeeze\"."),
):
    """Load a stock Live effect, or one of its presets, onto a track."""
    with _connect() as live:
        target = _resolve_track(live, track)
        _run(lambda: live.load_device(target["index"], device, preset, target["is_return"]))
    loaded = f"{device} ({preset})" if preset else device
    typer.echo(f"Loaded {loaded} on track {target['label']}.")


@effect_app.command("remove")
def effect_remove(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    device: str = typer.Argument(..., help="Effect name or its number from `effect list`."),
):
    """Remove an effect from a track."""
    with _connect() as live:
        target = _resolve_track(live, track)
        rows = _run(lambda: live.list_devices(target["index"], target["is_return"]))
        row = _pick(rows, device, "effect")
        result = _run(lambda: live.delete_device(target["index"], row["index"], target["is_return"]))
    typer.echo(f"Removed {result['name']} from track {target['label']}.")


@effect_app.command("presets")
def effect_presets(device: str = typer.Argument(..., help="Stock effect name, e.g. Compressor.")):
    """List the stock presets for an effect."""
    with _connect() as live:
        names = _run(lambda: live.list_presets(device))
    if not names:
        typer.echo(f"{device} has no stock presets.")
    for name in names:
        typer.echo(name.removesuffix(".adv"))


# -- song ------------------------------------------------------------------
# A song is a scene in Session view: one row of clips that launch together,
# optionally with its own tempo.


def _key_text(semitones):
    if semitones is None:
        return "mixed transpose"
    return f"{semitones:+d} semitone{'' if abs(semitones) == 1 else 's'}"


def _song_line(s):
    bpm = f"  ({s['tempo']:g} BPM)" if s["tempo"] is not None else ""
    key = "" if s.get("transpose", 0) == 0 else f"  [{_key_text(s['transpose'])}]"
    return f"{s['index'] + 1:>3}  {s['name']}{bpm}{key}"


@song_app.command("list")
def song_list():
    """List the songs (scenes) in the set."""
    with _connect() as live:
        rows = _run(live.list_scenes)
    for s in rows:
        typer.echo(_song_line(s))


@song_app.command("add")
def song_add(
    name: str = typer.Argument(None, help="Song title."),
    bpm: float = typer.Option(None, "--bpm", help="Tempo Live switches to when this song starts."),
):
    """Add an empty song at the end."""
    with _connect() as live:
        result = _run(lambda: live.create_scene(name, bpm))
    typer.echo("Added song: " + _song_line(result).strip())


@song_app.command("rename")
def song_rename(
    song: str = typer.Argument(..., help="Song name or number."),
    name: str = typer.Argument(..., help="New name."),
):
    """Rename a song."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), song, "song")
        result = _run(lambda: live.set_scene(row["index"], name=name))
    typer.echo("Song " + _song_line(result).strip())


@song_app.command("tempo")
def song_tempo(
    song: str = typer.Argument(..., help="Song name or number."),
    bpm: float = typer.Argument(..., help="Tempo Live switches to when this song starts."),
):
    """Give a song its own tempo."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), song, "song")
        result = _run(lambda: live.set_scene(row["index"], bpm=bpm))
    typer.echo("Song " + _song_line(result).strip())


@song_app.command("transpose", context_settings={"ignore_unknown_options": True})  # so -2 is a number
def song_transpose(
    song: str = typer.Argument(..., help="Song name or number."),
    semitones: int = typer.Argument(..., min=-12, max=12, help="Semitones up (2) or down (-3); 0 is the original key."),
):
    """Shift every audio clip in a song to a new key."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), song, "song")
        result = _run(lambda: live.transpose_song(row["index"], semitones))
    typer.echo(f"Song {_song_line(result).strip()}: {result['clips']} clip(s) at {_key_text(semitones)}")


@song_app.command("delete")
def song_delete(song: str = typer.Argument(..., help="Song name or number.")):
    """Delete a song and its clips. Undo in Live with Cmd+Z."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), song, "song")
        _run(lambda: live.delete_scene(row["index"]))
    typer.echo(f"Deleted song {row['index'] + 1}: {row['name']}")


@song_app.command("play")
def song_play(song: str = typer.Argument(..., help="Song name or number.")):
    """Start a song from the top."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), song, "song")
        _run(lambda: live.fire_scene(row["index"]))
    typer.echo(f"Playing song {row['index'] + 1}: {row['name']}")


def _song_title(name):
    """Scene name without a trailing tempo tag, e.g. "Song [170]" -> "song"."""
    return re.sub(r"\s*\[\d+(\.\d+)?\]\s*$", "", name).casefold()


def _empty_song_slot(live, scenes):
    """First unnamed scene with no clips, so a fresh set's blank rows get used."""
    for s in scenes:
        if not s["name"] and _run(lambda: live.count_scene_clips(s["index"])) == 0:
            return s
    return None


def _select_stems(stems, patterns):
    """Stems matching any pattern, in pattern order, so --only also sets track order."""
    if not patterns:
        return stems
    chosen = []
    for pattern in patterns:
        matches = [p for p in stems if fnmatch.fnmatch(p.stem.casefold(), pattern.casefold())]
        if not matches:
            names = ", ".join(p.stem for p in stems)
            _fail(f"No stem matches {pattern!r}. Stems in that folder: {names}.")
        chosen += [p for p in matches if p not in chosen]
    return chosen


def _level_match_gain(level):
    """Clip gain that brings a level to STEM_LEVEL_DB, and a note when it can't."""
    wanted = STEM_LEVEL_DB - level.active_rms_db
    headroom = STEM_PEAK_CEILING_DB - level.peak_db
    low, high = CLIP_GAIN_RANGE_DB
    gain = max(low, min(high, wanted, headroom))
    return gain, ("held back to avoid clipping" if headroom < wanted else "")


def _level_match_gains(stems):
    """Clip gain and a note for each stem. Both sides of a stereo pair share one gain."""
    levels = {p: stem_level(p) for p in stems}
    by_name = {p.stem: p for p in stems}
    partner = {}
    for left, right in stereo_pairs(list(by_name)):
        partner[by_name[left]] = by_name[right]
        partner[by_name[right]] = by_name[left]

    gains = {}
    for stem in stems:
        other = partner.get(stem)
        level = levels[stem]
        if level is None and stem.suffix.casefold() != ".wav":
            gains[stem] = (0.0, "not a WAV, level not matched")
            continue
        if other is not None and (other.suffix.casefold() == ".wav" or levels[other] is not None):
            level = pair_level(level, levels[other])
        if level is None:
            gains[stem] = (0.0, "silent")
            continue
        gain, note = _level_match_gain(level)
        if other is not None:
            note = ", ".join(n for n in (f"same gain as {other.stem}", note) if n)
        gains[stem] = (gain, note)
    return gains


@song_app.command("import")
def song_import(
    folder: Path = typer.Argument(..., help="Folder of stems, one audio file per track."),
    name: str = typer.Option(None, "--name", help="Song title. Defaults to the folder name."),
    bpm: float = typer.Option(None, "--bpm", help="Tempo Live switches to when this song starts."),
    only: list[str] = typer.Option(
        None, "--only", help='Import just these stems, by name or wildcard, e.g. "Loops*". Repeat for more.'
    ),
    match_levels: bool = typer.Option(
        True, "--match-levels/--keep-levels", help="Even out stem levels so one fader setting suits every song."
    ),
):
    """Add a song from a folder of stems.

    Each stem goes on the track with the same name, or a new track if there
    isn't one. Stems play once at their own speed, so they stay in sync.
    With --match-levels (the default), clip gain brings every stem to the same
    loudness, so the faders set the mix for all songs at once. New tracks
    start with their faders down far enough that all the stems together don't
    clip; existing tracks keep theirs. A new SMPTE/timecode track starts muted.
    Stereo pairs ("GTR L" and "GTR R") get one clip gain between them, so the
    image stays centred.
    """
    if not folder.is_dir():
        _fail(f"There's no folder at {folder}.")
    stems = sorted(p for p in folder.iterdir() if p.suffix.casefold() in AUDIO_SUFFIXES)
    if not stems:
        _fail(f"There are no audio files in {folder}.")
    stems = _select_stems(stems, only)
    name = name or folder.name
    gains = _level_match_gains(stems) if match_levels else {p: (None, "") for p in stems}

    with _connect() as live:
        scenes = _run(live.list_scenes)
        if any(_song_title(s["name"]) == _song_title(name) for s in scenes):
            _fail(f"There's already a song called {name!r}.")
        slot = _empty_song_slot(live, scenes)
        if slot is None:
            slot = _run(lambda: live.create_scene(name, bpm))
        else:
            _run(lambda: live.set_scene(slot["index"], name=name, bpm=bpm))
        scene = slot["index"]

        tracks = {t["name"].casefold(): t["index"] for t in _run(live.list_tracks)}
        fader = starting_fader_db(len(stems))
        for stem in stems:
            index = tracks.get(stem.stem.casefold())
            is_new = index is None
            if is_new:
                index = _run(lambda: live.create_audio_track(name=stem.stem))["index"]
                tracks[stem.stem.casefold()] = index
                _run(lambda: live.set_volume(index, fader))
                if is_timecode(stem.stem):
                    _run(lambda: live.set_mute(index, True))
            gain, note = gains[stem]
            clip = _run(lambda: live.import_audio(index, str(stem.resolve()), scene, stem.stem, gain))
            muted = "muted, it's timecode" if is_new and is_timecode(stem.stem) else ""
            notes = ", ".join(n for n in (f"new track at {fader:g} dB" if is_new else "", muted, note) if n)
            typer.echo(f"{index + 1:>3}  {stem.stem:<20} gain {clip['gain']:>9}" + (f"  ({notes})" if notes else ""))

    typer.echo(f"Added song {scene + 1}: {name} ({len(stems)} stems).")


# -- marker ----------------------------------------------------------------


@marker_app.command("list")
def marker_list():
    """List markers, in time order."""
    with _connect() as live:
        per_bar = _beats_per_bar(_run(live.get_song))
        rows = _run(live.list_locators)
    if not rows:
        typer.echo("No markers.")
    for c in rows:
        typer.echo(f"{c['index'] + 1:>3}  bar {c['time'] / per_bar + 1:g}  {c['name']}")


@marker_app.command("add")
def marker_add(
    bar: float = typer.Argument(..., help="Bar number, starting at 1."),
    name: str = typer.Argument(None, help="Marker name, e.g. the song title."),
):
    """Add a marker at a bar."""
    if bar < 1:
        _fail("Bars start at 1.")
    with _connect() as live:
        per_bar = _beats_per_bar(_run(live.get_song))
        result = _run(lambda: live.add_locator((bar - 1) * per_bar, name))
    typer.echo(f"Added marker {result['name']!r} at bar {bar:g}.")


@marker_app.command("delete")
def marker_delete(marker: str = typer.Argument(..., help="Marker name or number.")):
    """Delete a marker."""
    with _connect() as live:
        row = _pick(_run(live.list_locators), marker, "marker")
        _run(lambda: live.delete_locator(row["index"]))
    typer.echo(f"Deleted marker {row['name']!r}.")


@marker_app.command("jump")
def marker_jump(marker: str = typer.Argument(..., help="Marker name or number.")):
    """Move the playhead to a marker."""
    with _connect() as live:
        row = _pick(_run(live.list_locators), marker, "marker")
        _run(lambda: live.jump_to_locator(row["index"]))
    typer.echo(f"Jumped to {row['name']!r}.")


if __name__ == "__main__":
    app()
