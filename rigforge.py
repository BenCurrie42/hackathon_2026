"""Command line for editing a running Live Set through RigLink.

    uv run rigforge.py status
    uv run rigforge.py tracks
    uv run rigforge.py add-track "Click"
    uv run rigforge.py output Click "Ext. Out" "3/4"
    uv run rigforge.py volume Click -6
    uv run rigforge.py add-device "Lead Vocal" Compressor --preset "Gentle Squeeze"

Tracks are picked by name, by the number Live shows beside them (1-based), or
for return tracks by their letter (A, B, ...). Scenes, locators and devices are
picked by name or number.
"""

import re
import socket

import typer

from runtime import LiveConnection, RigLinkError

# Loading a device walks Live's browser tree, which can take a while.
TIMEOUT_SECONDS = 30.0

# Lets negative numbers like -6 through as arguments instead of options.
NEGATIVE_NUMBERS = {"ignore_unknown_options": True}

app = typer.Typer(no_args_is_help=True, add_completion=False)


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


# -- Tracks ----------------------------------------------------------------


@app.command(rich_help_panel="Tracks")
def status():
    """Check that Live and RigLink are connected."""
    with _connect() as live:
        _run(live.ping)
    typer.echo("Connected to Live.")


@app.command(rich_help_panel="Tracks")
def tracks():
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


@app.command("add-track", rich_help_panel="Tracks")
def add_track(
    name: str = typer.Argument(None, help="Name for the new track."),
    midi: bool = typer.Option(False, "--midi", help="Make a MIDI track instead of audio."),
):
    """Add a track to the end of the set."""
    with _connect() as live:
        create = live.create_midi_track if midi else live.create_audio_track
        result = _run(lambda: create(name=name))
    typer.echo(f"Added track {result['index'] + 1}: {result['name']}")


@app.command("add-return", rich_help_panel="Tracks")
def add_return(name: str = typer.Argument(None, help="Name for the new return track.")):
    """Add a return track (for shared reverb or delay)."""
    with _connect() as live:
        result = _run(lambda: live.create_return_track(name=name))
    typer.echo(f"Added return {_return_letter(result['index'])}: {result['name']}")


@app.command(rich_help_panel="Tracks")
def rename(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    name: str = typer.Argument(..., help="New name."),
):
    """Rename a track."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_track_name(target["index"], name, target["is_return"]))
    typer.echo(f"Track {target['label']} is now {result['name']}.")


@app.command("delete-track", rich_help_panel="Tracks")
def delete_track(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """Delete a track or return track. Undo in Live with Cmd+Z."""
    with _connect() as live:
        target = _resolve_track(live, track)
        _run(lambda: live.delete_track(target["index"], target["is_return"]))
    typer.echo(f"Deleted track {target['label']}.")


# -- Routing ---------------------------------------------------------------


def _routing_text(side):
    return f"{side['type']} / {side['channel']}" if side["channel"] else side["type"]


def _echo_routing_side(label, options_label, side):
    typer.echo(f"{label:<7} {_routing_text(side)}")
    typer.echo(f"        {options_label}: {', '.join(side['types'])}")
    channels = [c for c in side["channels"] if c]  # "No Input" lists one blank channel
    if channels:
        typer.echo(f"        channels: {', '.join(channels)}")


@app.command(rich_help_panel="Routing")
def routing(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
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


@app.command("input", rich_help_panel="Routing")
def set_input(
    track: str = typer.Argument(..., help="Track name or number."),
    source: str = typer.Argument(..., help='Input type as Live shows it, e.g. "Ext. In" or "No Input".'),
    channel: str = typer.Argument(None, help='Channel as Live shows it, e.g. "1" or "1/2".'),
):
    """Set where a track gets its audio or MIDI from."""
    _set_routing(track, "input", source, channel)


@app.command("output", rich_help_panel="Routing")
def set_output(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    destination: str = typer.Argument(..., help='Output type as Live shows it, e.g. "Ext. Out" or "Master".'),
    channel: str = typer.Argument(None, help='Channel as Live shows it, e.g. "3/4".'),
):
    """Set where a track sends its audio, e.g. click to the drummer's outputs."""
    _set_routing(track, "output", destination, channel)


# -- Mixer -----------------------------------------------------------------


