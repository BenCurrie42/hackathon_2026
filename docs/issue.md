# Open: can we make live edits to a running Ableton session?

**Status:** researched 2026-09-23. Route 1 confirmed viable at the source level.
Recommend prototyping it this week; chase Extensions SDK beta access in
parallel as a possible later upgrade, not a blocker.

## Why it matters

Our current approach writes a `.als` file while Live is closed. That means no live
edits — Live reads the file once at open and keeps the set in memory, so rewriting
the file underneath it does nothing and gets clobbered on the next save. Every
change is regenerate-and-reopen.

That's fine for "build me a rig." It's bad for "add more reverb on the vocal,"
which is the conversational refinement loop that will demo best and is probably
what users actually want. If a runtime path works, the interface layer should
probably target it too, and the file writer becomes the export path rather than
the only path.

## Three possible routes

### 1. Remote script + socket bridge — most proven
A Python remote script runs inside Live and opens a TCP socket; an external
process sends it JSON commands, which it executes against the official Live API.
This is how every existing Ableton MCP server works.

Confirmed capable of: creating audio and MIDI tracks, **loading devices from
Ableton's browser onto a track** (`app.browser.load_item()`), setting routing,
volume, pan, tempo, clips, transport.

The device-loading part matters most — it's the thing AbletonOSC can't do and
the reason we assumed runtime device insertion was impossible.

Cost: user must install a remote script into their User Library and select it as
a Control Surface in Preferences → MIDI. Live must be running.

### 2. Extensions SDK — official, but gated
TypeScript/JS on Node 24, bundles to a `.ablx`, triggered from a right-click
context menu. Official and documented. Requires **Live 12.4.5+ Suite Beta**.

**Still unanswered:** whether the API can *create* tracks and *insert* devices,
as opposed to only editing ones that already exist. The API reference ships
inside the SDK bundle behind the beta signup. Nobody has published the method
signatures. This is the single fact that decides whether this route is viable.

### 3. AbletonOSC — creates tracks, can't place devices
Clean OSC API, well documented, used in research contexts. Creates tracks, sets
names and routing. Its endpoint list has **no device instantiation at all**.
Probably only useful in combination with something else.

## Leads to chase

- https://github.com/ahujasid/ableton-mcp — the reference implementation of route 1.
  Remote script + MCP server over JSON/TCP. Read the remote script's browser
  handling; that's the device-loading mechanism.
- https://github.com/uisato/ableton-mcp-extended — extends the above. Loads
  devices by URI, batch-sets device parameters as normalized 0.0–1.0 floats,
  imports audio files into audio tracks. Hybrid TCP/UDP for low latency.
  Known-incomplete: automation points, VST support, Arrangement View.
- https://ablx.live/guide/ — community guide to building Extensions. Best
  non-gated shot at learning the Extensions API surface without beta access.
- https://github.com/Ronvaknins/ableton-extensions-skill — claims to document
  "the full object model: ExtensionContext, Song/Track/Clip/MidiClip, enums,
  scopes." Its `api.md` may answer the track-creation question directly.
- https://github.com/ideoforms/AbletonOSC — route 3, and a clean reference for
  what the Live API exposes.
- https://cdm.link/first-ableton-extensions-arriving/ — what people have actually
  shipped as Extensions so far; a read on real capability vs marketing.

## Answers found (2026-09-23)

1. **Extensions SDK track/device creation — partially confirmed.** A shipped
   extension, "Track Creator" (0.0.6, requires Live Suite 12.4.5 beta), creates
   multiple named audio/MIDI tracks — direct proof extensions can create tracks,
   not just edit existing ones. Ableton's own copy says extensions "can read and
   edit your Set's structure — tracks, clips, MIDI, devices, tempo," which implies
   device insertion but no single example confirms it directly. The official API
   reference (`ableton.github.io/extensions-sdk`, and `Ronvaknins/
   ableton-extensions-skill`'s `api.md`) is still gated behind Centercode beta
   signup — hit a 403. **Still the deciding unknown for route 2**, unresolved
   because the docs are inaccessible, not because the capability looks absent.

2. **Remote script device loading — confirmed by reading the source.**
   `ahujasid/ableton-mcp`'s `AbletonMCP_Remote_Script/__init__.py`:
   `_load_browser_item(track_index, item_uri)` → `_find_browser_item_by_uri`
   walks `app.browser.instruments/.sounds/.drums/.audio_effects/.midi_effects` —
   the whole browser tree, not favorites/hotswap only. Generalizes to all 47
   stock effects, same universe our `.adv` scan covers. Also present:
   `_create_midi_track`, `_create_audio_track`, `_set_track_name`, `_set_tempo`,
   `_create_clip`/`_create_audio_clip`, `_add_notes_to_clip`, `_fire_clip`/
   `_stop_clip`, `_create_locator`, device parameter get/set. No explicit
   input/output routing setter found in this pass — check before relying on it.

   `uisato/ableton-mcp-extended` adds: device loading by URI directly (skips
   name search), batch device-parameter setting as normalized 0–1 floats, audio
   file import. Its own README lists gaps: automation points not fully working,
   no VST parameter support, Arrangement View editing incomplete.

3. **Runtime vs. file generator — not either/or.** File renderer stays the
   export/handoff path (email a finished set, works with Live closed, no
   install). Runtime path is additive, for the live-refinement loop.

4. **Remote script + our generated file coexist — architecturally sound,
   inferred not documented.** The remote script (a Control Surface) binds to
   the live `Song` object in whatever set is open, not to a file path. So:
   generate `.als` with our renderer → user opens it in Live (remote script
   already selected as Control Surface in Preferences) → an MCP-style client
   connects to the same running Live instance and edits that same `Song`.
   Nothing in the remote-script API ties it to a specific file. The install/
   Control Surface selection is a one-time setup step, not per-session — worth
   weighing against the CLAUDE.md "no manual install steps" framing as a
   deliberate, disclosed exception rather than silently violating it.

## Other routes considered

Max for Live hosting an OSC/API bridge device was considered and set aside —
it would need to be loaded into every generated set, and gets us roughly what
AbletonOSC already provides for free, without solving the device-insertion gap
AbletonOSC has.

## Recommendation

Prototype route 1 (remote script + socket bridge) against the existing
renderer this week — same Python stack, low integration risk, and the only
route confirmed at the source-code level so far. Keep chasing Extensions SDK
beta access in parallel: if it clears in time, it removes the manual-install
step entirely and route 1 becomes the fallback instead of the primary.
