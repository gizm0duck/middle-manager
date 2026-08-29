# Middle Manager initial implementation plan

This is the milestone-gated implementation plan derived from the approved
"Probe-first Middle Manager implementation" plan. Code edits stay small and
gated by the current milestone. After each milestone: run `make lint`,
`make fmt`, `make test`; update [`docs/tasks.md`](../docs/tasks.md); create a
checkpoint commit; re-read this plan; append a drift review; then wait for human
approval before the next milestone.

The [`design/middle-manager-plan.html`](../design/middle-manager-plan.html) surface
records the seven decisions accepted on 2026-08-28; do not reopen them.

## Why probe-first

The technical plan names interrupted provider turns and half-alive processes as
the largest technical uncertainty and asks to re-estimate after real resume and
interruption probes. So the first code measures the installed CLIs rather than
building the kernel top-down. This is a sequencing choice only; the kernel,
supervisor, adapters, and delivery layers remain exactly as designed.

Interrupt steering is capability-gated everywhere downstream: until a
provider/version proves mid-flight resume, adapters expose only
checkpoint-at-completion steering plus a compact fresh-session recovery packet.

## Milestones

### M0 — Live Claude/Codex interrupt and resume (current)

First code. Measure the installed CLIs; build no supervisor, Treehouse, setup
wizard, or real adapters yet.

Deliverables:

- `pyproject.toml`, `Makefile`, Ruff + pytest, `src/middle_manager/` package
  stub, zero runtime dependencies.
- `schemas/provider-capability.schema.json` — fail-closed capability schema.
- `src/middle_manager/probes/` — isolated-fixture probe harness that spawns real
  `claude -p` and `codex exec`, applies interrupts, attempts resume, and records
  a fail-closed matrix.
- `docs/provider-capabilities.md` — recorded matrix for the installed versions.
- Hermetic unit tests plus an opt-in `-m live` probe test (`make test-live`).

Probe matrix: clean complete + resume; SIGINT during a model turn; SIGINT during
a long tool command (with orphan/dirty observation); resume from a worktree and
from the main checkout; duplicate/late resume; mid-process steer attempt;
session-file survival across clean exit / SIGINT / SIGKILL; session id and usage
presence in the stream.

Acceptance: both installed CLIs have a recorded capability row; default tests
stay hermetic; `make test-live` documented and opt-in; checkpoint commit and
drift review exist; no later milestone started.

### M1 — Kernel foundation (after approval)

Idempotent `bin/install`, shared setup skill, task-oriented `README.md`, harness
discovery, `mm setup` / `mm config` / `mm doctor` stubs, SQLite schema,
`mm repo add`, config validation. No live workers.

### M2 — Kernel runtime (after approval)

Supervisor Unix socket, per-task runner guardian, append-only spool, fake
provider, Treehouse lease lifecycle, `mm wait --until-actionable`,
`mm reconcile`. Lands the technical-plan Phase 1 acceptance, including a
feedback-capable artifact and run reconcile.

### M3 — Real workers (after approval)

Claude and Codex adapters behind `probe/start/resume/steer/interrupt/reconcile/
parse_event/parse_usage/result`, consuming the M0 capability flags. Task/result
schemas, usage accounting, ceiling enforcement, checkpoint steer, blocked-worker
resume. Mixed-provider fixture task.

### M4 — Autonomous completion (after approval)

Lifecycle hooks, question routing, durable scoped decisions, Lavish feedback
loop, exact-HEAD review gate, `gh-axi` push/PR, cross-repo dependency checks.

### M5 — Hardening (after approval)

Crash/replay/CLI-drift tests, memory controls, usage reports, doctor polish,
docs. Re-estimate delivery after M0 evidence, per the technical plan.

## Guardrails

- Small diffs gated by the current milestone.
- Prefer the planned tree: `src/middle_manager/`, `schemas/`, `templates/`,
  `tests/`. No Firstmate source copies, no vendored companion trees, no tmux in
  the protocol.
- Use `uv` for all Python commands.
- Record milestone state and drift in [`docs/tasks.md`](../docs/tasks.md) as each
  lands.
