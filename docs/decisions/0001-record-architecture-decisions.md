# 0001. Record architecture decisions

Date: {{FILL IN — YYYY-MM-DD}}

## Status

Accepted

## Context

Every project accumulates decisions that are expensive to revisit and hard to
reconstruct later: the tech stack, the data model, which third-party services to
depend on, where a boundary sits. The reasoning behind them lives in someone's
head, or in a chat log, and then it doesn't.

This is worse on a project driven by an agent loop. Agents have no memory across
sessions. Without a written record they will either re-litigate settled questions
or quietly make a choice that contradicts one you already made.

## Decision

We record significant decisions as ADRs in `docs/decisions/`, numbered
`NNNN-short-title.md`.

An ADR is short: context, the decision, the consequences. It captures *why*, not
*how* — the how belongs in the code and in PRDs.

Every ADR must be **approved by a human**. An agent may draft one — when a call
meets the test in `AGENTS.md` §8, a worker surfaces it in its summary and may write
the draft with status `Proposed`. Only the operator moves an ADR to `Accepted`. No
ADR counts as a decision until a human has signed off on it.

Which decisions warrant an ADR is a fixed test rather than a judgment call, and it
lives in `AGENTS.md` so each project can tune it. This ADR deliberately does not
restate it — this file is immutable, that test is not.

Once accepted, an ADR is immutable. A decision that changes gets a new ADR that
supersedes the old one, with a note added to both.

## Consequences

- Anyone joining the project, human or agent, can read the decision history in
  order and understand how the project got its shape.
- There is a small overhead per decision. That is the point — it filters out
  decisions not worth recording.
- The record is only as good as the discipline. An undocumented decision is
  indistinguishable from an accident six months later.

---

*This ADR is seeded by `ralph init`. It is a real decision, not a placeholder —
keep it, and fill in the date above. Delete it only if you genuinely don't want
ADRs on this project.*
