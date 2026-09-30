"""The conversation: plain English in, proposed changes out.

Claude sees the current state of the set and answers in two ways: plain text
(questions, explanations) and a propose_changes tool call carrying a batch of
actions (app/actions.py). The tool input is validated with pydantic; if it
doesn't validate, the errors go back to Claude to fix, a couple of times at
most. Nothing reaches Live until the volunteer presses Apply -- the tool call
is only a proposal, and its result tells Claude what the volunteer decided.
"""

from __future__ import annotations

import itertools
import os
import threading

from pydantic import ValidationError

from app.actions import Proposal, is_destructive

MODEL = os.environ.get("HOLYSOUND_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("HOLYSOUND_EFFORT", "medium")
MAX_TOKENS = 16000
FIX_ATTEMPTS = 2

# Common stock devices, used when Live isn't connected to tell us its own list.
FALLBACK_DEVICES = {
    "audio_effects": [
        "Auto Filter", "Channel EQ", "Chorus-Ensemble", "Compressor", "Delay", "Echo",
        "EQ Eight", "EQ Three", "Gate", "Glue Compressor", "Hybrid Reverb", "Limiter",
        "Multiband Dynamics", "Reverb", "Saturator", "Utility",
    ],
    "instruments": ["Drift", "Drum Rack", "Operator", "Sampler", "Simpler", "Wavetable"],
}

SYSTEM = """\
You are Holy Sound, the assistant that sets up Ableton Live for a church worship team.

The person you're talking to is a volunteer running tech at a small church. They are not an \
audio engineer and may never have used Ableton beyond pressing play. Talk like a calm, \
friendly sound tech helping out on a Saturday: short sentences, plain words, no jargon \
unless you explain it. Never show JSON, file names, or technical identifiers.

## What you can do

You change the Ableton Live set that is open on their computer, using the propose_changes \
tool. The tool doesn't change anything by itself: it shows the volunteer a list of steps \
with an Apply button, and the next message tells you whether they applied them and what \
happened. So:

- Put every change for one request in a single propose_changes call, in the order they \
should happen. A later step may refer to a track an earlier step creates.
- In your text, say briefly what you're suggesting and why. Don't repeat the step list — \
the volunteer sees it. Don't claim anything has happened until you're told it was applied.
- If you're missing something you need, ask instead of guessing. The usual gaps: which \
input number each mic or instrument is plugged into, and which outputs go to in-ear \
monitors. Ask everything you need in one message, and propose what you can already do.
- Only delete tracks, effects or songs when the volunteer clearly asks for that.

## How to describe changes

You name intent; the app handles Ableton's internals.

- Effects are stock Live devices by name, e.g. "Compressor", "EQ Eight", "Reverb". Only \
use names from the stock device list in the session notes. You may name a stock preset, \
but only one you're sure ships with Live; otherwise leave the preset out.
- Levels are in dB (0 is unity, -70 is off). Pan is -1 (left) to 1 (right).
- Hardware inputs are written as printed on the audio interface: "1" for a mono mic or DI, \
"3/4" for a stereo pair like keys.
- Playback tracks (click, guide, pads, stems) have no input.
- Songs are Live scenes: one per song, with the song's tempo.
- Shared reverbs and delays are return tracks. Send tracks to them with set_send rather \
than putting a reverb on every track.

## Worship rig know-how

- Click and guide cues are for the band's ears only. They must never reach the Master \
(the congregation). Route them to an interface output that feeds in-ears ("Ext. Out") — \
ask which outputs if you don't know.
- Vocals: a gentle EQ and compressor is a good default; send to a shared reverb.
- Keep levels conservative (0 dB or a little below). Don't boost above +3 dB.

## Session notes

Each message from the volunteer starts with <session> notes showing the set as it is right \
now: tracks, returns, songs, tempo, and the stock devices this copy of Live has. Trust \
them over earlier messages — the volunteer may have changed things by hand. If the notes \
say Live isn't connected, you can still propose a new set of tracks; the volunteer can \
download it as a session file instead of applying it.
"""


def _tool():
    return {
        "name": "propose_changes",
        "description": (
            "Propose a batch of changes to the open Live set. The volunteer reviews the list "
            "and applies it with one button. Include every change for this request, in order."
        ),
        "input_schema": _inline_refs(Proposal.model_json_schema()),
    }


def _inline_refs(schema):
    """Replace $ref pointers with the definitions they name."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "discriminator"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


NOT_SET_UP = (
    "The AI isn't set up yet. Put ANTHROPIC_API_KEY=... in a .env file next to "
    "README.md and restart Holy Sound."
)


class AssistantUnavailable(RuntimeError):
    """No API key, or the AI service can't be reached. Message is a sentence."""


class Conversation:
    """One shared conversation. The laptop and the phone see the same one."""

    def __init__(self, client_factory=None):
        self._client_factory = client_factory or _default_client
        self._client = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.busy = False
        self.setup_error = None  # set when a request shows the key is missing or wrong
        self.reset()

    def reset(self):
        self.messages = []
        self.transcript = []
        self.proposals = {}
        self._open_tool_use = None  # (tool_use_id, proposal_id) awaiting a tool_result

    # -- public -----------------------------------------------------------

    def client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def send(self, text, session_notes):
        """Handle one message from the volunteer. Returns the new transcript entries."""
        with self._lock:
            self.busy = True
            try:
                return self._send(text, session_notes)
            finally:
                self.busy = False

    def proposal(self, proposal_id):
        return self.proposals.get(proposal_id)

    def record_outcome(self, proposal_id, status, results=None):
        """The volunteer applied or dismissed a proposal."""
        with self._lock:
            p = self.proposals[proposal_id]
            p["status"] = status
            p["results"] = results

    # -- internals ----------------------------------------------------------

    def _entry(self, **fields):
        entry = {"id": next(self._ids), **fields}
        self.transcript.append(entry)
        return entry

    def _send(self, text, session_notes):
        client = self.client()
        content = []
        reopen = self._open_tool_use
        if reopen:
            content.append(self._tool_result(*reopen))
            self._open_tool_use = None
        content.append({"type": "text", "text": f"<session>\n{session_notes}\n</session>\n\n{text}"})
        self.messages.append({"role": "user", "content": content})
        mark = len(self.messages)
        user_entry = self._entry(role="user", text=text)

        try:
            for _attempt in range(FIX_ATTEMPTS + 1):
                response = self._create(client)
                if response.stop_reason == "refusal":
                    raise _Refused()
                self.messages.append({"role": "assistant", "content": _replayable(response.content)})

                reply = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
                call = next((b for b in response.content if b.type == "tool_use"), None)
                if call is None:
                    return [user_entry, self._entry(role="assistant", text=reply or "…")]

                try:
                    proposal = Proposal.model_validate(call.input)
                except ValidationError as e:
                    self.messages.append({"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": call.id, "is_error": True,
                        "content": "The changes didn't validate, so the volunteer hasn't seen them. "
                                   f"Fix these and call propose_changes again:\n{_errors(e)}",
                    }]})
                    continue

                pid = str(next(self._ids))
                self.proposals[pid] = {
                    "id": pid,
                    "actions": proposal.actions,
                    "status": "pending",
                    "results": None,
                }
                for other in self.proposals.values():
                    if other["id"] != pid and other["status"] == "pending":
                        other["status"] = "superseded"
                self._open_tool_use = (call.id, pid)
                return [user_entry, self._entry(role="assistant", text=reply, proposal_id=pid)]

            return [user_entry, self._entry(
                role="assistant",
                text="Sorry — I couldn't work out the right changes for that. Could you say it another way?",
            )]
        except _Refused:
            self._rewind(mark, reopen)
            return [user_entry, self._entry(role="assistant", text="Sorry, I can't help with that one.")]
        except Exception:
            self._rewind(mark, reopen)
            self.transcript.remove(user_entry)
            raise

    def _rewind(self, mark, reopen):
        """Forget a turn that never got an answer, so the history stays valid."""
        del self.messages[mark - 1:]
        self._open_tool_use = reopen

    def _tool_result(self, tool_use_id, proposal_id):
        p = self.proposals[proposal_id]
        if p["status"] == "applied":
            lines = [("✓ " if r["ok"] else "✗ ") + r["text"] for r in p["results"]]
            outcome = "The volunteer applied these changes. Results:\n" + "\n".join(lines)
        elif p["status"] == "dismissed":
            outcome = "The volunteer chose not to apply these changes."
        elif p["status"] == "exported":
            outcome = "The volunteer downloaded these tracks as a session file instead of applying them."
        else:
            outcome = "The volunteer hasn't applied these changes; they sent another message instead."
            p["status"] = "superseded"
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": outcome}

    def _create(self, client):
        import anthropic

        try:
            return client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=[_tool()],
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=self.messages,
                output_config={"effort": EFFORT},
                # If a safety classifier declines, retry on Anthropic's recommended model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            self.setup_error = (
                "The AI key isn't valid. Check ANTHROPIC_API_KEY in the .env file and restart Holy Sound."
            )
            raise AssistantUnavailable(self.setup_error) from e
        except TypeError as e:
            # The SDK only notices a missing key when it makes the first request.
            if "authentication" not in str(e):
                raise
            self.setup_error = NOT_SET_UP
            raise AssistantUnavailable(self.setup_error) from e
        except anthropic.RateLimitError as e:
            raise AssistantUnavailable("The AI service is busy right now. Try again in a minute.") from e
        except anthropic.APIConnectionError as e:
            raise AssistantUnavailable(
                "Couldn't reach the AI service. Check this computer's internet connection."
            ) from e
        except anthropic.APIStatusError as e:
            raise AssistantUnavailable(f"The AI service had a problem ({e.status_code}). Try again.") from e


