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
