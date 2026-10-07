# Developer documentation

How the code works, module by module. Product framing is in the top-level
[README](../../README.md); what's missing, ranked, is in [td_next.md](../td_next.md).

| Page | Covers |
| --- | --- |
| [Architecture](architecture.md) | The two backends, the web app, how they connect, design principles |
| [File renderer](file-renderer.md) | `RigSpec` → `.als`: `file_builder/`, ID pools, devices, format invariants |
| [RigLink](riglink.md) | The Control Surface inside Live, its socket protocol, `LiveConnection` |
| [CLI and live control](cli-and-live-control.md) | `rig.py` commands, `song import`, stem level/pairing/timecode helpers |
| [Web server API](web-server-api.md) | `app/server.py` endpoints, shared Live connection, `fake_live` |
| [Assistant and actions](assistant-and-actions.md) | Conversation loop, providers, every proposable action |
| [Audio analysis and memory](audio-analysis-and-memory.md) | WAV/AIFF reading, song maps, room memory, mixer folders |
| [Frontend](frontend.md) | `app/static/`: chat, action cards, mixer |
| [Testing and development](testing-and-development.md) | Setup, test suite, Live dev loop, release process |

Start with Architecture, then whichever backend you're touching.
