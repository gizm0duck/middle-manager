# Task status, steering, and resume

A task's status is a claim about what is happening right now. It has to be true:
the manager schedules, nags, and reports off it, and the heartbeat treats
`failed` as an incident. This is the contract.

## Statuses

| Status | Meaning |
| --- | --- |
| `requested` | The task row exists; no worker has started. |
| `in_progress` | A worker session is live, or is being started right now. |
| `worker_done` | The worker finished a turn and there is a reviewable head. |
| `blocked` | The worker asked a question and stopped. |
| `failed` | The worker's *current* session ended without a result. |
| `needs_recovery` | The runner process vanished without recording a result. |
| `delivered` | Pushed (and normally a PR opened) after an approved review. |

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
