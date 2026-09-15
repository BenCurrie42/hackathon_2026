# Build plan — first working version

## Where we are

The renderer works. A `RigSpec` becomes a `.als` that opens in Live 12.4.5 with
no repair prompt, and diffing our output against Live's own save of the same set
shows **zero structural differences** — same element paths, same tree. Only
float formatting differs.

Proven so far: gzip round-trip, lxml re-serialization, track cloning with full ID
renumbering, stock device insertion from Live's `.adv` library, track naming,
hardware input routing, no-input routing, volume in dB, pan.

What's missing is everything between a person talking and a `RigSpec` existing.

## The four steps

### 1. Lock the renderer

Tests that assert spec → XML facts against probe files, so nothing silently
regresses. Every fact we've learned becomes an assertion:

- input *n* → `AudioIn/External/M{n-1}`, display `Ext. In` / `n`
- no input → `AudioIn/None`, display `No Input` / `""`
- `volume_db` → `10^(dB/20)` as a linear gain
- pan → −1..1
- device name → the right `.adv`, spliced with a fresh `Id`
- pointee pool unique, no dangling refs, watermark ahead of max
- one `TrackSendHolder` per return track, IDs `0..N-1`

Also: commit the probe sets into `templates/` as fixtures, and a script that runs
the generate → open → save → diff loop, since that's what actually proves we're
writing canonical Ableton.

**Blocked on:** pytest as a dev dependency. Outside the approved list, needs a yes.

### 2. Model layer

One function: plain English → validated `RigSpec`.

Claude with structured output, Pydantic validates the result, retry with the
validation errors fed back on failure. The model never sees XML, never sees a
device parameter — it names a device and the renderer picks the preset.

This is also where the venue profile lives: on a first run the model asks what's
plugged in, and we persist the answer so the second week takes one sentence.

### 3. Thin CLI

```
rigforge "click, pad L/R, guide"  →  renders  →  opens in Live
```

The whole product working end to end, minus the UI. Typer, already a dependency.
This is the thing to iterate on for three weeks.

### 4. Chat UI

A wrapper around steps 2 and 3, and the least risky part of the project. Don't
build it until the loop works.

## Still open

- **Setlist shape — Session scenes or Arrangement locators?** Gates extending the
  spec past the rig. Worship rigs are usually one scene per song with BPM in the
  scene name, but that's unconfirmed for this church.
- **Hardware output routing.** Only `AudioOut/Main` is known. A click/pad/guide
  rig is pointless until click can go to the drummer and guide to the band.
  Needs one probe: set a track's Audio To to a hardware output pair and save.
- **Live edits to a running session.** See `issue.md`.

## Not in scope for v1

Editing existing user sessions, MIDI tracks beyond the basics, third-party
plugins, audio file references, MainStage.
