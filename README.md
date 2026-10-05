# Holy Sound

**Describe your Sunday. Get a working Ableton session.**

[Watch the 57-second intro](https://drive.google.com/file/d/1JS_2oy9hb-i6gZmTE7OLHSQjQRqbP8_h/view?usp=sharing)

Most small churches running tracks on Sunday have one volunteer who figured out
Ableton on their own, and they rebuild the same session every week: name the
tracks, patch the inputs, send click to the drummer, set each song's tempo. Some
churches don't have that person at all.

The idea here is that you just say what your Sunday looks like (who's playing,
what's plugged in, what songs) and get a session that's ready to go.

We're building it for the 2026 Gloo AI Hackathon (Ministry Resourcing track).
It's a prototype.

## How it works

There are two ways it talks to Ableton:

- **Build a file.** Write a `.als` from scratch with Live closed. You open it and
  everything's there. Works without Ableton even installed, which makes it easy
  to test.
- **Drive Live directly.** A small script that runs inside Live (RigLink) takes
  commands over a local connection, so changes show up in the set you already
  have open. This is the one that feels like magic in a demo.

The AI never touches Ableton's file format or dials in knob values. It says what
it wants ("lead vocal, gentle compression") and our code picks a real stock
Ableton preset for that. LLMs are bad at guessing compressor settings and great
at understanding what a person means, so we let each side do its part.

## What works today

Right now it's a command line, `rig.py`, talking to Live through RigLink:

```sh
uv run rig.py track add "Click"
uv run rig.py route out Click "Ext. Out" 1
uv run rig.py mix volume Click -6
uv run rig.py effect add "Lead Vocal" Compressor --preset "Gentle Squeeze"
uv run rig.py song import ~/Downloads/"Let's Have Church" --bpm 170
uv run rig.py song play "Let's Have Church"
```

Tracks, mixing, routing, stock effects, songs with their own tempo, markers, and
importing a folder of stems (it evens out their levels so one mix works for every
song).

## Running it

You'll need macOS, Python 3.11+, [uv](https://docs.astral.sh/uv/), and Ableton
Live 12.

```sh
uv sync
ln -s "$PWD/ableton_script/RigLink" ~/Music/Ableton/User\ Library/Remote\ Scripts/RigLink
```

Then in Live, open Preferences → Link/MIDI, pick **RigLink** as a Control
Surface, and run `uv run rig.py status` to check it's connected. If you change
RigLink's code, quit and reopen Live, because it only loads RigLink at startup.

[CLAUDE.md](CLAUDE.md) has the deep stuff if into that: what we've figured out about
Ableton's file format and the rules that keep generated sets from breaking.

## License

[MIT](LICENSE)
