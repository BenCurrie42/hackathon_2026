# What's next

What the tool can't do yet, and what each gap costs a volunteer on Sunday.
Ordered by how much it blocks a real service.

Today RigLink can: add, rename, colour and delete tracks; set volume, pan,
mute, solo and sends; route inputs and outputs; load stock effects and their
presets; add, name, tempo, transpose and fire songs (scenes); import a folder of stems;
add and jump to markers; read output levels.

## 1. Conversation → RigSpec — done in 1.0.0

The web app (`app/`) turns a conversation into intent-only actions that run
through RigLink on Apply, and into a downloadable `.als` with Live closed.
What's left of this item is the open question in CLAUDE.md: RigLink still
doesn't consume `RigSpec`, so the two backends speak different vocabularies.

## 2. Record-arm and monitoring

RigLink never touches a track's arm button or its monitor mode (In / Auto /
Off). A vocal mic routed to an audio track is silent until someone arms it or
sets monitoring to In by hand in Live.

For a worship rig this is the most common reason for "the mic isn't working."
The Live API exposes `track.arm` and `track.current_monitoring_state`, so this
is a small command pair: `mix monitor <track> in|auto|off` and, if needed,
`track arm`.

## 3. Effect settings

`effect` loads a stock device or one of its presets and removes it. Beyond
that, the only settings it can change are EQ Eight's bands (below). Delay time,
feedback, reverb decay and dry/wet are still out of reach; the only way to
"make the reverb longer" is to pick a different preset.

This was on purpose. The model describes intent, never raw numbers, because
LLM-written parameter values are guesswork. But some adjustments are intent
in disguise:

- **Delay synced to the song.** A worship delay is almost always a dotted
  eighth at the song's tempo. That changes every song.
- **Dry/wet.** "A bit less reverb on the vocal" is the most common mix request
  there is.

The fix keeps the principle: expose a short, named list of safe controls per
device (Delay time and feedback, Reverb decay, dry/wet everywhere) and set them
the way volume is already set: by searching the parameter's own display text,
so Live remains the source of truth for units. No arbitrary parameter access.

**EQ Eight bands: done.** RigLink's `get_eq` / `set_eq_band` (on, type, Hz,
dB, Q per band) work that way, the assistant's `set_eq` fixes a messed-up EQ
from rule-based problems (`app/eq.py`), EQ is part of each song's mix, and the
channel drawer has a draggable curve. Parameter names were probed in Live
12.4.6 (see `docs/documentation/riglink.md`).
Delay time/feedback, reverb decay and dry/wet remain.

## 4. Non-stock and user content

RigLink runs on whatever set is open, so an existing church set with its own
track names and interface works. What doesn't:

- **Third-party plugins (VST/AU).** Can't be loaded. If already on a track,
  `effect list` shows them and `effect remove` deletes them, but nothing can
  adjust them.
- **User Library presets.** The browser search only covers stock audio
  effects, MIDI effects and instruments. A church that saved its own
  "Sunday Vocal" chain can't load it by name.
- **Racks.** A rack loads as one device. Its chains, inner devices and macros
  are not reachable.

User Library search is the cheapest of the three and the most useful: it lets
a church keep its own sound and still use the tool.

## 5. Moving between songs

Each song is a scene, and `song play` starts one from the top. There's no
follow action, no "next song", no stop-at-end. Whoever runs tech has to fire
every song by hand at the right moment.

This depends on the open question in `CLAUDE.md`: Session scenes or
Arrangement locators. If scenes, follow actions on the last clip (or a
`song next` command) cover it. If locators, it's a different design entirely.
Ask the church before building.

## 6. MIDI mapping

No way to map a footswitch or pad controller to "start next song" or "stop."
Many worship drummers or MDs trigger tracks from stage, not the booth.

Live's MIDI map lives in the set, not the Live API, so this likely means
either writing mappings into the `.als` (needs a probe) or handling the MIDI
input inside RigLink itself and triggering scenes from there.

## 7. Clip editing

Audio import works, and so does clip gain. Missing:

- **Warping.** Stems import unwarped on purpose so they stay sample-locked to
  each other. The side effect: `song tempo` changes Live's tempo but does
  **not** speed stems up or down. Correct for stems recorded at one tempo,
  surprising to anyone who expects to slow a song for a new band.
- **Loop points and start offsets.** No way to loop an intro pad or skip a
  count-in.
- **MIDI notes.** No MIDI clip content at all.
- **Single-clip launch.** Only whole songs fire; you can't trigger one clip.

## 8. Group tracks and automation

No group (folder) tracks, so a 20-stem song is 20 flat faders with no "all
the band stems" master. No automation, so nothing like a filter sweep into a
bridge or a reverb throw on the last line.

Groups are worth doing; automation probably isn't for v1. A volunteer won't
ask for it by name.

## 9. File renderer gaps

The `.als` renderer (`file_builder/write_als.py`) lags behind RigLink:

- **Hardware output routing.** Only `AudioOut/Main` is known. Click to the
  drummer and guide to the band (the point of a click/pad/guide rig) needs
  one probe to learn the target string.
- **Stereo input pairs.** Unknown; only mono inputs are verified.
- **Scenes, locators, imported audio.** RigLink has them; the spec and
  renderer don't.

If the runtime driver becomes the main path, some of these may never need
the file renderer. Decide that before probing.
