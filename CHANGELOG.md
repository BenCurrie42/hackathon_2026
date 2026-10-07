# Changelog

All notable changes are documented here.

---

## [1.1.0] — 2026-10-06

### Added
- **Song transpose** — `transpose_song` in RigLink sets `pitch_coarse` on every audio clip in a song (scene), -12 to 12 semitones; scene rows carry `transpose` (`None` when clips disagree). `rig.py song transpose <song> <semitones>`, a − / Key / + control on each song in the Songs tab (the middle button resets to the original key), and a `transpose_song` action the assistant can propose; session notes show each song's transpose.
- **Transpose keeps tempo** — a transposed clip is warped (Complex Pro) and pinned 1:1 at the song's tempo with an added warp marker, since transposing an unwarped clip speeds it up like tape (verified in Live 12.4.6); back at the original key it's unwarped again. Click, guide, count and SMPTE tracks keep their key, and any track can opt in or out ("Changes with the song key", `/api/track-key`, `follows_key` in `~/.holysound/folders.json`).
- **Key detection** — `app/song_key.py` guesses each imported song's key from its pitched stems (Krumhansl-Kessler profiles, stdlib only) and reports the runner-up; imports carry `<song_keys>`.
- **Vendor sets** — `app/vendor_set.py` reads a Washed/MultiTracks one-song `.als` beside the stems (Live 8 and newer), read only: tempo, section locators and the stem on each track, sent with the import as `<vendor_set>`.
- **Part tracks** — every song uses the same ~20 part tracks (`app/parts.py`); several stems for one part are mixed into one 24-bit WAV under `~/Music/Holy Sound/Parts` (`app/mixdown.py`), the -1 dBFS scaling handed back as clip gain. `import_part` replaces the per-stem `import_audio` action and makes missing part tracks itself; `rig.py song import` does the same.
- **Tidy into parts** — `tidy_into_parts` / `rig.py track tidy [--assign "Song:Track=Part"]` (`app/tidy.py`) rebuilds a track-per-stem set on part tracks, baking clip gain and old faders into the mix, keeping transposes and outputs, and deleting the emptied stem tracks.
- **Per-song mix** — a Song mix picker in the mixer shows one song's tracks, each with that song's level (clip gain) and an On/Off switch (Live's clip activator, `set_clip_active`), saved in the set.
- **Song order** — `move_song` and `add_song` with a position; `rig.py song move` and `song add --at`. RigLink `move_scene` copies clips with `duplicate_clip_to` (keeps warp and transpose, verified in Live).
- **RigLink** — `set_clip_active`, `delete_clip`, `move_scene`, `create_scene` at an index; clip rows carry `active`.

### Fixed
- **No Input on new tracks** — RigLink's `create_audio_track` gives every new track No Input; Live's default (input 1) left 51 playback tracks listening to a live input.
- **Click tempo** — the stem listener averages click gaps near the median instead of taking the median of 10 ms-rounded gaps, which read a 139 BPM click as 136.
- **Imports remembered between runs** — imported file paths are stored as strings (saving them crashed the import) and read back as paths.
- **Renderer routing references** — a cloned track's `Track.N` routing strings now follow the clone only when they name the donor itself; references to return tracks are kept, and a reference to a track no longer in the set is refused instead of being silently pointed at the new track.
- **Server errors** — an unexpected exception in any route now answers the page with a sentence (HTTP 500) and logs the traceback, instead of dropping the connection.
- **Test discovery** — `uv run python -m unittest` now finds the suite (it ran 0 tests before), and the renderer has tests of its own (`tests/test_write_als.py`).

### Changed
- **Developer documentation** in `docs/documentation/`.

## [1.0.0] — 2026-10-06

First release, for the 2026 Gloo AI Hackathon (Ministry Resourcing track).

### Added
- **Session file renderer** — `file_builder/write_als.py` renders a `RigSpec` (named tracks, hardware or no input, volume in dB, pan, stock devices from Live's own `.adv` presets) to a gzipped `.als` with Live closed. Owns pointee/track ID renumbering and checks the pointee pool before writing; generated sets open in Live 12.4.5 with no repair prompt.
- **RigLink control surface** — `ableton_script/RigLink` runs inside Live on `localhost:9877`: tracks (add, rename, colour, delete), mixer (volume and sends in dB via Live's own display text, pan, mute, solo, output meters), input/output routing, stock effects and presets, songs as scenes with per-song tempo, Arrangement markers, audio import with clip gain, and `song_files` (the audio file behind each clip in a song).
- **`rig.py` CLI** — `track`, `mix`, `route`, `effect`, `song`, `marker` command groups over RigLink; `song import` brings in a stem folder with level matching (`--only` to pick stems), starts faders with headroom so the full song doesn't clip, and mutes SMPTE/timecode stems.
- **Holy Sound web app** — `uv run python -m app`: chat plus a live mixer and song list that follow the open set. The assistant proposes typed, intent-only actions (`app/actions.py`) and nothing changes until Apply. `--lan` serves phones on the same Wi-Fi with a key; `--fake-live` runs against an in-memory set, and the page says plainly when it's the demo set.
- **Import with AI** — import a stem folder; each file's peak, loud-parts level and sounding time go to the assistant, which proposes tracks, songs, colours and clip gain. Imported folders persist in `~/.holysound/imports.json`.
- **Listening** — `listen` plays a song and reads Live's meters; `listen_to_stems` (`app/song_map.py`) reads a song's stem files without playing them and reports when each part sounds, section changes, tempo from the click, and the likely lead vocal.
- **Room memory** — the assistant saves lasting facts (interface, inputs, in-ear outputs) to `~/.holysound/room.json` with `remember`, and forgets ones that stop being true.
- **Mixer folders** — tracks sort into Vocals, Instruments, Click & playback and Other by name (`app/folders.py`); moves are remembered in `~/.holysound/folders.json`, survive renames, and give the track the folder's colour in Live. The assistant can move tracks with `move_to_folder` and sees each track's folder in its session notes. Narrow upright strips, one row per folder, sticky headers, and a glowing edge on channels the assistant is changing.
- **Streaming replies** — replies, thinking and tool status stream over `/api/events`, with a token count; the open thinking block follows its newest line.
- **OpenCode Go provider** — `HOLYSOUND_PROVIDER=opencode-go` uses OpenCode Go models (OpenAI Chat or Anthropic format) over stdlib `urllib`; `--list-models` lists what's available.
- **`.als` download** — with Live closed, proposed `add_track` actions become a `RigSpec` and download as a session file.
