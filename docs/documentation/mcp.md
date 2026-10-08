# MCP server

`app/mcp.py` lets any MCP agent (Claude Code, Claude Desktop, Cursor, ...) run the Sunday set: read what's in it, propose changes, apply them. It is a thin layer over the web app's JSON API ([web-server-api.md](web-server-api.md)), so the app must be running. An agent's changes go through the same actions, validation and Apply as the built-in assistant ([assistant-and-actions.md](assistant-and-actions.md)), and show on every open page as a proposal.

Related pages: [web-server-api.md](web-server-api.md), [assistant-and-actions.md](assistant-and-actions.md), [testing-and-development.md](testing-and-development.md).

## Setup

Start the app (with Ableton, or `--fake-live` without it):

```
uv run python -m app --fake-live --no-browser
```

Add the server to the agent. Claude Code, from the repo root:

```
claude mcp add holy-sound -- uv run --directory "$PWD" python -m app.mcp
```

Then `/mcp` in Claude Code (or restart it) to load the tools. Claude Desktop or Cursor, in their MCP config:

```json
{"mcpServers": {"holy-sound": {
  "command": "uv",
  "args": ["run", "--directory", "/path/to/hackathon2026", "python", "-m", "app.mcp"]
}}}
```

`HOLYSOUND_URL` (environment or `.env`) points it at an app on another port; default `http://127.0.0.1:8765`. The server always reaches the app as localhost, so the `--lan` key isn't needed.

## Tools

| Tool | API | What it does |
| --- | --- | --- |
| `read_session` | `GET /api/notes` | The session notes as text: tracks, levels, routing, effects, EQ, songs and saved mixes, imports, room facts. Read first. |
| `read_state` | `GET /api/state` | The whole state as compact JSON. |
| `propose_changes` | `POST /api/proposals` | `actions` (the built-in assistant's `propose_changes` schema, from `_tools()`) plus an optional `say` sentence. Returns the proposal id and its steps. Nothing changes yet. |
| `apply_changes` | `POST /api/proposals/<id>/apply` | Runs the steps; returns each result (`ok:` / `FAILED:`, with meter detail for a listen). |
| `dismiss_changes` | `POST /api/proposals/<id>/dismiss` | Drops a pending proposal. |
| `ask_assistant` | `POST /api/chat` | Messages Holy Sound's own assistant; returns its reply and any proposal. Needs the app's AI provider set up. |
| `browse_folders` | `GET /api/folders` | A folder listing, for finding stems. |
| `import_folder` | `POST /api/import` `report_only` | Measures a folder's audio and remembers its file ids; returns the report the built-in assistant would read (file ids, levels, parts, key guesses, vendor set). Then propose `add_song` / `import_part` with those ids. |
| `mixer_command` | `POST /api/live` | One direct RigLink command from `DIRECT_COMMANDS` (play, stop, a fader by index...), no proposal. |
| `song_mix` | `POST /api/song-mix` | `pick`, `checkpoint`, `restore`, `delete`. |
| `remember` | `POST /api/room` | `add` a room fact, `forget` one by `[id]`, or list them. |
| `list_devices`, `list_presets` | `GET /api/devices`, `/api/presets` | Stock devices and a device's presets. |
| `export_session_file` | `GET /api/proposals/<id>/export` | Writes a proposal's new tracks to a `.als` at `path`; returns what couldn't go in. Marks the proposal exported. |
| `reset_conversation` | `POST /api/reset` | Clears the chat; the set and memory stay. |

Errors come back as tool results with `isError: true` and the app's own sentence ("Those changes didn't validate: ...", "That suggestion has already been dealt with.", "Holy Sound isn't running at ..."), so the agent can fix and retry.

## Protocol

Newline-delimited JSON-RPC 2.0 on stdin/stdout, written by hand (no `mcp` package; the dependency list is fixed). Handled: `initialize` (echoes the client's protocol version if it's one of `2025-06-18`, `2025-03-26`, `2024-11-05`, else the newest; capabilities `tools` only; `instructions` tell the agent to read first and speak in intent), `ping`, `tools/list`, `tools/call`. Notifications get no reply; anything else is `-32601`. Bad JSON is `-32700`.

The HTTP client (`HolySound`) uses `urllib` with a 600 s timeout, since a chat turn or an Apply that loads devices can be slow.

## Testing

`McpTest` in `tests/test_app.py` runs `mcp.serve` over in-memory stdio against a real `App` and HTTP server on the fake Live: the handshake and tool list, propose then apply (and applying twice is refused), a bad proposal, the import report, browsing, room memory, and the app not running.

By hand, with the app running, drive the real process:

```
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_session","arguments":{}}}' \
  | uv run python -m app.mcp
```

## Limits

- The built-in assistant's history doesn't hear of an outside proposal; its next session notes show the result.
- `ask_assistant` blocks while the assistant answers; `propose_changes` is refused while it's busy.
- No resources or prompts, only tools.
