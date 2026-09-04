# Task status, steering, and resume

A task's status is a claim about what is happening right now. It has to be true:
the manager schedules, nags, and reports off it, and the heartbeat treats
`failed` as an incident. This is the contract.

## Statuses

| Status | Meaning |
| --- | --- |
| `requested` | The task row exists; no worker has started. |
| `in_progress` | A worker session is live, or is being started right now. |
| `worker_done` | The worker finished a turn and the evidence says it finished the gate: a `done` progress note, nothing unpushed, and no backgrounded command left hanging. |
| `worker_stopped` | The turn ended mid-gate. Not terminal, not a failure — the work is there and unfinished, and `mm resume` picks it up. |
| `blocked` | The worker asked a question and stopped. |
| `failed` | The worker's *current* session ended without a result. |
| `needs_recovery` | The runner process vanished without recording a result. |
| `delivered` | Pushed (and normally a PR opened) after an approved review. |

## A turn ending is not the same as finishing

A Claude worker started the full backend suite, the tool's foreground cap pushed
it into the background, and the worker ended its turn to wait for it. The
backgrounded suite was killed with the session, the branch was never pushed, no
`done` note was ever filed — and the harness recorded `worker_done` (task 103,
2026-09-04; task 104 repeated it with browser smoke retries).

So a completed turn is now judged on evidence. Any of these means the task lands
in `worker_stopped` instead:

| Signal | What it means |
| --- | --- |
| The newest progress note is not `--phase done` | The worker never said it got to the end. |
| The branch holds commits no remote has | The work was never pushed, so nobody but that worktree has it. |
| The session's last tool call was a backgrounded command | Whatever it was waiting on died with the session. |

"Commits no remote has" is asked by patch, not by SHA, so a branch that a stack
cascade rebased upstream reads as pushed rather than as a pile of unpushed work.
The check runs *before* the supervisor's end-of-task auto-commit, so the
supervisor's own safety commit is never what makes a worker look like it withheld
work.

`mm run` surfaces the reason next to the event, `mm task <id>` shows it, and
`mm resume <id>` **with no `--message`** sends the worker back in with what was cut
short — the unfinished gate, the unpushed commits, and the instruction to run the
suite in the foreground, file the done note, and push. Claude workers are told the
same thing up front in the injected command rules.

### The push the harness finishes for the worker

Unpushed commits are the one signal that does not need a resume. On 2026-09-04
three of four workers filed a done note and still could not get their commits onto
the remote — a push hook failing on advisories already on the default branch, a
push held by the permission layer, a turn that ended first — and the manager
pushed each lease branch by hand. A lease branch is named after its task and
nothing else writes to it, so that push is always safe and the harness makes it.

When a completed turn's *newest* note is `done` and the branch still has commits,
the supervisor pushes the lease worktree's head to that branch itself (no force,
after the auto-commit so the safety commit goes up too), records it as a
`pushed_by_manager` event naming the branch and the SHA, judges the turn again on
what is true afterwards, and only then lands on `worker_done`. A turn that stopped
*before* `done` is missing more than a push, so nothing is pushed and it stays
`worker_stopped` exactly as before.

If the remote refuses the push, the task stays `worker_stopped` and the reason
carries the last 20 lines of the push's stderr, so `mm task <id>` shows the hook's
own words rather than a guess. `mm task push <id>` is the same push run by hand —
for that task once the hook is fixed, or for any task whose lease worktree is
still on disk. It prints the SHA it pushed, or the refusal it got.

## Steering a live worker

`mm steer <task> --message ...` picks its mode from the provider's proven
capabilities (`provider-capabilities.json`), and reports which one it used:

| Mode | When | What happens |
| --- | --- | --- |
| `interrupt_resume` | The provider proved resumable mid-work interrupts (Claude) **and** a turn is live | The live runner is retired, the turn is interrupted, and the session is auto-resumed with the steer text. **The task stays `in_progress` throughout.** |
| `checkpoint_pending` | The provider cannot take a mid-flight steer (Codex) and a turn is live | The message is queued and applied by resuming the session the moment the current turn ends. |
| `resume` | No turn is live (finished, delivered, blocked, failed, or the process is gone) | The session is resumed immediately with the steer text, because there is no checkpoint coming. |

The interrupt path used to leave the task `failed`. The interrupted process exits
non-zero with no result event, which the adapter correctly reads as a failed
*session* — but a steer is a redirection, not a failure. So the runner is marked
**superseded** before the signal goes out; its exit is recorded as a
`runner_superseded` event against the retired session id and never touches the
task. A manual `mm resume` is no longer needed after a steer.

## Superseded sessions

Every resume — an explicit `mm resume`, an auto-answer, a queued steer landing at
its checkpoint, or the auto-resume after a steer's interrupt — starts a *new*
provider session and retires the old one. Retired runners are recorded with
`superseded_at` and their session id, and at finalization they:

- do **not** set the task's status,
- do **not** raise an actionable `worker_done` / `question` / `error` event,
- record a `runner_superseded` event carrying the session id, exit code, and what
  the retired session's result would have been.

This closes the window where a superseded session's late `exit=1` overwrote
`in_progress` with `failed` while the resumed worker was alive and working. Every
terminal event now also carries the `session_id` it came from, so a late arrival
is attributable rather than anonymous.

## Resume

`mm resume <task> [--message ...]` flips the task to `in_progress` **before** the
resumed process is started, so a task that was `failed`, `blocked`, or
`needs_recovery` stops reading as stalled the moment work resumes rather than
whenever the new worker happens to emit its first event.

## Cascade: a worktree whose branch was rewritten upstream

A stack's upper branches are *rebased* when the layer below merges, so a task's
worktree can be sitting on commits that no longer exist on its own branch. Both
`mm resume` and `mm deliver` check for that first:

| What git says | What happens |
| --- | --- |
| The remote branch holds commits the worktree lacks, and the worktree holds nothing whose patch is not already upstream | The worktree is reset onto the remote branch and a `worktree_cascaded` event records both commits. |
| Both sides hold commits nothing upstream matches | The command is **refused**, naming both commits. Nothing is force-pushed: which commits survive is a judgement call, not a default. |
| Anything else | Nothing happens. |

"Holds commits the remote lacks" is a question about *patches*, not SHAs
(`git cherry`): a rebase gives every replayed commit a new SHA, so counting SHAs
would read a freshly cascaded branch as full of unpushed work and refuse the whole
stack. A cascade does move the head, so a review approved before it no longer
binds — the delivery gate asks for a re-review at the new commit.
