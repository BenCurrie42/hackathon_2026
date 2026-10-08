# Getting started

How to install Holy Sound and open it for the first time. You do not need to know Ableton's file format or use a terminal beyond the few commands below.

## What you need

- A Mac. Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) to run it.
- Ableton Live 12. (The format facts were verified on Live 12.4.5; RigLink was probed on 12.4.6.)
- An Anthropic API key, an [OpenCode Go](https://opencode.ai/docs/go/) key, or a [Gloo AI Studio](https://studio.ai.gloo.com) key.

You can chat, plan and save new tracks as an Ableton file without Ableton running. To change a set that is open in Ableton, you need RigLink (below).

## Install

Run these from the project folder.

```sh
uv sync
uv run python scripts/install_riglink.py   # sets up RigLink; no Ableton settings to click
cp .env.example .env            # then paste your API key into .env
```

The RigLink setup links it into Ableton's Remote Scripts folder, picks it as a Control Surface by editing Ableton's settings file while Ableton is closed (it asks you to quit first, and keeps a backup as `Preferences.cfg.before-riglink`), then opens Ableton and waits until RigLink answers. If Ableton has never been opened on this Mac, or its settings file isn't in the layout the script knows, it tells you to do the one step by hand: in Ableton, open **Settings**, then **Link, Tempo & MIDI**, and under Control Surface pick **RigLink**. RigLink listens on `localhost:9877`. The app shows the same three steps whenever Ableton isn't connected.

### Using OpenCode Go instead of Anthropic

Put this in `.env` instead of the Anthropic key:

```sh
HOLYSOUND_PROVIDER=opencode-go
OPENCODE_API_KEY=your-key
HOLYSOUND_MODEL=kimi-k3    # optional, kimi-k3 is the default
```

`uv run python -m app --list-models` lists the models available right now. A few (Grok, the GPT ones) speak a format Holy Sound does not support, and it says so if you pick one. Claude is what the project is tested against. Provider details: [assistant-and-actions.md](assistant-and-actions.md).

### Using Gloo AI Studio instead

Put this in `.env` (the key is on Gloo AI Studio's API Credentials page):

```sh
HOLYSOUND_PROVIDER=gloo
GLOO_API_KEY=your-key
HOLYSOUND_MODEL=gloo-anthropic-claude-sonnet-5.5   # optional; or auto, or anthropic / openai / google / open source
GLOO_TRADITION=evangelical                         # optional
```

An older account with a client ID and secret sets `GLOO_CLIENT_ID` and `GLOO_CLIENT_SECRET` instead of `GLOO_API_KEY`. `--list-models` lists Gloo's models that can call tools.

## Run it

```sh
uv run python -m app               # opens http://127.0.0.1:8765
uv run python -m app --lan         # also prints a link for phones on the same Wi-Fi
uv run python -m app --fake-live   # no Ableton needed: a pretend set to play with
```

- `--lan` generates a random key and prints a link ending in `?key=...`. A phone must open that link once; after that it is remembered in a cookie. Without `--lan`, only the computer running the app can connect.
- `--fake-live` runs against an in-memory set. The top of the page says "Demo set, not Ableton" so nobody mistakes it for the real thing. Imports still need real audio files.
- `--no-browser` starts it without opening a browser window. `--port` picks another port.
- `uv run rig.py status` checks that Ableton is connected.

## What you see

The top bar shows whether Ableton is connected ("Ableton connected" with the number of tracks, or "Ableton isn't connected"), and, once it is, **Play**, the tempo in BPM and the overall **Main** level.

- **Left: the chat.** On a fresh start it shows the coming Sunday's date, a line about your set, and a few things to try, including **Import song files from a folder**. Type what you want under it and press **Send**.
- **Right: your set**, in three tabs: **Mixer**, **Songs** and **Room**. On a phone these four (Chat, Mixer, Songs, Room) are buttons along the bottom instead.

If Ableton is not open, the page still opens. You can chat and plan, and a list of changes that adds tracks can be saved as an Ableton file instead of applied. See [weekly-workflow.md](weekly-workflow.md).

## After you change RigLink

Ableton loads RigLink once, when it starts. If you edit anything in `ableton_script/RigLink/` (or pull an update that does), quit and reopen Ableton. Edits to the app and to `rig.py` need no restart. See [troubleshooting.md](troubleshooting.md).

## Where it keeps things

| What | Where |
| --- | --- |
| Room memory | `~/.holysound/room.json` |
| Mixer folder moves, and which tracks change with the song key | `~/.holysound/folders.json` |
| Each song's own mix and its checkpoints | `~/.holysound/song_mixes.json` |
| Folders you have imported (so you need not import them again) | `~/.holysound/imports.json` |
| Parts mixed from several stems | `~/Music/Holy Sound/Parts/<song>/<part>.wav` |

Setting `HOLYSOUND_HOME` moves the `.holysound` files, and puts the mixed parts under `$HOLYSOUND_HOME/Parts`. Your original stems are never changed or written into.

Next: [weekly-workflow.md](weekly-workflow.md).
