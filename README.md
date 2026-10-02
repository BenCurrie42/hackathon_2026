# Holy Sound

**Describe your Sunday. Get a working Ableton session.**

[Watch the Holy Sound introduction (57 seconds)](https://drive.google.com/file/d/1JS_2oy9hb-i6gZmTE7OLHSQjQRqbP8_h/view?usp=sharing)

Holy Sound helps church worship teams turn a plain-English description of a
service into a ready-to-open Ableton Live session. A volunteer describes the
musicians, equipment, inputs, and setlist; Holy Sound prepares the tracks,
routing, levels, and stock effects that would otherwise take hours to configure
by hand.

## The problem

Church production teams repeat the same setup work every week. Someone must
name tracks, patch inputs, configure effects, route monitoring, and prepare each
song at the right tempo. That person is often a volunteer who learned Ableton on
their own—and many smaller churches have nobody who can do it at all.

Holy Sound is designed to make that setup accessible without requiring Ableton
expertise, configuration files, or a terminal.

## How it works

1. A volunteer describes the service in everyday language.
2. AI translates that description into a validated rig specification.
3. A deterministic renderer converts the specification into an Ableton Live
   `.als` file.
4. The volunteer opens the file and finishes any creative adjustments in Live.

The AI never writes Ableton's undocumented XML or invents device parameters. It
expresses intent—such as a lead vocal with gentle compression—while the renderer
owns every Ableton-specific decision. This separation makes generated sessions
predictable and prevents plausible-looking AI output from corrupting a set.

## Development status

Holy Sound is an active prototype built for the **2026 Gloo AI Hackathon**, in
the Ministry Resourcing track. It is not yet ready for production use.

The renderer currently supports:

- Audio and MIDI track creation
- Track names, hardware inputs, volume, and pan
- Ableton stock-device insertion
- Safe ID allocation and structural validation
- Gzip-compressed `.als` output

Generated test sessions have opened successfully in Ableton Live 12.4.5 without
repair warnings. The current implementation has also been round-tripped through
Live with no structural differences beyond float formatting.

The web app (`app/`, first version) adds:

- A chat where the volunteer describes the service and reviews Claude's
  suggested changes before pressing **Apply**
- Live refinement of the open Ableton session through the RigLink control
  surface: tracks, inputs and outputs, levels, pan, stock effects, sends, and
  songs as scenes with their own tempos
- A mixer and song list that follow the open set, usable from a phone or tablet
- Downloading a suggested set of tracks as a `.als` when Live isn't open
- **Import with AI**: pick a folder of stems and Claude reads every file's
  levels (peak, how loud it is while sounding, how often it sounds), then
  suggests tracks, songs, colours and clip gain to balance them
- **Listening**: a suggested step plays a song and reads Live's meters, and
  Claude follows up on what it heard; the mixer shows live meters, track
  colours and each clip's level

Still in development:

- Remembering a church's room and gear from week to week
- Hardware output routing in the `.als` renderer (it works live, via RigLink)
- Automated regression coverage for the renderer

## Local development

The prototype currently targets macOS, Python 3.11+, and Ableton Live 12.4.5.
Dependencies are managed with [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
```

### Running the app

1. Copy `remote_script/RigLink` into Live's Remote Scripts folder
   (`~/Music/Ableton/User Library/Remote Scripts/`), restart Live, and pick
   **RigLink** as a Control Surface in Settings → Link, Tempo & MIDI.
2. Copy `.env.example` to `.env` and add an Anthropic API key.
3. Start it:

```sh
uv run python -m app            # opens http://127.0.0.1:8765 in a browser
uv run python -m app --lan      # also prints a link for phones on the same Wi-Fi
uv run python -m app --fake-live   # no Ableton: a pretend Live Set for trying the UI
```

Tests run without Ableton or an API key:

```sh
uv run python -m unittest discover tests
```

The renderer reads Ableton's stock device presets from the local Live
installation, so it is not yet portable across editions or installation paths.
See [CLAUDE.md](CLAUDE.md) for the verified file-format findings and
[plan.md](plan.md) for the current build order.

## Scope

The first release is focused on creating new worship sessions with Ableton stock
devices. Editing arbitrary existing sessions, third-party plugins, and MainStage
are outside the initial scope.

## License

[MIT](LICENSE)
