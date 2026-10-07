# Testing and Development

How to set up, run the tests, iterate against Live, learn new `.als` facts, and cut a release. For system structure see [architecture.md](architecture.md).

## Environment setup

| Item | Fact | Source |
| --- | --- | --- |
| Python | `>=3.11` (uv currently resolves CPython 3.11.15) | `pyproject.toml` |
| Runner | `uv` only (`uv run ...`); no bare `pip`, no hand-rolled venv | global preferences |
| Dependencies | `anthropic>=1.9.0`, `lxml>=5.3.0`, `pydantic>=2.9.0`, `typer>=0.15.0` | `pyproject.toml` |
| Packaging | `[tool.uv] package = false`: the repo is not installable; run from the repo root | `pyproject.toml` |
| Lockfile | `uv.lock` | repo root |
| Nix / flake | None in this repo. No `flake.nix`, no `.python-version`, no CI config | |
| Secrets | `.env` (gitignored), loaded by `app/server.py:load_dotenv` with `os.environ.setdefault` | |

**Dependency rule.** Only the four packages above, nothing else without asking. pytest is explicitly not approved, so tests are stdlib `unittest`. The web server is stdlib `http.server`; OpenCode Go models go over stdlib `urllib`, not the `openai` package.

Always run from the repo root. Imports are package-relative (`from app import ...`, `from live_control.stem_level import ...`).

## Running tests

```
uv run python -m unittest                                          # full suite
uv run python -m unittest tests.test_app.ServerTest                # one class
uv run python -m unittest tests.test_app.RoomMemoryTest.test_xxx   # one test
```

Result: **`Ran 105 tests in ~25s, OK`** (65 in `test_app.py`, 24 in `test_providers.py`, 16 in `test_write_als.py`). `tests/__init__.py` makes the folder discoverable; before 1.1.0 bare discovery silently ran 0 tests.

`tests/test_write_als.py` renders a click/pad/guide/keys spec against `templates/test.als` and checks formatting, the header, track order, input routing, gain, send holders, the pointee pool, `Track.N` references, and that the template is untouched. Its `DeviceTest` reads Live's `.adv` presets and skips when Live isn't installed. RigLink itself has no tests (see [riglink.md](riglink.md)).

## What the tests cover

`tests/test_app.py` (65 tests):

| Class | Tests | Covers |
| --- | --- | --- |
| `ActionsTest` | 10 | Each proposable action running through `app/actions.py` against the fake Live; `run_all` |
| `SnapshotTest` | 2 | The mixer/song snapshot read from Live |
| `ConversationTest` | 9 | `app/assistant.py:Conversation` with `ScriptedClaude`: tool loop, streaming, thinking, request shape, `session_notes` |
| `ServerTest` | 13 | Real `ThreadingHTTPServer` on an ephemeral port: JSON API, events, apply flow, 500 on an unexpected error |
| `ListenToStemsTest` | 5 | `app/song_map.py` on synthetic WAV stems |
| `StartingFaderTest` | 1 | `live_control/starting_fader.py` |
| `TimecodeTest` | 1 | `live_control/timecode.py` |
| `AudioFilesTest` | 4 | `app/audio_files.py` folder browsing and WAV/AIFF levels |
| `AudioActionsTest` | 5 | Import/audio actions against the fake Live |
| `ImportServerTest` | 2 | Import endpoints on the real server |
| `FolderTest` | 3 | `app/folders.py` classification and remembered moves |
| `RoomMemoryTest` | 4 | `app/room.py` persistence |
| `ProposalLimitTest` | 1 | `Proposal` size limit (pydantic `ValidationError`) |
| `StereoPairsTest` | 4 | `live_control/stereo_pairs.py` |
| `AddTrackWordingTest` | 1 | `AddTrack` wording / `to_rigspec` mapping |

`tests/test_providers.py` (24 tests):

| Class | Tests | Covers |
| --- | --- | --- |
| `ChatTranslationTest` | 5 | Anthropic-style messages to OpenAI Chat format and back (`chat_messages`, `chat_reply`) |
| `ChatStreamTest` | 2 | Streaming a chat completion |
| `ChatErrorTest` | 2 | HTTP error mapping to `AssistantUnavailable` |
| `ChatConversationTest` | 3 | `Conversation` driven through `OpenAIChatProvider` |
| `ToolSchemaTest` | 1 | Tool schema from `app/assistant.py:_tools` is accepted by the chat format |
| `DiscoveryTest` | 5 | OpenCode Go model discovery (`discover_models`, `models_dev_formats`, `default_model`) |
| `ProviderChoiceTest` | 6 | `provider_from_env` selection and setup errors |

`tests/test_write_als.py` (16 tests):

| Class | Tests | Covers |
| --- | --- | --- |
| `RenderTest` | 11 | Rendering a click/pad/guide/keys spec against `templates/test.als`: gzip and formatting, header copied verbatim, track order and names, input routing, dB → gain and pan, send holders, pointee pool, no dangling `Track.N`, template untouched |
| `DonorRoutingTest` | 3 | `Track.N` in a cloned track's routing: donor → clone, return kept, removed track refused |
| `DeviceTest` | 2 | Stock devices from `.adv` presets in chain order; unknown device refused. Skips without Live installed |

## How tests avoid Live and an API key

