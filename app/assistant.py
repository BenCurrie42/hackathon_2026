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

from pydantic import BaseModel, Field, ValidationError

from app.actions import Proposal, is_audible, is_destructive
from rig import TRACK_COLORS

MODEL = os.environ.get("HOLYSOUND_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("HOLYSOUND_EFFORT", "medium")
MAX_TOKENS = 16000
FIX_ATTEMPTS = 2
MAX_STEPS = 6  # model calls per message: remembering, fixing, then answering

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
- Colour related tracks alike so the volunteer can find them at a glance: drums and bass \
red or orange, keys and pads blue or teal, guitars green, vocals purple or pink, click and \
guide grey. The colours are: """ + ", ".join(TRACK_COLORS) + """.

## Audio files

When the volunteer imports a folder, their message lists every audio file in it with \
measurements taken from the file: peak and "loud parts" in dBFS (how loud it is while it's \
actually sounding), and how much of the time it sounds at all.

- Make one audio track per part (Click, Guide, Pad, Bass, Drums, Keys, BGVs…), shared by \
every song — not one track per file. Playback tracks have no input.
- Folder and file names usually say which song a file belongs to. Make one song (scene) per \
song; use a tempo only if a name states it, otherwise ask.
- Put each file in its track and song with import_audio, naming the file exactly as listed.
- Balance the parts with clip gain (gain_db): bring their loud parts roughly in line with \
each other, and never let a clip's peak plus its gain go above -1 dBFS. Leave faders at \
0 dB so the volunteer has room to ride them.
- Say which files are silent or clip, and leave silent ones out.

## Listening

You can't hear the room, but a listen step plays a song and reads Live's level meters. It \
plays out loud, so say that, and only suggest it when the volunteer is setting up, never \
mid-service. After it's applied you get each track's meter readings (Live's own 0-1 meter, \
after the fader). Compare tracks with each other — the 0-1 scale isn't verified as dB, so \
never quote a reading as dB. Then suggest fader or clip gain changes in small steps \
(1-3 dB), and offer to listen again.

## Remembering their church

You have a memory of this church that lasts from week to week; it's at the end of the \
session notes. Use the remember tool to save lasting facts the volunteer tells or confirms: \
their audio interface and how many inputs it has, who sings or plays on which input, which \
outputs feed whose in-ears, how they like things set up. Save each fact as one short, \
self-contained sentence ("Lead vocal (Sarah) is on input 1."). Don't save one-off requests \
or anything about this week's songs. When a fact changes, forget the old one and save the \
new one. Rely on what you remember instead of asking again, and mention it briefly when it \
saves the volunteer a step ("Using input 1 for Sarah like last week").

## Session notes

Each message from the volunteer starts with <session> notes showing the set as it is right \
now: tracks, returns, songs, tempo, and the stock devices this copy of Live has. Trust \
them over earlier messages — the volunteer may have changed things by hand. If the notes \
say Live isn't connected, you can still propose a new set of tracks; the volunteer can \
download it as a session file instead of applying it.
"""


class Remember(BaseModel):
    """What the remember tool takes."""

    facts: list[str] = Field(default_factory=list, max_length=20, description=(
        "New lasting facts about the church's room, gear or team, each one short sentence."))
    forget: list[int] = Field(default_factory=list, description=(
        "Numbers (#) of remembered facts that are no longer true."))


def _tools():
    return [
        {
            "name": "propose_changes",
            "description": (
                "Propose a batch of changes to the open Live set. The volunteer reviews the list "
                "and applies it with one button. Include every change for this request, in order."
            ),
            "input_schema": _inline_refs(Proposal.model_json_schema()),
        },
        {
            "name": "remember",
            "description": (
                "Save or forget lasting facts about this church's room, gear and team, so next week "
                "you don't need to ask again. Takes effect immediately; the volunteer can see and "
                "delete every fact in the Room tab."
            ),
            "input_schema": _inline_refs(Remember.model_json_schema()),
        },
    ]


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

    def __init__(self, client_factory=None, room=None):
        self._client_factory = client_factory or _default_client
        self.room = room
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

    def send(self, text, session_notes, attachment=None):
        """Handle one message from the volunteer. Returns the new transcript entries.

        attachment goes to Claude with the message but isn't shown in the chat
        (the measurements of an imported folder, say).
        """
        with self._lock:
            self.busy = True
            try:
                return self._send(text, session_notes, attachment)
            finally:
                self.busy = False

    def follow_up(self, session_notes):
        """Let Claude react to applied results without a new message (after listening)."""
        with self._lock:
            if self._open_tool_use is None:
                return []
            self.busy = True
            try:
                return self._send(None, session_notes)
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

    def _send(self, text, session_notes, attachment=None):
        client = self.client()
        content = []
        reopen = self._open_tool_use
        if reopen:
            content.append(self._tool_result(*reopen))
            self._open_tool_use = None
        body = f"<session>\n{session_notes}\n</session>\n\n"
        if text is None:
            body += ("(Automatic: the changes were applied and their results are above. The "
                     "volunteer hasn't said anything new. Tell them briefly what the results mean, "
                     "and propose fixes if any are needed.)")
        else:
            body += text
        if attachment:
            body += f"\n\n{attachment}"
        content.append({"type": "text", "text": body})
        self.messages.append({"role": "user", "content": content})
        mark = len(self.messages)
        user_entry = self._entry(role="user", text=text) if text is not None else None
        shown = [user_entry] if user_entry else []

        try:
            fixes = 0
            unanswered = None  # a tool call we gave up on without a tool_result
            for _step in range(MAX_STEPS):
                response = self._create(client)
                if response.stop_reason == "refusal":
                    raise _Refused()
                self.messages.append({"role": "assistant", "content": _replayable(response.content)})

                reply = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
                call = next((b for b in response.content if b.type == "tool_use"), None)
                if call is None:
                    return shown + [self._entry(role="assistant", text=reply or "…")]

                if call.name == "remember":
                    if reply:
                        shown.append(self._entry(role="assistant", text=reply))
                    outcome, note = self._remember(call.input)
                    if note:
                        shown.append(self._entry(role="note", text=note))
                    self.messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": call.id, "content": outcome},
                    ]})
                    continue

                try:
                    proposal = Proposal.model_validate(call.input)
                except ValidationError as e:
                    fixes += 1
                    if fixes > FIX_ATTEMPTS:
                        unanswered = call
                        break
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
                return shown + [self._entry(role="assistant", text=reply, proposal_id=pid)]

            # Gave up. Answer the dangling tool call, or the history stops being valid.
            if unanswered is not None:
                self.messages.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": unanswered.id, "is_error": True,
                    "content": "Stopped here; the volunteer saw an apology instead.",
                }]})
            return shown + [self._entry(
                role="assistant",
                text="Sorry — I couldn't work out the right changes for that. Could you say it another way?",
            )]
        except _Refused:
            self._rewind(mark, reopen)
            return shown + [self._entry(role="assistant", text="Sorry, I can't help with that one.")]
        except Exception:
            self._rewind(mark, reopen)
            if user_entry:
                self.transcript.remove(user_entry)
            raise

    def _remember(self, data):
        """Run the remember tool. Returns (tool result text, note for the chat or None)."""
        if self.room is None:
            return "Memory isn't available in this session.", None
        try:
            request = Remember.model_validate(data)
        except ValidationError as e:
            return f"Nothing saved; fix these and try again:\n{_errors(e)}", None
        removed = self.room.remove(request.forget)
        added = self.room.add(request.facts)
        parts = []
        if added:
            parts.append("Remembered: " + " · ".join(f["text"] for f in added))
        if removed:
            parts.append("Forgot: " + " · ".join(f["text"] for f in removed))
        outcome = (f"Saved {len(added)} fact(s), forgot {len(removed)}. "
                   "Now answer the volunteer.") if parts else "Nothing new to save."
        return outcome, "\n".join(parts) or None

    def _rewind(self, mark, reopen):
        """Forget a turn that never got an answer, so the history stays valid."""
        del self.messages[mark - 1:]
        self._open_tool_use = reopen

    def _tool_result(self, tool_use_id, proposal_id):
        p = self.proposals[proposal_id]
        if p["status"] == "applied":
            lines = []
            for r in p["results"]:
                lines.append(("✓ " if r["ok"] else "✗ ") + r["text"])
                if r.get("detail"):
                    lines.append(r["detail"])
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
                tools=_tools(),
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


def session_notes(snapshot, stock_devices, live_error=None, imports=None, room=None):
    """The set as Claude sees it at the top of each message.

    imports: {folder name: number of files} for folders imported this session.
    room: the RoomMemory, whose facts go last.
    """
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
        scene_names = {s["index"]: s["name"] or f"song {s['index'] + 1}" for s in snapshot["scenes"]}
        for t in snapshot["tracks"]:
            lines.append(f"  {t['index'] + 1}. {_strip_line(t, scene_names)}")
        lines.append("Returns (shared effects):" if snapshot["returns"] else "Returns: none.")
        for r in snapshot["returns"]:
            lines.append(f"  {chr(ord('A') + r['index'])}. {_strip_line(r)}")
        scenes = snapshot["scenes"]
        lines.append("Songs (scenes):" if scenes else "Songs: none.")
        for s in scenes:
            bpm = f" — {s['tempo']:g} BPM" if s["tempo"] else ""
            lines.append(f"  {s['index'] + 1}. {s['name'] or '(unnamed)'}{bpm}")
    if imports:
        lines.append("")
        lines.append("Imported audio folders (import_audio can use their files): "
                     + ", ".join(f"{name} ({n} files)" for name, n in imports.items()))
    lines.append("")
    lines.append("Stock devices in this Live:")
    for category, names in devices.items():
        lines.append(f"  {category.replace('_', ' ')}: {', '.join(names)}")
    if room is not None:
        lines.append("")
        lines.append(room.notes())
    return "\n".join(lines)


def _routing_text(side):
    if not side:
        return "?"
    return f"{side['type']} {side['channel']}".strip()


def color_name(rgb):
    """The nearest named colour to one of Live's track colours."""
    if rgb is None:
        return None

    def distance(other):
        return sum((((rgb >> shift) & 255) - ((other >> shift) & 255)) ** 2 for shift in (16, 8, 0))

    return min(TRACK_COLORS, key=lambda name: distance(TRACK_COLORS[name]))


def _strip_line(t, scene_names=None):
    parts = [t["name"]]
    if t.get("color") is not None:
        parts.append(f"colour {color_name(t['color'])}")
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
    clips = []
    for c in t.get("clips", []):
        where = (scene_names or {}).get(c["scene_index"], f"song {c['scene_index'] + 1}")
        clips.append(f"{where}: {c['name']}" + (f" ({c['gain']})" if c.get("gain") else ""))
    if clips:
        parts.append("clips: " + ", ".join(clips))
    return " | ".join(parts)


def describe_proposal(p):
    """A proposal as the UI shows it."""
    return {
        "id": p["id"],
        "status": p["status"],
        "results": p["results"],
        "steps": [
            {"text": a.describe(), "destructive": is_destructive(a), "audible": is_audible(a)}
            for a in p["actions"]
        ],
        "exportable": any(a.action == "add_track" for a in p["actions"]),
    }
