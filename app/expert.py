"""Expert mode: a lead engineer and four specialists fix the mix on their own.

    uv run python -m app.expert                     # the running app fixes the mix on the mixer
    uv run python -m app.expert "vocals on top"     # with a goal

The lead reads the set and briefs whichever specialists it needs. They work in
parallel, each on its own tracks only (the vocals tech can't move a drum fader),
and only with mixer moves: fader, pan, mute, sends and EQ. The lead merges what
they hand back into one proposal, applies it without waiting for Apply, reads
the set again and checks the result, for at most MAX_ROUNDS rounds.

Every move is undoable (Cmd+Z in Live), and the song's mix is checkpointed
before and after, so the volunteer can flip between them.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, ValidationError

from app import parts
from app.actions import SetEq, SetMute, SetPan, SetSend, SetVolume
from app.assistant import _errors, _inline_refs, _replayable
from app.providers import AssistantUnavailable, token_usage

MAX_ROUNDS = 3
FIX_ATTEMPTS = 2  # a model call whose tool input doesn't validate gets this many retries

BEFORE = "Before expert mode"
AFTER = "After expert mode"

# key, title shown in the chat, what it looks after
CREW = [
    ("vocals", "Vocals tech", "the lead vocal, backing vocals and choir"),
    ("rhythm", "Rhythm tech", "drums, percussion and bass: the groove and the low end"),
    ("band", "Band tech", "guitars, keys, piano, organ, synths, strings and horns"),
    ("playback", "Playback tech", "click, guide, loops, FX, crowd, SMPTE and the shared reverb and delay"),
]
TITLES = {key: title for key, title, _ in CREW}
LEAD = "Lead engineer"

VOCAL_PARTS = {"Lead Vocal", "Choir", "BGVs"}
RHYTHM_PARTS = {"Drums", "Perc", "Synth Bass", "Bass"}
PLAYBACK_PARTS = {"Click", "Guide", "SMPTE", "Loops", "FX", "Crowd"}

MIX_RULES = """\
How a worship mix should sit (guides what you do; don't recite it):
- Vocals on top: lead vocal, then BGVs and choir, then keys and pads. Words must be heard.
- One source owns the low end. Drums and bass carry the groove; neither buried nor booming.
- Click, guide, count and SMPTE are for the band, not the room: low or muted in this mix, \
centre panned. SMPTE stays muted.
- Part tracks stay panned centre (their stereo is inside the clip). Hard pans are a mistake.
- Faders from -70 (off) to +6 dB; most parts sit between -18 and -3 dB. Nothing parked at +6.
- Reverb and delay sends are seasoning: vocals around -18 to -10 dB, never a wash. The \
shared reverb and delay returns sit around -6 dB.
- EQ: when a track's EQ has problems, start it over (flat_first true) with a gentle curve of two \
to four bands. Boosts +4 dB at most and broad (Q 0.7-1.5); cuts down to -6 dB; shelves under \
±3 dB. Low cut: vocals 80-120 Hz, guitars 80-120 Hz, keys and pads 40-80 Hz, bass and kick \
30-40 Hz or none. No high cut below 12 kHz except on bass. Muddy = cut 200-400 Hz; boxy or \
honky = 500 Hz-1 kHz; harsh = 2.5-5 kHz.
- A part the band plays live, or one the song leaves out on purpose, may stay muted; a \
muted bass with no live bassist is a mistake.
- Trust what the notes remember about this church over these defaults.
"""

LEAD_SYSTEM = f"""\
You are the lead engineer of Holy Sound's expert mode, mixing a church worship team's Ableton \
Live set on your own. Nobody presses Apply: what your crew hands back is applied straight away, \
then you see the set again.

Your crew, each working only on their own tracks:
""" + "\n".join(f"- {key}: {title}, {what}" for key, title, what in CREW) + f"""

Each round, read the <session> notes (every track, its level, pan, sends and EQ, and EQ \
PROBLEMS found by rule) and call brief_crew once:
- briefs: one short, concrete brief for each specialist whose tracks need work ("Choir and \
BGVs are buried at -30 dB and drowning in reverb; bring them up under the lead and pull the \
sends back"). Say how their tracks should sit against the others: balance between groups is \
your job. Leave out specialists whose tracks are fine.
- done: true when the mix is ready for Sunday and nothing important is left to change.
- summary: one or two plain sentences for the volunteer, no jargon: what you found or what \
changed, and what's left if anything.
Work on the song the mixer is on. Don't chase perfection: fix what's clearly wrong.

{MIX_RULES}"""

SPECIALIST_SYSTEM = """\
You are the {title} in Holy Sound's expert mode, mixing a church worship team's Ableton Live \
set. You look after {what}. The lead engineer briefs you; your changes are applied straight away.

Call hand_back once with every change for your tracks, in plain intent: set_volume, set_pan, \
set_mute, set_send and set_eq only, and only on these tracks: {tracks}. Leave song null (the \
song on the mixer). If your tracks are already fine, hand back no actions and say so.
summary is one plain sentence for the volunteer: what was wrong and what you did.

{rules}"""


CrewAction = Union[SetVolume, SetPan, SetMute, SetSend, SetEq]


class HandBack(BaseModel):
    """What a specialist's hand_back tool takes."""

    summary: str = Field(description="One plain sentence: what was wrong and what you changed.")
    actions: list[Annotated[CrewAction, Field(discriminator="action")]] = Field(
        default_factory=list, max_length=40, description="Your changes, in order. Empty if your tracks are fine.")


class Brief(BaseModel):
    crew: Literal["vocals", "rhythm", "band", "playback"]
    brief: str = Field(description="What's wrong with their tracks and how they should sit in the mix.")


class BriefCrew(BaseModel):
    """What the lead's brief_crew tool takes."""

    done: bool = Field(description="True when the mix is ready and nothing important is left to change.")
    summary: str = Field(description="One or two plain sentences for the volunteer.")
    briefs: list[Brief] = Field(default_factory=list, max_length=4,
                                description="Work for this round, one per specialist that needs it.")


def crew_for(name, folder=None):
    """Which specialist owns a track: by the mixer folder it's in, then by its part name."""
    part = name if name in parts.PART_NAMES else parts.part_for(name)
    if folder == "vocals":
        return "vocals"
    if folder == "playback":
        return "playback"
    if folder == "instruments":
        return "rhythm" if part in RHYTHM_PARTS else "band"
    if part in VOCAL_PARTS:
        return "vocals"
    if part in RHYTHM_PARTS:
        return "rhythm"
    if part in PLAYBACK_PARTS or part is None:
        return "playback"
    return "band"


def roster(snapshot):
    """{crew key: [track names]} for a snapshot with folders (App.with_folders). Returns go to playback."""
    owned = {key: [] for key, _, _ in CREW}
    for t in snapshot["tracks"]:
        owned[crew_for(t["name"], t.get("folder"))].append(t["name"])
    owned["playback"].extend(r["name"] for r in snapshot["returns"])
    return owned


class Run:
    """One expert-mode run against the app (app/server.py App)."""

    def __init__(self, app, goal=""):
        self.app = app
        self.chat = app.chat
        self.goal = goal.strip()
        self._usage_lock = threading.Lock()

    def __call__(self):
        chat = self.chat
        chat.busy = True
        chat.feed.publish("crew", "The crew is reading the set…")
        worked, applied, summary = 0, 0, ""
        try:
            song = self.app.mixes.current
            if song:
                self.app.checkpoint_song_mix(BEFORE)
            say = f"Expert mode on {song}." if song else "Expert mode on the shared mix."
            chat.post_expert(LEAD, say + (f" Goal: {self.goal}" if self.goal else ""))
            # One more look than there are rounds of work: the last only checks.
            for rounds in range(1, MAX_ROUNDS + 2):
                chat.feed.publish("crew", f"{LEAD}: checking the mix…")
                lead = self._lead(rounds)
                summary = lead.summary
                if lead.done or not lead.briefs or rounds > MAX_ROUNDS:
                    chat.post_expert(LEAD, lead.summary)
                    break
                chat.post_expert(LEAD, lead.summary + "\n\n" + "\n".join(
                    f"- **{TITLES[b.crew]}**: {b.brief}" for b in lead.briefs))
                actions = self._crew(lead.briefs)
                if not actions:
                    chat.post_expert(LEAD, "The crew had nothing to change.")
                    break
                chat.feed.publish("crew", f"Applying {len(actions)} changes…")
                pid = chat.add_proposal(actions, f"Round {rounds}: {len(actions)} changes from the crew.",
                                        agent=LEAD)
                results = self.app.apply(pid)
                worked += 1
                applied += sum(1 for r in results if r["ok"])
            if song:
                self.app.checkpoint_song_mix(AFTER)
                chat.post_expert(LEAD, f"Saved “{BEFORE}” and “{AFTER}” in Checkpoints, to compare.")
            return {"ok": True, "rounds": worked, "applied": applied, "summary": summary}
        except AssistantUnavailable as e:
            chat.post_expert(LEAD, f"The crew stopped: {e}")
            return {"ok": False, "rounds": worked, "applied": applied, "summary": str(e)}
        finally:
            chat.busy = False
            chat.feed.publish("end")

    # -- the lead ---------------------------------------------------------------

    def _lead(self, round_no):
        notes = self.app.notes()
        if round_no > MAX_ROUNDS:
            ask = ("Final check: this is the set after your crew's last changes, and there's no time for "
                   "another round. Set done true and sum up what changed and anything left for the volunteer.")
        elif round_no > 1:
            ask = (f"Round {round_no} of at most {MAX_ROUNDS}. This is the set after your crew's last "
                   "changes. Check them: done, or brief again for what's still wrong.")
        else:
            ask = f"Round 1 of at most {MAX_ROUNDS}. Find what's wrong and brief your crew."
        if self.goal:
            ask += f"\nThe volunteer's goal: {self.goal}"
        prompt = f"<session>\n{notes}\n</session>\n\n{ask}"
        tool = {"name": "brief_crew", "description": "Brief the crew for this round, or say the mix is done.",
                "input_schema": _inline_refs(BriefCrew.model_json_schema())}
        lead, text = self._ask(LEAD_SYSTEM, tool, BriefCrew, prompt)
        if lead is None:
            return BriefCrew(done=True, summary=text or "The lead engineer couldn't decide what to do next.")
        return lead

    # -- the specialists --------------------------------------------------------

    def _crew(self, briefs):
        """Run the briefed specialists in parallel. Returns their actions merged, in crew order."""
        snapshot = self.app.live_state()["snapshot"]
        if snapshot is None:
            return []
        owned = roster(snapshot)
        notes = self.app.notes()
        order = [key for key, _, _ in CREW]
        briefs = sorted(briefs, key=lambda b: order.index(b.crew))
        for b in briefs:
            self.chat.feed.publish("crew", f"{TITLES[b.crew]} is working…")
        with ThreadPoolExecutor(max_workers=len(briefs) or 1) as pool:
            done = list(pool.map(lambda b: self._specialist(b, owned[b.crew], notes), briefs))
        merged = []
        for b, (hand_back, text) in zip(briefs, done):
            if hand_back is None:
                self.chat.post_expert(TITLES[b.crew], text or "Couldn't work out the right changes.")
                continue
            self.chat.post_expert(TITLES[b.crew], hand_back.summary)
            merged.extend(hand_back.actions)
        return merged

    def _specialist(self, brief, tracks, notes):
        key, title, what = next(c for c in CREW if c[0] == brief.crew)
        if not tracks:
            return HandBack(summary="No tracks of mine in this set.", actions=[]), ""
        system = SPECIALIST_SYSTEM.format(title=title, what=what, tracks=", ".join(tracks), rules=MIX_RULES)
        prompt = f"<session>\n{notes}\n</session>\n\nThe lead engineer's brief: {brief.brief}"
        tool = {"name": "hand_back", "description": "Hand your changes back to the lead engineer.",
                "input_schema": _inline_refs(HandBack.model_json_schema())}
        mine = {t.casefold() for t in tracks}

        def check(hand_back):
            others = sorted({a.track for a in hand_back.actions if a.track.casefold() not in mine})
            if others:
                return (f"Not your tracks: {', '.join(others)}. Use exact names from yours only: "
                        f"{', '.join(tracks)}.")
            return None

        return self._ask(system, tool, HandBack, prompt, check)

    # -- one model call, retried until its tool input validates ---------------------

    def _ask(self, system, tool, model, prompt, check=None):
        """(validated tool input, reply text), or (None, reply text) after FIX_ATTEMPTS retries."""
        provider = self.chat.client()
        provider.session_id = self.chat.session_id
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        text = ""
        for attempt in range(FIX_ATTEMPTS + 1):
            response = provider.create(system, [tool], messages)
            self._count(response)
            blocks = _replayable(response.content)
            text = "\n\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
            calls = [b for b in blocks if b["type"] == "tool_use"]
            messages.append({"role": "assistant", "content": blocks})
            if not calls:
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": f"(Automatic: call {tool['name']} now.)"}]})
                continue
            problem = None
            try:
                result = model.model_validate(calls[0]["input"])
                problem = check(result) if check else None
            except (ValidationError, TypeError) as e:
                problem = _errors(e) if isinstance(e, ValidationError) else "That input wasn't one JSON object."
            if problem is None:
                return result, text
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                 "content": (f"Fix this and call {tool['name']} again:\n{problem}" if c is calls[0]
                             else "Only one call per reply.")}
                for c in calls]})
        return None, text

    def _count(self, response):
        spent_in, spent_out = token_usage(response)
        with self._usage_lock:
            self.chat.usage["input"] += spent_in
            self.chat.usage["output"] += spent_out
            usage = dict(self.chat.usage)
        self.chat.feed.publish("usage", usage)


