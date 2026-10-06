# Changelog

All notable changes are documented here.

---

## [Unreleased]

### Added
- **Song transpose** — `transpose_song` in RigLink sets `pitch_coarse` on every audio clip in a song (scene), -12 to 12 semitones; scene rows carry `transpose` (`None` when clips disagree). `rig.py song transpose <song> <semitones>`, a − / Key / + control on each song in the Songs tab (the middle button resets to the original key), and a `transpose_song` action the assistant can propose; session notes show each song's transpose.

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
