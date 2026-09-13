# Idea

A Python CLI that turns a structured JSON "Rig Spec" into a real Ableton Live
session file (`.als`). An `.als` is (reportedly) gzip-compressed XML, so the
generator works by taking a template session I built by hand in Live,
decompressing it, editing the XML with lxml, and recompressing it into a new
file. The spec is a Pydantic model: a list of tracks, each with a name, an
audio/MIDI type, a hardware input channel, an output route, a list of stock
Live devices, a volume in dB, and a pan value. `render(spec, template_path)`
returns the bytes of a valid `.als`. No LLM anywhere in this phase — the input
is JSON that a human (or a later layer) writes. Stack is Python 3.11+, lxml,
pydantic, typer, nothing else.

The hard part is not the CLI, it's the file format: the `.als` schema is
undocumented and changes between Live versions, so every structural claim has
to be read out of the actual template file rather than recalled or guessed.
Order of work: verify empirically that `.als` is gzipped XML and whether Live
will also open plain XML with an `.als` extension; then inspect the template
and report how an audio track is represented, where the track name lives, where
input/output routing lives, and how a stock device (Reverb, Compressor,
EQ Eight) and its parameters sit in a track's device chain. Phase 1 is a
minimal round-trip — decompress, rename one track, recompress — which Ben opens
in Live to confirm it isn't corrupt, and nothing proceeds until he does. Phase 2
is the renderer, starting with exactly one case that works end to end: a single
mono vocal audio track on input 1 with a Compressor and a Reverb. Golden-file
tests assert specific XML nodes and values and must run without Ableton
installed. The template is never mutated — always copy first. Out of scope:
natural language input, MainStage, editing existing user sessions, and any web
or GUI layer.
