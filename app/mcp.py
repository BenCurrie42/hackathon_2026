"""Holy Sound as an MCP server, so any agent can run the Sunday set.

    uv run python -m app.mcp                         # headless: no web app needed
    uv run python -m app.mcp --fake-live             # headless, on a pretend Live Set
    HOLYSOUND_URL=http://127.0.0.1:8765 uv run python -m app.mcp   # through the running web app

Headless (the default), it runs the app's own App and JSON API (app/server.py `api`)
in this process and talks to RigLink itself, so only Ableton has to be open. With
HOLYSOUND_URL it is a thin layer over a running web app instead, and the agent's
proposals show on every open page. Either way the agent's changes go through the
same actions, validation and Apply as the built-in assistant's.

MCP over stdio, by hand: newline-delimited JSON-RPC 2.0 on stdin/stdout, no
SDK (the project's dependencies are fixed). Only tools are offered. Stdout is the
protocol, so nothing else may print to it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.assistant import _tools

DEFAULT_URL = "http://127.0.0.1:8765"
FOLLOW_SECONDS = 1.0  # headless: how often to look at Live, as the page's poll does
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
TIMEOUT = 600  # seconds; a chat turn or an Apply that loads devices can take a while

NOT_RUNNING = ("Holy Sound isn't running at {url}. Start it with `uv run python -m app` "
               "(add --fake-live to try it without Ableton).")


class HolySoundError(Exception):
    """The app answered with an error sentence, or couldn't be reached."""


class HolySound:
    """The running web app, over HTTP."""

    def __init__(self, url=None):
        self.url = (url or os.environ.get("HOLYSOUND_URL") or DEFAULT_URL).rstrip("/")

    def request(self, method, path, body=None, raw=False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                payload = res.read()
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read())["error"]
            except (ValueError, KeyError, TypeError):
                message = f"Holy Sound answered {e.code}."
            raise HolySoundError(message) from e
        except (urllib.error.URLError, OSError) as e:
            raise HolySoundError(NOT_RUNNING.format(url=self.url)) from e
        return payload if raw else json.loads(payload)

    def get(self, route, **query):
        query = {k: v for k, v in query.items() if v is not None}
        return self.request("GET", route + ("?" + urllib.parse.urlencode(query) if query else ""))

    def post(self, path, body=None):
        return self.request("POST", path, body or {})


