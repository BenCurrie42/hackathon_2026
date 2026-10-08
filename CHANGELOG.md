# Changelog

All notable changes are documented here.

---

## [1.3.0] — 2026-10-08

### Added
- **Headless MCP** — `uv run python -m app.mcp` no longer needs the web app: it builds the `App` itself (`build_app`) and answers each tool through the same `api()` the HTTP handler uses (`LocalHolySound`), talking to RigLink directly. A background `follow_live` looks at Live every second, as the page's poll does, so song mixes are still saved and put back. `--fake-live` runs it on a pretend Live Set; `HOLYSOUND_URL` or `--url` goes through a running web app as before.
- **Expert mode** — `app/expert.py`: a lead engineer agent briefs four specialists (vocals, rhythm, band, playback), who work in parallel, each only on its own tracks and only with fader, pan, mute, sends and EQ. Their changes apply without Apply, for up to three rounds plus a final check, with the song's mix checkpointed "Before expert mode" and "After expert mode". The **Expert mode** button in the chat, `POST /api/expert`, the `expert_mode` MCP tool and `uv run python -m app.expert [goal]`.
- **RigLink installer** — `scripts/install_riglink.py` links RigLink into Ableton's Remote Scripts and picks it as a Control Surface by editing `Preferences.cfg` with Live closed (backup kept), then opens Live and waits for it. Verified against 12.2.7, 12.4.5 and 12.4.6 settings files.
- **Live meters** — RigLink `get_live_meters` and `GET /api/meters`: the page polls meters on their own feed at RigLink's ~100 ms tick and draws peak-and-fall, falling back to the snapshot's meters on an older RigLink.

### Changed
- **The JSON API is one function** — `api(app, method, path, query, body)` in `app/server.py` holds every route's logic; the HTTP handler keeps access checks, the event stream, static files and status codes.
- **MCP default** — with no `HOLYSOUND_URL`, the MCP server runs headless instead of looking for the app on port 8765. A running web app and a headless MCP don't share pending proposals or the chat.
- **Play starts the picked song** — with a song picked in Song mix, the header's Play button fires that song's scene and reads "Play <song>".
- **README** rewritten as a short pitch, with the finals demo video.

## [1.2.0] — 2026-10-07

### Added
- **MCP server** — `app/mcp.py` (`uv run python -m app.mcp`) lets any MCP agent (Claude Code, Claude Desktop, Cursor…) run the Sunday set through the running app's API. Tools: `read_session`, `read_state`, `propose_changes` / `apply_changes` / `dismiss_changes` (the same actions, validation and Apply as the built-in assistant, shown on every open page), `ask_assistant`, `browse_folders`, `import_folder`, `mixer_command`, `song_mix`, `remember`, `list_devices`, `list_presets`, `export_session_file`, `reset_conversation`. Hand-written JSON-RPC over stdio, no SDK; `HOLYSOUND_URL` points it at the app (default `http://127.0.0.1:8765`).
- **Gloo AI Studio provider** — `HOLYSOUND_PROVIDER=gloo` with `GLOO_API_KEY` (or the older `GLOO_CLIENT_ID` / `GLOO_CLIENT_SECRET`, OAuth client credentials with a cached token). `GlooProvider` speaks Gloo's Completions V2 (`/ai/v2/guarded`): `HOLYSOUND_MODEL` is a Gloo model id, `auto` (`auto_routing`) or a family (`model_family`); optional `GLOO_TRADITION`; the session id goes as `prompt_cache_key`. Models are checked against Gloo's public catalog (must exist and call tools); `--list-models` lists them. Default `gloo-anthropic-claude-sonnet-5.5`.
- **Outside proposals** — `POST /api/proposals {actions, text?}` validates against `Proposal` and adds a pending proposal to the chat (`Conversation.add_proposal`), superseding older ones.
- **Session notes over HTTP** — `GET /api/notes` returns the text the assistant reads.
- **Import report** — `POST /api/import` with `report_only: true` measures and remembers the files and returns the report without asking the built-in assistant (`App.measure_folder`).
- **EQ Eight control** — RigLink `get_eq` / `set_eq_band` by display text (probed in Live 12.4.6); a Tone (EQ) graph in the channel drawer; the `set_eq` action with `flat_first`; each track's EQ and rule-based `EQ PROBLEMS` in the session notes (`app/eq.py`); EQ saved per song; `POST /api/eq-flat`; `scripts/eq_eval.py`.
- **Animation** — faders, pan, sends, levels, tempo and the EQ curve glide to new values; chat, proposal steps, strips, drawer, tabs, toasts and dialogs ease in. Reduced motion turns it off.

