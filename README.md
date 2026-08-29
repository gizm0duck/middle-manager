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
  `mm repo add`. No live workers yet.
- Later milestones add the supervisor/runner, real worker adapters, autonomous
  delivery, and hardening.

## Requirements

- macOS or Linux (Windows is out of scope for v1).
- Python 3.12+, Git, and [uv](https://docs.astral.sh/uv/).
- Node 22+ and the official `gh` CLI (for companion tools and delivery).
- An authenticated Claude Code and/or Codex CLI.

## Getting started

```bash
# 1. Clone this repository and enter it.
# 2. Bootstrap the local environment (idempotent, safe to rerun).
./bin/install

# 3. Configure the manager profile and the hard worker ceiling.
./bin/mm setup                 # interactive; offers only authenticated harnesses
# or, scriptable:
./bin/mm setup --non-interactive --manager-provider claude --worker-provider codex

# 4. Register a repository (URL or local path).
./bin/mm repo add https://github.com/you/your-repo

# 5. Inspect health and state at any time.
./bin/mm doctor
./bin/mm status
```

`mm start` (launching the interactive manager) and delegated work arrive in
later milestones.

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
