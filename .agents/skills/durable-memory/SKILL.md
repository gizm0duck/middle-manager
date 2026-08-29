---
name: durable-memory
description: >-
  Persist and recall what you learn so you never relearn a repo, a cross-repo
  relationship, or the user's preferences across sessions. Use at the start of every
  session to load context, and whenever you learn something durable while working.
  The store is machine-local Markdown under `.mm/memory/`; the authoritative state is
  `.mm/state.db`. This skill owns the judgment — what's worth remembering and how to
  keep it curated.
user-invocable: false
---

# durable-memory

You keep a durable memory for this instance so each session starts smart, not blank.
It lives under `.mm/memory/` (gitignored, machine-local) in **two tiers, split by who
owns them**:

**Per-instance (this `mm`) — your layer, spanning every repo.** Cross-repo knowledge
and your own work tracking:

- `preferences.md` — how the user likes things done everywhere (comms, PRs,
  engineering defaults, standing decisions).
- `relationships.md` — how the registered repos relate (dependencies, shared
  contracts, rollout order).
- `tasks.md` — **your work board.** What you're tracking, delegating, and have
  finished across every repo. This is where *you* keep the plot; workers don't touch
  it.

**Per-repo — the shared worker workspace, under `repos/<name>/`.** Auto-seeded when a
repo is registered; **workers** read and write it as they work, and **you peek at it
to follow progress without interrupting them**:

- `notes.md` — what the repo is, build/test/run commands, conventions, gotchas,
  key areas. Workers consult it and extend it.
- `tasks.md` — repo work plus a **live progress log**: workers record status here as
  they go, so you can check in without asking. (Dispatched workers are handed the
  absolute paths automatically — see below.)

Two layers, two owners: the instance tier is yours to steer the whole operation; the
per-repo tier is the shared bench where the work actually happens. Resolve paths with
`mm memory path [--repo <name>]`; `mm memory show [--repo <name>]` prints them (the
plain `show` includes your board); `mm memory init` scaffolds the instance tier and
seeds a directory per registered repo. You edit the Markdown directly.

## Track work on your board; watch workers via their files

- **Your board (`tasks.md`, instance tier)** is how *you* keep the plot across repos —
  what's delegated, in flight, blocked on you, and done. Live lifecycle state is in
  `.mm/state.db` (`mm status`, `mm wait`); the board is the durable, human-readable
  overview that outlives the run. Keep it current as you delegate and land work.
- **Peek, don't distract.** Every dispatched worker is pointed at its repo's
  `repos/<name>/tasks.md` and told to log progress there. To check on a worker, *read
  that file* (or `mm memory show --repo <name>`) rather than asking — the narrative is
  already there.
- **Review the plan early; steer if it's wrong.** Workers post their intended approach
  to `repos/<name>/tasks.md` *before* implementing. Read it while course is still cheap
  to change: if the approach is off, `mm steer <task_id> --message ...` (or `mm resume
  <task_id> --message ...`) to correct it — don't wait to reject the finished diff.
  That's what "genuine course-correction" is for; reserve interrupts for it, not routine
  status.

## Recall — load before you work

- **At session start**, read the per-instance tier (`preferences.md` and
  `relationships.md`). When the user names a repo, also read its `repos/<name>/`
  (`notes.md`, and `tasks.md` if relevant) before doing anything on it. Treat these
  as ground truth — don't re-derive what's already written.
- **Before asking the user**, check memory (and durable decisions, `mm decision
  list`). If the answer is on file, use it; don't make them repeat themselves.

## Record — capture as you learn

Write it down the moment it's durable, not at the end. Keep entries short and factual.

- **Your work tracking** → `tasks.md` (instance tier): update the board as you
  delegate, land, or park cross-repo work — it's your durable view of the operation.
- **Repo facts** → `repos/<name>/notes.md`: the real build/test/lint/run commands,
  the architecture in a line, conventions, and the gotchas that bit you this session.
- **Repo work / progress** → `repos/<name>/tasks.md`: durable follow-ups, backlog,
  and tech debt for that repo, plus the running progress log workers append to (live
  run state stays in `.mm/state.db`).
- **Cross-repo facts** → `relationships.md`: a dependency you discovered, a shared
  contract, the order changes must roll out.
- **Preferences (all repos)** → `preferences.md`: how the user wants updates, PRs,
  branches, reviewers, defaults — anything told once that should hold next time.
  Repo-specific conventions go in that repo's `notes.md`, not here.
- **Reusable decisions** → also record via `mm answer ... --scope run|global` so the
  runtime reuses them automatically; summarize durable global ones in `preferences.md`.

## Curate — keep it trustworthy

- **Update in place; prune the stale.** Memory is a living cheat-sheet, not an
  append-only log. Wrong or outdated notes are worse than none — fix them when you
  notice.
- **Facts and preferences, not transcripts.** Don't paste large output or narrate;
  capture the durable takeaway.
- **Live task/run state is not memory.** Ongoing work lives in `.mm/state.db`
  (`mm status`, decisions). Memory is for what should outlive the run.

## Boundaries

- Memory is machine-local and gitignored — never commit it, and never write secrets
  or credentials into it.
- It records the user's world (their repos, their preferences), never edits to this
  framework's own source, contract, or skills.
