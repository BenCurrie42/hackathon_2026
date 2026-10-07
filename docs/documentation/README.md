# Holy Sound

Describe your Sunday, get a working Ableton Live session. Holy Sound builds and runs a Live set for a church worship team from a plain-English chat. Product framing is in the top-level [README](../../README.md); what is missing, ranked, is in [td_next.md](../td_next.md). Current version: 1.1.0 (see the [changelog](../../CHANGELOG.md)).

This folder is mirrored to the GitHub wiki: edit the pages here, then publish them with `scripts/publish_wiki.py`.

## Using it

| Page | Covers |
| --- | --- |
| [Getting started](getting-started.md) | Install, RigLink, running the app, `--lan`, `--fake-live`, where files are kept |
| [Weekly workflow](weekly-workflow.md) | Import a song folder or vendor set, Apply, song mix, key changes, song order, tidy |
| [CLI and live control](cli-and-live-control.md) | `rig.py` command reference |
| [Troubleshooting](troubleshooting.md) | "unknown cmd", No Input, silent mic, import errors |

## Developer documentation

How the code works, module by module.

| Page | Covers |
| --- | --- |
| [Architecture](architecture.md) | The two backends, the web app, how they connect, design principles |
| [Part tracks, imports, key and transpose](part-tracks-and-imports.md) | `parts.py`, `mixdown.py`, `tidy.py`, `song_key.py`, `vendor_set.py`, transpose that keeps tempo |
| [File renderer](file-renderer.md) | `RigSpec` to `.als`: `file_builder/`, ID pools, devices, format invariants |
| [Ableton format notes](ableton-format-notes.md) | Where the verified `.als` facts live and how to add one |
| [RigLink](riglink.md) | The Control Surface inside Live, its socket protocol, `LiveConnection` |
| [Web server API](web-server-api.md) | `app/server.py` endpoints, shared Live connection, `fake_live` |
| [Assistant and actions](assistant-and-actions.md) | Conversation loop, providers, every proposable action |
| [Audio analysis and memory](audio-analysis-and-memory.md) | WAV/AIFF reading, song maps, room memory, mixer folders and key choices |
| [Frontend](frontend.md) | `app/static/`: chat, change slip, console mixer, channel drawer, song mix |
| [Testing and development](testing-and-development.md) | Setup, test suite, Live dev loop, release process |

New to the code: read Architecture, then whichever backend you are touching.

## Planning

| Page | Covers |
| --- | --- |
| [Holy Sound for Agents of Flourishing](../agents_of_flourishing.md) | What to add so it reads as an agent for the Gloo Challenge 1 judges, ranked |
