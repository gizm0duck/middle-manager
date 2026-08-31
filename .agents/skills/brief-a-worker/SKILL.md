---
name: brief-a-worker
description: >-
  The checklist a task brief must satisfy before `mm dispatch` — the things a
  worker cannot recover once it has started: the exact starting commit, the design
  reference for UI work, one authoritative verification suite, the test-hygiene
  checks that only fail on CI, and the tables that turn prose semantics into
  something a worker can be right about. Use every time you write or revise a brief;
  it exists because each item on it cost a worker cycle on 2026-08-30/31.
user-invocable: false
---

# brief-a-worker

A worker does exactly what the brief says and nothing it cannot know. Codex
workers cannot be redirected mid-turn, so a brief that is wrong at the start is a
wasted dispatch. Before `mm dispatch --brief <file>`, walk this list; every item was
paid for once already.

## 1. The packet itself

- **Durable file, never a shell substitution.** Write the brief under
  `.mm/briefs/<repo>/` and dispatch with `--brief <file>`. The tool refuses an empty
  file; a session restart once wiped a scratch brief and a worker started blank.
- **Exact starting commit, in commands.** For stacked work: `git fetch origin
  <branch> && git reset --hard origin/<branch>` and the SHA the worker must see. Never
  "merge the base in" — a fresh worktree loses nothing and a merge fails when main
  has moved. Name the base branch the PR opens against.
- **Scope fence, both directions.** What is in (the plan section, verbatim), what is
  explicitly out (the neighbouring sections by name), and the two report headings
  the worker must fill: "Outside scope, required to build" and "Flagged, not done".
- **The plan-note gate.** Require a `--phase plan` note (≤ 16 lines) mapping every
  sub-part to files before code, and say you will read it against the section.

## 2. UI work: name the design reference

- **Ask "which mockup are we designing from?" before dispatching.** Pin the file
  (copy it under `.mm/briefs/<repo>/` if it lives outside the repo) and its written
  spec; say which wins on structure, which on data (the frozen contract), which on
  materials (the repo's design system). A worker sent with tokens but no mockup
  rebuilt eight signature devices as generic UI.
- **Turn the mockup into a row-by-row acceptance checklist**: each signature device
  (header sentence, coverage line, column set, action placement, side-by-side
  quotes, timeline shape, sources pill) named as a pass/fail item with the spec row.
- **Canonical strings and keys are acceptance criteria**, not context: the copy
  file's exact strings, the keyboard table, the "never ship internal vocabulary" rule.
- **Determinism**: times rendered from fixtures use a fixture-pinned IANA zone; the
  unit suite must pass under `TZ=UTC` and under the machine zone before done.
- **Evidence**: committed screenshots for every reference state, sized to compare
  with the mockup's, under the repo's generated-evidence directory.

## 3. Verification: one authoritative suite, then subsets

- **Name one complete verification command or suite** that subsumes feature tests,
  migration inventory pins, cross-phase compatibility, generated-artifact
  freshness, and performance gates; list focused commands only as faster subsets.
  Two of three CI failures on one PR were pins outside the "focused" list.
- **Collection-only run** (`pytest --collect-only -q`, zero errors) whenever a PR adds
  or renames test modules — parallel test trees without packages refuse duplicate
  basenames, and targeted runs never see it.
- **Fresh-chain proof for migrations**: the database *reset* target, not the
  ensure target, and the expected single head.
- **Generated artifacts**: which commands regenerate them and that they are
  committed; the frozen fixtures that must stay byte-identical.
- **Merge main before pushing a PR that sat**: the forge runs no pull-request checks
  on a conflicting PR, so it shows "no checks" rather than failing.

## 4. Semantics the worker must be right about

- **Concurrency / back-off / retry: a state-transition truth table.** Rows are
  states, columns are events, cells are the next state and side effect — especially
  "own streak" versus "any peer's success". Prose here produced a back-off that
  reset on anyone's success.
- **Contract or freeze documents: an expected-discrepancy matrix.** Plan claim →
  shipped reality → what to do (document, flag, never invent). A worker rediscovered
  a "band" list parameter that never shipped.
- **Environment facts that bite**: per-worktree databases, package-cache
  sandboxes, CI runner slowdown factors and the budget margins they imply, tools
  that must not run locally.

## 5. Performance briefs

- **Never prescribe a fix from a local measurement when local and CI diverge
  sharply.** State the required outcome (the budget, where it is frozen) and ask for
  CI-side stage timing or query plans first; separate the hypothesis from the
  requirement. A round-trip fix moved a CI number by 3 ms.
- **Escalation is built in**: after two identical gate failures with a code fix in
  between, the budget-versus-infrastructure decision goes to the user with the
  evidence — not a third code round.

## 6. Before you send it

`test -s` the file; confirm disk and pool capacity (`mm dispatch` checks the disk);
read the brief once as the worker: is there any instruction it would have to guess?
