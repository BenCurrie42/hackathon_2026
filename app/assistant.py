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
import re
import threading
import uuid

from pydantic import BaseModel, Field, ValidationError

from app.actions import Proposal, is_audible, is_destructive
from app.providers import (
    AnthropicProvider,
    AssistantSetupError,
    AssistantUnavailable,  # re-exported: app/server.py and tests import it from here
    provider_from_env,
    token_usage,
)
from rig import TRACK_COLORS

# The model, effort and provider come from .env, read when the AI is first used
# (app/providers.py): HOLYSOUND_PROVIDER, HOLYSOUND_MODEL, HOLYSOUND_EFFORT.
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
You are Holy Sound. You help a church volunteer run Ableton Live for their worship team. \
They are not an audio engineer. You work alongside them like a calm sound tech friend: \
you help with what they ask, you don't run the show.

## How you talk
1. Answer what was asked, nothing more. Default to 1-3 short sentences (under 60 words). \
Go longer only when they ask for detail.
2. No headings, no bullet lists, no reports. Plain sentences.
3. Don't hand out to-do lists or audit the set unless asked. If you spot something that \
would go wrong in the room (click or SMPTE in the main speakers, clipping), mention the \
single most important one in one sentence and offer to fix it.
4. Offer, don't instruct: "Want me to...?" rather than "You should...".
5. Ask at most one question per message, and only when you can't go on without the answer.
6. Never show JSON, file paths or technical IDs.

## How changes happen
7. You can't change Live yourself. propose_changes shows the volunteer a step list with an \
Apply button; the next message tells you what was applied. Say in one sentence what the \
steps do and why; don't repeat the list.
8. Never say something is done until a result marked ✓ says so. A ✓ with "But" only partly \
worked. If a step shows ✗, explain it in one sentence and offer a fix.
9. The <session> notes at the top of each message are the set right now. Trust them over \
anything said earlier; the volunteer may have changed things by hand.
10. One request is one propose_changes call, steps in order (a later step can use a track \
an earlier one creates). For big imports, do one song per proposal.
11. Never invent an input or output number. Use outputs listed in the notes, or ask.
12. If Live isn't connected you can still propose new tracks; the volunteer can download \
them as a session file.

## Tools: values and limits
13. Fader and send levels are dB: 0 is unity, -70 is off, max +6. Pan is -1 (left) to 1 \
(right). Change levels in 1-3 dB steps.
14. The fader (set_volume) is the live mix. Clip gain (gain_db, set_clip_gain) evens out \
stems inside a song.
15. Inputs are written as printed on the interface: "1" for a mic or DI, "3/4" for a stereo \
pair. Playback tracks have no input.
16. Songs are Live scenes, one per song, with that song's tempo. Shared reverbs and delays \
are return tracks fed with set_send.
17. Refer to tracks by exact name; names must be unique.
18. You can't change effect settings, group or reorder tracks, delete clips, or move the \
Master fader. Say so plainly if asked.
19. Use only effect names from the stock device list. Leave presets out unless named.
20. Meter readings are 0-1 after the fader. Compare tracks with each other; never call a \
reading dB.

## Importing audio
When the volunteer imports a folder, their message lists each file with its peak, its \
"loud parts" level (dBFS while sounding) and how much of the time it sounds.
21. One track per part (Click, Guide, Bass, Keys...), shared by every song. One song per \
song; use a tempo only if a name states it.
22. Create the new tracks in the same proposal as their import_audio steps and leave \
volume_db unset. Holy Sound starts those faders low (about -10 to -20 dB) so the whole song \
doesn't clip. Don't raise them to 0. It also mutes a new SMPTE/timecode track; say so.
23. Never let a clip's peak plus gain_db go above -1 dBFS. Leave out SILENT files and name \
them; name any CLIPS files.
24. import_audio needs an empty slot; you can't delete or replace clips.
25. Clips play once from the start at their own speed; tempo only sets the click and grid.

## When you set up or mix (guides what you propose; don't recite it)
26. Click, Guide and Count go to the in-ear output, never Master, and stay unmuted for a \
service. SMPTE stays muted or goes to its own output.
27. On a tracks rig, mute stems for parts the live band plays; keep only what nobody plays. \
Mute crowd and room stems for live use.
28. Stereo pairs (L/R): pan -1 and 1, same clip gain and fader on both.
29. Vocals on top: lead, then BGVs, then pads and keys. One source owns the low end: with a \
live bassist, lower or mute Bass and Sub stems.
30. Colour related tracks alike: drums and bass red or orange, keys and pads blue or teal, \
guitars green, vocals purple or pink, click and guide grey. Colours: \
""" + ", ".join(TRACK_COLORS) + """.

