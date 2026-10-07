"""Folding a set's track-per-stem layout into the church's part tracks.

Before part tracks, every stem name got its own track, so three songs made 93.
This rebuilds each song onto the parts (app/parts.py): for every song and part it
mixes that part's clips into one (app/mixdown.py), puts it on the part track, and
finally deletes the stem tracks it emptied. It changes the open set; Live's undo
covers it, but it's many steps, so save a copy first.

What each song sounds like is kept: a clip's gain and its old track's fader are
baked into the mix, and part tracks end at 0 dB. A song's transpose is put back
afterwards. Stems that were switched off in a song, or on a muted track, are left
out. Effects and sends on the old tracks aren't carried over; the result says so.
"""

from __future__ import annotations

from pathlib import Path

from app import mixdown, parts
from app.actions import ActionFailed, _file_safe, _song_name
from live_control.timecode import is_timecode


def tidy(ex, assign=None):
    """Rebuild every song on part tracks. Returns a list of sentences about what happened.

    assign: [(song or None, track, part)] for tracks whose names say nothing about
    their part, such as singers' names. A song of None applies in every song.
    The lead often changes from song to song, so give it per song.
    """
    chosen = {((song or "").casefold(), track.casefold()): part for song, track, part in assign or []}
    snap_tracks = {t["index"]: t for t in ex.call("get_snapshot")["tracks"]}
    scenes = ex.call("list_scenes")
    kept_key = ex.folders.kept_tracks(list(snap_tracks.values())) if ex.folders is not None else []
    notes, emptied, built = [], set(), 0
    routes = {}  # part name -> the output its stems went to, for a part track made here

    plans = []
    for scene in scenes:
        rows = ex.call("song_files", scene_index=scene["index"])
        if not rows:
            continue
        grouped = {}
        for row in rows:
            track = snap_tracks[row["track_index"]]
            part = (chosen.get((scene["name"].casefold(), track["name"].casefold()))
                    or chosen.get(("", track["name"].casefold()))
                    or parts.part_for(track["name"]) or track["name"])
            clip = next(c for c in track.get("clips", []) if c["scene_index"] == scene["index"])
            grouped.setdefault(part, []).append((track, clip, row["file_path"]))
            routes.setdefault(part, track.get("output"))
        # Part order, so tracks this makes come out Click, Guide, ... Drums, Bass, ... BGVs.
        order = {name: i for i, name in enumerate(parts.PART_NAMES)}
        grouped = dict(sorted(grouped.items(), key=lambda kv: order.get(kv[0], len(order))))
        plans.append((scene, grouped))

    for scene, grouped in plans:
        name = _song_name(scene)
        for part, sources in grouped.items():
            # Timecode is kept muted on its track on purpose; it still belongs in the song.
            heard = [(t, c, f) for t, c, f in sources
                     if c.get("active", True) and (not t["mute"] or is_timecode(t["name"]))]
            left_out = [t["name"] for t, c, f in sources if (t, c, f) not in heard]
            if left_out:
                notes.append(f"In {name}, left {', '.join(left_out)} out of {part}: "
                             "it was switched off or muted.")
            mixing = heard or sources  # all off: keep it, switched off below
            target, _created = ex.part_track(part)
            # The part track's own clip in this song is one of the sources; it's read from disk.
            for t, _c, _f in sources:
                if t["index"] == target.index:
                    ex.call("delete_clip", track_index=target.index, scene_index=scene["index"])
            if len(mixing) == 1:
                t, c, f = mixing[0]
                file, gain = f, (c.get("gain_db") or 0.0) + (t.get("volume_db") or 0.0)
            else:
                names = [Path(f).stem for _t, _c, f in mixing]
                sides = parts.pans(names)
                out = ex.parts_dir / _file_safe(name) / f"{_file_safe(part)}.wav"
                try:
                    mixed = mixdown.mix([(f, (c.get("gain_db") or 0.0) + (t.get("volume_db") or 0.0),
                                          sides[Path(f).stem]) for t, c, f in mixing], out)
                except mixdown.MixdownError as e:
                    raise ActionFailed(f"{name}, {part}: {e}") from e
                file, gain = mixed["path"], -mixed["gain_db"]
            ex.call("import_audio", track_index=target.index, file_path=str(file), scene_index=scene["index"],
                    name=part, gain_db=max(-70.0, min(24.0, gain)))
            if not heard:
                ex.call("set_clip_active", track_index=target.index, scene_index=scene["index"], on=False)
            built += 1
            emptied.update(t["index"] for t, _c, _f in sources if t["index"] != target.index)
        if scene.get("transpose"):
            current = ex.call("list_tracks")
            keep = ex.folders.kept_tracks(current) if ex.folders is not None else kept_key
            ex.call("transpose_song", scene_index=scene["index"], semitones=scene["transpose"], skip_tracks=keep)

    # Part tracks: faders to 0 dB (their levels are in the clips now), outputs as before.
    current = {t["name"].casefold(): t for t in ex.call("list_tracks")}
    for part, output in routes.items():
        row = current.get(part.casefold())
        if row is None:
            continue
        ex.call("set_volume", track_index=row["index"], is_return=False, db=0.0)
        if output and output.get("type") not in (None, "Main", "Master"):
            try:
                ex.call("set_routing", track_index=row["index"], is_return=False, direction="output",
                        type_name=output["type"], channel_name=output.get("channel") or None)
            except ActionFailed:
                notes.append(f"Couldn't send {part} to {output['type']} {output.get('channel', '')}; check it.")

    # Delete the stem tracks that are now empty, last first so the numbers don't shift.
    lost = sorted({e for i in emptied for e in snap_tracks[i]["devices"]})
    for index in sorted(emptied, reverse=True):
        ex.call("delete_track", track_index=index, is_return=False)
    ex.tracks_changed()
    if lost:
        notes.append("Effects on the old stem tracks weren't carried over: " + ", ".join(lost) + ".")
    summary = (f"Rebuilt {len(plans)} song{'s' if len(plans) != 1 else ''} on part tracks: "
               f"{built} part clips, {len(emptied)} stem tracks removed.")
    return [summary] + notes
