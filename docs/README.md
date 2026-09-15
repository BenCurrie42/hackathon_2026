# Docs

Project documentation, split four ways so it doesn't turn into one
undifferentiated pile.

| Directory | Contents |
|---|---|
| `prds/` | Product requirement docs — one per unit of work, numbered `NN_short_name.md`. The PRD loop reads these. |
| `prds/completed/` | PRDs whose work is finished and verified. Moved here at the end of the lifecycle. |
| `decisions/` | Architecture decision records. Numbered, short, focused on *why*. Human-approved. |
| `research/` | External inputs you're studying: API documentation, briefs, prior art, vendor comparisons. Durable — worth keeping and re-reading. |
| `notes/` | Your own working scratch: meeting notes, half-formed ideas, debugging trails. Disposable by design. |

The line between `research/` and `notes/`: research is material that came from
**outside** and stays useful; notes are what **you** thought while working and
usually stop mattering once the work lands. When in doubt it's a note — promoting
a note to research later is easy, and nobody minds a thin `notes/`.

## Conventions

- **PRDs:** `NN_short_name.md`, zero-padded, sequential. Never renumber an
  existing PRD — the number appears in commit messages and branch names.
- **ADRs:** `NNNN-short-title.md`, zero-padded to four digits, next unused
  number. An ADR reaches `Accepted` only when a human approves it, and is
  immutable from then on; to change a decision, write a new ADR that supersedes it
  and note the supersession in both. What does and doesn't warrant an ADR is a
  fixed test — see [`../AGENTS.md`](../AGENTS.md) §8.
- **Research and notes:** no naming rules. Date-prefix them if it helps.
- Write in plain prose. These are read by both people and agents.

## Process

The PRD lifecycle, quality gates, and agent constraints live in
[`../AGENTS.md`](../AGENTS.md). That file is the source of truth; this one is
just a map.
