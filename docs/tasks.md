# Middle Manager task log

Read this first each session. It records current state, recent changes, and open
considerations. Milestones and gating live in
[`implementation_plans/initial-plan.md`](../implementation_plans/initial-plan.md).
Product contract: [`goals.md`](goals.md). Implementation contract:
[`technical-plan.md`](technical-plan.md).

## Current state

- Milestone: **M3 — real workers** (done). Next: **M4 — autonomous completion**.
- M0 probes; M1 control plane; M2 runtime; M3 adds real Claude/Codex worker
  adapters gated by the M0 capability matrix, ceiling enforcement, task/result
  schemas, and blocked-worker resume / checkpoint steer. Autonomous delivery
  (hooks, questions/decisions, Lavish, review gate, PR) is still pending.
- Installed harnesses on this machine: Claude Code 2.1.251 (authenticated),
  Codex CLI 0.150.1 (ChatGPT login), Python 3.13, Node 24.16, `gh` 2.92, uv 0.11.

## M0 changes

What and why: the technical plan names interrupted provider turns and half-alive
processes as the largest technical uncertainty and asks to re-estimate after real
resume/interrupt probes. M0 measures the installed CLIs before building the
kernel, so downstream steering/recovery is gated by evidence, not assumption.

Files added:

- [`pyproject.toml`](../pyproject.toml), [`Makefile`](../Makefile),
  [`.gitignore`](../.gitignore) — zero-runtime-dependency Python project, uv-run
  dev tooling (Ruff, pytest), hermetic-by-default test config.
- [`schemas/provider-capability.schema.json`](../schemas/provider-capability.schema.json)
  — fail-closed capability schema (every flag defaults to `false`).
- [`src/middle_manager/`](../src/middle_manager) — package stub plus the
  `probes/` harness: `process.py` (process-group spawn/stream/signal),
  `fixtures.py` (isolated git repos/worktrees), `providers.py` (Claude/Codex
  command construction and event parsing), `scenarios.py` (the probe matrix),
  `report.py` (matrix rendering), `__main__.py` (CLI).
- [`tests/`](../tests) — hermetic tests for the fail-closed record, schema sync,
  and event parsing; plus an opt-in `-m live` probe test.
- [`implementation_plans/initial-plan.md`](../implementation_plans/initial-plan.md)
  — milestone-gated plan.

## Example commands

```bash
make test        # hermetic tests only (no provider spawning)
make lint        # ruff check
make fmt         # ruff format
make test-live   # opt-in: spawn real Claude/Codex probes
make probe       # run the matrix; writes docs/provider-capabilities.md
```

Probe run also writes raw evidence and a machine record under the gitignored
`.mm/probes/<run-id>/` (transcripts + `capabilities.json`).

## Risks / follow-ups

- Probe classification uses raw-line substring heuristics for tool/model-turn
  boundaries because provider event shapes drift. Evidence transcripts are kept
  so a mis-heuristic can be re-derived without re-running.
- `resume_after_clean_exit` is treated as sufficient proof of
  `session_survives_process_exit`; a dedicated on-disk session-file check can be
  added if a provider ever resumes from memory.
- Codex probe uses the CLI default model unless `MM_CODEX_MODEL` is set, to avoid
  invalid-model failures; the model used is recorded in the matrix.
- Live probes cost real (small) model turns and take a few minutes.

## M1 changes

What and why: the kernel foundation gives the manager a deterministic control
surface (config, state, repo registration, diagnostics) before any live worker
exists, so later milestones build on enforced policy rather than prompt trust.

Files added:

- [`src/middle_manager/paths.py`](../src/middle_manager/paths.py) — `.mm/` layout
  with `MM_HOME` override.
- [`src/middle_manager/config.py`](../src/middle_manager/config.py) — fail-closed
  `MMConfig` (manager profile, hard worker ceiling, cost posture, authority) with
  stdlib TOML read and a small writer; merge authority defaults off.
- [`src/middle_manager/state/`](../src/middle_manager/state) — versioned SQLite
  schema (repos, runs, tasks, deps, sessions, events, decisions, usage, reviews,
  artifacts) and a thin store DAL.
- [`src/middle_manager/setup/`](../src/middle_manager/setup) — environment
  discovery (harness/version/auth), `mm doctor`, and a scriptable `mm setup` /
  `mm config` that only offers authenticated harnesses.
- [`src/middle_manager/repos.py`](../src/middle_manager/repos.py) — `mm repo
  add|list|sync` as isolated base clones under `.mm/repos/`.
- [`src/middle_manager/cli.py`](../src/middle_manager/cli.py) — the `mm` dispatch
  (`version`, `doctor`, `setup`, `config`, `repo`, `status`).
