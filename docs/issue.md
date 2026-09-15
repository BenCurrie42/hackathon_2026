# Open: can we make live edits to a running Ableton session?

**Status:** unresolved, parked. Revisit before committing to the interface design.

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

## Questions to answer

1. Can the Extensions SDK create tracks and insert devices? (Decides route 2.)
2. If we go route 1, is "install a remote script" acceptable for our user, or
   does it kill adoption for non-technical church volunteers?
3. Does a runtime path make the file generator redundant, or do we want both —
   runtime for iteration, file export for handoff?
4. Can a remote script and our generated file coexist — e.g. generate the set,
   open it, then refine it live?
