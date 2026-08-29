# Middle Manager

A lightweight, local framework for directing autonomous software work across one
or more Git repositories. You talk to one high-capability **manager** agent
(Claude or Codex). It decomposes work, routes bounded tasks to the smallest
eligible **worker**, resolves routine uncertainty, reviews the exact final diff,
and opens a pull request only after that review passes.

See [`docs/goals.md`](docs/goals.md) for the product contract and
[`docs/technical-plan.md`](docs/technical-plan.md) for the design.

## Status

This repository is built in human-approved milestones (see
[`implementation_plans/initial-plan.md`](implementation_plans/initial-plan.md)
and [`docs/tasks.md`](docs/tasks.md)):

- **M0 (done):** live Claude/Codex interrupt/resume probes. Results:
  [`docs/provider-capabilities.md`](docs/provider-capabilities.md).
- **M1 (done):** kernel foundation — installer, setup/doctor, SQLite state,
  `mm repo add`.
- **M2 (done):** kernel runtime — supervisor Unix socket, per-task runner
  guardian, append-only event spool, worktree leases, `mm dispatch`/`wait`/
  `reconcile`, and a fake provider.
- **M3 (done):** real Claude/Codex worker adapters gated by the M0 capability
  matrix, task/result schemas, ceiling enforcement, and checkpoint/blocked resume.
- **M4 (done):** autonomous completion — durable scoped decisions, autonomous
  question routing, the exact-HEAD review gate, gated `gh`/`gh-axi` delivery, the
  local Lavish review loop, cross-repo rollout ordering, and lifecycle hooks.
- **M5 (done):** hardening — restart/replay/reconcile recovery, decision
  staleness and memory controls, usage reports, and `mm doctor` drift/schema
  checks.
- **Conversational entry point (done):** `mm start` launches your manager harness
  with the runtime contract in force, so you delegate everything by talking to
  the manager; it drives the `mm` control plane on your behalf.

## Requirements

- macOS or Linux (Windows is out of scope for v1).
- Python 3.12+, Git, and [uv](https://docs.astral.sh/uv/).
- Node 22+ and the official `gh` CLI (for companion tools and delivery).
- An authenticated Claude Code and/or Codex CLI.

## Getting started

You use Middle Manager by **talking to the manager**, not by running commands.
You bootstrap once, then enter a manager session and describe what you want in
plain language; the manager operates the whole `mm` control plane for you.

```bash
# 1. Clone this repository and enter it.
# 2. Bootstrap the local environment (idempotent, safe to rerun).
./bin/install

# 3. Enter Middle Manager and just talk to it.
./bin/mm start                         # launches your configured manager harness
./bin/mm start --provider claude       # or enter as a specific harness
./bin/mm start "add a health endpoint to my-service"   # hand it an objective
```

On first run there is no config yet — that's fine. The manager runs `mm doctor`
itself, walks you through choosing a manager profile and a hard worker ceiling in
conversation, writes the config for you, and then asks what you'd like to
accomplish. From then on you delegate objectives; the manager registers
repositories, dispatches the smallest eligible worker, answers routine worker
questions from durable decisions, reviews the exact diff, and opens a
reviewed-at-head pull request — reporting back in prose. You never touch `mm`
yourself.

The manager's runtime operating contract is
[`docs/manager-runtime.md`](docs/manager-runtime.md).

### What the manager runs for you (reference)

You don't type these — the manager does — but the control plane is a plain CLI,
so everything is inspectable and scriptable if you want it:

```bash
mm doctor                              # harnesses, capability-matrix drift, schema
mm setup --non-interactive --manager-provider claude --worker-provider codex
mm repo add https://github.com/you/your-repo
mm supervisor serve                    # per-task runners, durable state
mm dispatch --repo your-repo --title "add health endpoint" --provider claude
mm wait <run_id>                       # block until an actionable state
mm answer <task_id> --answer "use /v2/health" --scope run   # recorded + reused
mm decision list                       # inspect/invalidate/forget memory
mm review show <task_id> && mm review approve <task_id>
mm deliver <task_id>                   # refused unless approved at current head
mm artifact <run_id> --title "Design review" --sections '[...]'   # Lavish HTML
mm plan <run_id>                       # cross-repo rollout order
mm usage <run_id>                      # model token accounting
```

## What lives under `.mm/`

Canonical runtime state is gitignored under `.mm/`:

- `config.toml` — manager profile, worker ceiling, cost posture, authority.
- `state.db` — SQLite source of truth (repos, runs, tasks, decisions, sessions,
  events, usage, reviews).
- `repos/` — read-only base clones.
- `runs/` — per-run plans, task packets, results, and append-only event logs.
- `tools/`, `worktree-pools/`, `memory/`, `run/` — companion tools, task
  worktrees, durable memory, and supervisor sockets.
- `probes/` — raw provider-probe evidence.

## Manager vs. worker policy

During setup you choose an expensive manager profile and a hard worker ceiling
(provider, maximum model, maximum reasoning). The manager selects the smallest
eligible worker per task and escalates only on evidence. The runtime enforces the
ceiling in code — it never silently uses the manager's expensive model as a
worker.

## Autonomy and the merge boundary

The manager autonomously performs normal, reversible work (inspection,
decomposition, worktrees, delegation, testing, bounded retries, commits, and
opening an approved PR). It asks you only for genuine product/direction
decisions, new authority or credentials, destructive/irreversible actions, scope
expansion, ceiling changes, and **merging** — which stays off unless you grant a
standing policy.

## Development

```bash
make test        # hermetic tests (no provider spawning)
make lint        # ruff check
make fmt         # ruff format
make test-live   # opt-in: spawn real Claude/Codex probes
make probe       # re-run the provider capability matrix
```
