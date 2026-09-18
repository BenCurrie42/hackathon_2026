# Holy Sound

**Describe your Sunday. Get a working Ableton session.**

[Watch the Holy Sound introduction (57 seconds)](assets/holy-sound-intro.mp4)

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

Still in development:

- Plain-English conversation to validated rig specification
- A volunteer-friendly chat interface
- Setlists, scenes, and song tempos
- Hardware output and in-ear monitor routing
- Live refinement of an open Ableton session
- Automated regression coverage

## Local development

The prototype currently targets macOS, Python 3.11+, and Ableton Live 12.4.5.
Dependencies are managed with [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
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