- **Fake Live.** `app/fake_live.py` is an in-memory `FakeSet` served over a real local socket by `fake_live.serve(port=0, latency=0)`. `FakeLiveCase.setUp` starts it on an ephemeral port and points a real `LiveLink` at it, so the actual client code path (`app/live.py`, the RigLink wire protocol) is exercised. `tearDown` closes both. The same fake backs `uv run python -m app --fake-live`.
- **Scripted Claude.** `ScriptedClaude` replaces `anthropic.Anthropic`: it exposes `beta.messages.stream`, replays canned replies via `ScriptedStream` as stream events, and records each request (deep-copied) so tests can assert on what was sent. Helpers `text`, `call`, `reply`, `thinking` build the canned blocks.
- **Provider HTTP stubs.** `tests/test_providers.py` routes every HTTP call through `FakeOpener` / `FakeResponse` (patched with `unittest.mock`), so no network.
- **Real files, temp dirs.** Audio tests (`StemFolderCase`) write synthetic WAVs to temp directories. Room and folder memory take their location from `HOLYSOUND_HOME` / explicit paths rather than touching `~/.holysound`.

## Live dev loop

1. Regenerate and reopen: `open -a "Ableton Live 12 Trial" out.als` (`out/` is gitignored).
2. **Never generate over a set that is currently open.** Live reads the `.als` once and holds it in memory; the rewrite does nothing and is clobbered on Live's next save.
3. **RigLink edits need a Live restart.** Live imports `ableton_script/RigLink` once at startup. Symptom of stale code: `unknown cmd`. Edits to `rig.py`, `live_control/` and `app/` need no restart.
4. RigLink install: symlink `ableton_script/RigLink` into `~/Music/Ableton/User Library/Remote Scripts/`, select it under Preferences > Link/MIDI. Socket `localhost:9877`.
5. No Live handy: `uv run python -m app --fake-live`.

## Probe-and-diff loop for `.als` facts

Live is the source of truth; do not infer format facts from training data.

1. Generate a set containing the thing you are unsure about.
2. Open it in Live, change that one thing by hand, save.
3. Diff Live's save against ours (`templates/test.reference.xml` is the decompressed reference for diffing); the difference is the answer.
4. Keep the saved probe in `templates/` as a fixture (e.g. `probe_noinput.als`, which established `AudioIn/None`). Fixtures are `chmod a-w`; never mutate `templates/`.

Add only verified facts to the "Verified facts" list in `CLAUDE.md`. See [file-renderer.md](file-renderer.md).

## Release process

There is no release script. The repo has a `release-docs` skill for this, and the steps it covers are:

- **Version.** The only machine-readable version is `pyproject.toml` (`version = "1.1.0"`). No `__version__` anywhere in the code. (`server_version = "HolySound"` in `app/server.py` is an HTTP banner, not a version.)
- **Changelog.** `CHANGELOG.md`, Keep-a-Changelog style: an `[Unreleased]` section on top, then `## [x.y.z] — date`, with `### Added` bullets. Each bullet is a bold feature name, then one sentence of what and where.
- **Docs to sync.** `README.md`, `CLAUDE.md`, and `docs/td_next.md` ("done in 1.0.0" markers).
- **Branches.** `main` is the PR target; releases are cut on `release/vX.Y.Z` branches (currently `release/v1.1.0`).
- **Commits.** Short, say what changed. No AI attribution trailer is the owner's stated preference.

## Coding conventions observed

- Docstring at the top of each module saying what it is and why (including the dependency constraints, e.g. the unittest rationale).
- Intent-only actions: the model never emits device parameters; the renderer or RigLink translates (`app/actions.py`, `file_builder/rig_spec.py`).
- Pydantic models are the contract at boundaries (`RigSpec`, `Proposal`); invalid states rejected by validation rather than policed later.
- Errors are phrased as plain sentences for a non-technical volunteer (for example the setup messages in `app/providers.py`, `AssistantSetupError`, `AssistantUnavailable`).
- Stdlib first: `http.server`, `urllib`, `wave`, `unittest`; `.env` is parsed by hand.
- Tests drive real client and server code against fakes at the socket/HTTP boundary rather than mocking internal functions.
- Persistent user state lives under `~/.holysound/` (`room.json`, `folders.json`, `imports.json`); overridable with `HOLYSOUND_HOME`.
- Env config: `ANTHROPIC_API_KEY`, `HOLYSOUND_PROVIDER` (`anthropic` | `opencode-go`), `HOLYSOUND_MODEL`, `HOLYSOUND_EFFORT`, `HOLYSOUND_BASE_URL`.

## Where to start

Ranked by Sunday impact from `docs/td_next.md`:

1. Item 1 (conversation to RigSpec) is done in 1.0.0; the leftover question is whether RigLink should consume `RigSpec`.
2. **Record-arm and monitoring** (`mix monitor`, `track arm`): small command pair, most common "mic isn't working" cause. Touches [riglink.md](riglink.md) and [cli-and-live-control.md](cli-and-live-control.md).
3. **Effect settings**: a short named list of safe controls (delay time/feedback, reverb decay, dry/wet), set by searching the parameter's display text like volume.
4. **User Library presets** (cheapest part of non-stock content).
5. **Song-to-song transitions**: blocked on asking the church whether the setlist is scenes or locators.
6. MIDI mapping, clip editing (warping, loops), group tracks, renderer gaps (hardware output routing needs one probe).

Cheap first contributions: a renderer test for a new format fact, or a unittest for RigLink-facing behavior in `app/fake_live.py`. Related pages: [assistant-and-actions.md](assistant-and-actions.md), [web-server-api.md](web-server-api.md), [audio-analysis-and-memory.md](audio-analysis-and-memory.md), [frontend.md](frontend.md).
