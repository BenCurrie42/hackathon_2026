# Holy Sound for Agents of Flourishing

What to add so Holy Sound reads as an agent, not a chatbot, against the Gloo
**Challenge 1: Agents of Flourishing** brief. Ranked by how much each moves the
judges. Written 2026-10-07, at the start of the event.

## The risk first

Today the model proposes a batch of changes, a person presses Apply on each
batch, and nothing more happens until the next message. The brief rules that
out: "if a human still performs every step and the AI only drafts text, this is
the wrong track." The engine is strong (typed intent-only actions, RigLink,
listening to stems, per-song mixes). What's missing is a goal-driven run on top
that plans, acts, checks its own result and fixes it.

Check the track: `CLAUDE.md` says Holy Sound entered **Ministry Resourcing**.
If it is judged on Challenge 1, the story has to meet the brief's framing of
burden: Saturday-night set building is the hours that crowd out the worship
leader's time with the band and the people.

## What to add, ranked

### 1. A "Get Sunday ready" run

**Status (2026-10-08):** partly done. Expert mode (`app/expert.py`) is the
plan-act-check-fix loop for mixing: one button, a lead and four specialists,
up to three rounds, no Apply. Import and routing still go through the chat's
Apply.

One goal from the volunteer: the setlist, a folder of song files, and "click
and guide to the in-ears". The agent then works without further prompting:

1. Plans the steps and shows the plan once for approval, not once per step.
2. Imports the songs onto part tracks, sets tempo and key, listens to the stems.
3. Levels the parts.
4. Checks its own result against the goal: meters, routing, the snapshot.
5. Fixes what's wrong and checks again, until the set matches the goal or it
   needs a person.

Every edit is undoable in Live with Cmd+Z, so the brief's confirmation rule
(irreversible actions only) does not require a stop at every step.

Already built: the proposable actions (`app/actions.py`), `listen` and
`listen_to_stems`, the follow-up turn after a listen, per-song mixes. Missing:
the loop that verifies against the goal and corrects.

### 2. Edge cases to show live

The brief wants at least one live case where the agent hits an edge and handles
it. A real one already exists: in the Washed session, **Click and Guide were
routed to Main**, so the room hears the click. The agent catching and fixing (or
flagging) that is the moment the judges are looking for. Others:

- A song with no lead vocal stem.
- A stem whose name doesn't match its part.
- A key the band can't reach.
- Ableton closed: it falls back to a downloadable `.als`.

### 3. A finished artifact beyond the set

A run sheet for the band: songs, keys, tempos, who is on which input, what
changed this week. Then a drafted message to the band that **waits for a
person to confirm** before it is sent, which shows the brief's "no
communications without confirmation" guardrail working.

### 4. Clear handoff points

It stops and asks when:

- its confidence is low (it can't tell which vocal is the lead);
- it needs a hardware output it can't verify (output routing is still an open
  question in `CLAUDE.md`);
- the change would make sound in the room during a service.

Write each one down as a rule. The judges score this directly.

### 5. An auditable session log

Every model call, tool call, result and approval for each run, in a file a
judge can read through. The Evaluation section requires it; there isn't one
today.

### 6. An eval set on the fake Ableton set

About 20 hand-built cases with clear pass rules ("click ends up off Main", "no
more tracks than parts"), runnable without Live on `app/fake_live.py`. Also
earns the "published eval set" bonus.

### 7. Gloo models

**Status:** done in v1.2.0. `HOLYSOUND_PROVIDER=gloo` (Gloo AI Studio,
guarded completions), named in the build doc.

Gloo runs this challenge. `app/providers.py` already takes any
OpenAI-compatible endpoint through `HOLYSOUND_BASE_URL`, so a Gloo AI Studio
model may plug in with little work. Name it in the build doc and say why.

### 8. Install with no developer

**Status:** mostly done. `scripts/install_riglink.py` links RigLink and picks it
as the Control Surface with no Ableton settings to click. It's still one
terminal command, not a first-run screen.

Installing RigLink today means symlinking a folder and picking it in Ableton's
settings. A first-run screen that copies the script and walks the volunteer
through selecting it answers "runnable without a developer on retainer".

## Build doc material that already exists

- **An earlier prompt and what was wrong with it:** commit `50548b6`, "Make the
  assistant reliable on smaller models".
- **Tried and abandoned:**
  - One track per vendor stem: a set grew to 93 tracks for three songs. Led to
    the fixed part tracks (`app/parts.py`).
  - Live mode (2026-10-07): a loop that turned down any track louder than the
    rest. Comparing each track with Master pulled every other track down in a
    cascade, because cutting one track lowers Master. Fixed by comparing with
    the median of the other tracks, then scrapped after a first run on a real
    set. (Add the team's reason here.) The code is kept in a local `git stash`,
    "scrapped: live mode".
- **Reusable pattern:** the model emits intent, never parameters; levels are set
  by searching Live's own display text, so Live stays the source of truth for
  units.
- **Shared schema:** `RigSpec` could be published, or exposed as an MCP server,
  for the open-endpoint bonus.

## Only the team can supply

- How many hours a volunteer really spends building the set each week. Ask the
  event mentors; don't guess.
- A real worship tech using it during the event and saying something specific
  about it.