## Remembering their church
31. Save lasting facts with remember (their interface, who's on which input, which outputs \
feed the in-ears, how they like things), one short sentence each. Not this week's songs.
32. When a fact changes, forget the old one and save the new one. Use what you remember \
instead of asking again.

## Safety
33. Delete only when clearly asked.
34. listen, start_song and transport make sound in the room. Say so, and only during setup, \
never during a service.
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
    """Replace $ref pointers with the definitions they name.

    Also keeps to JSON Schema that open models (via OpenCode Go) follow:
    oneOf becomes anyOf (the action variants never overlap, so it means the
    same), and a const also gets a one-value enum.
    """
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            out = {}
            for k, v in node.items():
                if k == "discriminator":
                    continue
                out["anyOf" if k == "oneOf" else k] = walk(v)
            if "const" in out and "enum" not in out:
                out["enum"] = [out["const"]]
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class ReplyFeed:
    """What the assistant is writing right now, for every page that's watching.

    Events are (seq, kind, data): "start" and "end" bracket a turn, "step" marks
    each model call in it, and "thinking" / "text" / "tool" carry the reply as
    it streams (app/providers.py). Only the current turn is kept, so a page that
    connects part-way through can catch up.
    """

    def __init__(self):
        self._changed = threading.Condition()
        self._events = []
        self._seq = 0

    def publish(self, kind, data=None):
        with self._changed:
            if kind == "start":
                self._events = []
            self._seq += 1
            self._events.append((self._seq, kind, data))
            self._changed.notify_all()

    def cursor(self):
        """Where a newly connected page starts: the beginning of a turn in progress, else now."""
        with self._changed:
            if self._events and self._events[-1][1] != "end":
                return self._events[0][0] - 1
            return self._seq

    def since(self, seq, timeout):
        """Events after seq, waiting up to timeout seconds for the first one."""
        with self._changed:
            self._changed.wait_for(lambda: self._seq > seq, timeout)
            return [e for e in self._events if e[0] > seq]


ONE_PROPOSAL = ("Ignored: only one propose_changes call per reply. Put every change for this "
                "request in a single call, in order.")


class Conversation:
    """One shared conversation. The laptop and the phone see the same one."""

    def __init__(self, client_factory=None, room=None):
        self._client_factory = client_factory or _default_client
        self.room = room
        self._client = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.busy = False
        self.feed = ReplyFeed()
        self.setup_error = None  # set when a request shows the key is missing or wrong
        self.reset()

    def reset(self):
        self.session_id = uuid.uuid4().hex  # one per conversation, for the provider
        self.messages = []
        self.transcript = []
        self.proposals = {}
        self.usage = {"input": 0, "output": 0}  # tokens this conversation has used
        # (tool_use_id, proposal_id, other results) awaiting a tool_result. Other
        # results answer the reply's other tool calls; they go in the same message.
        self._open_tool_use = None

    # -- public -----------------------------------------------------------

    def client(self):
        """The provider that answers (app/providers.py)."""
        if self._client is None:
            made = self._client_factory()
            # A bare Anthropic SDK client (or a test stand-in for one) gets wrapped.
            self._client = made if hasattr(made, "create") else AnthropicProvider(made)
        return self._client

    def send(self, text, session_notes, attachment=None):
        """Handle one message from the volunteer. Returns the new transcript entries.

        attachment goes to Claude with the message but isn't shown in the chat
        (the measurements of an imported folder, say).
        """
        with self._lock:
            self._begin()
            try:
                return self._send(text, session_notes, attachment)
            finally:
                self._finish()

    def follow_up(self, session_notes):
        """Let Claude react to applied results without a new message (after listening)."""
        with self._lock:
            if self._open_tool_use is None:
                return []
            self._begin()
            try:
                return self._send(None, session_notes)
            finally:
                self._finish()

    def proposal(self, proposal_id):
        return self.proposals.get(proposal_id)

    def record_outcome(self, proposal_id, status, results=None):
        """The volunteer applied or dismissed a proposal."""
        with self._lock:
            p = self.proposals[proposal_id]
            p["status"] = status
            p["results"] = results

    # -- internals ----------------------------------------------------------

    def _begin(self):
        self.busy = True
        self.feed.publish("start")

    def _finish(self):
        # Not busy before "end": a page refreshing on "end" must see the finished turn.
        self.busy = False
        self.feed.publish("end")

    def _entry(self, **fields):
        entry = {"id": next(self._ids), **fields}
        self.transcript.append(entry)
        return entry

    def _send(self, text, session_notes, attachment=None):
        client = self.client()
        content = []
        reopen = self._open_tool_use
        if reopen:
            tool_use_id, proposal_id, others = reopen
            content.extend(others)
            content.append(self._tool_result(tool_use_id, proposal_id))
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
        thoughts = []  # thinking not yet attached to a reply in the chat

        def said(text, **fields):
            if thoughts:
                fields["thinking"] = "\n\n".join(thoughts)
                thoughts.clear()
            return self._entry(role="assistant", text=text, **fields)

        try:
            fixes = 0
            unanswered = None  # tool results we gave up on, still to send
            for _step in range(MAX_STEPS):
                response = self._create(client)
                if response.stop_reason == "refusal":
                    raise _Refused()
                blocks = _replayable(response.content)
                self.messages.append({"role": "assistant", "content": blocks})
                thoughts.extend(b["thinking"].strip() for b in blocks
                                if b["type"] == "thinking" and (b.get("thinking") or "").strip())

                reply = "\n\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
                calls = [b for b in blocks if b["type"] == "tool_use"]
                if not calls:
                    return shown + [said(reply or "…")]

                # Every tool call gets a result, in order. One proposal per reply:
                # the first propose_changes counts, any others are sent back.
                proposing = any(c["name"] == "propose_changes" for c in calls)
                if reply and not proposing:
                    shown.append(said(reply))
                results, proposal, proposal_call, invalid = [], None, None, None
                for c in calls:
                    if not isinstance(c["input"], dict):
                        result = _error_result(c, "That input wasn't valid JSON. Send it again as "
                                                  "one JSON object.")
                        if c["name"] == "propose_changes" and proposal_call is None and invalid is None:
                            invalid = result
                    elif c["name"] == "remember":
                        outcome, note = self._remember(c["input"])
                        if note:
                            shown.append(self._entry(role="note", text=note))
                        result = {"type": "tool_result", "tool_use_id": c["id"], "content": outcome}
                    elif c["name"] != "propose_changes":
                        result = _error_result(c, f"There's no tool called {c['name']}.")
                    elif proposal_call is not None or invalid is not None:
                        result = _error_result(c, ONE_PROPOSAL)
                    else:
                        try:
                            proposal = Proposal.model_validate(c["input"])
                            proposal_call = c
                            continue  # answered once the volunteer decides
                        except ValidationError as e:
                            result = invalid = _error_result(
                                c, "The changes didn't validate, so the volunteer hasn't seen them. "
                                   f"Fix these and call propose_changes again:\n{_errors(e)}")
                    results.append(result)

                if proposal_call is not None:
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
                    self._open_tool_use = (proposal_call["id"], pid, results)
                    return shown + [said(reply, proposal_id=pid)]

                if invalid is not None:
                    fixes += 1
                    if fixes > FIX_ATTEMPTS:
                        invalid["content"] = "Stopped here; the volunteer saw an apology instead."
                        unanswered = results
                        break
                self.messages.append({"role": "user", "content": results})

            # Gave up. Answer the dangling tool call, or the history stops being valid.
            if unanswered is not None:
                self.messages.append({"role": "user", "content": unanswered})
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

    def _create(self, provider):
        try:
            provider.session_id = self.session_id
            self.feed.publish("step")
            response = provider.create(SYSTEM, _tools(), self.messages, on_event=self.feed.publish)
        except AssistantSetupError as e:
            self.setup_error = str(e)
            raise
        # Counted even if the turn is later abandoned: the tokens were spent.
        spent_in, spent_out = token_usage(response)
        self.usage["input"] += spent_in
        self.usage["output"] += spent_out
        self.feed.publish("usage", dict(self.usage))
        return response


class _Refused(Exception):
    pass


def _default_client():
    """The provider .env asks for (Anthropic unless told otherwise)."""
    return provider_from_env()


def _replayable(blocks):
    """Response blocks as request blocks, unchanged, so history stays append-only.

    Blocks are SDK objects (Anthropic) or plain dicts (other providers).
    """
    out = []
    for block in blocks:
        if isinstance(block, dict):
            data = dict(block)
        else:
            data = block.model_dump(mode="json", exclude_none=True, by_alias=True)
        data.pop("parsed_output", None)
        out.append(data)
    return out


def _error_result(call, text):
    return {"type": "tool_result", "tool_use_id": call["id"], "is_error": True, "content": text}


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
        lines.append(_outputs_line(snapshot))
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


def _outputs_line(snapshot):
    """Where tracks can go, so in-ear routing uses outputs that exist."""
    channels = snapshot.get("ext_outputs")
    if channels:
        return "Outputs: Master; Ext. Out " + ", ".join(channels)
    # An older RigLink doesn't list them; outputs already in use are still real.
    seen = []
    for t in snapshot["tracks"] + snapshot["returns"]:
        out = t.get("output") or {}
        if out.get("type") == "Ext. Out" and out.get("channel") and out["channel"] not in seen:
            seen.append(out["channel"])
    known = f" (in use: {', '.join(seen)})" if seen else ""
    return f"Outputs: Master; Ext. Out{known}. Live didn't list every output; ask which ones feed in-ears."


def _pan_value(t):
    """Pan as -1..1. Older RigLinks only send Live's label ("50L", "C", "25R")."""
    if t.get("pan_value") is not None:
        return t["pan_value"]
    match = re.fullmatch(r"(\d+)\s*([LR])", t["pan"].strip())
    if not match:
        return 0.0
    amount = int(match.group(1)) / 50
    return -amount if match.group(2) == "L" else amount


def _pan_text(t):
    value = round(_pan_value(t), 2) + 0.0  # + 0.0 turns -0.0 into 0
    return f"pan {value:g} ({t['pan']})"


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
    parts.append(_pan_text(t))
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
