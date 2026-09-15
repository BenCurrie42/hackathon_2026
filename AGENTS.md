# hackathon2026 — Project & Agent Ruleset

This document is binding for all AI agents working in this repo. The PRD loop and
its workers read it on every task. Edit the `{{...}}` placeholders and the
command blocks to match this project, then delete this line.

## 1. Project Overview

{{ONE_PARAGRAPH_DESCRIPTION}}

- **Language / Framework:** {{e.g. TypeScript / Angular 21}}
- **Package manager / runner:** {{e.g. pnpm, npm, cargo, uv}}
- **Testing:** {{e.g. Vitest (unit) + Playwright (e2e)}}
- **Environment wrapper:** {{e.g. `nix develop --command bash -c "<cmd>"`, or "none"}}

## 2. Repository layout

Documentation is split four ways. Put things in the right one — a project where
everything lands in `prds/` loses the distinction within a week.

| Path | What goes here |
|---|---|
| `docs/prds/` | Product requirement docs, numbered `NN_short_name.md`. One per unit of work. |
| `docs/prds/completed/` | Finished, verified PRDs. |
| `docs/decisions/` | Architecture decision records, `NNNN-short-title.md`. **Human-approved — see §8.** |
| `docs/research/` | External material you're studying: API docs, briefs, prior art. Durable. |
| `docs/notes/` | Your own working scratch: meeting notes, debugging trails. Disposable. |

`docs/README.md` has the full conventions. When in doubt between `research/` and
`notes/`: research came from outside and stays useful, notes are what you thought
while working.

## 3. Core Principles

- **Iterative development:** small, logical, testable increments.
- **Design-first:** every unit of work is a PRD in `docs/prds/`, approved before
  implementation.
- **TDD-native:** a failing test exists before application code changes.

## 4. Mandatory Quality Gates

Code is not "done" until ALL pass:

1. **Tests green:** `{{TEST_COMMAND}}`
2. **Coverage:** global `> {{COVERAGE_PCT}}%` ({{any 100% modules}}).
3. **Build:** `{{BUILD_COMMAND}}` succeeds.
4. **Lint:** `{{LINT_COMMAND}}` clean.

Never report a gate as passing if it was not actually run. If a gate cannot run,
say so and stop.

## 5. PRD Lifecycle

1. **Draft:** create `docs/prds/NN_short_name.md`. Implementation needs approval.
2. **Branch:** create a feature branch off latest `main`. **Never** push to `main`.
3. **Test-first:** write failing tests for the PRD's acceptance criteria.
4. **Implement:** make them pass.
5. **Verify:** run the full suite + build + lint (section 4).
6. **Finalize:** move the PRD to `docs/prds/completed/`.
7. **PR:** push the branch and open a PR — only after explicit operator approval.

## 6. Conventions

- **Commits:** natural-language summary, prefixed with the PRD number
  (e.g. "PRD-03: add login form"). No conventional-commit prefixes.
- **PRD numbers** are permanent. Never renumber an existing PRD — the number
  appears in commit messages and branch names.
- **ADRs** move to `Accepted` only on human approval, and are immutable once
  accepted. To change a decision, add a new ADR that supersedes the old one and
  note the supersession in both.
- {{PROJECT_SPECIFIC_CONVENTIONS — styling vars, timezone handling, etc.}}

## 7. Agent Constraints

- **NEVER** bypass quality gates or coverage requirements.
- **NEVER** commit or push without explicit operator approval.
- **NEVER** modify application source without an established failing test.
- **NEVER** mark an ADR in `docs/decisions/` as `Accepted`. Every ADR must be
  approved by a human. You may draft one as `Proposed` when §8's test is met, and
  you must surface the call in your summary either way — but the operator is the
  only one who accepts it.
- **NEVER** rewrite git history or force-push.
- **NEVER** run destructive or irreversible commands ({{e.g. prod DB migrations}})
  without explicit approval.
- **Stop on blocked workflows:** if a tool needs interactive input or is blocked,
  do NOT hack around it. Stop, explain, and ask for guidance.

## 8. ADRs

Write an ADR in `docs/decisions/` if the decision meets **any** of the following.
This is a test, not a judgment call:

1. It adds, drops, or swaps a third-party dependency, service, or protocol.
2. It defines or changes a persisted data model, schema, or wire format.
3. It introduces or moves a boundary between modules, services, or layers.
4. It sets a convention later PRDs must follow — auth, error handling, state
   management, {{project-specific: styling, timezones, ...}}.
5. Reversing it later would mean editing files outside this PRD's scope.

Never an ADR — just do it and list it under `STEPS:`:

- naming, file placement within an existing structure, internal refactors
- test structure, fixtures, mocks
- anything the PRD's acceptance criteria already specify
- anything an accepted ADR already settles — cite it instead

**Gray zone:** if no criterion is clearly met but the call still feels
consequential, do **not** create the file. Name it under `DECISIONS:` and let the
operator decide. Writing an ADR is bounded by the list above; reporting one is
not.

**Status:** `Proposed` → `Accepted` → `Superseded`. An agent may write a
`Proposed` ADR. Only a human sets `Accepted`, and the file is immutable from that
point (§6). `Superseded` is set by a later ADR, noted in both.

**Numbering:** scan `docs/decisions/` and take the highest existing number + 1.
Never reuse or renumber.

## 9. Definition of Done

Success for any PRD is strictly bound to the Quality Gates in section 4. Do not
signal completion until every gate is met.
