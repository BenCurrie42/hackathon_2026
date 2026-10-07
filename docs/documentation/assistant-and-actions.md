# Assistant and Actions

The chat layer of the web app. `app/assistant.py` runs the conversation, `app/providers.py` talks to whichever model answers, and `app/actions.py` defines the typed changes the model may propose and how each is carried out against Live through RigLink. See [web-server-api.md](web-server-api.md) for the HTTP endpoints that drive this, [riglink.md](riglink.md) for the commands actions call, [audio-analysis-and-memory.md](audio-analysis-and-memory.md) for `song_map` and room memory, and [part-tracks-and-imports.md](part-tracks-and-imports.md) for how `import_part` and `tidy_into_parts` work.

## End-to-end flow

```mermaid
sequenceDiagram
    participant V as Volunteer (browser)
    participant S as server.App
    participant C as Conversation
    participant P as Provider
    participant L as Live (RigLink)
    V->>S: POST /api/chat {message}
    S->>C: send(text, session_notes())
    loop up to MAX_STEPS (6) model calls
        C->>P: create(SYSTEM, tools, messages)
        P-->>C: Reply(stop_reason, content blocks)
        alt remember / listen_to_stems
            C->>C: run locally, send tool_result, loop
        else propose_changes (valid)
            C-->>S: transcript entry with proposal_id (turn ends)
        else invalid propose_changes
            C->>P: tool_result is_error with validation errors (max 2 fixes)
        else text only
            C-->>S: assistant entry (turn ends)
        end
    end
    V->>S: POST /api/proposals/{id}/apply
    S->>L: run_all(live, actions, files, folders, parts_dir, mixes, on_step)
    S->>C: record_outcome(pid, "applied", results)
    Note over C: Next send() (or follow_up()) delivers the tool_result with the results
```

