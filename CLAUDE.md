# CLAUDE.md

Project context: what the code does and what the .als format actually does.

## What this is

A tool that builds a ready-to-run Ableton Live session for a church worship team
from a plain-English conversation. The user describes their Sunday (what's
plugged in, who's playing, what songs) and gets a finished `.als` they open and
run. No Ableton expertise required.

Built for the **2026 Gloo AI Hackathon** (Oct 6–8, Boulder), Ministry Resourcing
track. Pre-build window opened Sept 8, 2026.

The user is a volunteer running tech at a small church, not an audio engineer.
That single fact settles most design arguments here: no JSON in the UI, no CLI,
no manual install steps, errors phrased as sentences.

Product framing is in `README.md`. What's missing, ranked, is in
`docs/td_next.md`. Module-by-module developer docs are in `docs/documentation/`
(mirrored to the GitHub wiki by `scripts/publish_wiki.py`); update the matching page
when you change a module, then republish. Edit the repo pages, never the wiki directly.

## Architecture

The **Rig Spec is the product.** It's a Pydantic model describing a session in
terms a human would use. Everything else is replaceable around it.

```
conversation → RigSpec (rig_spec.py) → render() (write_als.py) → .als
                    ↑                        ↑
              model writes it          swappable backend
```

Two backends, both built:

1. **File renderer** (`file_builder/write_als.py`) writes a `.als` with Live closed. Works
   on every Live edition, tests without Ableton installed, produces a file you
   can email. Consumes `RigSpec`.
2. **Runtime driver** (RigLink) is a Control Surface inside Live, driven over a
   local socket by `live_control/live_connection.py` and the `rig.py` CLI. Edits appear in the
   open set without reopening. Better demo, worse install story. Does **not**
   consume `RigSpec` yet. Its vocabulary (scenes, locators, stem import,
   meters) has run ahead of the spec.

RigLink never writes raw device parameters either. Volume and sends are set in
dB by binary-searching the parameter's own `str_for_value()` text, so Live's
fader law is the only source of truth for the conversion.

**The model emits intent, never parameters.** "Lead vocal, gentle compression",
not `Ratio: 3.5, Attack: 12ms`. An LLM writing device parameter values produces
hallucinated, version-specific garbage. The renderer owns the translation from
intent to a concrete stock preset.

**Invalid states are unrepresentable rather than policed.** The model hands over a
whole spec; it never calls `add_track()`. It therefore cannot violate Live's
structural invariants, because it never touches the mechanism.

## Verified facts about the .als format

The format is undocumented and version-specific. **Everything below was verified
empirically against real files on this machine**. Do not add to this list from
training data or plausible inference. If something is ambiguous, say so and ask.

Reference set: `templates/test.als`, saved by **Ableton Live 12.4.5** on macOS.

### Container

- `.als` is **gzip-compressed XML**. Magic bytes `1f 8b`.
- Live accepts any valid gzip stream. Python's `gzip.compress` opens fine.
  Compression level and header fields don't matter.
- **Live 12.4.5 also opens plain uncompressed XML with a `.als` extension.**
  Confirmed by experiment; prior community reports only covered Live 8–10. Useful
  for debugging. Ship gzip anyway.
- Re-serializing with lxml is safe, including lxml rewriting Ableton's
  single-quoted `ViewData` JSON attribute into `&quot;` entities.

### Ableton's own formatting

Matched by `write_als.py` out of caution, not demonstrated necessity:

- Declaration `<?xml version="1.0" encoding="UTF-8"?>` then `\n`
- Tab indentation, trailing newline at EOF
- Self-closing tags written `<Foo />` with a space (lxml emits `<Foo/>`)

Live writes floats at 10 significant digits (`0.7079457641`); we write full
Python precision (`0.7079457843841379`). Live accepts ours.

### Structure

- Tracks are children of `<LiveSet><Tracks>`, one element per type:
  `<AudioTrack Id="8">`, `<MidiTrack Id="12">`, `<ReturnTrack>`. `<MasterTrack>`
  and `<PreHearTrack>` sit outside. Audio vs MIDI is the element name.
- Regular tracks come **before** return tracks in document order.
- Name: `<Name><EffectiveName/><UserName/></Name>`. `EffectiveName` displays;
  `UserName` is what the user typed, empty until renamed. Which one Live reads on
  load is still unknown, so write both.
- Routing: `AudioInputRouting` / `AudioOutputRouting` / `MidiInputRouting` /
  `MidiOutputRouting`, each with a machine `<Target Value>` plus
  `UpperDisplayString` / `LowerDisplayString` for the UI.

  | Routing                   | Target                    | Upper      | Lower |
  | ------------------------- | ------------------------- | ---------- | ----- |
  | Hardware input _n_ (mono) | `AudioIn/External/M{n-1}` | `Ext. In`  | `n`   |
  | No input                  | `AudioIn/None`            | `No Input` | `""`  |
  | Main out                  | `AudioOut/Main`           | `Master`   | `""`  |

  Verified for inputs 1–4. **Hardware output routing is still unknown**: only
  `AudioOut/Main`. Stereo input pairs are unknown.

- **Volume is a linear gain factor, not dB.** 0.0 dB is `<Manual Value="1"/>`.
  **gain = 10^(dB/20)**, derived from the fader range (min `0.0003162277571` =
  10^(−70/20), max `1.99526238` = 10^(6/20)) and confirmed in Live at −6 dB.
- Pan: `<Manual Value="0"/>`, range −1 to 1.
- Devices live at `DeviceChain > DeviceChain > Devices` (nested twice).

### Devices come from Live's own preset files

Live ships its stock library as `.adv` files: same gzipped XML, each containing
exactly the device element that goes inside `<Devices>`:

```
/Applications/Ableton Live 12 Trial.app/Contents/App-Resources/Core Library/
    Defaults/Audio Effects/<Device>.adv     ← 10 true factory defaults
    Devices/Audio Effects/<Device>/*.adv    ← 47 device folders, 2557 presets
```

Verified: the `.adv` `<Reverb>` and an in-set `<Reverb>` are structurally
identical: same 53 children, same tags, same order. The only root difference is
that the in-set one carries an `Id`.

Inserting a device is: read the `.adv`, add an `Id`, renumber its pointee IDs,
append to `<Devices>`. Generalizes to all 47 stock effects.

Element names are internal, not display names: `Compressor2`, `Eq8`,
`AutoFilter2`, `Chorus2`, `PhaserNew`, `Hybrid`. Scan for the mapping; never
guess it.

Only 10 devices have a factory default on disk. Compressor isn't one, so
`PRESET_FALLBACKS` in `write_als.py` names a preset instead.

### Vendor sets (read only)

Verified on a Washed download (`MultiTrack.als`, `Creator="Ableton Live 8.4.2"`):

- One song per set: 43 AudioTracks, one Arrangement clip each, all at `Time="0"`,
  unwarped, `PitchCoarse` 0, so a Session import from the file start lines up the
  same. Sections are `Locators/Locators/Locator` (`Time` in beats, `Name`); the
  8 Session scenes are empty.
- Clips at `Tracks/AudioTrack/DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip`.
- Live 8 sample path: `FileRef > RelativePath > RelativePathElement Dir=...` plus
  `FileRef > Name`, relative to the set's folder. Live 12 has `RelativePath Value`
  and an absolute `Path Value` instead.
- Live 8 tempo: `MasterTrack/MasterChain/Mixer/Tempo/ArrangerAutomation/Events/FloatEvent
  Value`; Live 12 uses `Tempo/Manual Value`.

## Hard invariants: violating these corrupts the set

1. **The pointee ID pool spans 11 tag names, not 3.** `AutomationTarget`,
   `ModulationTarget`, `Pointee`, plus eight specialised variants that live in
   clip and sample nodes: `TranspositionModulationTarget`,
   `ComplexProEnvelopeModulationTarget`, `GrainSizeModulationTarget`,
   `FluxModulationTarget`, `SampleOffsetModulationTarget`,
   `TransientEnvelopeModulationTarget`, `VolumeModulationTarget`,
   `ComplexProFormantsModulationTarget`. All 326 in the template are unique
   across the whole document. Match by suffix (`Pointee`, or ends with `Target`),
   never by a hand-written list. **We shipped this bug once**, and the ones a
   hand-written list misses are exactly the ones a cloned track brings with it.

   Failure mode is a _repair prompt_, not a rejection: Live silently reconciles
   what it can't resolve, so the set looks fine and has quietly lost state. Worse
   than a hard failure. `_Renderer.check_pointee_pool` runs before anything
   reaches disk.

2. **`<NextPointeeId>` must exceed every ID in that pool.** Bump after allocating.
3. **Send lists must match return tracks exactly.** One `<TrackSendHolder>` per
   return track, IDs sequential `0..N-1`. Mismatch crashes Live on load with
   `invalid vector subscript`.
4. **Track IDs are their own global namespace.** Allocate `max + 1`. Device IDs
   are context-scoped and legitimately repeat across tracks.
5. **Routing targets embed track IDs inside strings**: `AudioIn/Track.14/TrackOut`.
   A reference hiding in plain text, not an `Id` attribute. Renumbering must
   regex these.
6. **Never synthesize `<Ableton MajorVersion ... Creator>`.** Copy verbatim from
   the template. Version compatibility is one-directional; forging the header
   produces sets that open with silently mangled devices.
7. `LomId` is a runtime handle, always `0` in saved files. Leave it alone.

## Track layout rules: keep these when changing imports

A set once grew to 93 tracks for three songs because every vendor stem name got its
own track, each with Live's default input. These hold everywhere audio comes in
(the app's `import_part`, `rig.py song import`, `tidy_into_parts`), and
`TrackLayoutGuardTest` checks them:

1. **Every song uses the same part tracks** (`app/parts.py`: Click, Guide, SMPTE,
   Loops, Drums, Perc, Synth Bass, Bass, Acoustic, Electric, Piano, Organ, Keys,
   Strings, Horns, Synths, FX, Lead Vocal, Choir, BGVs, Crowd). Never a track per
   stem. Several stems for one part are mixed into one file per song
   (`app/mixdown.py`, written under `~/Music/Holy Sound/Parts`, never into the
   vendor's folder); the scaling to -1 dBFS goes back as clip gain.
2. **The model can't create stem sprawl**: `import_audio` (one file onto a named
   track) isn't an action it can propose; `import_part` is, and it makes a missing
   part track itself.
3. **Playback tracks have No Input.** RigLink's `create_audio_track` sets it;
   live sources set their input explicitly afterwards.
4. **A song's own level lives in its clips**: clip gain, which Live applies when the song
   starts, app open or not. A part is left out of one song by muting it in that song (the
   clip activator stops a playing clip but won't restart it mid-song, so it isn't used for
   this any more). Faders, pan, mute and sends are per song only through
   the app (`app/song_mixes.py`): it saves them while the mixer is on a song and puts
   them back when that song is picked or starts. With no song picked, or the app closed,
   they're shared by every song.

## Constraints

- Python 3.11+. **lxml, pydantic, typer, anthropic only.** Nothing else
  without asking; pytest is not yet approved (tests use stdlib unittest).
  `anthropic` is for the chat. The web app itself is stdlib `http.server`
  plus static files, no framework or build step. OpenCode Go's
  OpenAI-compatible models go over stdlib `urllib`, not the `openai` package;
  its Anthropic-format models reuse the `anthropic` SDK with a custom `base_url`.
  Gloo AI Studio uses the same stdlib Chat path. The MCP server is hand-written
  JSON-RPC, not the `mcp` package.
- Stock Ableton devices only. No third-party plugins.
- **Never mutate `templates/`.** Fixtures are `chmod a-w` as a backstop. Git
  doesn't record that bit, so a fresh clone needs `chmod a-w templates/*` again.
- Renderer tests (`tests/test_write_als.py`) must run without Ableton installed;
  the ones that read Live's `.adv` presets skip when it's absent.
- We own the ID renumbering rather than depending on `kmontag/buildable`. Read
  that project for reference (it solved this first), but a 0-star dependency
  failing at hour 40 in Boulder is worse than 60 lines we control.

## Layout

```
rig.py                          CLI over RigLink: track, mix, route, effect,
                                song (= scene), marker
ableton_script/RigLink/         Control Surface that runs inside Live; socket
                                bridge for the live-edit path
live_control/
    live_connection.py          LiveConnection: client for RigLink
    stem_level.py               Active RMS and peak of a WAV stem, stdlib only;
                                drives level matching on `song import`
    starting_fader.py           Fader for tracks an import creates, so the sum of
                                every stem doesn't clip
    timecode.py                 Spots SMPTE/timecode stems, which start muted
    stereo_pairs.py             Pairs L/R stems so both sides get the same gain
file_builder/                   Writes a .als with Live closed
    rig_spec.py                 RigSpec / TrackSpec, the contract
    write_als.py                render(spec, template_path) -> bytes
app/                            Web app: chat + mixer, `uv run python -m app`
    server.py                   HTTP server and JSON API
    assistant.py                Claude conversation → proposed actions
    expert.py                   Expert mode: a lead and four specialists fix the mix and
                                apply it without asking; also `python -m app.expert`
    providers.py                Who answers: Anthropic, or OpenCode Go models
                                (OpenAI Chat or Anthropic format), and discovery
    actions.py                  Proposable actions (intent only) and how each runs
    live.py                     Shared, self-reconnecting RigLink connection
    audio_files.py              Folder browsing + stdlib WAV/AIFF level measurement
    room.py                     Week-to-week room memory (~/.holysound/room.json)
    parts.py                    The church's fixed part tracks; which part a stem is
    mixdown.py                  Sums a part's stems into one 24-bit WAV (stdlib)
    tidy.py                     Folds a track-per-stem set into part tracks
    song_key.py                 Guesses a song's key from its pitched stems (stdlib)
    vendor_set.py               Reads a vendor's one-song set (Washed, MultiTracks):
                                tempo, section locators, stem per track. Read only
    song_map.py                 Reads a song's stems as text: when each part sounds,
                                sections, tempo from the click, likely lead vocal
    folders.py                  Mixer folders (Vocals / Instruments / Click & playback / Other):
                                sorted by track name, plus moves remembered in ~/.holysound/folders.json
    song_mixes.py               Each song's faders, pan, mute and sends, and named checkpoints,
                                in ~/.holysound/song_mixes.json; the server puts them back on a pick
    eq.py                       EQ Eight in words: filter types, the curve as text for the
                                assistant, rule-based EQ problems, set_eq_band diffs
    fake_live.py                In-memory stand-in for Live + RigLink, for tests/demo
    mcp.py                      MCP server (stdio JSON-RPC, stdlib): any agent drives the
                                running app's JSON API
    static/                     The page: HTML/CSS/JS, served as-is
tests/                          unittest suite; needs neither Live nor an API key.
                                `uv run python -m unittest` runs all of it
templates/test.als              Reference Live 12.4.5 set, read-only
templates/test.reference.xml    Its decompressed XML, for diffing
templates/probe_noinput.als     Live's own save of a generated set; source of
                                truth for AudioIn/None
docs/td_next.md                 What's missing, ranked by Sunday impact
docs/agents_of_flourishing.md   What to add for the Gloo Challenge 1 judges, ranked
scripts/install_riglink.py      Links RigLink and picks it as Live's Control Surface
scripts/publish_wiki.py         Copies docs/documentation into a wiki clone
scripts/eq_eval.py              Messes up an EQ on fake Live, asks the model to fix it, scores it

Run everything from the repo root: imports are package-relative to it
(`from live_control.live_connection import ...`).
```

## How we learn new format facts

Live is the source of truth, not inference. The loop:

1. Generate a set with the thing we're unsure about.
2. Open it in Live, change that one thing by hand, save.
3. Diff Live's save against ours; the difference is the answer.

This is how `AudioIn/None` was found. The same diff also validates the renderer:
our generated file and Live's own save of it had **zero structural differences**
(same element paths, same tree, only float formatting apart). Keep saved probes
in `templates/` as fixtures.

## Status

Renderer works end to end. A `RigSpec` with named tracks, hardware or no input,
volume in dB, pan, and stock devices renders to a `.als` that opens in Live with
no repair prompt. Verified with a four-track click/pad/guide rig.

RigLink works against any open set, not just our template: tracks (add, rename,
colour, delete), mixer (volume, pan, mute, solo, sends, output meters), input and
output routing, stock effects and their presets (load, list, remove), songs as
scenes with per-song tempo and transpose (`pitch_coarse` on every audio clip in
the scene, -12 to 12), Arrangement markers, and `song import` of a stem folder
with level matching via clip gain.

The web app (`app/`) is the conversation layer. Claude proposes typed actions
(`app/actions.py`), the volunteer presses Apply, and each action runs through
RigLink. With Live closed, the `add_track` actions become a `RigSpec` and download
as a `.als`.

Missing, in order (detail in `docs/td_next.md`): record-arm and monitoring; any
effect parameter control; plugins, User Library presets and rack internals;
song-to-song transitions; MIDI mapping; clip editing beyond gain and transpose;
group tracks.

## Open questions


- **Setlist shape: Session scenes or Arrangement locators?** Worship rigs are
  usually one scene per song with BPM in the scene name, but unconfirmed for this
  church. Gates extending the spec past the rig. **Ask, don't assume.**
- **Hardware output routing.** Only `AudioOut/Main` known. A click/pad/guide rig
  is pointless until click reaches the drummer and guide reaches the band. Needs
  one probe.
- **Should RigLink consume `RigSpec`?** The two backends speak different
  vocabularies. The app currently targets RigLink through its own actions and
  only maps `add_track` onto `RigSpec`.
- **One stem loudness measure.** `app/audio_files.py` (90th percentile of 0.4 s
  windows) and `live_control/stem_level.py` (active RMS, drives `rig.py song
  import`) measure differently. Pick one. `app/song_map.py` adds a third use
  (1 s envelope, "sounding" relative to each stem's own loud level), which only
  needs relative levels, so it can stay separate.

## Working notes

- Live 12 Trial at `/Applications/Ableton Live 12 Trial.app`, auto-updated to
  12.4.6. Format facts above were verified on 12.4.5. Trial runs with Suite
  features.
- RigLink is installed by `uv run python scripts/install_riglink.py`: it symlinks
  `ableton_script/RigLink` into `~/Music/Ableton/User Library/Remote Scripts/` and
  picks it as a Control Surface by editing `Preferences.cfg` with Live closed
  (falls back to Preferences → Link/MIDI by hand). Socket is `localhost:9877`.
- Live imports RigLink once at startup. **After editing it, quit and reopen
  Live**; the symptom otherwise is `unknown cmd`. `rig.py` and client edits need
  no restart.
- **Transposing an unwarped clip changes its speed too**, like tape: `pitch_coarse`
  +2 plays about 12% faster. Confirmed by ear in Live 12.4.6. So a transposed clip is
  warped (Complex Pro) and pinned 1:1 at the song's tempo. Probed in 12.4.6:
  - `clip.warp_mode = 6` is Complex Pro (reads back 6).
  - Turning Warp on for a long stem warps it at Live's own tempo guess (a 120 BPM
    click came out at ~128 BPM: 64 beats instead of 60). That guess lives in a
    "shadow" marker, which `move_warp_marker` refuses ("The shadow marker can't be
    moved").
  - Fix: keep the first marker at (0 s, beat 0) and `add_warp_marker(
    Live.Clip.WarpMarker(sample_time=s, beat_time=s * bpm / 60))`. The clip then
    measured exactly 60 beats for 30 s at 120 BPM.
  - Unwarping keeps the warped end marker's number, now read as seconds, so the
    clip ran 30 s past its file. Reset `end_marker`/`loop_end` to the file length.
- Probed in 12.4.6 through RigLink: `clip.muted` is the clip activator (set and read
  back both ways). `ClipSlot.duplicate_clip_to` carries a warped, transposed clip
  intact (same warp markers, length, pitch), so `move_scene` (insert, copy clips,
  delete the original) keeps a transposed song transposed. Live has no move-scene call.
- Stems import unwarped so they stay sample-locked. Song tempo therefore does not
  stretch them. Auto-Warp still moves each clip's start to its guessed first
  beat (different per stem, up to ~3 s here), so the import resets every clip
  start to 0. With warping off, clip markers are in seconds.
- EQ Eight through the LOM, probed in 12.4.6 with RigLink's `device_parameters`: 84
  parameters, `Device On`, `Output`, `Scale`, `Adaptive Q`, then per band n (1-8)
  `n Filter On A`, `n Filter Type A`, `n Frequency A`, `n Gain A`, `n Q A` and the same
  with `B` (the second curve in L/R and M/S modes). The width is `Q`, not "Resonance".
  Filter type `value_items`: `High Pass 48dB`, `High Pass 12dB`, `Low Shelf`, `Bell`,
  `Notch`, `High Shelf`, `Low Pass 12dB`, `Low Pass 48dB`. Frequency and Q are 0..1
  internally; displays read `37.5 Hz`, `1000 Hz`, `3.23 kHz`, `-15.0 dB`, `0.71`.
  A new EQ Eight's bands match `Defaults/Audio Effects/EQ Eight.adv`.
- Live reads a `.als` once at open and holds it in memory. Rewriting the file
  underneath a running Live does nothing and gets clobbered on its next save.
  **Never generate over a set that's currently open.**
- Dev loop is regenerate-and-reopen:
  `open -a "Ableton Live 12 Trial" out.als`.
- Tab toggles Session and Arrangement view. Which view a set opens in does not
  appear to be stored in the file; `<ViewStates>` controls panel visibility only.
