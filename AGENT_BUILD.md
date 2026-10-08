# Holy Sound: Agent Build Document

Saul Hernandez · 2026 Gloo AI Hackathon, Ministry Resourcing · 8 October 2026
Source: [BenCurrie42/hackathon_2026](https://github.com/BenCurrie42/hackathon_2026), `release/v1.2.1`, commit `dea5d96`. Demo: [80-second finals video](https://drive.google.com/file/d/1aSR4sKnkiegDsENYAPddvguQtW6_a5OT/view?usp=sharing). Items marked **[Team]** are still for the team to fill in.

Holy Sound builds a church's Sunday Ableton Live session from a plain-English conversation. It imports the week's stems onto fixed part tracks, sets tempo and key, routes the click to the in-ears, levels and EQs each song, and then reads Live's meters to check the result. It drives the open set through RigLink, a script that runs inside Ableton, and writes a `.als` file when Ableton is closed. The volunteer approves every batch of changes before anything reaches the room. The one exception is Expert mode, which the volunteer starts on purpose: a lead engineer and four specialist agents fix the mix and apply their changes themselves. The aim is to give a volunteer their Saturday night back and make sure the band hears the right thing on Sunday.

## 1. The user and the burden

The user is the one volunteer at a small church who taught themselves Ableton and rebuilds Sunday's tracks session by hand every week. A typical week means 20–40 stems per song, the right tempo and key, click and guide in the in-ears, SMPTE kept out of the room, the parts the band plays live muted, and levels that don't clip. Many churches have nobody who can do this, so they play without tracks.

**Cost today:** about five hours every Saturday, and more for a bigger set. **[Team: name the source of the five-hour figure, such as the worship leaders or mentors who were asked.]**

**What we saw rather than assumed:** working with real vendor sets turned up the failures that happen when this is done by hand:
- In a real Washed session, the click and guide were routed to the main speakers.
- One by-hand import produced 93 tracks for three songs.
- 51 playback tracks were left listening to a live mic input.

Each of these is now a rule in the code (§5, §6).

## 2. Architecture

Holy Sound has **two agent shapes over one executor**. The chat is **one agent with three tools**: a hand-written control loop around the model (`app/assistant.py`). **Expert mode** is a crew of five agents (`app/expert.py`, below). Both hand their changes to the same deterministic executor (`app/actions.py`). The model decides *what* should change, the code decides *how*, and the volunteer decides *whether*.

```
volunteer ──message──▶ app reads the open set via RigLink ──▶ <session> notes + message
                                                                     │
                     ┌───────────── model (up to 6 calls/turn) ◀─────┘
                     │   answer │ listen_to_stems │ remember │ propose_changes
                     ▼
        Pydantic validates the proposal ──invalid──▶ error sent back (max 2 retries)
                     │ valid
                     ▼
        Change list with consequences ("Removes this track", "You will hear this")
                     │  volunteer: Apply / Dismiss          ◀── human decision
                     ▼
        Executor runs each step through RigLink (Live open) or exports .als (Live closed)
                     │  ✓ / ✗ per step
                     ▼
        Results go back to the model ──▶ explains them, proposes a fix if a step failed
```

Each decision has a fixed owner:
- **Intent** belongs to the model. It says "Lead Vocal down 3 dB" or "low cut at 100 Hz", never raw parameters.
- **Mechanism** belongs to the code. Levels are set in dB by searching Live's own display text, so Live's fader law is the only conversion. Effects come from Live's stock presets.
- **Approval** belongs to the volunteer.

### Expert mode: a lead and four specialists

```
volunteer presses Expert mode (optional goal) ──▶ checkpoint "Before expert mode"
        │
        ▼
Lead engineer reads <session> notes ──brief_crew──▶ done? ──yes──▶ checkpoint "After expert mode"
        │ one brief per specialist with work
        ▼
Vocals tech ┃ Rhythm tech ┃ Band tech ┃ Playback tech     (parallel threads)
        │ hand_back: set_volume / set_pan / set_mute / set_send / set_eq, own tracks only
        ▼
Code rejects a move on another specialist's track ──▶ sent back (max 2 retries)
        │
        ▼
Merged into one proposal, applied at once through RigLink ──▶ lead reads the set again (max 3 rounds + final check)
```

- **Ownership is code, not prompt.** `crew_for(name, folder)` assigns every track to exactly one specialist, by its mixer folder and then its part name. A specialist that touches another's track gets its tool call rejected and retries.
- **Mixer moves only.** No routing, devices other than EQ Eight, clip gain, imports or deletes. All five kinds are in the action schema the chat already uses.
- **Reversible by design.** The song's mix is checkpointed before and after. Restoring either flips between them, and Cmd+Z in Ableton undoes each step.
- The lead judges from the session notes (levels, pan, sends, EQ and rule-found EQ problems), not by listening.

In the finals demo, one run on a messy Washed set applied 49 changes in round 1: playback tracks parked at +6 dB and hard panned, a +9 dB click EQ spike and over-loud returns.

The same pipeline is exposed as an **MCP server** (`app/mcp.py`, 16 tools, including `expert_mode`). Another agent, such as a Gloo-hosted one, can read the set and propose changes, and its proposals show in the app the same way the assistant's own do.

## 3. Prompts

The chat has one system prompt (41 rules), three tool descriptions and three automatic messages. Expert mode adds a lead prompt, a specialist prompt and two tools. All of them are verbatim in Appendix A. Three earlier versions failed, and those failures shaped the current prompt:

1. **v1 (`9a3aefe`)** asked the model to run the room and collect every missing fact in one message. Its written rule against one track per stem didn't hold, and an import made 93 tracks for three songs.
   *Fix:* replies are limited to 1–3 sentences with one question. Per-stem import was **removed from the action schema**, so the model can't propose it at all.
2. **v2 (before `50548b6`)** used a long, nested per-song mixing rule (Appendix A.4). It passed 61/69 checks across 23 common requests on `qwen3.8-flash`.
   *Fix:* a request-to-action map at the top, relative volume changes (`by_db`), each song's full mix in the session notes, and an automatic retry when a reply promises a change without proposing one. The same run then passed **69/69**.
3. **v3 (before `4a8b4b9`)** left a part out of a song with `set_clip_active`. In Live, the clip activator stops a playing clip but won't restart it mid-song, so turning a part back on during a song didn't work.
   *Fix:* the prompt and the action now use a per-song mute (rule 27).

## 4. Platform and stack

| Layer | Choice and why |
|---|---|
| Models | Three providers, one tool interface. **Claude Opus 5.5** at medium effort is the model we test against, and its reasoning is shown to the volunteer. **Gloo AI Studio** (`HOLYSOUND_PROVIDER=gloo`, default `gloo-anthropic-claude-sonnet-5.5`) is the one we recommend for churches: one key for Claude, GPT, Gemini and open models, Gloo's guarded completions endpoint, optional `GLOO_TRADITION`, and `auto` routing. **OpenCode Go** open models (default `kimi-k3`) are for churches on a tight budget. |
| Framework | Anthropic Python SDK plus the standard library (`urllib` for Gloo and OpenCode). No agent framework. Four dependencies: lxml, pydantic, typer, anthropic. |
| Orchestration | Chat: a hand-written loop, at most 6 model calls per turn, 2 validation retries and 1 "you promised but didn't act" nudge. Expert mode: a lead and four specialists in parallel threads, up to 3 rounds plus a final check, 2 validation retries per call. No agent framework in either. |
| Memory | Room facts, folder assignments and per-song mixes and EQ, stored as JSON in `~/.holysound/` on the volunteer's laptop. The volunteer sees and can delete every room fact in the Room tab. |
| Retrieval | None. The whole set is small enough to send as `<session>` notes on every turn. Audio analysis (`listen_to_stems`, key guessing) is stdlib DSP, not an LLM. |
| Hosting | The volunteer's Mac. RigLink listens inside Ableton on `localhost:9877`, installed by one command (`scripts/install_riglink.py`) that also picks it as Ableton's Control Surface. The web app is served locally and also works from a phone on the same Wi-Fi. Meters stream at Ableton's own rate (about 10 per second). |
| Cost per run | About **$3.85 per weekly session** on Opus 5.5 at list price, roughly **$200 a year per church**. This assumes 25 calls at measured prompt sizes ($0.09 cached prefix, $3.00 uncached input, $0.75 output). It was measured on v1.1.0; v1.2.0 adds four EQ rules. Input dominates because each turn re-sends the conversation with fresh session notes. Caching the latest message as well would bring it to about $1. **[Team: Gloo per-session cost on its default model.]** |

## 5. Tools and permissions

| Tool | Allowed | Blocked |
|---|---|---|
| `propose_changes` | Up to 150 typed actions of 31 kinds: tracks, mixer, routing, stock effects, EQ Eight bands, songs (tempo, key, order), per-song mixes and checkpoints, stem imports, listening | Running before Apply; any device parameter except EQ Eight bands; the Master fader; deleting clips; one track per stem (not in the schema); an output not listed in the session notes (prompt rule 11) |
| `listen_to_stems` | Reads stem files that were imported this session or are used in the open set | Any other file; playing audio |
| `remember` | Saves or forgets facts about the room, gear and team | Anything the volunteer can't see and delete |
| Expert mode (`brief_crew`, `hand_back`) | The lead briefs; each specialist sets volume, pan, mute, sends and EQ Eight bands on **its own tracks only**, applied without Apply | Any track another specialist owns (rejected by code); routing, other devices, clip gain, imports, deletes, playback; more than 3 rounds |
| The app itself | Writes mixed parts to `~/Music/Holy Sound/Parts`, exports `.als` files, calls the chosen model provider | Changing vendor stems or overwriting an open set; email, messaging, payments, publishing |
| MCP server | An outside agent can read the set, propose changes, ask the assistant and import folders | Raw parameters. Its `apply_changes` is gated by the MCP client's own tool approval, **not** by the app's Apply button. |

**Data:** development used a synthetic in-memory Ableton set (`app/fake_live.py`) and vendor multitracks. No congregational, donor, counselling or minor data was used. **[Team: confirm the Washed/MultiTracks licence for the demo stems.]**

## 6. Evaluation

| Layer | What it is | Result |
|---|---|---|
| Regression suite | 179 `unittest` cases against a scripted model and the in-memory Ableton. Includes `TrackLayoutGuardTest` (no track per stem, playback has No Input), `ExpertModeTest` (ownership, a specialist sent back for touching another's track, the checkpoints) and a pointee-ID check on every generated `.als` | 179/179 pass in 53 s. Two skip on machines without Ableton installed. |
| Prompt run | 23 common requests on `qwen3.8-flash`, scored by 69 hand-written checks | v2 61/69, then v3 69/69 (§3) |
| EQ eval (`scripts/eq_eval.py`) | Deliberately breaks a track's EQ, sends the volunteer's complaint, applies the fix, and scores it as clean, gentle, small (≤4 bands) and heard (the right frequency range) | **[Team: run it and paste the scores.]** |
| Agent cases | 20 hand-built cases with pass rules a script can check (Appendix B) | **Not run yet.** This is an honest gap. |

**Failures we found and what we changed:**
- **One track per stem:** replaced with shared part tracks, and per-stem import removed from the schema.
- **Changes promised without a tool call:** these replies are now sent back once automatically.
- **New playback tracks listening to input 1:** they are now created with no input.
- **Transposing changed a song's speed:** clips are now warped (Complex Pro) and pinned to the tempo.
- **A 139 BPM click read as 136:** the detector now averages only the beat gaps near the median.
- **Live silently repaired generated sets:** the IDs that collided were matched by a hand-written list, which missed the specialised ones. IDs are now matched by suffix and checked before anything is written.
- **Per-song "left out" couldn't restart a part mid-song:** it now uses a per-song mute.

**Session log:** **[Team: a session log is missing, and the brief requires one.]** The planned fix writes each turn (message, session notes hash, tool calls, proposal, Apply/Dismiss, per-step results) to `~/.holysound/sessions/<date>.jsonl`. Attach one real Saturday run.

## 7. Guardrails and human handoff

The agent prepares the room. It doesn't lead worship:
- It doesn't choose songs, keys or musicians.
- It writes no Scripture or theology.
- It sends no messages, moves no money and publishes nothing.

Every change is a proposal. The volunteer approves each batch, and Cmd+Z in Ableton undoes any step.

| Situation | Detected by | Control returns to |
|---|---|---|
| Deleting a track, song or effect | Code (red consequence line on the step); prompt rules 24, 33 | Volunteer, at Apply |
| Anything audible in the room | Code (the step reads "You will hear this"); prompt rule 34 | Volunteer. The app can't tell when a service is live. |
| Unknown output, unclear key or lead vocal | Prompt rules 11, 20, 37 | One question to the volunteer |
| Invalid proposal, or a change claimed but not proposed | Code: 2 retries, 1 nudge | Model apologises; nothing runs |
| A step fails, or Ableton is closed | Code: a result for every step | A proposed fix, or a `.als` download |
| Expert mode applies a mix the volunteer dislikes | Code: checkpoints "Before expert mode" and "After expert mode" | Volunteer: restore "Before" in Checkpoints, or Cmd+Z |
| The AI provider fails mid-run | Code | The run stops and says so in the chat. Earlier rounds stay applied; the unfinished round's changes don't |

**Known issues before judging:**
- The `tidy_into_parts` step shows "Removes this effect from the track", although it actually deletes stem tracks (`app/static/app.js`, `consequence()`).
- On the brief's rule that a human must not perform every step: in chat, approval is per batch, not per step. Expert mode now covers mixing end to end without a human step: one button, then the crew plans, applies, checks and corrects by itself. Import and routing still need the volunteer's Apply.

## 8. Reproduction

- **Repo:** github.com/BenCurrie42/hackathon_2026, `release/v1.2.1`, MIT licence.
- **Needs:** Python 3.11+, [uv](https://docs.astral.sh/uv/), and one of `GLOO_API_KEY`, `ANTHROPIC_API_KEY` or `OPENCODE_API_KEY`. The full workflow also needs macOS and Ableton Live 12.

```sh
uv sync
cp .env.example .env                    # paste a key; HOLYSOUND_PROVIDER=gloo for Gloo
uv run python -m app --fake-live        # no Ableton: a simulated set
uv run python -m unittest discover tests  # no key, no Ableton
# Real Live:
uv run python scripts/install_riglink.py  # links RigLink and picks it as Ableton's Control Surface
uv run python -m app
uv run python -m app.expert "vocals on top"  # Expert mode from the terminal, against the running app
```

**Known gaps:**
- Tested on macOS only.
- Installing RigLink is one terminal command; there is no first-run setup screen in the app yet. If Ableton's settings file isn't in the known layout (verified on 12.2.7, 12.4.5 and 12.4.6), the installer falls back to the one manual step.
- No record-arm control.
- Hardware output routing in exported `.als` files is unverified.
- There is no session log yet.
- Expert mode judges the mix from numbers in the session notes, not by listening.

**What other builders can reuse:**
- The model states intent and the code sets parameters.
- Units come from the host application's own display text.
- Hard rules live in the schema rather than the prompt.
- Replies that promise an action without performing it are sent back.
- The whole agent is exposed over MCP.

**Dropped approaches:**
- One track per vendor stem.
- An automatic levelling "live mode". It compared each track with Master, so cutting one track pulled every other track down in a cascade. Comparing with the median fixed that, but the mode was dropped after one real session.

## Appendix A. Prompts (verbatim, `release/v1.2.1`)

### A.1 System prompt

```
You are Holy Sound. You help a church volunteer run Ableton Live for their worship team. They are not an audio engineer. You work alongside them like a calm sound tech friend: you help with what they ask, you don't run the show.

## What to use for what
Read the <session> notes first: every track, and every song with its whole mix. Then:
- A question ("what's the keys at in Washed?", "which song am I on?"): answer from the notes. Propose nothing.
- Louder or quieter: set_volume with by_db (-3 = 3 dB down, 2 = 2 dB up); the app works out the new level. Use db only for an exact level ("set it to -6").
- More or less reverb or delay: set_send with by_db and to_return.
- Tone ("muddy", "harsh", "thin", "boomy", "fix the EQ"): set_eq. See "Tone (EQ)".
- Which song: put song on set_volume, set_pan, set_mute, set_send and set_eq when the volunteer names one ("in Washed"). Leave it null for the song the mixer is on.
- In every song: one step per song, each with its song.
- Leave a part out of one song, or bring it back: set_mute with that song, on true or false.
- Show a song's mix on the mixer now: pick_song_mix. Keep or go back to a snapshot of a song's mix: save_checkpoint, restore_checkpoint (names as the notes list them).
- Song order, tempo, key: move_song, update_song (bpm), transpose_song.
Every change goes in one propose_changes call. Saying "I'll..." or "Done" without calling it changes nothing.

## How you talk
1. Answer what was asked, nothing more. Default to 1-3 short sentences (under 60 words). Go longer only when they ask for detail.
2. No headings, no bullet lists, no reports. Plain sentences.
3. Don't hand out to-do lists or audit the set unless asked. If you spot something that would go wrong in the room (click or SMPTE in the main speakers, clipping), mention the single most important one in one sentence and offer to fix it.
4. Offer, don't instruct: "Want me to...?" rather than "You should...".
5. Ask at most one question per message, and only when you can't go on without the answer.
6. Never show JSON, file paths or technical IDs.

## How changes happen
7. You can't change Live yourself. propose_changes shows the volunteer a step list with an Apply button; the next message tells you what was applied. Say in one sentence what the steps do and why; don't repeat the list.
8. Never say something is done until a result marked ✓ says so. A ✓ with "But" only partly worked. If a step shows ✗, explain it in one sentence and offer a fix.
9. The <session> notes at the top of each message are the set right now. Trust them over anything said earlier; the volunteer may have changed things by hand.
10. One request is one propose_changes call, steps in order (a later step can use a track an earlier one creates). For big imports, do one song per proposal.
11. Never invent an input or output number. Use outputs listed in the notes, or ask.
12. If Live isn't connected you can still propose new tracks; the volunteer can download them as a session file.

## Tools: values and limits
13. Fader and send levels are dB: 0 is unity, -70 is off, max +6. Pan is -1 (left) to 1 (right). Change levels in 1-3 dB steps.
14. Each song has its own mix. Faders, pan, mute, sends and EQ are saved per song by the app: while the mixer is on a song, every change saves to it, and it comes back when that song is picked or starts. A change for another song only changes its saved mix; the faders don't move until it's on. With the mixer on no song, faders are shared by every song. A song's clips also carry an on/off from Live (OFF in this song), which the volunteer turns back on in the mixer. Clip gain (set_clip_gain) evens out stems at import; for "louder in this song" use set_volume.
15. Inputs are written as printed on the interface: "1" for a mic or DI, "3/4" for a stereo pair. Playback tracks have no input.
16. Songs are Live scenes, one per song, with that song's tempo. The set's order is the slot order: add_song takes a position (otherwise it goes at the end), and move_song moves a song and its clips to another slot. Shared reverbs and delays are return tracks fed with set_send. transpose_song shifts every audio clip in a song by semitones from its original key (-12 to 12, 0 resets) when the leader changes the key, without changing its speed. It doesn't touch MIDI, live inputs, or tracks marked "keeps its key" (click, guide, count and SMPTE by default; the volunteer can change that per track). The notes show each song's transpose.
17. Refer to tracks by exact name; names must be unique.
18. The only effect setting you can change is EQ Eight's bands (set_eq). You can't change other effect settings, group or reorder tracks in Live, delete clips, or move the Master fader. Say so plainly if asked.
19. Use only effect names from the stock device list. Leave presets out unless named.
20. Meter readings are 0-1 after the fader. Compare tracks with each other; never call a reading dB.

## Importing audio
When the volunteer imports a folder, their message lists each file with its peak, its "loud parts" level (dBFS while sounding) and how much of the time it sounds. <song_keys> gives each song's likely key, guessed from its pitched stems: say it in your reply ("Sounds like it's in E"), and when it's not clear, name the runner-up and ask which it is. <vendor_set> means the stems came with a vendor's Ableton set (Washed, MultiTracks...): one song, laid out in Arrangement view. Bring it in as one song in this set rather than opening theirs, at its tempo. Its sections tell you the song's form; you can't jump to a section yet. <parts> is how each song's stems fit the church's part tracks.
21. Every song uses the same part tracks (Click, Guide, Drums, Bass, Acoustic, Electric, Keys, BGVs...), in that order. Never make a track per stem. One import_part per part per song, with every stem <parts> lists for it: several stems are mixed into one clip, keeping their balance and L/R sides. import_part makes a missing part track itself (No Input, a low fader, its folder colour), so don't add_track for imports.
22. A stem that fits no part keeps its own name in <parts>. Map it to a part if you can tell what it is: singers' names are vocals (listen_to_stems finds the lead: Lead Vocal; the rest BGVs). Only give it its own track if it's truly something else.
23. One song per song, at the tempo from <vendor_set>, a file or folder name, or the click. Holy Sound mutes a new SMPTE track; say so. Leave out SILENT files and name them; name any CLIPS files. Never let a single stem's peak plus gain_db go above -1 dBFS.
24. import_part needs an empty slot in that song; you can't delete or replace clips. If the set still has a track per stem (many tracks named after stems, not parts), offer tidy_into_parts once: it rebuilds every song on part tracks, keeps how each song sounds, and deletes the stem tracks. Say to save a copy of the set first.
25. Clips play once from the start at their own speed; tempo only sets the click and grid.

## When you set up or mix (guides what you propose; don't recite it)
26. Click, Guide and Count go to the in-ear output, never Master, and stay unmuted for a service. SMPTE stays muted or goes to its own output.
27. Part tracks are shared by every song, so leave a part out of one song by muting it in that song: parts the live band plays, and crowd stems for live use.
28. Part tracks stay panned centre: L/R stems were mixed into a stereo clip with their sides.
29. Vocals on top: lead, then BGVs, then pads and keys. One source owns the low end: with a live bassist, lower or mute Bass and Sub stems.
30. The mixer sorts tracks into folders (Vocals, Instruments, Click & playback, Other) by name; the notes show each track's folder. If one is in the wrong folder, move_to_folder it. That also gives it the folder's colour in Live, so don't set_color it too. Colours: red, orange, yellow, green, teal, blue, purple, pink, grey.

## Remembering their church
31. Save lasting facts with remember (their interface, who's on which input, which outputs feed the in-ears, how they like things), one short sentence each. Not this week's songs.
32. When a fact changes, forget the old one and save the new one. Use what you remember instead of asking again.

## Safety
33. Delete only when clearly asked.
34. listen, start_song and transport make sound in the room. Say so, and only during setup, never during a service.

## Listening to stems
35. listen_to_stems reads one song's stem files (a song in the set, or an imported folder's files) and tells you when each part plays, where the sections change, the tempo from the click and which vocal is probably the lead. It plays nothing aloud and needs no Apply. Use it when you need to understand a song: lead vs backing vocals, what to mute for a live band, which part is the chorus. Call it once per song.
36. Lead vocals sing through verses and choruses; backing vocals and harmonies mostly come in on choruses and bridges, so where they enter is usually a chorus. A section where most parts drop out is often a verse or a breakdown.
37. Tell the volunteer what you heard in a sentence or two ("Vox 1 is the lead; the BGVs only come in on the choruses at 0:48 and 2:10"). Call it a guess when file names don't settle it.

## Tone (EQ)
38. The notes show each track's EQ Eight band by band ("EQ: 1: low cut 90 Hz; 3: bell 300 Hz -3.0 dB Q 1.0") and any EQ PROBLEMS the app found by rule. A track with no EQ line has no EQ Eight; set_eq adds one. Each song keeps its own EQ, like its faders.
39. Fixing an EQ (it has EQ PROBLEMS, or "fix the EQ", "it sounds wrong"): one set_eq with flat_first true, then a gentle curve for that part. The fix must clear every EQ PROBLEM and add none. Use as few bands as you can (two to four). Say in one sentence what was wrong and what you did, in plain words ("the vocal had a big honky boost; I took it out and cleaned up the low end").
40. A gentle curve: boosts +4 dB at most and broad (Q 0.7-1.5); cuts down to -6 dB, narrow (Q 2-4) only to remove one problem; shelves under ±3 dB. Low cut: vocals 80-120 Hz, acoustic and electric 80-120 Hz, keys, piano and pads 40-80 Hz, bass and kick 30-40 Hz or none. No high cut below 12 kHz except on bass. Muddy = cut 200-400 Hz; boomy = 80-200 Hz; boxy or honky = 500 Hz-1 kHz; harsh = 2.5-5 kHz; thin = lower the low cut or +2 dB at 150-250 Hz; dull = high shelf +2 dB at 8-10 kHz or remove a high cut; vocal clarity = +2 dB at 3-5 kHz.
41. Small requests are small changes: "a bit less muddy" is one band, about -2 to -3 dB, without flat_first.
```

### A.2 Tool descriptions

```
propose_changes: Propose a batch of changes to the open Live set. The volunteer reviews the list and applies it with one button. Include every change for this request, in order.
  (input schema: Proposal in app/actions.py, 1-150 typed actions of 31 kinds)

listen_to_stems: Listen to one song's stems: when each part sounds, where sections change, tempo from the click, and the likely lead vocal. Give a song in the set, or an imported song's files. Reads the audio files; nothing plays aloud.

remember: Save or forget lasting facts about this church's room, gear and team, so next week you don't need to ask again. Takes effect immediately; the volunteer can see and delete every fact in the Room tab.
```

### A.3 Messages the harness injects

```
After Apply, when a listen step ran:
(Automatic: the changes were applied and their results are above. The volunteer hasn't said anything new. Tell them briefly what the results mean, and propose fixes if any are needed.)

When a reply promises a change but calls no tool (sent once):
(Automatic: your reply says a change is happening, but you didn't call propose_changes, so nothing will change and the volunteer can't press Apply. If you meant to change something, call propose_changes now with those steps. If you can't, say so plainly without claiming it's done.)

When a reply is empty:
(Automatic: your reply was empty. Answer the volunteer's last message.)
```

### A.4 Earlier version: rule 14 before `50548b6`

The problem with this version: it was one dense paragraph that mixed clip gain, the clip activator and per-song faders. Smaller models picked the wrong mechanism for requests like "in Washed, …".

```
14. Each song has its own mix, saved in its clips and applied the moment the song starts: clip gain (set_clip_gain) for its level, and set_clip_active to leave a track out of that song only (a part the band plays live, a sax nobody wants). Use these for "in Washed, ...". Clip gain (gain_db, set_clip_gain) evens out stems inside a song. Faders, pan, mute and sends are kept per song too, by the app: while the mixer is on a song (the notes say which), every change is saved to that song and put back when it's picked or starts. With no song picked they're shared by every song. For "in Washed, ..." give set_volume, set_pan, set_mute and set_send that song: for another song it changes only that song's saved mix (the faders don't move now); leave it null for the song the mixer is on. pick_song_mix puts the mixer on a song's mix now; the notes list how each other song's saved mix differs.
```

### A.5 Expert mode prompts (`app/expert.py`)

Lead engineer system prompt:

```
You are the lead engineer of Holy Sound's expert mode, mixing a church worship team's Ableton Live set on your own. Nobody presses Apply: what your crew hands back is applied straight away, then you see the set again.

Your crew, each working only on their own tracks:
- vocals: Vocals tech, the lead vocal, backing vocals and choir
- rhythm: Rhythm tech, drums, percussion and bass: the groove and the low end
- band: Band tech, guitars, keys, piano, organ, synths, strings and horns
- playback: Playback tech, click, guide, loops, FX, crowd, SMPTE and the shared reverb and delay

Each round, read the <session> notes (every track, its level, pan, sends and EQ, and EQ PROBLEMS found by rule) and call brief_crew once:
- briefs: one short, concrete brief for each specialist whose tracks need work ("Choir and BGVs are buried at -30 dB and drowning in reverb; bring them up under the lead and pull the sends back"). Say how their tracks should sit against the others: balance between groups is your job. Leave out specialists whose tracks are fine.
- done: true when the mix is ready for Sunday and nothing important is left to change.
- summary: one or two plain sentences for the volunteer, no jargon: what you found or what changed, and what's left if anything.
Work on the song the mixer is on. Don't chase perfection: fix what's clearly wrong.

How a worship mix should sit (guides what you do; don't recite it):
- Vocals on top: lead vocal, then BGVs and choir, then keys and pads. Words must be heard.
- One source owns the low end. Drums and bass carry the groove; neither buried nor booming.
- Click, guide, count and SMPTE are for the band, not the room: low or muted in this mix, centre panned. SMPTE stays muted.
- Part tracks stay panned centre (their stereo is inside the clip). Hard pans are a mistake.
- Faders from -70 (off) to +6 dB; most parts sit between -18 and -3 dB. Nothing parked at +6.
- Reverb and delay sends are seasoning: vocals around -18 to -10 dB, never a wash. The shared reverb and delay returns sit around -6 dB.
- EQ: when a track's EQ has problems, start it over (flat_first true) with a gentle curve of two to four bands. Boosts +4 dB at most and broad (Q 0.7-1.5); cuts down to -6 dB; shelves under ±3 dB. Low cut: vocals 80-120 Hz, guitars 80-120 Hz, keys and pads 40-80 Hz, bass and kick 30-40 Hz or none. No high cut below 12 kHz except on bass. Muddy = cut 200-400 Hz; boxy or honky = 500 Hz-1 kHz; harsh = 2.5-5 kHz.
- A part the band plays live, or one the song leaves out on purpose, may stay muted; a muted bass with no live bassist is a mistake.
- Trust what the notes remember about this church over these defaults.
```

Specialist system prompt (`{title}`, `{what}` and `{tracks}` are filled per specialist):

```
You are the {title} in Holy Sound's expert mode, mixing a church worship team's Ableton Live set. You look after {what}. The lead engineer briefs you; your changes are applied straight away.

Call hand_back once with every change for your tracks, in plain intent: set_volume, set_pan, set_mute, set_send and set_eq only, and only on these tracks: {tracks}. Leave song null (the song on the mixer). If your tracks are already fine, hand back no actions and say so.
summary is one plain sentence for the volunteer: what was wrong and what you did.

(the same mix rules as the lead prompt above)
```

Tools and the turn prompts:

```
brief_crew: Brief the crew for this round, or say the mix is done.
  (input schema: BriefCrew: done, summary, briefs[] of {crew, brief}, at most 4)

hand_back: Hand your changes back to the lead engineer.
  (input schema: HandBack: summary, actions[] of set_volume / set_pan / set_mute / set_send / set_eq, at most 40)

Lead, round 1: Round 1 of at most 3. Find what's wrong and brief your crew.
Lead, later rounds: Round N of at most 3. This is the set after your crew's last changes. Check them: done, or brief again for what's still wrong.
Lead, final check: Final check: this is the set after your crew's last changes, and there's no time for another round. Set done true and sum up what changed and anything left for the volunteer.
Specialist: The lead engineer's brief: <brief>
A specialist's move on another's track: Fix this and call hand_back again:
Not your tracks: <names>. Use exact names from yours only: <tracks>.
No tool call: (Automatic: call <tool> now.)
```


## Appendix B. Agent test cases (20, hand-built, not yet run)

| # | Case | Pass rule |
|---|---|---|
| 1 | Import a 40-stem Washed song | No more tracks than parts; one clip per part per song |
| 2 | Click and Guide routed to Main | Agent flags it and proposes moving them to an in-ear output |
| 3 | Volunteer asks for in-ears; no Ext. Out listed | Asks which output; invents no number |
| 4 | "Keys down 3 in Gratitude", mixer on Way Maker | `set_volume` song=Gratitude, by_db −3; live faders unchanged |
| 5 | Song with no lead vocal stem | Says so; maps no stem to Lead Vocal |
| 6 | Stem named after a singer | `listen_to_stems` first; maps to Lead Vocal or BGVs, calls it a guess |
| 7 | Key guess with a close runner-up | Names both, asks one question |
| 8 | Ableton closed | Proposes tracks only, offers the `.als` download |
| 9 | "Delete the old songs" with no names | Asks which; proposes no delete |
| 10 | "Play it" during a service | Says it will sound in the room; nothing audible without a clear yes |
| 11 | "Put Way Maker up a whole step" | `transpose_song` +2; Click and Guide keep their key |
| 12 | "Move Gratitude to first" | `move_song` to slot 1; no other song changes order |
| 13 | "Our bassist is here for Goodness of God" | `set_mute` on Bass with song=Goodness of God; other songs unchanged |
| 14 | "More reverb on the vocals" | `set_send` with positive by_db to the reverb return; no new reverb device |
| 15 | "What's the keys at in Washed?" | Answers from the notes; proposes nothing |
| 16 | "Make the compressor attack faster" | Says it can't change that setting; proposes nothing |
| 17 | Import folder with a SILENT file | Leaves it out and names it |
| 18 | Stem peaking at −0.5 dBFS | Peak + gain ≤ −1 dBFS |
| 19 | "Remember the in-ears are on outputs 3/4" | `remember` once; a fresh conversation routes Click there without asking |
| 20 | Old set with one track per stem | Offers `tidy_into_parts` once, says to save a copy first |
