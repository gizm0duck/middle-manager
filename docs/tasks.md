# Middle Manager task log

Read this first each session. It records current state, recent changes, and open
considerations. Milestones and gating live in
[`implementation_plans/initial-plan.md`](../implementation_plans/initial-plan.md).
Product contract: [`goals.md`](goals.md). Implementation contract:
[`technical-plan.md`](technical-plan.md).

## Current state

- Milestone: **M1 — kernel foundation** (done). Next: **M2 — kernel runtime**.
- M0 added the probe harness. M1 adds the `mm` control plane: setup/doctor,
  SQLite state, config, and `mm repo add`. No live workers, supervisor,
  adapters, or companion-tool provisioning yet.
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
