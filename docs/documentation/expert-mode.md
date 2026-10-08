# Expert mode

`app/expert.py`. A crew of agents fixes the mix on the song the mixer is on, applying as it goes, with no Apply button. Started by the **Expert mode** button in the chat, the `expert_mode` MCP tool, or the CLI:

```
uv run python -m app.expert                    # the running app's mix
uv run python -m app.expert "vocals on top"    # with a goal
```

Text typed in the message box when the button is pressed is the goal.

## The crew

| Agent | Owns |
| --- | --- |
| Lead engineer | Nothing directly. Reads the set, briefs the specialists, owns balance between groups, decides when it's done |
| Vocals tech | Lead Vocal, BGVs, Choir, and anything in the Vocals folder |
| Rhythm tech | Drums, Perc, Bass, Synth Bass |
| Band tech | Every other instrument |
| Playback tech | Click, Guide, SMPTE, Loops, FX, Crowd, unmatched tracks, and the returns |

Ownership is `crew_for(name, folder)`: the mixer folder first (so a track moved by hand follows), then `parts.part_for`. `roster(snapshot)` lists each specialist's tracks.

## A run

1. Checkpoint the song's mix as "Before expert mode".
2. The lead reads the session notes and calls `brief_crew` (`done`, `summary`, one `brief` per specialist that has work).
3. The briefed specialists run in parallel threads. Each sees the whole set but can call `hand_back` only with `set_volume`, `set_pan`, `set_mute`, `set_send` and `set_eq` on its own tracks. A move on someone else's track is sent back to it (up to `FIX_ATTEMPTS` retries).
4. Their actions are merged in crew order into one proposal (`Conversation.add_proposal(..., agent=...)`) and applied with `App.apply`.
5. The lead reads the set again. Repeat for up to `MAX_ROUNDS` (3) rounds of work, then a final check.
6. Checkpoint "After expert mode". Restoring either one in Checkpoints flips between before and after.

Each agent's lines go into the chat as `role: "expert"` entries with an `agent` name, published on the event stream as `crew` events. The page shows the agent's name as the speaker and the current step in the working line. Token use counts toward the conversation's total.

## API

`POST /api/expert {"goal"?: str}` blocks for the whole run and returns `App.state()` plus `expert: {ok, rounds, applied, summary}`. 400 if the chat is busy or Live isn't connected. If the AI provider fails part-way, the run stops, says so in the chat, and returns `ok: false`.

## Limits

- Mixer moves only: no routing, devices other than EQ Eight, clip gain or imports.
- The lead judges from the session notes (levels, pan, sends, EQ and EQ problems), not from listening.
- The built-in assistant's history doesn't hear of the run; its next session notes show the result.

`ExpertModeTest` in `tests/test_app.py` covers ownership, a two-round run with a specialist sent back for touching another's track, and the checkpoints.
