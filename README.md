# Holy Sound

**Sunday-ready sound, without a sound engineer.**

[Watch the demo (80 seconds)](https://drive.google.com/file/d/1aSR4sKnkiegDsENYAPddvguQtW6_a5OT/view?usp=sharing)

8 out of 10 churches say they don't have a qualified sound technician. Those
that run backing tracks usually have one volunteer who taught themselves Ableton
and rebuilds the session by hand every week: importing dozens of stems, setting
tempos and keys, sending the click to the band's in-ears and balancing every
song. That can take up to five hours a week. Churches without that volunteer
play without tracks.

Holy Sound gives those hours back.

## Say what's wrong. Hear it fixed.

- **"The lead vocal is getting buried."** The vocal comes up, live, while the
  song plays.
- **"The acoustic sounds like it's in a tin can."** Holy Sound finds the cause
  (a harsh EQ boost and two deep cuts), explains it in plain words and puts in
  a gentle curve once you approve.
- **"Here are this week's songs."** Point it at the download from Washed or
  MultiTracks. Every stem lands on the right track, at the right tempo and key,
  with the click and guide in the in-ears and away from the room.
- **Expert mode.** One button. A lead engineer and four AI specialists (vocals,
  rhythm, band, playback) work through the whole mix together. On a messy
  vendor set they made 49 fixes in one pass. Before and after are saved, so
  you can flip between them.

It remembers your room (your interface, who's on which input, where the in-ears
go), so next Sunday starts where this one ended. Each song keeps its own mix,
and it works from a phone on the church Wi-Fi.

## Safe by design

- **You approve every change** in chat before it reaches the room. Expert
  mode is the one exception you choose: it only moves mixer settings, and it
  saves the mix before it starts. Ableton's undo still works for every step.
- **The AI says what it wants, never knob values.** It asks for "gentle
  compression on the lead vocal." Holy Sound turns that into Ableton's own stock
  settings, so the result is always something Ableton would make itself.
- **Nothing plays in the room without you.** Anything you'd hear is flagged
  before you apply it.

## Under the hood

Holy Sound runs Ableton Live, which many worship teams already use for click and
tracks. A small add-on inside Ableton, RigLink, makes changes in the open set
live. When Ableton is closed, Holy Sound writes a session file instead. The
conversation runs on **Claude**, or on **Gloo AI Studio** with one key for
Claude, GPT, Gemini and open models. Other AI agents can drive Holy Sound
through MCP.

Built in Python, with a plain web app and no install for the volunteer beyond
one setup command.

## Run it yourself

You'll need a Mac, Python 3.11+, [uv](https://docs.astral.sh/uv/), Ableton Live
12, and an API key from Gloo AI Studio, Anthropic or OpenCode Go.

```sh
uv sync
uv run python scripts/install_riglink.py   # connects Ableton; no settings to click
cp .env.example .env                       # then paste your API key in
```

The RigLink setup asks you to quit Ableton, picks RigLink as a Control Surface,
reopens Ableton and waits until RigLink answers. If it can't do that on your
Mac, it tells you the one step to do by hand: in Ableton, open **Settings →
Link, Tempo & MIDI** and pick **RigLink** under Control Surface.

Then start the app:

```sh
uv run python -m app               # opens http://127.0.0.1:8765
uv run python -m app --lan         # also prints a link for phones on your Wi-Fi
uv run python -m app --fake-live   # no Ableton needed, a pretend set to play with
```

### Choosing the AI

**Gloo AI Studio.** One key for Claude, GPT, Gemini and open models (the key is
on Studio's API Credentials page):

```sh
HOLYSOUND_PROVIDER=gloo
GLOO_API_KEY=your-key
HOLYSOUND_MODEL=gloo-anthropic-claude-sonnet-5.5   # optional; or auto, or anthropic / openai / google / open source
GLOO_TRADITION=evangelical                         # optional
```

**Anthropic.** Put `ANTHROPIC_API_KEY=your-key` in `.env`. This is the default,
and Claude is what we test against.

**OpenCode Go.** Open models like Kimi, GLM and Qwen, for a tighter budget:

```sh
HOLYSOUND_PROVIDER=opencode-go
OPENCODE_API_KEY=your-key
HOLYSOUND_MODEL=kimi-k3    # optional, kimi-k3 is the default
```

`uv run python -m app --list-models` shows which models your provider offers
that Holy Sound can use.

### From another agent (MCP)

Start the app, then add Holy Sound to any MCP client. For Claude Code:

```sh
claude mcp add holy-sound -- uv run --directory "$PWD" python -m app.mcp
```

### Tests

They need neither Ableton nor an API key:

```sh
uv run python -m unittest discover tests
```

---

Built for the 2026 Gloo AI Hackathon, Ministry Resourcing track.
[Build document](AGENT_BUILD.md) · [Setup and docs](docs/documentation/README.md) ·
[Changelog](CHANGELOG.md) · [MIT licence](LICENSE)
