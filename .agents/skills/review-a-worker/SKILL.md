---
name: review-a-worker
description: >-
  How to review a worker's finished work so nothing is approved in slices and
  nothing is sent back on a wrong premise: read the full progress log before
  judging the report, review the exact head, run one consolidated rubric before
  approving anything, verify a spec claim before requesting a change, and know
  when a failing gate is a decision for the user rather than another round. Use at
  every `mm review` and every CI failure on a delivered PR.
user-invocable: false
---

# review-a-worker

The review gate is exact-head and it is the step that pays for itself: on the
Radar milestone every change request fixed a real defect. What went wrong was the
*order* and the *premise* — approving a slice before the whole rubric had run, and
one checkpoint written from memory of the spec. This is the order.

## 1. Read everything the worker filed, first

- `mm progress <id>` is the report; `mm task <id>` shows only the latest note.
  Read the whole log before calling anything "missing" — a worker was nearly sent
  back for evidence it had already filed.
- `mm review show <id>` prints the latest report above the diffstat. The diff you
  review is the one at the exact head the worker named; if the head moved, start
  over.

## 2. One consolidated rubric, then one verdict

Run every applicable check before approving *anything*; never approve the search
box today and the design fidelity tomorrow — the worker recaptures all evidence
and re-runs all gates each round.

- **Scope**: only the named section; existing files touched only where the brief
  allowed, each touch listed under "Outside scope, required to build".
- **Contract / data**: wire shapes exactly as frozen; no invented fields; no
  client-side recomputation of server-owned values.
- **Spec structure and signature devices**: compare the built thing to the named
  reference row by row (header sentence, coverage line, columns, action placement,
  side-by-side devices, timeline, sources) — with the screenshots open next to the
  mockup's, not from memory.
- **Canonical copy and keys**: the copy file's strings verbatim; the key table;
  internal vocabulary never on screen.
- **Determinism**: time-sensitive tests under `TZ=UTC`; collection-only run when
  test modules were added; generated artifacts regenerated, frozen fixtures intact.
- **Semantics**: the truth table (concurrency, retry, undo) satisfied cell by cell;
  isolation and permission tests present where the brief listed them.
- **Evidence**: every verification command the brief required, with results, at
  the exact head; screenshots committed where UI changed.

## 3. Verify before you request a change

A wrong change request costs a worker round and your credibility. Before writing
"remove X": open the spec row, the contract line, or the mockup and confirm. If the
worker's choice matches the source, the checkpoint was wrong — say so in the log.

## 4. When a gate fails on CI

- Read the failing assertion, not the job name. Identify owner: worker, brief,
  environment, or pre-existing on main (check main's own runs before accepting
  "unrelated").
- Two identical failures of the same gate with a code fix between them means the
  decision is the user's: present budget-versus-infrastructure with the numbers.
  Do not prescribe a third fix from a local measurement that CI does not reproduce.
- Accept an outside-scope repair only when the worker proves the gate cannot pass
  without it, and say so in the PR body.

## 5. Approve, deliver, and say what shipped

`mm review approve <id>` at the exact head, `mm deliver <id> --base <branch>` for
stacked work (or the task's recorded stacked base), then tell the user in plain
terms what landed, what was flagged and not done, and what needs them.