### Changed
- **Silent proposals get a sentence** — a proposal the model sends with no text gets one from `proposal_sentence`, with no extra model call.
- **Chat provider hooks** — `OpenAIChatProvider` builds its request in `body()` and `headers()`, so a gateway can change them.

### New files
- `app/mcp.py` — MCP server over the web app's API
- `app/eq.py` — EQ Eight in words
- `scripts/eq_eval.py` — scores the model fixing a broken EQ
- `docs/documentation/mcp.md` — MCP setup and tool reference

## [1.1.0] — 2026-10-06

### Added
- **Song transpose** — `transpose_song` in RigLink sets `pitch_coarse` on every audio clip in a song (scene), -12 to 12 semitones; scene rows carry `transpose` (`None` when clips disagree). `rig.py song transpose <song> <semitones>`, a − / Key / + control on each song in the Songs tab (the middle button resets to the original key), and a `transpose_song` action the assistant can propose; session notes show each song's transpose.
- **Transpose keeps tempo** — a transposed clip is warped (Complex Pro) and pinned 1:1 at the song's tempo with an added warp marker, since transposing an unwarped clip speeds it up like tape (verified in Live 12.4.6); back at the original key it's unwarped again. Click, guide, count and SMPTE tracks keep their key, and any track can opt in or out ("Changes with the song key", `/api/track-key`, `follows_key` in `~/.holysound/folders.json`).
- **Key detection** — `app/song_key.py` guesses each imported song's key from its pitched stems (Krumhansl-Kessler profiles, stdlib only) and reports the runner-up; imports carry `<song_keys>`.
- **Vendor sets** — `app/vendor_set.py` reads a Washed/MultiTracks one-song `.als` beside the stems (Live 8 and newer), read only: tempo, section locators and the stem on each track, sent with the import as `<vendor_set>`.
- **Part tracks** — every song uses the same ~20 part tracks (`app/parts.py`); several stems for one part are mixed into one 24-bit WAV under `~/Music/Holy Sound/Parts` (`app/mixdown.py`), the -1 dBFS scaling handed back as clip gain. `import_part` replaces the per-stem `import_audio` action and makes missing part tracks itself; `rig.py song import` does the same.
- **Tidy into parts** — `tidy_into_parts` / `rig.py track tidy [--assign "Song:Track=Part"]` (`app/tidy.py`) rebuilds a track-per-stem set on part tracks, baking clip gain and old faders into the mix, keeping transposes and outputs, and deleting the emptied stem tracks.
- **Per-song mix** — a Song mix picker in the mixer shows one song's tracks, each with that song's level (clip gain) and a Playing / Left out switch. The switch is that song's mute, so it works at once while the song plays; Live's clip activator stops a playing clip but won't restart one mid-song (verified in Live 12.4.6). The assistant leaves a part out with `set_mute` and a song; the `set_clip_active` action is gone.
- **Song mixes and checkpoints** — faders, pan, mute and sends are kept per song (`app/song_mixes.py`, `~/.holysound/song_mixes.json`, by song and track name). Picking a song in Song mix, pressing Start, or starting it in Live puts its mix back; changes from the volunteer, the assistant or Live are saved to the picked song as they happen. A Checkpoints dialog saves named copies to go back to; going back first checkpoints the mix it replaces. The assistant's `set_volume`, `set_pan`, `set_mute` and `set_send` take a `song`: for a song other than the one on the mixer they change only its saved mix; `pick_song_mix` puts the mixer on a song, and the session notes show how each saved song differs. `POST /api/song-mix`, `song_mix` in `/api/state`.
- **Song order** — `move_song` and `add_song` with a position; `rig.py song move` and `song add --at`. RigLink `move_scene` copies clips with `duplicate_clip_to` (keeps warp and transpose, verified in Live).
- **RigLink** — `set_clip_active`, `delete_clip`, `move_scene`, `create_scene` at an index; clip rows carry `active`.