def run(app, goal=""):
    """Run expert mode on the app's set. Returns {"ok", "rounds", "applied", "summary"}."""
    return Run(app, goal)()


def main(argv=None):
    """The CLI: run expert mode in the running app and print what the crew said."""
    from app.mcp import HolySound, HolySoundError
    from app.server import load_dotenv

    load_dotenv()
    argv = sys.argv[1:] if argv is None else argv
    hs = HolySound()
    try:
        before = len(hs.get("/api/state")["chat"])
        state = hs.post("/api/expert", {"goal": " ".join(argv)})
    except HolySoundError as e:
        print(e, file=sys.stderr)
        return 1
    print(transcript_text(state["chat"][before:]))
    return 0 if state["expert"]["ok"] else 1


def transcript_text(entries):
    """What the crew said and did, as plain text (the CLI and the MCP tool)."""
    out = []
    for entry in entries:
        if entry.get("text"):
            out.append(f"{entry.get('agent') or 'Holy Sound'}: {entry['text']}")
        p = entry.get("proposal")
        if p and p.get("results"):
            ok = sum(1 for r in p["results"] if r["ok"])
            out.append(f"  applied {ok} of {len(p['results'])} changes")
            out += [f"  FAILED: {r['text']}" for r in p["results"] if not r["ok"]]
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