class _Refused(Exception):
    pass


def _default_client():
    import anthropic

    try:
        return anthropic.Anthropic()
    except anthropic.AnthropicError as e:
        raise AssistantUnavailable(NOT_SET_UP) from e


def _replayable(blocks):
    """Response blocks as request blocks, unchanged, so history stays append-only."""
    out = []
    for block in blocks:
        data = block.model_dump(mode="json", exclude_none=True, by_alias=True)
        data.pop("parsed_output", None)
        out.append(data)
    return out


def _errors(error):
    lines = []
    for err in error.errors()[:12]:
        where = ".".join(str(p) for p in err["loc"])
        lines.append(f"- {where}: {err['msg']}")
    return "\n".join(lines)


# -- session notes ----------------------------------------------------------


def session_notes(snapshot, stock_devices, live_error=None):
    """The set as Claude sees it at the top of each message."""
    if snapshot is None:
        devices = FALLBACK_DEVICES
        lines = [
            f"Live is NOT connected ({live_error or 'not running'}). Changes can't be applied "
            "right now; new tracks can be downloaded as a session file.",
        ]
    else:
        devices = stock_devices or FALLBACK_DEVICES
        song = snapshot["song"]
        lines = [
            f"Live is connected. Tempo {song['tempo']:g} BPM, {song['numerator']}/{song['denominator']}, "
            + ("playing." if song["is_playing"] else "stopped."),
            "",
            "Tracks:" if snapshot["tracks"] else "Tracks: none yet.",
        ]
        for t in snapshot["tracks"]:
            lines.append(f"  {t['index'] + 1}. {_strip_line(t)}")
        lines.append("Returns (shared effects):" if snapshot["returns"] else "Returns: none.")
        for r in snapshot["returns"]:
            lines.append(f"  {chr(ord('A') + r['index'])}. {_strip_line(r)}")
        scenes = snapshot["scenes"]
        lines.append("Songs (scenes):" if scenes else "Songs: none.")
        for s in scenes:
            bpm = f" — {s['tempo']:g} BPM" if s["tempo"] else ""
            lines.append(f"  {s['index'] + 1}. {s['name'] or '(unnamed)'}{bpm}")
    lines.append("")
    lines.append("Stock devices in this Live:")
    for category, names in devices.items():
        lines.append(f"  {category.replace('_', ' ')}: {', '.join(names)}")
    return "\n".join(lines)


def _routing_text(side):
    if not side:
        return "?"
    return f"{side['type']} {side['channel']}".strip()


def _strip_line(t):
    parts = [t["name"]]
    if not t["is_return"]:
        parts.append("MIDI" if t.get("is_midi") else "audio")
        parts.append(f"in: {_routing_text(t.get('input'))}")
    parts.append(f"out: {_routing_text(t.get('output'))}")
    parts.append(t["volume"])
    parts.append(f"pan {t['pan']}")
    if t["mute"]:
        parts.append("MUTED")
    if t["solo"]:
        parts.append("SOLO")
    if t["devices"]:
        parts.append("effects: " + ", ".join(t["devices"]))
    sends = [f"{s['return']} {s['level']}" for s in t["sends"] if s.get("level_db") is not None]
    if sends:
        parts.append("sends: " + ", ".join(sends))
    return " | ".join(parts)


def describe_proposal(p):
    """A proposal as the UI shows it."""
    return {
        "id": p["id"],
        "status": p["status"],
        "results": p["results"],
        "steps": [{"text": a.describe(), "destructive": is_destructive(a)} for a in p["actions"]],
        "exportable": any(a.action == "add_track" for a in p["actions"]),
    }