- Agent home: [`README.md`](../README.md), [`AGENTS.md`](../AGENTS.md),
  [`CLAUDE.md`](../CLAUDE.md), [`bin/install`](../bin/install),
  [`bin/mm`](../bin/mm), the shared `setup-middle-manager` skill,
  [`tools.lock`](../tools.lock), [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
- Tests: config, state, setup/discovery, repos, CLI (all hermetic, offline).

Example commands:

```bash
mm setup --non-interactive --manager-provider claude --worker-provider codex
mm repo add /path/to/local/repo      # or an https URL
mm doctor --json
mm status
```

## M2 changes

What and why: the kernel runtime makes the manager able to actually dispatch,
supervise, and recover bounded work — durably and without model cost — so M3 can
drop in real adapters behind a proven runtime.

Files added:

- [`worktree/lease.py`](../src/middle_manager/worktree/lease.py) — identity-bound
  worktree leases; default `git worktree` backend, prefers a pinned `treehouse`
  binary when present. Recorded in a new `leases` table.
- [`providers/base.py`](../src/middle_manager/providers/base.py) — the adapter
  contract (`probe/start/resume/steer/interrupt/reconcile/parse_event/
  parse_usage/result`) and the normalized TaskSpec/ProviderEvent/WorkerResult/
  UsageInfo protocol.
- [`providers/fake.py`](../src/middle_manager/providers/fake.py) +
  [`fake_worker.py`](../src/middle_manager/providers/fake_worker.py) — a
  deterministic worker subprocess that emits a normalized JSONL stream and makes
  a real commit in its leased worktree; instruction markers drive the
  completed/blocked/failure paths.
- [`supervisor/spool.py`](../src/middle_manager/supervisor/spool.py) — append-only
  per-task JSONL event spool.
- [`supervisor/runner.py`](../src/middle_manager/supervisor/runner.py) — per-task
  runner guardian; process-group spawn, event streaming to spool + SQLite,
  fail-closed result (a missing result event is a failure, never a silent
  success).
- [`supervisor/core.py`](../src/middle_manager/supervisor/core.py),
  [`server.py`](../src/middle_manager/supervisor/server.py),
  [`client.py`](../src/middle_manager/supervisor/client.py),
  [`protocol.py`](../src/middle_manager/supervisor/protocol.py) — the supervisor
  business logic plus its Unix-socket transport (dispatch / task_status /
  run_status / wait_actionable / reconcile / shutdown).
- New `runners` table and store helpers (leases, runners, sessions, usage,
  actionable events).
- CLI: `mm supervisor serve|status|stop`, `mm dispatch`, `mm wait`,
  `mm reconcile`.
- Tests: leases, fake-provider runner (completed/blocked/failed), and an
  end-to-end supervisor test over the real socket.

Example (verified end to end):

```bash
mm repo add /path/to/repo
mm supervisor serve &            # or run in a dedicated terminal
mm dispatch --repo repo --title "kernel demo"
mm wait 1 --timeout 15           # -> worker_done with a real head SHA
mm supervisor stop
```

## M3 changes

What and why: real workers make the framework useful — but only behind the M0
evidence and the enforced ceiling, so cost and steering claims stay honest.

Files added/changed:

- [`provider-capabilities.json`](../provider-capabilities.json) — committed
  machine-readable capability matrix (from M0) that adapters read; the probe CLI
  now refreshes it on every run.
- [`providers/capability.py`](../src/middle_manager/providers/capability.py) —
  fail-closed capability gate; `allows_interrupt_steer` requires the strong
  proofs (in-progress tool interrupt + uncatchable crash + on-disk session) and
  disables a record on CLI version drift.
- [`providers/claude.py`](../src/middle_manager/providers/claude.py) and
  [`providers/codex.py`](../src/middle_manager/providers/codex.py) — real
  adapters (argv, event parsing, usage, fail-closed result). No permission-bypass
  flags; Claude uses `acceptEdits`.
- [`router.py`](../src/middle_manager/router.py) — worker ladder, smallest-eligible
  selection, and hard ceiling enforcement.
- [`schemas.py`](../src/middle_manager/schemas.py) +
  [`schemas/task.schema.json`](../schemas/task.schema.json) +
  [`schemas/worker-result.schema.json`](../schemas/worker-result.schema.json) —
  zero-dependency validator; task packets are validated before dispatch.
- Supervisor: real adapter selection, ceiling enforcement on dispatch, plus
  `resume_task` (blocked-worker resume) and `steer_task` (capability-gated
  interrupt vs. checkpoint). Runner now finalizes a completed worktree into a
  reviewable commit and head SHA.
- CLI: `mm dispatch --provider claude|codex --model --reasoning`, `mm resume`,
  `mm steer`.
- Tests: capability gate, router ceilings, adapter parsing on real M0 stream
  shapes, schema validation, blocked→resume, and an opt-in **live mixed-provider**
  test that runs real Claude and Codex workers.

## M4 changes

_Pending._

## Drift review

Appended at each milestone close (what was proven, what changed, whether any goal
or plan sentence is now wrong, and the exact next milestone).

### M0 drift review (2026-08-29)

What was proven (live, versions Claude Code 2.1.251 / Codex CLI 0.150.1; full
matrix in [`provider-capabilities.md`](provider-capabilities.md), raw evidence
under gitignored `.mm/probes/`):

- Both CLIs emit a durable session/thread id and token usage in their structured
  stream, and both resume by explicit id after: clean exit, SIGINT during an
  active turn, SIGINT during a real long tool command (`sleep 20`, observed
  `in_progress`), an uncatchable SIGKILL of the process group, from a linked git
  worktree, and when the same id is resumed twice.
- No orphaned `sleep` child survived the process-group SIGINT for either
  provider, and the worktree stayed clean.
- Claude print mode accepts a follow-up message into the same running process via
  `--input-format stream-json` (`mid_process_steer` yes). Codex `exec` is
  single-shot (stdin is `/dev/null`) and its resume always requires a prompt
  (`requires_resume_prompt` yes) — there is no promptless follow mode.

What changed versus assumptions: the pessimistic hypotheses did not materialize.
The public Claude print-mode "cannot resume" reports concern the interactive
`--resume` picker; resume by explicit session id worked in every scenario,
including from a git worktree. So the plan's optimistic branch holds: for these
two versions, M3 adapters MAY offer interrupt steering rather than being limited
to checkpoint-plus-recovery.

Important nuance (kept honest in the matrix): `resume_after_sigint_model_turn` is
the weakest proof. Claude was interrupted at the tail of the turn (base exit 0,
~3.7s) and Codex at `turn.started` (base exit 1, ~0.5s). Mid-generation interrupt
timing is inherently racy and per-provider. The strong, unambiguous guarantees
are `resume_after_sigint_tool` (interrupts a real in-progress command) and
`resume_after_sigkill` (crash recovery). Recovery must still never infer success
from a missing process — that goal is unchanged and remains enforced by design,
not by these probe outcomes.

Is any goal or plan sentence now wrong? No. [`goals.md`](goals.md) is unchanged.
[`technical-plan.md`](technical-plan.md) keeps its fail-closed policy ("until a
provider/version passes those tests, expose only checkpoint-at-completion
steering"); we now have a passing record for these two versions, and the delivery
phases are annotated with M0. The adapter capability record must be re-probed on
any CLI version change (both drift often).

Next milestone: **M1 — kernel foundation** (installer, setup/doctor, SQLite,
`mm repo add`, README; no live workers). Blocked on human approval per the
operating rule.

### M1 drift review (2026-08-29)

What was proven: `mm setup` (interactive and scriptable), `mm doctor`,
`mm config`, and `mm repo add|list|sync` work end to end against the real
environment; discovery correctly gates selection to authenticated harnesses;
SQLite state initializes idempotently; config is fail-closed with merge off by
default. 26 hermetic tests pass; lint/format clean.

What changed vs. the plan: the technical plan's Phase 1 lists an idempotent
installer that also provisions pinned companion tools (Treehouse, gh-axi,
lavish-axi). M1 delivers the installer and env checks but defers real
companion-tool provisioning/pinning to M2/M4, where those tools are first used.
`tools.lock` and `THIRD_PARTY_NOTICES.md` record the intent with `UNPINNED`
placeholders. This is a sequencing deferral, not a design change; no goal or
technical-plan sentence is now wrong.

Substitution note for later milestones: this machine may not have the external
Treehouse/gh-axi/lavish-axi binaries. To keep the system runnable and testable,
M2+ will implement each companion behind an adapter with a built-in local
fallback (git `worktree` for leases, `gh` for delivery, a local HTML+feedback
file for Lavish), selecting the real pinned binary when present. This preserves
the architecture while not blocking on unavailable binaries; it will be called
out explicitly in each milestone's drift review.

Next milestone: **M2 — kernel runtime** (supervisor socket, per-task runner,
append-only spool, fake provider, worktree lease lifecycle,
`mm wait --until-actionable`, `mm reconcile`).

### M2 drift review (2026-08-29)

What was proven: a task dispatched over the Unix socket acquires a real worktree
lease, runs a worker subprocess under a runner guardian, streams normalized
events to an append-only spool and to SQLite, records fail-closed results and
usage, and surfaces an actionable `worker_done` event that `mm wait` returns —
verified both by tests and a live CLI smoke (`serve`/`dispatch`/`wait`/`stop`).
The blocked/question and failure paths work; reconcile marks vanished runners
`needs_recovery` rather than assuming success. 33 hermetic tests, lint/format
clean.

Substitutions vs. the technical plan (called out per the M1 note):

- Worktree leases use a built-in `git worktree` backend; the pinned Treehouse
  binary is preferred automatically when installed but is not required. This
  keeps the runtime testable on a machine without Treehouse while preserving the
  lease abstraction the plan specifies.
- `mm wait --until-actionable` is spelled `mm wait <run> [--timeout]`; behavior
  matches (blocks until an actionable event or all-terminal). Naming noted so the
  three plan sources stay aligned; not a design change.

AF_UNIX note: macOS caps socket paths near 104 bytes, so the supervisor socket
lives at a short, stable `/tmp/mm-<hash>.sock` derived from the `.mm` home rather
than under the deep `.mm/run/` path. The pid file stays under `.mm/run/`.

Is any goal or plan sentence now wrong? No. The supervisor/runner split, durable
SQLite state, append-only spool, and fail-closed recovery all match the plan.
The Phase 1 "feedback-capable artifact" (Lavish) is intentionally deferred to
M4, where the artifact loop is first used; the runtime it depends on now exists.

Next milestone: **M3 — real workers** (Claude/Codex adapters gated by the M0
capability flags, task/result schemas, usage accounting, ceiling enforcement,
checkpoint steer, blocked-worker resume, a mixed-provider fixture task).

### M3 drift review (2026-08-29)

What was proven: the opt-in live mixed-provider test passes — a real Claude
(haiku) worker and a real Codex (account-default model) worker each make a change
that the runtime commits into the leased worktree and records as `worker_done`
with a head SHA and usage. Adapters parse the real M0 stream shapes; the ceiling
is enforced in code at dispatch; blocked→resume completes via checkpoint steer.
57 hermetic tests, lint/format clean.

Two real-world facts discovered while validating (now encoded):

- Codex on a ChatGPT account rejects an explicit `--model gpt-5-codex` (400
  "not supported when using Codex with a ChatGPT account"); it only works with
  the account default. So `model=None` now means "provider account default" and
  the runtime never fabricates a model name. Codex cost is steered via reasoning
  effort, not model tier — the codex ladder holds only the known-valid model.
- A fabricated model name (e.g. a nonexistent "-mini") makes Codex emit metadata
  errors and fail, reinforcing that model ladders must be real.

Capability sourcing: adapters read the committed `provider-capabilities.json`
(the gitignored `.mm/probes/` evidence is not present in a fresh checkout). The
probe CLI refreshes this committed file, and the gate disables a record whose
recorded CLI version no longer matches the installed one.

Is any goal or plan sentence now wrong? No. Manager/worker peering, ceiling
enforcement in the runtime, capability-gated steering, and fail-closed results
all match the plan. Mid-flight interrupt steering remains gated and, for Codex,
is never offered regardless of resume support because exec cannot accept mid-run
input — consistent with the technical plan and the M0 matrix.

Next milestone: **M4 — autonomous completion** (lifecycle hooks, question
routing, durable scoped decisions, the Lavish feedback loop, an exact-HEAD review
gate, `gh`/`gh-axi` push+PR, and cross-repo dependency checks).

## M4 — autonomous completion (2026-08-29)

The manager can now close the loop without a human in the routine path: resolve a
worker's question from a durable decision, review the exact work, and deliver a
PR — with a hard gate that refuses to ship anything not reviewed at its current
head.

What changed and why:

- **Durable scoped decisions** ([`src/middle_manager/decisions.py`](../src/middle_manager/decisions.py)).
  A decision records the normalized-question fingerprint, answer, scope
  (`task` | `run` | `global`), provenance, and can be superseded without erasing
  history. `find_matching` resolves by precedence (global > run > task, most
  specific/recent wins). This is goal 6 ("preserve decisions without making them
  timeless").
- **Autonomous question routing**
  ([`supervisor/core.py`](../src/middle_manager/supervisor/core.py)). When a
  worker blocks on a question, the supervisor consults decisions and, on a match,
  emits `auto_answered` and resumes the worker itself — no human. A question that
  reappears *after* its stored answer was applied is escalated as `blocked`
  (actionable) instead of looping. `mm answer <task> --answer ... [--scope]`
  records the user's answer as a decision and resumes; the same answer is then
  reused for equivalent questions in scope.
- **Exact-HEAD review gate** ([`src/middle_manager/review.py`](../src/middle_manager/review.py)).
  A review is bound to the precise commit SHA it examined. `is_approved_at_head`
  returns false if the latest verdict is not `approved` or if the worktree moved
  past the reviewed SHA. `mm review show|approve|request-changes|status`.
- **Gated delivery** ([`src/middle_manager/delivery.py`](../src/middle_manager/delivery.py)).
  `mm deliver <task>` refuses unless approved-at-head, then pushes the lease
  branch (plain git, any origin) and opens a PR via `gh-axi`/`gh` when present.
  This is the only outbound path and the most conservative step (goal 5, delivery
  gates enforced in code, and goal, "approvals match exact head commits").
- **Lavish feedback loop** ([`src/middle_manager/artifacts.py`](../src/middle_manager/artifacts.py)).
  `mm artifact <run> --title --sections` writes a self-contained HTML review
  surface plus a JSON feedback sidecar; `mm feedback <id>` reads structured
  feedback back and flips the artifact to `closed` when the user closes the
  session. Fully local and dependency-free; `lavish-axi` is preferred when
  installed (goal 9).
- **Cross-repo rollout order** ([`src/middle_manager/crossrepo.py`](../src/middle_manager/crossrepo.py)).
  `mm plan <run>` topologically orders tasks over their dependency edges and
  refuses on cycles or dangling (out-of-run) edges, so shared-contract changes
  ship in a safe sequence (goal 4).
- **Lifecycle hooks** ([`src/middle_manager/hooks.py`](../src/middle_manager/hooks.py)).
  `mm hook <event>` reads a JSON payload on stdin and records a durable
  `hook_<event>` lifecycle event against the task/run — the supervision seam for
  interactive managers. Tolerates non-JSON input.

Example commands:

```
mm dispatch --repo demo --title "needs input" --provider fake --instructions "ASK:which port?"
mm answer 2 --answer "8080" --scope run          # records a decision + resumes
mm review show 1                                  # base..head diffstat
mm review approve 1 --findings lgtm               # binds approval to the head SHA
mm deliver 1 --no-pr                              # refused unless approved-at-head
mm artifact 1 --title "Design review" --sections '[{"heading":"Naming","body":"...","options":["keep","rename"]}]'
mm feedback 1
mm plan 1
```

Verified by 74 hermetic tests (decisions precedence/supersede, the review gate
reopening when head moves, delivery refusal + real push after approval, cross-repo
ordering/cycles/missing edges, the artifact roundtrip, hook ingestion, and an
end-to-end autonomous auto-answer through the supervisor + fake worker), plus a
live CLI smoke: `dispatch → review show → deliver (refused) → approve → deliver
(pushed) → plan`, the `answer` auto-resume path, and rendering the Lavish HTML
artifact in a browser. Lint/format clean.

### M4 drift review (2026-08-29)

What was proven: the routine autonomous loop runs without a human — a blocked
worker is auto-answered from a standing decision and completes — and the delivery
path is gated by an approval bound to the exact reviewed SHA, so a post-review
commit reopens review and blocks delivery (both asserted by tests and observed in
the live smoke). Decisions are scoped and supersedable; cross-repo runs get a
topological rollout order or a refusal on cycles/dangling edges; the Lavish
artifact renders and its feedback sidecar round-trips.

Substitutions vs. the technical plan (consistent with the M1/M2 fallback note):

- Delivery uses plain `git push` plus `gh-axi`/`gh` for PR creation, auto-selected
  when installed. PR creation is skippable (`--no-pr`) so the push gate is testable
  without a GitHub remote; hermetic tests exercise the gate and a real local push.
- Lavish is the built-in local HTML + JSON-sidecar loop; `lavish-axi` is preferred
  when present but not required. This is the Phase 1 "feedback-capable artifact"
  the M2 review deferred to here, now delivered.
- `mm answer` / autonomous routing spells the "question routing + decision reuse"
  behavior; naming noted so the three plan sources stay aligned. Not a design
  change.

Deferred to M5 (as the plan schedules): the *validity-condition* check before
reusing a decision (goal 6's "changed premise" delta) is recorded structurally
(rationale/scope/supersede) but the automated staleness check — comparing changed
requirements/contracts/authority/time-sensitive facts before reuse — lands with
M5 memory controls. Today reuse is fingerprint+scope; a superseding decision wins.
Called out so no goal sentence is read as fully closed prematurely.

Is any goal or plan sentence now wrong? No. Question routing, durable scoped
decisions, the exact-HEAD gate, GitHub-only delivery, the Lavish loop, and
cross-repo ordering all match goals 1/4/5/6/9 and the technical plan's Phase-2
autonomous completion.

Next milestone: **M5 — hardening** (crash/replay/CLI-drift recovery tests,
decision-staleness/memory controls, usage reports, `mm doctor` polish, docs, and
the post-M0 re-estimate the technical plan requires).

## M5 — hardening (2026-08-29)

The final milestone makes the system safe to leave running: durable state
survives restarts, half-alive runners are reconciled fail-closed, decisions can
go stale and be managed, usage is auditable, and `mm doctor` surfaces the two
things that silently rot (capability-matrix drift and schema version).

What changed and why:

- **Restart / replay / reconcile recovery**
  ([`tests/test_recovery.py`](../tests/test_recovery.py)). Proven: a fresh
  supervisor over the same `.mm` reconstructs a completed run and its actionable
  events from SQLite; the append-only spool mirrors the streamed worker events
  one-to-one and each spool line is a valid record (replayable); a runner whose
  process vanished without a result is marked `orphaned` and its task
  `needs_recovery` with an `error` event — never assumed successful (goal 7).
- **Decision staleness + memory controls**
  ([`decisions.py`](../src/middle_manager/decisions.py),
  [`state/db.py`](../src/middle_manager/state/db.py) schema v3). Decisions now
  carry an optional premise `context` and an `invalidated_at`. On reuse,
  `find_matching(context=...)` raises `StaleDecision` when the winning decision
  was recorded under a different non-null premise, so the manager asks only for
  the delta instead of silently reusing a timeless preference (goal 6). The
  supervisor computes a task's context from its repo base SHA and escalates a
  stale premise as `blocked`. `mm decision list [--all] | invalidate | forget`
  lets the user inspect, invalidate (history preserved), or hard-delete (goal 8).
  A forward-only migration adds the columns to pre-existing databases.
- **Usage reports** ([`state/store.py`](../src/middle_manager/state/store.py),
  `mm usage <run>`). Per provider/model/reasoning token totals and call counts,
  plus a run total — the auditable "how much usage was incurred" the goals ask
  for.
- **`mm doctor` polish** ([`setup/doctor.py`](../src/middle_manager/setup/doctor.py)).
  Compares each provider's recorded probe version against the installed CLI and
  flags drift ("re-probe before trusting steering"), and reports the state-DB
  schema version against the expected one.

Example commands:

```
mm doctor                          # now includes capability drift + schema version
mm usage 1                         # tokens by provider/model, plus a total
mm decision list --all             # includes superseded/invalidated
mm decision invalidate 3           # stop reusing a decision (history kept)
mm reconcile                       # mark vanished runners needs_recovery
```

Verified by 83 hermetic tests (recovery/replay/reconcile, decision
staleness/invalidate/forget/list, usage aggregation, doctor drift+schema, semver
drift extraction) plus a live CLI smoke: `doctor` drift/schema output, the
`answer → decision list → invalidate` memory loop, and `usage`. Lint/format clean.

### M5 drift review (2026-08-29)

What was proven: killing and restarting the supervisor loses no obligations,
live events, or open decisions; the spool replays; vanished runners fail closed;
decisions can go stale on a changed premise and be invalidated/forgotten; usage
and capability/schema drift are inspectable. This closes goal 7 (survive
interruptions), completes goal 6's staleness delta deferred from M4, and delivers
goal 8's inspect/correct/forget controls and auditable usage.

Substitutions / scope notes:

- The decision premise is a conservative proxy (repo base SHA). Richer validity
  conditions (contract versions, dependent-decision graphs, time-sensitive facts)
  can extend `context` later without changing the interface; the staleness gate
  and `StaleDecision` escalation already exist.
- CLI-drift coverage is the capability-matrix version check (adapters already
  fail closed on a version mismatch; `mm doctor` now surfaces it) rather than a
  broad snapshot of every CLI flag. This matches the plan's "re-probe on CLI
  version change" contract.

Is any goal or plan sentence now wrong? No. All ten goals are now represented in
code and tests. The remaining top-level surface is the interactive `mm start`
manager loop; every subsystem it orchestrates (routing, delegation, decisions,
review gate, delivery, Lavish, recovery) exists and is tested.

Post-M0 re-estimate: the largest technical uncertainty (interrupted provider
turns / half-alive processes) resolved favorably — both installed CLIs proved
resumable after SIGINT/SIGKILL and clean exit, with session survival and
worktree resume (see [`docs/provider-capabilities.md`](provider-capabilities.md)).
That removes the biggest schedule risk the technical plan flagged: safe in-flight
steering did not require a bespoke recovery-only path, and checkpoint + resume
covers the common case. The hardening band accordingly lands at the lower end of
the plan's 20–30 day range for this scope; the remaining spend is the `mm start`
manager UX and live multi-repo dogfooding rather than the resume/interrupt matrix.

Next: **M5 is the final planned milestone.** Follow-ups (not gating): the
interactive `mm start` manager loop, real Treehouse/`gh-axi`/`lavish-axi`
provisioning to replace the built-in fallbacks, and live multi-repo dogfood runs.

## Conversational entry point — `mm start` (2026-08-29)

User correction: everything (configuration and delegation) must be done by
**talking to the manager** from within a harness, not by the user running `mm`
subcommands. This delivers the goals' central promise — "make multi-agent
development feel like delegating to a capable engineering organization, not
operating a collection of terminals" — as the actual front door.

What changed and why:

- **`mm start` / `mm manager`** ([`manager/launch.py`](../src/middle_manager/manager/launch.py),
  wired in [`cli.py`](../src/middle_manager/cli.py)). Launches the configured
  manager harness (Claude or Codex) **interactively** in the repo root, with:
  the repo `bin/` prepended to PATH so the manager calls `mm` directly; the
  runtime role injected (Claude via `--append-system-prompt`, both via the seed
  prompt) pointing at the authoritative runtime contract; `mm` pre-allowed for
  Claude (`--allowedTools "Bash(mm:*)"`) so the manager operates fluidly while
  `mm` itself remains the authority gate; and an optional objective seeded as the
  first message. `--provider/--model/--reasoning` override the configured profile;
  `--dry-run` prints the invocation. `build_launch` is a pure function so the argv/
  env/seed are unit-tested without spawning a harness.
- **Runtime contract** ([`docs/manager-runtime.md`](manager-runtime.md)). A
  dedicated contract for a *runtime* manager session (distinct from the repo
  development contract in `AGENTS.md`/`CLAUDE.md`, which it overrides for the
  session). Its one rule: never ask the user to run a command — drive `mm`
  yourself for setup, delegation, review, delivery, memory, and cost; on first
  run, walk the user through configuration conversationally. Includes an
  intent→command reference so the manager knows the surface.
- **Docs**: README now leads with "talk to the manager" (`mm start`) and demotes
  the raw CLI to a "what the manager runs for you" reference; `bin/install`
  points at `mm start`.

Example (the user only does the first line):

```
./bin/mm start "add a health endpoint to my-service"
# ...then converse; the manager runs mm doctor/setup/repo add/dispatch/wait/
# answer/review/deliver itself and reports back in prose.
```

Verified by 91 hermetic tests (argv/env/seed for both providers, provider
override not inheriting a mismatched model, explicit-override precedence,
objective + unconfigured seeds, no-provider error, and a CLI `--dry-run`) plus a
live smoke: a launched Claude manager, given only the runtime role and `mm`
allowlisted, ran `mm doctor` on its own and reported state in one sentence, then
in a second run ran `mm setup` + `mm repo add` + `mm repo list` itself — the
persisted `config.toml` and registered repo confirm it operated the real control
plane, not a hallucination. Lint/format clean.

Drift note: no goal or plan sentence is now wrong; this closes the "front door"
the goals imply and the technical plan deferred. The manager still cannot exceed
ceilings or bypass the review gate — those remain enforced by `mm`, which the
manager merely invokes. Interactive-harness process handoff (`os.execvpe`) is the
one untested-by-unit path (it replaces the process); it is covered by the live
smoke and kept trivial. Real companion-binary provisioning and multi-repo dogfood
remain the open follow-ups.

## Real companion integration — treehouse + lavish-axi (2026-08-29)

User pointed at the real upstreams
([kunchenguid/treehouse](https://github.com/kunchenguid/treehouse),
[kunchenguid/lavish-axi](https://github.com/kunchenguid/lavish-axi)). Both are
installed on this machine, so we integrated against their **real** interfaces
rather than placeholders, and pinned them with published integrity.

What changed and why:

- **Real Treehouse lease backend**
  ([`worktree/lease.py`](../src/middle_manager/worktree/lease.py)). Previously the
  backend was labeled `"treehouse"` but always used `git worktree`. It now uses
  treehouse's purpose-built non-interactive lease interface:
  `treehouse get --lease --json --lease-holder mm-task-<id>` durably reserves a
  pre-warmed pooled worktree and prints `{path, lease_id, ...}`; we create the
  task branch inside it (`git switch -c mm/task-…`) so delivery pushes
  `HEAD:<branch>` exactly as before, and release runs
  `treehouse return <path> --force --if-lease-id <id>` (guarded so we never return
  someone else's worktree). Backend selection is now `resolve_backend()`:
  explicit arg > `MM_LEASE_BACKEND` env > auto-detect (treehouse if on PATH, else
  git). The always-available `git` backend is unchanged.
- **Hermetic suite stays hermetic**
  ([`tests/conftest.py`](../tests/conftest.py)). A new autouse fixture forces
  `MM_LEASE_BACKEND=git` so the supervisor's auto-selected backend never mutates
  the global `~/.treehouse` pool during tests. New hermetic tests cover the pure
  treehouse argv builders, lease-JSON parsing (and rejection of garbage/missing
  fields), and backend-resolution precedence
  ([`tests/test_lease.py`](../tests/test_lease.py)).
- **Live proof**
  ([`tests/live/test_treehouse_live.py`](../tests/live/test_treehouse_live.py)).
  Opt-in (`-m live`): leases a real pooled worktree, asserts the branch/base SHA,
  lands a commit, releases, verifies the DB, and destroys the pooled worktree so
  no global state leaks. Passing here confirms the exact interface the backend
  depends on.
- **lavish-axi at the contract level**
  ([`docs/manager-runtime.md`](manager-runtime.md)). lavish-axi is an *agent-first
  AXI* — its own docs say the agent authors the HTML and drives
  `lavish-axi <file>` / `lavish-axi poll <file>`, learning the surface live from
  `--help`/`design`/`playbook`. So we integrated at the contract level: the
  runtime manager prefers `lavish-axi` for rich review when present and learns it
  live; `mm artifact` stays the zero-Node local fallback. Added a "Companion
  tools" section (treehouse transparent isolation, lavish rich review, gh/gh-axi
  delivery, doctor drift).
- **Real pins** ([`tools.lock`](../tools.lock),
  [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)). Replaced the
  `UNPINNED`/`EXAMPLE` placeholders with real versions and integrity: treehouse
  `v2.3.0` with per-platform sha256 from the release `checksums.txt`; lavish-axi
  `0.1.62` and gh-axi `0.1.34` with their npm `integrity`/`shasum`. All MIT.

Verified: full hermetic suite green (`make lint`/`make test`), lint/format clean,
and the live treehouse round-trip passes on this machine.

Drift note: this narrows the largest remaining gap between the plan's "pinned
managed dependencies" and reality — Treehouse is now genuinely wired (not just
labeled), lavish-axi is integrated the way its own design intends, and gh-axi was
already wired in delivery. The honest remaining follow-up is *managed
provisioning*: downloading each pinned artifact into `.mm/tools/<tool>/<version>/`
and verifying it against the checksums now recorded in `tools.lock`, rather than
using whatever is on PATH. Nothing in the goals or technical plan is contradicted;
the companion story moved from "placeholder" to "wired + pinned, provisioning
pending."

## Managed provisioning + manager persona (2026-08-29)

Two follow-ups landed together: `mm setup` now actually *installs* the pinned
companions, and the runtime manager has a defined voice.

Managed provisioning — what changed and why:

- **Provisioner** ([`setup/provision.py`](../src/middle_manager/setup/provision.py)).
  Reads the real pins from [`tools.lock`](../tools.lock) and installs each
  companion into `.mm/tools/<tool>/<version>/`, activating a stable symlink in
  `.mm/tools/bin/`. treehouse is fetched from its GitHub release for the detected
  `platform_slug()` and its sha256 is **verified against `tools.lock` before
  activation**; lavish-axi/gh-axi are `npm install`ed at the exact pinned version
  into an isolated prefix (npm verifies registry integrity). The side-effecting
  primitives (`_download`/`_sha256`/`_extract_tar`/`_npm_install`) are injectable,
  so orchestration — the checksum gate, symlink activation, crash-safety — is unit
  tested without a network. Provisioning **never raises**: a failed companion
  leaves the documented fallback in place.
- **Resolver** ([`companions.py`](../src/middle_manager/companions.py)).
  `companion_bin(name)` prefers the managed `.mm/tools/bin/<name>` and falls back
  to `PATH`. Wired into the treehouse lease backend
  ([`worktree/lease.py`](../src/middle_manager/worktree/lease.py)) and delivery's
  PR tool ([`delivery.py`](../src/middle_manager/delivery.py)); the manager launch
  prepends `.mm/tools/bin` to PATH so `lavish-axi` resolves to the managed copy.
- **CLI/UX**. `mm setup` provisions by default (`--skip-tools` to opt out); new
  `mm tools install [names…] [--force]` and `mm tools status`
  ([`cli.py`](../src/middle_manager/cli.py)). `mm doctor` now labels each companion
  `managed (.mm/tools)` vs `on PATH`
  ([`setup/discovery.py`](../src/middle_manager/setup/discovery.py)).

Verified for real on this machine: `mm tools install --force` downloaded and
checksum-verified treehouse `v2.3.0`, npm-installed lavish-axi `0.1.62` and gh-axi
`0.1.34`, symlinked all three into `.mm/tools/bin`, and `mm tools status` reports
them as managed. (These pins are newer than the machine's global copies, so
managed provisioning also upgrades.) Plus 9 new hermetic tests
([`tests/test_provision.py`](../tests/test_provision.py)); full suite 104 green.

Manager persona — what changed and why:

- The runtime manager now has a **voice**: a dry, deadpan, Office-Space-flavored
  middle manager who is genuinely great at the job — competent first, funny
  second, never at the user's expense, and who drops the bit entirely when things
  are urgent or broken. It also **keeps the machinery backstage**: it speaks in
  outcomes, not `mm`/plumbing, unless the user asks how it works. Added as
  "Voice & tone" and "Keep the machinery backstage" sections in the runtime
  contract ([`docs/manager-runtime.md`](manager-runtime.md)), reinforced in
  Reporting, and injected into the launch role so it is actually in force
  ([`manager/launch.py`](../src/middle_manager/manager/launch.py)).
- The persona lives in the **runtime** contract, not the root
  [`AGENTS.md`](../AGENTS.md) (which stays plain/technical for engineers building
  the framework). A pointer in `AGENTS.md` now says so, to prevent the comedic
  voice from bleeding into dev-time commits/PRs.

Drift note: this closes the "managed provisioning" follow-up called out in the
prior entry — companions are now genuinely downloaded, verified, and pinned, with
graceful fallback intact. The persona is additive and enforced only at runtime; it
cannot weaken authority, ceilings, or the review gate (those remain in `mm`), and
the contract explicitly subordinates humor to competence and to dropping the bit
under pressure. Open follow-up: multi-repo dogfood, and optionally teaching the
manager to auto-run `mm tools install` on first setup failure rather than at
setup time only.

## Persona hardening — stronger voice, no status feed (2026-08-29)

User feedback from a real session: the manager's personality was too weak and it
narrated internals (echoed `./bin/install`/`mm doctor`, read back
`.mm/config.toml`, "State DB schema v3", capability-matrix status, a bordered repo
table with head SHA and base-clone path, and even handed the user `./bin/mm start`
to run). The user's ask: *"I don't care how it does its job, I just want to know
it's doing the things I'm asking."*

What changed and why:

- **Runtime contract** ([`docs/manager-runtime.md`](manager-runtime.md)). Rewrote
  "Voice & tone" into a stronger character (dry, deadpan, quietly cocky Lumbergh-
  who-is-actually-the-most-competent-person-in-the-building) with a litmus test:
  *if it could have been written by a generic assistant, rewrite it.* Replaced the
  soft "keep machinery backstage" with a hard **"Show up with results, not a
  status feed"** section: never narrate mechanics, never read internal state back
  like an audit (paths, SHAs, schema, versions, `mm` subcommands, raw output,
  plumbing tables), answer the exact question tightly, speak in outcomes, and
  never hand the user a command to run — with concrete before/after examples taken
  from the transcript. First-contact step 3 no longer asks for a setup summary.
- **Launch injection** ([`manager/launch.py`](../src/middle_manager/manager/launch.py)).
  The always-on role now carries the strongest signal (VOICE + RESULTS-NOT-
  NARRATION) so it holds even before the contract is read, and the seed prompt
  stops instructing the old "run `mm doctor`/`mm repo list` and summarize my setup"
  audit — it now says greet in one dry line and get to work, reporting no
  diagnostics/paths/versions.

Verified with live Claude print-mode smokes against the real role string:
- *"what repos are you managing?"* → "Just the one: **papaya-frontend-monorepo**,
  tracking `main`. Not exactly a sprawling empire yet, but it's a well-run one." —
  one line, in character, no SHA/path/table.
- first-contact greeting → "Morning. I've been in since before you, and
  everything's already where it should be — I'm running the floor, Codex is doing
  the heavy lifting, and nothing's on fire. So. What are we shipping today?" — no
  audit, no versions, no `./bin/mm start` handed back.

Lint/format clean; full suite 104 green (launch seed/role assertions still hold).

## Persona — eager-to-please-the-boss motivation (2026-08-29)

User: bake in that the manager speaks to the user as their **boss** and is eager to
please because their performance review — and bonus — depends on it. Added this as
the engine under the whole personality in "Voice & tone"
([`docs/manager-runtime.md`](manager-runtime.md)) and the always-on launch role
([`manager/launch.py`](../src/middle_manager/manager/launch.py)): confident/cocky
about the work, but keen to impress the boss (ownership, "consider it handled,
boss," occasional angle-for-a-raise aside). Hard guardrail kept intact — eagerness
manifests as **speed, ownership, and results, never groveling/flattery/filler**;
you please the boss by shipping, not fawning. Live smokes confirm: "Morning, boss.
Everything's green… consider it handled — what are we shipping?" and "Consider it
handled, boss… back to you with a reviewed deliverable, not a status feed." Lint
clean; launch tests green.

## Scope fix — only registered repos, ignore framework files (2026-08-29)

Bug from a real session: asked to "produce a visual of the services in the backend
repo," the manager guessed `~/workspace/papaya-backend-monorepo` and started
surveying the machine — and it prefaced with "I'll read the project contract and
task list," i.e. it was following *this framework's* `AGENTS.md`/`CLAUDE.md` and
treating the session as developing Middle Manager. Two boundary violations:
operating outside `.mm/repos/`, and reading the framework's own dev files.

What changed and why:

- **Hard repo-scoping rule** ([`docs/manager-runtime.md`](manager-runtime.md), new
  "Repositories: the only ground you work on" section; also tightened the delegate
  step). The only repositories that exist are those registered with Middle Manager
  (`mm repo list`, under `.mm/repos/`). Never scan `~`/`~/workspace`/the machine or
  guess a local path; resolve any named repo against `mm repo list`; if it isn't
  registered, ask for its URL/path and `mm repo add` it rather than hunting.
- **Ignore the framework's own files.** The contract and the always-on launch role
  now state that this repo's `AGENTS.md`/`CLAUDE.md`/`docs/` are for engineers
  *building* Middle Manager, not the runtime manager, and nothing outside `.mm/` is
  in scope.
- **Source-level guard** ([`CLAUDE.md`](../CLAUDE.md), [`AGENTS.md`](../AGENTS.md)).
  Because the harness auto-loads these from the repo root, both now open with a
  "Runtime manager session? STOP — follow `docs/manager-runtime.md`, don't read
  `docs/tasks.md`" banner keyed on `MM_MANAGER_SESSION=1`. `CLAUDE.md` previously
  told *every* session to "read `docs/tasks.md`," which was the direct trigger.
- **Launch role** ([`manager/launch.py`](../src/middle_manager/manager/launch.py))
  carries the SCOPE boundary so it holds before any file is read.

Verified live: given only `papaya-frontend-monorepo` registered and asked about
"the backend repo," the manager now replies (in character) "I don't have a backend
repo on the books… I'm not going to go rummaging around the machine guessing which
folder you mean… point me at a URL or local path and I'll get it registered" —
instead of scanning `~/workspace`. Lint clean; full suite 104 green.