class LocalHolySound(HolySound):
    """The app itself, in this process: the same API as over HTTP, with no server running."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, body=None, raw=False):
        from app.actions import _sentence
        from app.assistant import AssistantUnavailable
        from app.live import LiveUnavailable
        from app.server import UserError, api
        from live_control.live_connection import RigLinkError

        route, _, query = path.partition("?")
        query = dict(urllib.parse.parse_qsl(query))
        try:
            result = api(self.app, method, route, query, body)
        except (UserError, LiveUnavailable, AssistantUnavailable) as e:
            raise HolySoundError(str(e)) from e
        except RigLinkError as e:
            raise HolySoundError(_sentence(e)) from e
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            raise HolySoundError("Something went wrong inside Holy Sound. Try again; if it keeps "
                                 "happening, restart the agent's Holy Sound server.") from e
        if result is None:
            raise HolySoundError("Not found.")
        if isinstance(result, bytes):
            return result
        return json.loads(json.dumps(result))  # exactly what the HTTP API would have sent


def follow_live(app, stop, every=FOLLOW_SECONDS):
    """Look at Live on a timer, as the page's poll does, so each song's mix is saved as it
    changes and put back when that song starts, even while no agent is calling a tool."""
    while not stop.wait(every):
        try:
            app.live_state()
        except Exception:
            traceback.print_exc(file=sys.stderr)


# -- tools ----------------------------------------------------------------------


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required)}


def _propose_schema():
    schema = _tools()[0]["input_schema"]  # the built-in assistant's propose_changes
    schema["properties"]["say"] = {
        "type": "string",
        "description": "One plain sentence the volunteer sees with the proposal. Optional.",
    }
    return schema


def tool_list():
    return [
        {
            "name": "read_session",
            "description": (
                "What's in the open Ableton set right now, as text: tracks, levels, routing, effects, "
                "EQ, songs and their saved mixes, imported folders, and what's remembered about the "
                "room. The same notes the built-in assistant reads. Read this before proposing changes."
            ),
            "inputSchema": _schema(),
        },
        {
            "name": "read_state",
            "description": "The app's whole state as JSON: Live snapshot, chat, proposals, song mix, room facts.",
            "inputSchema": _schema(),
        },
        {
            "name": "propose_changes",
            "description": (
                "Propose a batch of changes to the open Live set, in plain intent (\"Lead Vocal down 3 dB\"), "
                "never raw device parameters. They show in the app for the volunteer; nothing changes until "
                "apply_changes. Include every change for this request, in order. Replaces any earlier "
                "pending proposal."
            ),
            "inputSchema": _propose_schema(),
        },
        {
            "name": "apply_changes",
            "description": "Run a proposal's steps in Ableton, as pressing Apply does. Returns each step's result.",
            "inputSchema": _schema({"proposal_id": {"type": "string"}}, ["proposal_id"]),
        },
        {
            "name": "dismiss_changes",
            "description": "Drop a pending proposal without running it.",
            "inputSchema": _schema({"proposal_id": {"type": "string"}}, ["proposal_id"]),
        },
        {
            "name": "ask_assistant",
            "description": (
                "Send a message to Holy Sound's own assistant, as the volunteer would type it in the app. "
                "Returns its reply and any proposal it made (apply it with apply_changes)."
            ),
            "inputSchema": _schema({"message": {"type": "string"}}, ["message"]),
        },
        {
            "name": "expert_mode",
            "description": (
                "Fix the mix on its own, as the app's Expert mode button does: a lead engineer briefs a "
                "vocals, rhythm, band and playback specialist, each changes only its own tracks (faders, "
                "pan, mute, sends, EQ), and their changes are applied without asking, for up to three "
                "rounds. Works on the song the mixer is on and checkpoints its mix before and after. "
                "Returns what each agent said and did."
            ),
            "inputSchema": _schema({"goal": {"type": "string", "description": "Optional, e.g. \"vocals on top\"."}}),
        },
        {
            "name": "browse_folders",
            "description": "List a folder on this computer, for finding stems to import. No path: a starting place.",
            "inputSchema": _schema({"path": {"type": "string"}}),
        },
        {
            "name": "import_folder",
            "description": (
                "Measure every WAV/AIFF in a folder and remember its files so import_part can use them. "
                "Returns each file's id, length and level, the part each stem belongs to, a key guess per "
                "song, and a vendor set's tempo and sections if one sits beside the stems. Then propose "
                "add_song / import_part actions with those file ids."
            ),
            "inputSchema": _schema({"folder": {"type": "string", "description": "Absolute path."}}, ["folder"]),
        },
        {
            "name": "mixer_command",
            "description": (
                "Send one direct command to Ableton, as the mixer panel does, skipping proposals. For quick "
                "moves like play, stop or one fader. Commands: set_volume(track_index, db), set_pan, "
                "set_mute, set_solo, set_send, set_tempo, play, stop, fire_scene(scene_index), load_device, "
                "delete_device, set_track_name, create_scene, set_scene, set_routing, create_audio_track, "
                "create_midi_track, create_return_track, get_routing, set_track_color, set_clip_gain, "
                "set_clip_active, transpose_song, set_eq_band. Prefer propose_changes for anything that "
                "names tracks by name or touches several things."
            ),
            "inputSchema": _schema({
                "cmd": {"type": "string"},
                "args": {"type": "object", "description": "Keyword arguments, e.g. {\"track_index\": 0, \"db\": -6}."},
            }, ["cmd"]),
        },
        {
            "name": "song_mix",
            "description": (
                "The mixer's per-song mix. pick: put the mixer on a song (scene_index, or null for the shared "
                "mix). checkpoint: save a named copy of the current song's mix (label). restore / delete: a "
                "checkpoint by id (ids are in read_state, song_mix.checkpoints)."
            ),
            "inputSchema": _schema({
                "action": {"type": "string", "enum": ["pick", "checkpoint", "restore", "delete"]},
                "scene_index": {"type": ["integer", "null"]},
                "label": {"type": "string"},
                "id": {"type": "string"},
            }, ["action"]),
        },
        {
            "name": "remember",
            "description": (
                "Lasting facts about this church's room, gear and team, kept week to week. add: a new fact. "
                "forget: a fact's [id] from the list this returns. Neither: just list them."
            ),
            "inputSchema": _schema({"add": {"type": "string"}, "forget": {"type": "integer"}}),
        },
        {
            "name": "list_devices",
            "description": "The stock Ableton effects and instruments that can be loaded, by category.",
            "inputSchema": _schema(),
        },
        {
            "name": "list_presets",
            "description": "The stock presets for one device, e.g. \"Compressor\" or \"Reverb\".",
            "inputSchema": _schema({"device": {"type": "string"}}, ["device"]),
        },
        {
            "name": "export_session_file",
            "description": (
                "Write a proposal's new tracks to an Ableton .als file, for when Live isn't open. Needs Live 12 "
                "installed (it copies Live's own effects). Returns what couldn't go in the file."
            ),
            "inputSchema": _schema({
                "proposal_id": {"type": "string"},
                "path": {"type": "string", "description": "Where to write the .als (absolute)."},
            }, ["proposal_id", "path"]),
        },
        {
            "name": "reset_conversation",
            "description": "Start the app's chat over. The set, mixes and remembered facts stay.",
            "inputSchema": _schema(),
        },
    ]


def _proposal_text(p):
    lines = [f"Proposal {p['id']} ({p['status']}):"]
    lines += [f"{n}. {s['text']}" for n, s in enumerate(p["steps"], 1)]
    if p["results"]:
        lines.append("Results:")
        lines += [("ok: " if r["ok"] else "FAILED: ") + r["text"] + (f"\n{r['detail']}" if r.get("detail") else "")
                  for r in p["results"]]
    return "\n".join(lines)


def _proposal_in(state, pid):
    for entry in state["chat"]:
        if entry.get("proposal", {}).get("id") == pid:
            return entry["proposal"]
    raise HolySoundError("That proposal isn't in the conversation any more.")


def _facts_text(state):
    facts = state["room"]
    if not facts:
        return "Nothing remembered yet."
    return "\n".join(f"[{f['id']}] {f['text']}" for f in facts)


def call_tool(hs, name, args):
    """Run one tool. Returns its text; raises HolySoundError with a sentence."""
    if name == "read_session":
        return hs.get("/api/notes")["notes"]
    if name == "read_state":
        return json.dumps(hs.get("/api/state"), separators=(",", ":"))
    if name == "propose_changes":
        p = hs.post("/api/proposals", {"actions": args.get("actions"), "text": args.get("say")})["proposal"]
        return _proposal_text(p) + "\nCall apply_changes with this id to run it."
    if name == "apply_changes":
        pid = str(args["proposal_id"])
        return _proposal_text(_proposal_in(hs.post(f"/api/proposals/{pid}/apply"), pid))
    if name == "dismiss_changes":
        pid = str(args["proposal_id"])
        hs.post(f"/api/proposals/{pid}/dismiss")
        return f"Proposal {pid} dismissed."
    if name == "ask_assistant":
        before = len(hs.get("/api/state")["chat"])
        state = hs.post("/api/chat", {"message": args["message"]})
        out = []
        for entry in state["chat"][before:]:
            if entry["role"] == "user":
                continue
            if entry.get("text"):
                out.append(entry["text"])
            if entry.get("proposal"):
                out.append(_proposal_text(entry["proposal"]))
        return "\n\n".join(out) or "The assistant didn't reply."
    if name == "expert_mode":
        from app.expert import transcript_text

        before = len(hs.get("/api/state")["chat"])
        state = hs.post("/api/expert", {"goal": args.get("goal") or ""})
        return transcript_text(state["chat"][before:]) or "The crew didn't say anything."
    if name == "browse_folders":
        return json.dumps(hs.get("/api/folders", path=args.get("path")), indent=1)
    if name == "import_folder":
        return hs.post("/api/import", {"folder": args["folder"], "report_only": True})["report"]
    if name == "mixer_command":
        result = hs.post("/api/live", {"cmd": args["cmd"], "args": args.get("args") or {}})["result"]
        return json.dumps(result, indent=1)
    if name == "song_mix":
        state = hs.post("/api/song-mix", {k: args[k] for k in ("action", "scene_index", "label", "id") if k in args})
        return json.dumps(state["song_mix"], indent=1)
    if name == "remember":
        body = {}
        if args.get("add"):
            body["add"] = args["add"]
        if args.get("forget") is not None:
            body["remove"] = int(args["forget"])
        state = hs.post("/api/room", body) if body else hs.get("/api/state")
        return _facts_text(state)
    if name == "list_devices":
        return json.dumps(hs.get("/api/devices")["devices"], indent=1)
    if name == "list_presets":
        return json.dumps(hs.get("/api/presets", device=args["device"])["presets"], indent=1)
    if name == "export_session_file":
        pid = str(args["proposal_id"])
        data = hs.request("GET", f"/api/proposals/{pid}/export", raw=True)
        path = Path(args["path"]).expanduser()
        try:
            path.write_bytes(data)
        except OSError as e:
            raise HolySoundError(f"Couldn't write {path}: {e.strerror}.") from e
        notes = hs.get("/api/proposals/export-notes", id=pid)["notes"]
        return f"Wrote {path}." + ("\nNot in the file:\n" + "\n".join(f"- {n}" for n in notes) if notes else "")
    if name == "reset_conversation":
        hs.post("/api/reset")
        return "The conversation starts over."
    raise HolySoundError(f"There's no tool called {name}.")


# -- JSON-RPC over stdio ----------------------------------------------------------


def handle(hs, message):
    """One JSON-RPC message in, the reply out (None for a notification)."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notifications/initialized, notifications/cancelled: nothing to answer

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "holy-sound", "version": "1.3.0"},
            "instructions": (
                "Holy Sound runs a church worship team's Ableton Live set. Read read_session first. "
                "Make changes with propose_changes then apply_changes; say what you want in intent "
                "(track names, dB, preset names), never raw device parameters."
            ),
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            text, failed = call_tool(hs, params.get("name"), params.get("arguments") or {}), False
        except HolySoundError as e:
            text, failed = str(e), True
        except (KeyError, TypeError, ValueError) as e:
            text, failed = f"That tool call was missing or had a bad argument ({e}).", True
        return ok({"content": [{"type": "text", "text": text}], "isError": failed})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}


def serve(stdin=sys.stdin, stdout=sys.stdout, hs=None):
    hs = hs or HolySound()
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(hs, message) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv=None):
    from app.server import load_dotenv

    parser = argparse.ArgumentParser(description="Holy Sound's MCP server, over stdio.")
    parser.add_argument("--url", help="Go through the web app running here instead of headless "
                                      "(or set HOLYSOUND_URL).")
    parser.add_argument("--fake-live", action="store_true", help="Headless, on a pretend Live Set.")
    args = parser.parse_args(argv)
    load_dotenv()  # HOLYSOUND_URL and the AI provider's settings may be in .env
    url = args.url or os.environ.get("HOLYSOUND_URL")
    if url:
        return serve(hs=HolySound(url))

    from app.server import build_app

    app = build_app(fake_live=args.fake_live)
    stop = threading.Event()
    threading.Thread(target=follow_live, args=(app, stop), daemon=True).start()
    try:
        serve(hs=LocalHolySound(app))
    finally:
        stop.set()
        app.live.close()


if __name__ == "__main__":
    main()