1. `App.send_message` (`app/server.py`) builds the session notes (`App.notes` -> `session_notes`) and calls `Conversation.send(text, notes, attachment)`. Only one turn runs at a time (`_lock`); `/api/chat` rejects a message while `busy`.
2. Each user message sent to the model is `<session>\n{notes}\n</session>\n\nThe volunteer says:\n{text}` plus an optional attachment (an imported folder's measurements, a `<parts>` block with each song's stems grouped into part tracks, a `<song_keys>` block with a key guess per song, and a `<vendor_set>` block when a vendor's `.als` sits beside the stems; not shown in the chat transcript). An automatic follow-up (`follow_up`) has no "The volunteer says:" line, only an "(Automatic: ...)" body.
3. The model answers with text and/or one tool call. History is kept in Anthropic content-block format regardless of provider (`_replayable` copies response blocks back verbatim, dropping `parsed_output`).
4. `propose_changes` is validated with `Proposal.model_validate`. If valid, a proposal record is stored (`status: "pending"`) and the turn **ends there**. The tool call stays open (`_open_tool_use = (tool_use_id, proposal_id, other_results)`); nothing reaches Live.
5. The volunteer presses Apply, Dismiss, or Download (export to `.als`). `App.apply` runs `run_all` (reporting each step's state to the page as it goes; see [web-server-api.md](web-server-api.md#apply-progress)), then `Conversation.record_outcome`. The `tool_result` is only built on the *next* `_send`, via `_tool_result`, so the model learns what happened (lines prefixed with check or cross, plus any `detail`).
6. If the batch contained a `Listen` action, `App.apply` immediately calls `Conversation.follow_up(notes)`, which sends the results with an "(Automatic: ...)" body so the model can read the meter numbers without a new message.

### Proposal lifecycle

| Status | Meaning |
| --- | --- |
| `pending` | Shown with Apply / Dismiss / Download. |
| `applied` | `run_all` finished; per-action `results` stored. |
| `dismissed` | Volunteer declined. |
| `exported` | Downloaded as `.als` instead (`App.export`, only if still pending). |
| `superseded` | A newer proposal was made, or the volunteer sent another message first. Cannot be applied (`App._pending` raises "already been dealt with"). |

### Loop rules (`Conversation._send`)

- `MAX_STEPS = 6` model calls per message; `FIX_ATTEMPTS = 2` validation retries. After the limit, the dangling tool call is answered ("Stopped here...") and the volunteer sees a generic apology.
- Every tool call gets a `tool_result`, in order. Unknown tool name -> error result. Non-dict input (provider returned unparseable JSON) -> error asking to resend.
- One `propose_changes` per reply; extra calls get `ONE_PROPOSAL` back.
- A reply to a new volunteer message that has no tool call but reads as making a change (`PROMISE`: "I'll…", "Sending…", "I've turned…", not ending in `?`), or is empty, is sent back once with `NUDGE_PROMISE` / `NUDGE_EMPTY`. Smaller models often describe a change instead of proposing it. Never done when reporting applied results, where "I've turned…" is true.
- `stop_reason == "refusal"` -> `_Refused`; history is rewound and the volunteer sees "Sorry, I can't help with that one."
- Any other exception (including `AssistantUnavailable`) rewinds history (`_rewind`), removes the user entry from the transcript, and re-raises, so a failed turn leaves no trace and the message can be resent.
- If a text reply accompanies a tool call that is not `propose_changes`, it is shown as its own entry; with a proposal, the text is attached to the proposal entry.
- Token usage accumulates in `Conversation.usage` via `token_usage(response)` and is published as a `usage` feed event.

### ReplyFeed (streaming to the page)

`Conversation.feed` is a `ReplyFeed`: a thread-safe event list (`start`, `step`, `thinking`, `text`, `tool`, `usage`, `end`) of the current turn only. `/api/events` streams it. `cursor()` lets a page that connects mid-turn replay from the turn start. `busy` is cleared *before* `end` is published so a refreshing page sees the finished turn.

## System prompt and context

Two layers, both in `app/assistant.py`:

**`SYSTEM`** (constant, sent as the system prompt every call; cached on Anthropic via `cache_control: ephemeral`). It opens with **What to use for what**, a short map from common requests to actions (questions answered from the notes, `by_db` for up/down, `song` for "in Washed", `set_mute` with a song to leave a part out, checkpoints), written for smaller models. Then 37 numbered rules in sections: how to talk (short, plain sentences, no JSON/IDs, offer don't instruct), how changes happen (propose only; never claim done until a check-marked result), tool values and limits (dB, pan, each song's own mix, song order and `move_song`, what `transpose_song` leaves alone, "can't change effect settings / group tracks / delete clips / move Master"), importing audio (every song on the same part tracks, one `import_part` per part per song, never a track per stem, say the key guess and ask when it is unclear, offer `tidy_into_parts` once for a track-per-stem set and say to save a copy first), mixing guidance (click to in-ears, SMPTE muted, leave parts out of one song by muting them in that song, part tracks stay centre, folders), remembering, safety (`listen`, `start_song`, `transport` make sound), and listening to stems. The colour list is interpolated from `rig.TRACK_COLORS`. The model still speaks in dB (rule 13); only the sentences actions show the volunteer turn fader levels into percentages (see [Wording](#wording)).

**`<session>` notes** (rebuilt by `session_notes(snapshot, stock_devices, live_error, imports, room, mix_song, saved_mixes, checkpoints)` on every user message; the prompt tells the model to trust these over earlier chat):

| Section | Source |
| --- | --- |
| Live connected? tempo, time signature, playing/stopped; or "Live is NOT connected (reason)" | `snapshot["song"]` / `live_error` |
| `Tracks:` numbered, one line each via `_strip_line`: name, folder, "keeps its key" (a track a transpose leaves alone), colour name (`color_name`, nearest of `TRACK_COLORS`), audio/MIDI, input, output, volume, pan, MUTED/SOLO, effects, sends, clips per song with gain and "OFF in this song" for a switched-off clip | snapshot + `FolderMemory` (`App.with_folders`) |
| `Returns (shared effects):` lettered A, B, ... | snapshot |
| `Mixer is on: <song>` (or no song), then `Songs (scenes)…`: index, name, BPM, transpose (`transposed +2`, or "clips at mixed keys"), `MIXER IS ON THIS SONG`, `PLAYING`; under each, `mix:` every track in that song with its fader, `OFF in this song`, `MUTED`, pan and sends (`song_mixes.describe_song`), from the live mixer for the current song, the saved mix for others, or "not saved yet"; and its `checkpoints` by name | snapshot scenes + `SongMixMemory` (`_songs_lines`) |
| `Outputs:` `Master; Ext. Out ...` from `ext_outputs`; falls back to outputs in use with a "ask which ones feed in-ears" hint for older RigLink | `_outputs_line` |
| `Imported audio folders (import_part can use their files)` name and file count | `App.imports` (also remembered across runs in `imports.json`) |
| `Stock devices in this Live:` by category; `FALLBACK_DEVICES` when Live is down or lists none | `App.live.stock_devices()` |
| `What you remember about this church` (`#id. fact`), last | `RoomMemory.notes()` |

Song maps (`app/song_map.py`) are **not** in the notes. They are produced on demand by the `listen_to_stems` tool and returned as a tool result.

## Tools given to the model

Defined by `_tools()` in `app/assistant.py`. Schemas come from pydantic `model_json_schema()` passed through `_inline_refs`, which inlines `$ref`/`$defs`, drops `discriminator`, rewrites `oneOf` to `anyOf`, and adds a one-value `enum` beside any `const` (to keep open models via OpenCode Go on schema they follow).

| Tool | Input model | Runs | Effect |
| --- | --- | --- | --- |
| `propose_changes` | `actions.Proposal`: `actions` list, 1 to 150 items, discriminated on `action` | Not run directly; stored as a pending proposal | Deferred until Apply. Result arrives next turn. |
| `listen_to_stems` | `ListenToStems`: `song` (name/number in the open set) and/or `files` (up to 64 imported file ids or a subfolder prefix) | `Conversation._listen` immediately | Resolves stems via `Conversation.song_stems` (set by `App.song_stems`, which calls RigLink `song_files`) or `Conversation.files` (imported-file id -> path; the model can only read these), then `song_map.listen(stems)`. Plays nothing, no Apply. Adds a `heard` note entry to the chat. |
| `remember` | `Remember`: `facts` (up to 20 short sentences) and `forget` (fact numbers) | `Conversation._remember` immediately | `room.remove(forget)` then `room.add(facts)`; adds a `note` entry. Returns "Memory isn't available" if `room is None`. |

`listen_to_stems` (offline file analysis) is distinct from the `listen` *action* (plays a scene in Live and reads output meters).

## Providers (`app/providers.py`)

A provider exposes `create(system, tools, messages, on_event=None) -> Reply(stop_reason, content, usage)` and a `session_id` attribute (set by `Conversation._create` each call). `Reply.content` is a list of Anthropic-style block dicts, so the conversation never knows who answered. `Conversation.client()` wraps a bare Anthropic SDK client (anything lacking `.create`) in `AnthropicProvider`; tests inject a `client_factory`.

| Provider | Transport | Used for |
| --- | --- | --- |
| `AnthropicProvider(gateway=False)` | `anthropic` SDK `client.beta.messages.stream` | Claude directly. Prompt caching on the system block, adaptive thinking with summarized display, `output_config.effort`, `disable_parallel_tool_use`, beta `server-side-fallback-2026-07-01` with `fallbacks="default"`. |
| `AnthropicProvider(gateway=True)` | `anthropic` SDK `client.messages.stream` against OpenCode Go's root URL | OpenCode Go models that speak Anthropic Messages (Qwen, MiniMax). Plain request only, with `x-opencode-session` header. |
| `OpenAIChatProvider` | stdlib `urllib.request` POST to `{base}/chat/completions`, SSE streaming | OpenCode Go models that speak Chat Completions (Kimi, GLM, DeepSeek, MiMo). `opener` is injectable for tests. |

`MAX_TOKENS = 16000`, `REQUEST_TIMEOUT = 180` s (chat provider).

### Choosing a provider

`chosen_provider_name(env)`: `HOLYSOUND_PROVIDER` if set (`anthropic`, `opencode-go`, or alias `opencode`); else `anthropic` if `ANTHROPIC_API_KEY` is set; else `opencode-go` if `OPENCODE_API_KEY` is set; else `anthropic`. `provider_from_env` is called lazily by `Conversation.client()` on first use (not at server start), so a missing key surfaces in `App.ai_state` as a sentence rather than crashing. `.env` is loaded by `server.load_dotenv` (`os.environ.setdefault`, so real env wins).

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOLYSOUND_PROVIDER` | auto (above) | `anthropic` or `opencode-go`; anything else raises `AssistantSetupError`. |
| `ANTHROPIC_API_KEY` | none | Anthropic key. |
| `HOLYSOUND_MODEL` | `claude-opus-5-5` (Anthropic); `kimi-k3` or first Chat model (OpenCode Go) | Model id. |
| `HOLYSOUND_EFFORT` | `medium` | Anthropic only. |
| `OPENCODE_API_KEY` | none | OpenCode Go key. |
| `HOLYSOUND_BASE_URL` | `https://opencode.ai/zen/go/v1` | OpenCode Go base URL. |

`uv run python -m app --list-models` prints `list_models_text()` and exits.

### OpenCode Go model discovery

`discover_models()` returns `({model id: format}, is_live)`, cached per process (`refresh=True` bypasses):

1. GET `{base}/models` (4 s timeout) for the ids. On any `OSError/ValueError/KeyError/TypeError`, return `FALLBACK_MODELS` with `is_live=False`.
2. GET `https://models.dev/api.json`; `models_dev_formats` reads provider `opencode-go`, mapping each model's npm package to a format (`@ai-sdk/openai-compatible` -> chat, `@ai-sdk/anthropic` -> messages, `@ai-sdk/openai` -> responses). If models.dev fails, use `guess_format` by family name.

Formats: `chat`, `messages`, `responses`. **Responses-only models (grok, gpt-*-luna, ...) are rejected** with an `AssistantSetupError` telling the volunteer to pick another. If discovery is live and the chosen model isn't listed, that is also a setup error. OpenCode Go does not translate between formats, so each model is called in its own.

### Format conversion for Chat Completions

`chat_messages` converts Anthropic-shaped history: `tool_result` blocks become `role: tool` messages (prefixed `Error: ` when `is_error`), text blocks merge into one user message, assistant `tool_use` becomes `tool_calls`, and `thinking` blocks are sent back as `reasoning_content` (thinking models want it). `chat_tool` wraps each tool as an OpenAI function. Requests set `parallel_tool_calls: False`, `stream: true`, `stream_options.include_usage`. `read_chat` accepts either SSE or a plain JSON body (some services ignore `stream`); `chat_stream` reassembles chunks and emits `thinking`/`text`/`tool` events; `chat_reply` maps `finish_reason` (`tool_calls`->`tool_use`, `stop`->`end_turn`, `length`->`max_tokens`, `content_filter`->`refusal`) and forces `tool_use` if any tool call is present. Malformed tool-call arguments are kept as the raw string; the conversation answers with an error and asks again.

### Errors

All provider failures become `AssistantUnavailable` with a volunteer-readable sentence; key/model problems are the subclass `AssistantSetupError` (the conversation records it in `setup_error`, and `ai_state` reports not ready). `assistant.py` re-exports `AssistantUnavailable`.

| Condition | Result |
| --- | --- |
| Anthropic auth/permission error, or SDK `TypeError` mentioning "authentication" (missing key) | `AssistantSetupError` (`bad_key` / `not_set_up`) |
| Anthropic `RateLimitError` / HTTP 429 | `AssistantUnavailable(BUSY)` |
| `APIConnectionError`, `URLError`, timeout, `ConnectionError` | `AssistantUnavailable(UNREACHABLE)` |
| Other API status / mid-stream error | `AssistantUnavailable` with status or "stopped part-way" |
| Chat HTTP 401/403 | `AssistantSetupError` |
| Chat HTTP 5xx | `AssistantUnavailable` ("Couldn't reach the AI model right now") |
| Other chat 4xx | `AssistantUnavailable` including the error body's message (`_error_message`, OpenAI or Anthropic shaped) |
| Non-JSON / empty / error chunk in stream | `AssistantUnavailable` |

## Actions (`app/actions.py`)

Each action is a pydantic model with a `Literal` `action` discriminator, `describe()` (the sentence shown in the step list) and `run(ex)` (returns the result sentence, or raises `ActionFailed`). Actions carry intent only (names, dB, stock device names), never raw parameters.

**References.** Tracks (`TrackRef`): exact name (case-insensitive), 1-based number, return letter `A`/`B`, or a name ignoring Live's `A-` prefix. Songs (`SongRef`): name or 1-based number. Both resolve against the live set *when the action runs*, so a later action can use a track an earlier one created (the executor caches track lists and clears the cache after structural changes). Ambiguous or missing names raise `ActionFailed` with the list of valid names.

**Shared types.** `Device{device, preset?}`; `Output{destination: "Master"|"Ext. Out"|"Sends Only", channel?}`; `InputChannel` is regex `^\d{1,2}(/\d{1,2})?$`; `ColorName` is a `Literal` of `rig.TRACK_COLORS` keys; `FolderKey` is a `Literal` of `app.folders.KEYS`. Level fields: fader/send `-70..6` dB, clip gain `-70..24` dB, pan `-1..1`, BPM `20..999`.

Flags: `destructive` (ClassVar, shown as "removes" tag in the UI) and `audible` ("plays out loud" tag). `Transport.audible` is a property that is true only when `playing`.

| `action` | Fields | Runs (RigLink cmd) | Notes / validation | Flags |
| --- | --- | --- | --- | --- |
| `add_track` | `name` (1-64), `kind` audio/midi (default audio), `input?`, `output?`, `volume_db?`, `pan?`, `devices[]`, `color?` | `create_audio_track` / `create_midi_track`, then `set_routing` (input: `Ext. In` or `No Input`; output), `set_volume`, `set_mute`, `set_pan`, `set_track_color`, `load_device` | Sub-steps are best-effort via `try_step`: the track is created even if a sub-step fails, and the result reads "Added ... But couldn't ...". A track with no `input` is set to No Input. Audio tracks with no `volume_db` get `ex.new_track_fader_db` if the batch imports audio. Timecode-named tracks are muted in that case. Only action that can be exported to `.als`. For imports use `import_part`, not this. | |
| `add_return` | `name`, `devices[]` | `create_return_track`, `load_device` | Returns lettered by index. | |
| `rename_track` | `track`, `new_name` | `set_track_name` | Also renames in `FolderMemory` so folder moves follow. | |
| `delete_track` | `track` | `delete_track` | Result says undo in Ableton brings it back. | destructive |
| `set_volume` | `track`, `db` or `by_db`, `song?` | `set_volume` | Any track or return. A `song` other than the one on the mixer edits only that song's saved mix (`Executor.save_for_song`); Live isn't touched. Same `song` rule for `set_pan`, `set_mute`, `set_send`. | |
| `set_pan` | `track`, `pan`, `song?` | `set_pan` | | |
| `set_mute` | `track`, `on`, `song?` | `set_mute` | | |
| `set_solo` | `track`, `on` | `set_solo` | | |
| `set_input` | `track`, `input` (null = off) | `set_routing` direction input | Returns not allowed (`allow_return=False`). | |
| `set_output` | `track`, `output` | `set_routing` direction output | `channel_name` passed through; Live validates the names. | |
| `set_send` | `track`, `to_return`, `db` or `by_db`, `song?` | `set_send` | Fails with a sentence if `to_return` is not a return track. | |
| `add_device` | `track`, `device{device, preset?}` | `load_device` | Appended at end of chain. A missing preset falls back to the device default with a note (`Executor.load_device`, matches RigLink's "no preset called" error). Any other load failure raises. | |
| `remove_device` | `track`, `device` (name on the track) | `list_devices`, `delete_device` | Case-insensitive; removes the **last** matching device. Error lists the devices present. | destructive |
| `set_tempo` | `bpm` | `set_tempo` | Whole-set tempo. | |
| `add_song` | `name`, `bpm?`, `position?` (1 = first) | `create_scene` (with `index`) | Songs are scenes. A position pushes later songs down; null adds at the end. | |
| `move_song` | `song`, `position` (1 = first) | `list_scenes`, `move_scene` | Moves the song and its clips. Fails with a sentence if the position is past the last slot. | |
| `update_song` | `song`, `new_name?`, `bpm?` | `set_scene` | Null keeps the field. | |
| `transpose_song` | `song`, `semitones` (-12..12) | `transpose_song` (with `skip_tracks` from `FolderMemory.kept_tracks`) | Keeps the song's tempo. Tracks that keep their key (click, guide, count, SMPTE by default, or per-track choice) are left at the original key; the result says how many. Fails ("has no audio clips") if RigLink reports zero clips. 0 resets. | |
| `delete_song` | `song` | `delete_scene` | | destructive |
| `start_song` | `song` | `fire_scene` | Launches the scene. | audible |
| `save_checkpoint` | `label`, `song?` | `App.checkpoint_song_mix` | A named copy of the song's mix (the mixer's song unless named). | |
| `restore_checkpoint` | `label`, `song?` | `SongMixMemory.find_checkpoint`, `App.restore_song_mix` | Matched by name (exact, then contains). Checkpoints the replaced mix first. For another song only its saved mix changes. | |
| `pick_song_mix` | `song` (null = no song) | `App.pick_song_mix` | Puts the song's saved faders, pan, mute and sends back. Fails with "Song mixes need the Holy Sound app." when `run_all` gets no `mixes`/`mix_control` (`Executor.need_mix_control`); same for the checkpoint actions. | |
| `transport` | `playing` | `play` / `stop` | | audible when playing |
| `set_color` | `track`, `color` | `set_track_color` | RGB from `TRACK_COLORS`. | |
| `move_to_folder` | `track`, `folder` | `FolderMemory.move`, then `set_track_color` | Fails if `ex.folders is None`. Folder is app-side memory only; Live shows it as the folder's colour (colour failure is a partial "But"). Returns not allowed. | |
| `import_part` | `part`, `files[]` (1-40), `song`, `gain_db?` (-70..24) | `create_audio_track` if the part track is missing (`Executor.part_track`), `mixdown.mix` if several files, `import_audio` | Replaces the old per-stem `import_audio` action, which is no longer proposable. Every `file` must be an id from `Executor._files` (imported folders); resolved case-insensitively, never an arbitrary path. Several files are mixed into one clip under `parts_dir`, keeping their balance and L/R sides, and the mix's scaling goes back as clip gain. Needs an empty slot in that song. | |
| `set_clip_gain` | `track`, `song`, `db` | `set_clip_gain` | Clip gain, not the fader. | |
| `tidy_into_parts` | `assign[]` (up to 100 `{song?, track, part}`: parts for tracks whose names don't say, e.g. a singer's name; null song = every song) | many; see [part-tracks-and-imports.md](part-tracks-and-imports.md) | Rebuilds every song on the part tracks and deletes the per-stem tracks they replace. Takes a minute or two per song. | destructive |
| `listen` | `song?`, `seconds` (3-30, default 10) | `get_song`, `fire_scene`, `reset_meters`, sleep, `get_meters`, `stop` | Fails if no song given and nothing is playing. Stops playback afterwards only if it started it. Sets `ex.detail` to a meter report (peak/average per track, Live's 0-1 meter scale, not dB). Blocks the apply request for the whole duration. | audible |

### Execution (`run_all`, `Executor`)

- `run_all(live, actions, files, on_touch, folders, parts_dir, mixes, mix_control, on_step)` runs actions **in order, with no rollback**. A failed action yields `{"ok": False, "text": "<describe>: <reason>"}` and the rest continue.
- `LiveUnavailable` (lost connection) aborts: that action and every remaining one are reported as failed ("not done. Lost touch with Ableton.").
- Success results are `{"ok": True, "partial": " But " in text, "text": ..., "detail"?}`. A result containing " But " is partial success. `detail` carries extra facts for the model only (the `listen` meter report); `_tool_result` forwards it.
- `on_step(n, state)` reports progress: `"working"` as step `n` starts, then `"done"`, `"check"` (partial) or `"failed"`; when Live is lost, that step and all later ones are `"failed"`. `App.apply` uses it for `applying` in `/api/state`.
- Bulk-import fader rule: if any action is `import_part`, `ex.new_track_fader_db = starting_fader_db(largest number of import_part steps for one song)`, so new part tracks start low enough that all parts together don't clip, unless the model set `volume_db`. `Executor.part_track` also gives a new part track its folder's colour and mutes it if it is timecode.
- `Executor.call` wraps `RigLinkError` in `ActionFailed` using `_sentence`, which rewrites terse RigLink messages (`no input called ... (options: ...)`, `no stock device called`, `index out of range`) into sentences.
- There is no undo. Safety comes from the approval step and the `destructive`/`audible` tags; `delete_track`'s message points to undo in Ableton.
- `on_touch(name)` is called for each track an action works on so the page can highlight it (`App.touch`).
- Names are matched loosely when there's no exact match (`_find_track`, `Executor.song`): case, `A-` return prefixes, "the"/"song", plurals, every word of the reference in one name ("the Glad song"), then `parts.part_for` ("lead vox" → Lead Vocal). Only a single match counts; otherwise the error lists the real names.
- `by_db` on `set_volume` / `set_send` is resolved by `Executor.level_now`: that song's saved level for another song, the live level otherwise (off reads as -70), clamped to -70..+6. Exactly one of `db` / `by_db` (`_one_of`).

### Wording

Result and step sentences are for the volunteer, so they say "Ableton", not "Live", and avoid mixer jargon ("shared effect", not "return track"; "balance", not "pan").

- **Fader and send levels** are shown as how far up the fader is, not dB. `_level(db)` interpolates `_FADER_LAW` (the page's fader travel: -70 dB is 0%, -40 is 16%, -30 is 28%, -20 is 42%, -10 is 60%, 0 dB is 80%, +6 is 100%) and returns e.g. `"68%"`, or `"off"` at -70 and below. `_level_text` does the same for Live's own display string (`"-4.0 dB"`, `"-inf dB"`) in `set_volume`/`set_send` results. Used by `add_track`, `set_volume`, `set_send` and their per-song variants.
- **Relative changes stay in dB**: `by_db` steps read "Turn “Keys” down 3 dB", "2 dB more of “A-Reverb”".
- **Clip gain stays in dB** (`_db`, e.g. `+2 dB`), in `import_part` and `set_clip_gain`.
- **Pan** reads `centre` or `40% left` (`_pan`, -1..1 as a percentage).

### Export to `.als`

`to_rigspec(actions)` converts only `AddTrack` actions to a `RigSpec` (see [file-renderer.md](file-renderer.md)) and returns notes for everything dropped: other actions ("only works with Ableton open"), stereo inputs, non-Master outputs, colours, and presets (default settings used). Returns `(None, notes)` if there are no tracks. `describe_proposal` sets `exportable` when any action is `add_track`.

## Adding a new action

1. **RigLink command** (if needed): implement in `ableton_script/RigLink/__init__.py`, add a client method in `live_control/live_connection.py` and optionally a `rig.py` command (see [riglink.md](riglink.md), [cli-and-live-control.md](cli-and-live-control.md)). Remember Live must be restarted after RigLink edits. Older-RigLink handling is the caller's job (`unknown cmd` surfaces through `_sentence` as "Ableton said: ...").
2. **`app/actions.py`**: add a `BaseModel` with `action: Literal["your_action"]`, constrained fields with `Field(description=...)` (descriptions are the model's documentation), `describe()`, and `run(ex)` returning a sentence. Use `ex.track(...)`/`ex.song(...)` to resolve references, `ex.call(...)` for RigLink, raise `ActionFailed("sentence")` for failures. Set `destructive: ClassVar[bool] = True` or `audible: ClassVar[bool] = True` as appropriate.
3. **Add the class to the `Action` Union.** The `Proposal` schema, discriminator and the tool schema all derive from it.
4. **`SYSTEM` prompt** in `app/assistant.py`: add or amend a rule so the model knows when to use it, and remove any "you can't ..." rule it contradicts (rule 18 lists current limits; rules 21 to 24 cover imports). Rule 34 lists audible actions.
5. **Session notes**: if the model needs to see new state to use the action well (like song transpose), extend `session_notes` / `_strip_line`, and the RigLink snapshot (`App.live.snapshot()` in `app/live.py`).
6. **Fake Live**: add the command to `app/fake_live.py` so tests and `--fake-live` demo work.
7. **Export**: if the action should appear in a `.als`, handle it in `to_rigspec` (and the renderer); otherwise it is reported as "only works with Ableton open" automatically.
8. **Tests** in `tests/test_app.py` (pattern: `run_actions({...})` against fake Live, plus a `Proposal.model_validate` rejection case); see [testing-and-development.md](testing-and-development.md).
9. **Docs**: `CHANGELOG.md`, and this table.

No frontend change is needed for a normal action: the UI renders `describe_proposal` output (`id`, `status`, `results`, `steps` of `{text, destructive, audible}`, `exportable`) generically, and step progress comes from `applying`. Write `describe()` and `run()` sentences in the plain words above. The mixer's direct-command allowlist in `app/server.py` (`transpose_song` and `set_clip_active` appear there too) is separate from actions; it covers only controls the page calls without the assistant.

## Discrepancies

- `CLAUDE.md` says the Anthropic SDK is "for the chat" without mentioning the provider layer, discovery via models.dev, or the `server-side-fallback` beta; these exist only in `app/providers.py`.
- `app/assistant.py`'s module comment says "the model, effort and provider come from .env, read when the AI is first used"; correct, but `load_dotenv` runs in `server.main`, so using `Conversation` outside the server (tests, scripts) reads only the real environment.