### Changed
- **New interface** — the page is redesigned as a console: neutral greys, scribble-strip name plates, Mute, Solo and a balance bar on every channel, and folders that fold. Tapping a channel opens a drawer below the mixer with its name, colour, sound, reverb and delay, effects and where it plays; with a song picked, the drawer also has that song's level and a Playing / Left out switch. The chat is a log, with the proposed changes docked above the message box; each step shows as working, done, partly done or failed while Apply runs (`applying` in `/api/state`, `run_all(on_step=)`). Songs read as a cue list, Room as a ledger. Fonts are bundled.
- **Plain words** — results and proposals say how far up a fader is ("68%", "off") instead of dB, and "Ableton" instead of "Live"; clip gain and "turn it up 3 dB" stay in dB.
- **Smaller models drive it reliably** — measured with `qwen3.8-flash` on 23 common requests against a demo set: 61/69 passing before, 69/69 after. The system prompt opens with a short map from requests to actions; every action has a one-line description in the tool schema; the session notes spell out each song's whole mix (`MIXER IS ON THIS SONG`, `PLAYING`, per-track fader, OFF, MUTED, sends, checkpoints) instead of differences; the volunteer's message is labelled after the notes.
- **Relative levels** — `set_volume` and `set_send` take `by_db` ("down 3"), resolved from that song's own level, so the model never does the arithmetic.
- **Promises without a proposal** — a reply that says it's making a change but proposes nothing (or is empty) is sent back once to the model.
- **Loose names** — "lead vox", "backing vocals", "the Glad song" find the one track or song meant.
- **Checkpoints for the assistant** — `save_checkpoint` and `restore_checkpoint` actions, by name, for any song.

### Fixed
- **Song from another set** — the session notes no longer say the mixer is on a song the open set doesn't have; the assistant took it at its word and pointed a whole import at a missing song.
- **Song mix picker jumped back to Every track** — the page redrew from the server before sending the pick, so it sent "no song".
- **Mixer strip layout** — the per-song level used the three-column control grid and spilled into the next strip; it's sized like pan now, and dB readouts no longer wrap.
- **No Input on new tracks** — RigLink's `create_audio_track` gives every new track No Input; Live's default (input 1) left 51 playback tracks listening to a live input.
- **Click tempo** — the stem listener averages click gaps near the median instead of taking the median of 10 ms-rounded gaps, which read a 139 BPM click as 136.
- **Imports remembered between runs** — imported file paths are stored as strings (saving them crashed the import) and read back as paths.
- **Renderer routing references** — a cloned track's `Track.N` routing strings now follow the clone only when they name the donor itself; references to return tracks are kept, and a reference to a track no longer in the set is refused instead of being silently pointed at the new track.
- **Server errors** — an unexpected exception in any route now answers the page with a sentence (HTTP 500) and logs the traceback, instead of dropping the connection.
- **Test discovery** — `uv run python -m unittest` now finds the suite (it ran 0 tests before), and the renderer has tests of its own (`tests/test_write_als.py`).

### Changed
- **Developer documentation** in `docs/documentation/`, now the source of the GitHub wiki (`scripts/publish_wiki.py`), including the user pages (getting started, weekly workflow, troubleshooting).
- **Planning** — `docs/agents_of_flourishing.md`: what to add for the Gloo Agents of Flourishing judges, ranked.

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
