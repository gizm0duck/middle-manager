---
name: update-middle-manager
description: >-
  Bring the harness itself up to date without losing the team's work: bring in-flight
  work to a good stopping point, save durable state, stop the supervisor and the
  heartbeat, pull the latest main, run whatever install and config changes the update
  needs, restart detached, and pick back up exactly where the work was paused. Use when
  the user asks to update, upgrade, or pull the latest Middle Manager, when `mm`
  reports it is behind main, or after you merge a Middle Manager change yourself.
  Mechanics live in `mm` and git; this skill owns the order and the judgment.
user-invocable: true
---

# update-middle-manager

Updating the harness is the one maintenance act that touches every worker at once:
the supervisor is the parent of every runner thread, and a supervisor that restarts
loses the event stream of any worker still running, which `mm reconcile` then marks
`needs_recovery`. So the order is fixed — **settle, save, stop, pull, reconfigure,
restart, resume** — and nothing is skipped because "nothing is running". An update
that ends with the manager unable to say where the team stands is a failed update.

Two repositories are involved and must not be confused: the **harness repo** (this
checkout, `mm`'s own source, where `git pull` happens) and the **registered repos**
under `.mm/repos/` (the user's code, never touched by an update). `.mm/` is
machine-local and gitignored; the pull cannot change it, and the update must not
either except through `mm` commands.

## 1. Settle: bring in-flight work to a good stopping point

1. `mm status`, `mm health`, `mm run <id>` for every open run — know what is live.
2. **Live turns.** A worker mid-turn is cheapest to let finish. If `mm health` shows
   workers alive and their last plan or progress note suggests the turn ends soon
   (a Codex worker at its gate, a Claude worker writing its done note), wait for it —
   `mm watch --once` every few minutes, not a blocking `mm wait` in a live turn. If a
   worker is early in a long turn, decide with the user whether to wait or accept a
   resume after the update; say which workers and roughly how far along.
3. **Questions.** Answer anything `blocked` that an existing decision settles
   (`mm answer`); leave the rest — a question survives a restart, a live turn does not.
4. **Reviews and deliveries.** Do not start a review or a delivery you cannot finish
   before stopping; a `worker_done` task waits safely.
5. Do **not** dispatch anything new from here until step 7.

## 2. Save durable state

Run the `handoff` skill in full: make the todo ledger true right now — including a
todo per worker you expect to resume after the update — drop durable facts into
memory, then `./bin/mm handoff` for the snapshot and the pickup prompt. Keep the
prompt; step 7 follows it. Note the harness commit you are on:
`git -C <harness root> rev-parse --short HEAD`.

## 3. Stop the team's daemons cleanly

`mm watch --stop`, then `mm supervisor stop`. Confirm with `mm supervisor status`
(it must say no supervisor). Any worker process that outlives the supervisor is
found by `mm reconcile` in step 7 — do not kill workers by hand.

## 4. Pull the latest main

In the harness root: `git status --porcelain` must be empty. A dirty harness checkout
is not yours to resolve during an update — stop and show the user what is modified.
Then `git fetch origin main` and `git pull --ff-only origin main`. A non-fast-forward
pull means the checkout is on a branch or has local commits; say so and stop. Record
what arrived: `git log --oneline <old>..HEAD`, and read the commit messages — they
are the record of *why* each change was made and what it changes for you.

## 5. Run the config changes the update needs

Every step is idempotent; run all of them, in order:

1. `./bin/install` — new or changed dependencies (`uv sync`).
2. `mm doctor` — validates `.mm/config.toml` against the new schema and reports any
   field the update added or renamed; fix with `mm setup` or `mm config …`, never by
   hand-editing the file. A harness that lost auth shows here too.
3. `mm tools install` — companions at the versions the new `tools.lock` pins.
4. `mm status` — any `mm` command migrates `.mm/state.db` forward; this is the one
   that shows you the result.
5. Reread [`docs/manager-runtime.md`](../../../docs/manager-runtime.md) — it is your
   contract and the update may have changed a command, a flag, a default, or a rule.
   Then any skill the commit messages name. Note what changed for the team in
   `improvements.md` if it changes how you operate.

## 6. Restart, detached

`mm supervisor start` (never `serve` in a harness background task) and
`mm watch --detach`. Confirm with `mm supervisor status` — it says `(detached)` and
names the log.

## 7. Pick back up where the work was paused

Follow the pickup prompt from step 2 exactly as a new session would (the `handoff`
skill's "On resume"): `mm reconcile` (workers that outlived the old supervisor land
in `needs_recovery`), `mm health`, then `mm resume <id>` for each — every resume
verifies liveness and says `worker alive` or reports an incident; a resumed Codex
worker gets its queued messages, a Claude worker its session. Read the snapshot,
load the ledger and memory, reconcile against live state, and greet the user with:
the harness commit you moved from and to, what the update changed for the team in
one or two sentences, which workers resumed and which finished on their own, and the
single most actionable thing now.

## Boundaries

- Never `git pull` with workers you have not accounted for; never `git reset`, stash,
  or force anything in the harness checkout — a dirty checkout is the user's call.
- Never touch the registered repos or their worktrees during an update.
- Never edit `.mm/config.toml` or `.mm/state.db` by hand; `mm doctor`, `mm setup`
  and `mm config` are the tools.
- An update that fails part-way stops and reports which step failed and what is
  still running; it does not improvise past a failed `install` or `doctor`.
