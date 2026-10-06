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

**The web app.** You chat with Claude about your Sunday, it suggests changes, and
nothing happens until you hit Apply. Next to the chat is a mixer and song list
that follow your open set, and it works from a phone on the same Wi-Fi. It can
also:

- import a folder of stems, read how loud each one is, and suggest tracks,
  songs, colours and clip gain to balance them
- play a song, listen to Live's meters, and follow up on what it heard
- remember your room (interface, who's on which input, which outputs go to the
  in-ears) so next week you don't have to say it again
- hand you a `.als` to download when Live isn't open

**The command line**, `rig.py`, does the same kind of thing by hand:

```sh
uv run rig.py track add "Click"
uv run rig.py route out Click "Ext. Out" 1
uv run rig.py mix volume Click -6
uv run rig.py effect add "Lead Vocal" Compressor --preset "Gentle Squeeze"
uv run rig.py song import ~/Downloads/"Let's Have Church" --bpm 170
uv run rig.py song play "Let's Have Church"
```

What it can't do yet is in [docs/td_next.md](docs/td_next.md).

## Running it

You'll need macOS, Python 3.11+, [uv](https://docs.astral.sh/uv/), Ableton Live
12, and an Anthropic API key (or an OpenCode Go one, see below).

```sh
uv sync
ln -s "$PWD/ableton_script/RigLink" ~/Music/Ableton/User\ Library/Remote\ Scripts/RigLink
cp .env.example .env            # then paste your API key in
```

Then in Live, open Preferences → Link/MIDI, pick **RigLink** as a Control
Surface, and start the app:

```sh
uv run python -m app               # opens http://127.0.0.1:8765
uv run python -m app --lan         # also prints a link for phones on your Wi-Fi
uv run python -m app --fake-live   # no Ableton needed, a pretend set to play with
```

### Using OpenCode Go

Don't have an Anthropic key? An [OpenCode Go](https://opencode.ai/docs/go/)
subscription works too, with open models like Kimi, GLM and Qwen. Put this in
`.env` instead:

```sh
HOLYSOUND_PROVIDER=opencode-go
OPENCODE_API_KEY=your-key
HOLYSOUND_MODEL=kimi-k3    # optional, kimi-k3 is the default
```

`uv run python -m app --list-models` shows every model OpenCode Go has right
now. A few (Grok, the GPT ones) only speak a format we don't support yet, and
Holy Sound will tell you if you pick one. Claude is still what we test against,
so expect open models to be a bit rougher.

`uv run rig.py status` checks that Live is connected. If you change RigLink's
code, quit and reopen Live, because it only loads RigLink at startup.

Tests don't need Ableton or an API key:

```sh
uv run python -m unittest discover tests
```

[CLAUDE.md](CLAUDE.md) has the deep stuff if you're into that: what we've figured
out about Ableton's file format and the rules that keep generated sets from
breaking.

## License

[MIT](LICENSE)
