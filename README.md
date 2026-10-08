# Holy Sound

**Describe your Sunday. Get a working Ableton session.**

[Watch the 90-second intro](https://drive.google.com/file/d/1Kq2hpUynWhhkNWITW27_fQQ-kfCkewfi/view?usp=sharing)

Most small churches running tracks on Sunday have one volunteer who figured out
Ableton on their own, and they rebuild the same session every week: name the
tracks, patch the inputs, send click to the drummer, set each song's tempo. Some
churches don't have that person at all.

The idea here is that you just say what your Sunday looks like (who's playing,
what's plugged in, what songs) and get a session that's ready to go.

We're building it for the 2026 Gloo AI Hackathon (Ministry Resourcing track).
It's a prototype. Latest release: [v1.2.0](https://github.com/BenCurrie42/hackathon_2026/releases/tag/v1.2.0)
([changelog](CHANGELOG.md)). The technical writeup for the judges is
[AGENT_BUILD.md](AGENT_BUILD.md).

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
nothing happens until you hit Apply, then each step shows as it runs. Next to the
chat is a console-style mixer and song list that follow your open set: tap a
channel to rename it, add an effect or change where it plays. It works from a
phone on the same Wi-Fi. It can also:

- import a folder of stems (or a Washed/MultiTracks download, reading its set
  for tempo and sections), guess the key, and put every song on the same part
  tracks (Drums, Keys, BGVs...), mixing a part's stems into one
- tidy an older set with a track per stem into those part tracks, keeping how
  each song sounds
- give each song its own mix: pick a song in the mixer to set its levels and
  leave parts out of that song only (instantly, even mid-song), and save
  checkpoints of a mix to go back to
- change a song's key without changing its speed, leaving the click alone
- reorder the setlist, carrying each song's clips with it
- play a song, listen to Live's meters, and follow up on what it heard
- remember your room (interface, who's on which input, which outputs go to the
  in-ears) so next week you don't have to say it again
- listen to a song's stem files (without playing them) to tell the lead vocal
  from the backing vocals and find the choruses
- sort the mixer into Vocals, Instruments and Click & playback folders, and
  move a track that landed in the wrong one
- show its reply and its thinking as it writes them
- hand you a `.als` to download when Live isn't open

**The command line**, `rig.py`, does the same kind of thing by hand:

```sh
uv run rig.py track add "Click"
uv run rig.py route out Click "Ext. Out" 1
uv run rig.py mix volume Click -6
uv run rig.py effect add "Lead Vocal" Compressor --preset "Gentle Squeeze"
uv run rig.py song import ~/Downloads/"Let's Have Church" --bpm 170
uv run rig.py song transpose "Let's Have Church" -2   # down a whole step
uv run rig.py song move "Let's Have Church" 1
uv run rig.py track tidy                    # a track per stem → part tracks
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

### Using Gloo AI Studio

A [Gloo AI Studio](https://studio.ai.gloo.com) key works too (API Credentials
page). Gloo hosts Claude, GPT, Gemini and open models behind one key:

```sh
HOLYSOUND_PROVIDER=gloo
GLOO_API_KEY=your-key
HOLYSOUND_MODEL=gloo-anthropic-claude-sonnet-5.5   # optional; or auto, or anthropic / openai / google / open source
GLOO_TRADITION=evangelical                         # optional
```

`HOLYSOUND_MODEL=auto` lets Gloo pick a model per message. An older Studio
account with a client ID and secret instead of a key can set `GLOO_CLIENT_ID`
and `GLOO_CLIENT_SECRET`. `--list-models` shows Gloo's models that can call
tools, which Holy Sound needs.

### Using it from another agent (MCP)

Any MCP client can drive Holy Sound: read the set, propose changes, apply them.
Start the app, then add the server. For Claude Code:

```sh
claude mcp add holy-sound -- uv run --directory "$PWD" python -m app.mcp
```

For Claude Desktop or Cursor, the same command in their MCP config. Details in
[docs/documentation/mcp.md](docs/documentation/mcp.md).

`uv run rig.py status` checks that Live is connected. If you change RigLink's
code, quit and reopen Live, because it only loads RigLink at startup.

Tests don't need Ableton or an API key:

```sh
uv run python -m unittest discover tests
```

How the code works, page by page, is in [docs/documentation](docs/documentation/README.md)
(mirrored to the [wiki](https://github.com/BenCurrie42/hackathon_2026/wiki)). What
to add for the Gloo Agents of Flourishing judges is in
[docs/agents_of_flourishing.md](docs/agents_of_flourishing.md).

[CLAUDE.md](CLAUDE.md) has the deep stuff if you're into that: what we've figured
out about Ableton's file format and the rules that keep generated sets from
breaking.

## License

[MIT](LICENSE)