@app.command(rich_help_panel="Mixer")
def mixer(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """Show a track's volume, pan, mute, solo and sends."""
    with _connect() as live:
        target = _resolve_track(live, track)
        m = _run(lambda: live.get_mixer(target["index"], target["is_return"]))
    flags = [f for f, on in (("muted", m["mute"]), ("soloed", m["solo"])) if on]
    typer.echo(f"Volume {m['volume']}, pan {m['pan']}" + (f", {' and '.join(flags)}" if flags else ""))
    for i, s in enumerate(m["sends"]):
        typer.echo(f"  Send {_return_letter(i)} ({s['return']}): {s['level']}")


@app.command(context_settings=NEGATIVE_NUMBERS, rich_help_panel="Mixer")
def volume(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    db: float = typer.Argument(..., help="Level in dB, e.g. -6 or 0. Use -inf for silent."),
):
    """Set a track's fader in dB."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_volume(target["index"], db, target["is_return"]))
    typer.echo(f"Track {target['label']} volume: {result['volume']}")


@app.command(rich_help_panel="Mixer")
def pan(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    position: str = typer.Argument(..., help="C for center, or 25L / 25R (up to 50)."),
):
    """Pan a track left or right."""
    value = _parse_pan(position)
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_pan(target["index"], value, target["is_return"]))
    typer.echo(f"Track {target['label']} pan: {result['pan']}")


@app.command(rich_help_panel="Mixer")
def mute(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    off: bool = typer.Option(False, "--off", help="Unmute instead."),
):
    """Mute a track (or unmute it with --off)."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_mute(target["index"], not off, target["is_return"]))
    typer.echo(f"Track {target['label']} is {'muted' if result['mute'] else 'unmuted'}.")


@app.command(rich_help_panel="Mixer")
def solo(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    off: bool = typer.Option(False, "--off", help="Unsolo instead."),
):
    """Solo a track (or unsolo it with --off)."""
    with _connect() as live:
        target = _resolve_track(live, track)
        result = _run(lambda: live.set_solo(target["index"], not off, target["is_return"]))
    typer.echo(f"Track {target['label']} is {'soloed' if result['solo'] else 'not soloed'}.")


@app.command(context_settings=NEGATIVE_NUMBERS, rich_help_panel="Mixer")
def send(
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


# -- Devices ---------------------------------------------------------------


@app.command(rich_help_panel="Devices")
def devices(track: str = typer.Argument(..., help="Track name, number, or return letter.")):
    """List the devices on a track, in order."""
    with _connect() as live:
        target = _resolve_track(live, track)
        rows = _run(lambda: live.list_devices(target["index"], target["is_return"]))
    if not rows:
        typer.echo(f"Track {target['label']} has no devices.")
    for d in rows:
        rack = ", rack" if d["is_rack"] else ""
        typer.echo(f"{d['index'] + 1:>3}  {d['name']}  ({d['class_name']}{rack})")


@app.command("add-device", rich_help_panel="Devices")
def add_device(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    device: str = typer.Argument(..., help="Stock device name as Live's browser shows it, e.g. Reverb."),
    preset: str = typer.Option(None, "--preset", help="A stock preset of that device, e.g. \"Gentle Squeeze\"."),
):
    """Load a stock Live device, or one of its presets, onto a track."""
    with _connect() as live:
        target = _resolve_track(live, track)
        _run(lambda: live.load_device(target["index"], device, preset, target["is_return"]))
    loaded = f"{device} ({preset})" if preset else device
    typer.echo(f"Loaded {loaded} on track {target['label']}.")


@app.command("remove-device", rich_help_panel="Devices")
def remove_device(
    track: str = typer.Argument(..., help="Track name, number, or return letter."),
    device: str = typer.Argument(..., help="Device name or its number from `devices`."),
):
    """Remove a device from a track."""
    with _connect() as live:
        target = _resolve_track(live, track)
        rows = _run(lambda: live.list_devices(target["index"], target["is_return"]))
        row = _pick(rows, device, "device")
        result = _run(lambda: live.delete_device(target["index"], row["index"], target["is_return"]))
    typer.echo(f"Removed {result['name']} from track {target['label']}.")


@app.command(rich_help_panel="Devices")
def presets(device: str = typer.Argument(..., help="Stock device name, e.g. Compressor.")):
    """List the stock presets for a device."""
    with _connect() as live:
        names = _run(lambda: live.list_presets(device))
    if not names:
        typer.echo(f"{device} has no stock presets.")
    for name in names:
        typer.echo(name)


# -- Song: tempo and transport ---------------------------------------------


@app.command(rich_help_panel="Song")
def song():
    """Show tempo, time signature, and whether Live is playing."""
    with _connect() as live:
        s = _run(live.get_song)
    state = "playing" if s["is_playing"] else "stopped"
    typer.echo(f"{s['tempo']:g} BPM, {s['numerator']}/{s['denominator']}, {state}.")


@app.command(rich_help_panel="Song")
def tempo(bpm: float = typer.Argument(..., help="Beats per minute, e.g. 72.")):
    """Set the song tempo."""
    with _connect() as live:
        result = _run(lambda: live.set_tempo(bpm))
    typer.echo(f"Tempo: {result['tempo']:g} BPM")


@app.command(rich_help_panel="Song")
def play():
    """Start playback."""
    with _connect() as live:
        _run(live.play)
    typer.echo("Playing.")


@app.command(rich_help_panel="Song")
def stop():
    """Stop playback."""
    with _connect() as live:
        _run(live.stop)
    typer.echo("Stopped.")


# -- Scenes ----------------------------------------------------------------


def _scene_line(s):
    bpm = f"  ({s['tempo']:g} BPM)" if s["tempo"] is not None else ""
    return f"{s['index'] + 1:>3}  {s['name']}{bpm}"


@app.command(rich_help_panel="Scenes")
def scenes():
    """List the scenes (one per song, usually)."""
    with _connect() as live:
        rows = _run(live.list_scenes)
    for s in rows:
        typer.echo(_scene_line(s))


@app.command("add-scene", rich_help_panel="Scenes")
def add_scene(
    name: str = typer.Argument(None, help="Scene name, e.g. the song title."),
    bpm: float = typer.Option(None, "--bpm", help="Tempo Live switches to when this scene starts."),
):
    """Add a scene at the end."""
    with _connect() as live:
        result = _run(lambda: live.create_scene(name, bpm))
    typer.echo("Added scene: " + _scene_line(result).strip())


@app.command("rename-scene", rich_help_panel="Scenes")
def rename_scene(
    scene: str = typer.Argument(..., help="Scene name or number."),
    name: str = typer.Argument(..., help="New name."),
):
    """Rename a scene."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), scene, "scene")
        result = _run(lambda: live.set_scene(row["index"], name=name))
    typer.echo("Scene " + _scene_line(result).strip())


@app.command("scene-tempo", rich_help_panel="Scenes")
def scene_tempo(
    scene: str = typer.Argument(..., help="Scene name or number."),
    bpm: float = typer.Argument(..., help="Tempo Live switches to when this scene starts."),
):
    """Give a scene its own tempo."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), scene, "scene")
        result = _run(lambda: live.set_scene(row["index"], bpm=bpm))
    typer.echo("Scene " + _scene_line(result).strip())


@app.command("delete-scene", rich_help_panel="Scenes")
def delete_scene(scene: str = typer.Argument(..., help="Scene name or number.")):
    """Delete a scene. Undo in Live with Cmd+Z."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), scene, "scene")
        _run(lambda: live.delete_scene(row["index"]))
    typer.echo(f"Deleted scene {row['index'] + 1}: {row['name']}")


@app.command("fire-scene", rich_help_panel="Scenes")
def fire_scene(scene: str = typer.Argument(..., help="Scene name or number.")):
    """Launch a scene."""
    with _connect() as live:
        row = _pick(_run(live.list_scenes), scene, "scene")
        _run(lambda: live.fire_scene(row["index"]))
    typer.echo(f"Launched scene {row['index'] + 1}: {row['name']}")


# -- Locators --------------------------------------------------------------


@app.command(rich_help_panel="Locators")
def locators():
    """List Arrangement locators, in time order."""
    with _connect() as live:
        per_bar = _beats_per_bar(_run(live.get_song))
        rows = _run(live.list_locators)
    if not rows:
        typer.echo("No locators.")
    for c in rows:
        typer.echo(f"{c['index'] + 1:>3}  bar {c['time'] / per_bar + 1:g}  {c['name']}")


@app.command("add-locator", rich_help_panel="Locators")
def add_locator(
    bar: float = typer.Argument(..., help="Bar number, starting at 1."),
    name: str = typer.Argument(None, help="Locator name, e.g. the song title."),
):
    """Add an Arrangement locator at a bar."""
    if bar < 1:
        _fail("Bars start at 1.")
    with _connect() as live:
        per_bar = _beats_per_bar(_run(live.get_song))
        result = _run(lambda: live.add_locator((bar - 1) * per_bar, name))
    typer.echo(f"Added locator {result['name']!r} at bar {bar:g}.")


@app.command("delete-locator", rich_help_panel="Locators")
def delete_locator(locator: str = typer.Argument(..., help="Locator name or number.")):
    """Delete an Arrangement locator."""
    with _connect() as live:
        row = _pick(_run(live.list_locators), locator, "locator")
        _run(lambda: live.delete_locator(row["index"]))
    typer.echo(f"Deleted locator {row['name']!r}.")


@app.command(rich_help_panel="Locators")
def jump(locator: str = typer.Argument(..., help="Locator name or number.")):
    """Move the playhead to a locator."""
    with _connect() as live:
        row = _pick(_run(live.list_locators), locator, "locator")
        _run(lambda: live.jump_to_locator(row["index"]))
    typer.echo(f"Jumped to {row['name']!r}.")


if __name__ == "__main__":
    app()
