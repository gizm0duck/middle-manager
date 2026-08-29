# Middle Manager

A lightweight, local framework for directing autonomous software work across one
or more Git repositories. You talk to one high-capability **manager** agent
(Claude or Codex). It decomposes work, routes bounded tasks to the smallest
eligible **worker**, resolves routine uncertainty, reviews the exact final diff,
and opens a pull request only after that review passes.

The whole point: **make multi-agent development feel like delegating to a capable
engineering organization, not operating a fleet of terminals.** You describe what
you want in plain language; the manager runs the machinery for you and reports back
in outcomes. (It also has a personality — a dry, deadpan middle manager who's
weirdly invested in its own performance review — but that's in service of shipping,
not instead of it.)

The mental model, in three lines:

- **You → manager:** you speak intent to one manager agent, like a boss delegating.
- **Manager → workers:** it plans, spins up isolated worktrees, and hands bounded
  tasks to the cheapest capable worker, escalating only on evidence.
- **Manager → you:** it resolves routine questions itself, reviews the exact diff,
  and comes back with a reviewed pull request — asking you only about the things
  that genuinely need a human.

See [`docs/goals.md`](docs/goals.md) for the product contract and
[`docs/technical-plan.md`](docs/technical-plan.md) for the design.

## Status

Middle Manager is functional today and under active development. What works end to
end:

- **Talk-to-the-manager entry point** (`mm start`) — configure and delegate
  entirely in conversation; you never run `mm` yourself.
- **Real Claude & Codex workers** with a hard, code-enforced cost ceiling and
  smallest-eligible-worker routing (mixed-provider runs supported).
- **Interrupt / resume steering** validated live against both CLIs
  ([`docs/provider-capabilities.md`](docs/provider-capabilities.md)).
- **Autonomous completion** — durable scoped decisions, automatic question
  routing, an exact-HEAD review gate, and gated `gh`/`gh-axi` PR delivery.
- **Durable & recoverable** — SQLite source of truth, append-only event logs, and
  restart/replay/reconcile recovery.
- **Cross-repo aware** — dependency-ordered rollout across multiple repositories.
- **Pinned companions** — treehouse (worktrees), lavish-axi (rich review), gh-axi
  (delivery), provisioned and checksum-verified by setup.

For the milestone-by-milestone history and current open items, see
[`docs/tasks.md`](docs/tasks.md) and
[`implementation_plans/initial-plan.md`](implementation_plans/initial-plan.md).

## Requirements

- **macOS or Linux** (Windows is out of scope for v1).
- **Python 3.12+**, **Git**, and **[uv](https://docs.astral.sh/uv/)**.
- **Node 22+** and the official **`gh` CLI** (for companion tools and PR delivery).
- **An authenticated Claude Code and/or Codex CLI.** Middle Manager never logs you
  in — sign in to your harness first (`claude` for Claude Code, `codex` for Codex).
  You need at least one; you can use one as the manager and the other as the worker
  ceiling, or the same for both. `mm doctor` tells you what it can see.

## Getting started

You use Middle Manager by **talking to the manager**, not by running commands.
There are exactly two things you do: bootstrap once, then enter a session and
describe what you want. The manager operates the whole `mm` control plane for you.

```bash
# 1. Clone this repository and enter it.
git clone <this-repo> && cd middle-manager

# 2. Bootstrap the local environment (idempotent, safe to rerun).
#    Verifies prerequisites and provisions the Python env with uv.
./bin/install

# 3. Enter Middle Manager and just talk to it.
./bin/mm start                         # launches your manager harness
./bin/mm start --provider claude       # or enter as a specific harness
./bin/mm start "add a health endpoint to my-service"   # hand it an objective now
```

That's the entire CLI surface **you** touch: `./bin/install` and `./bin/mm start`.
Everything after that is a conversation.

**First run (no config yet)** — that's fine. The manager checks the environment
itself, then walks you through picking a manager profile and a hard worker ceiling
*in conversation*, writes the config for you (which also provisions the pinned
companion tools into `.mm/tools/`), and asks what you'd like to accomplish. If you
haven't configured anything, `mm start` picks a detected, authenticated harness so
it can bootstrap you.

**After that**, you just delegate. Point it at a repo and describe outcomes; the
manager registers the repository, dispatches the smallest capable worker, answers
routine questions from durable decisions, reviews the exact diff, and opens a
reviewed-at-head pull request — surfacing only the decisions that genuinely need
you.

### Your first session (what it feels like)

```text
you    ▸ ./bin/mm start
mgr    ◂ Morning, boss. Everything's green and I'm ready. What are we shipping?
you    ▸ add a /healthz endpoint to github.com/me/my-service and open a PR
mgr    ◂ Consider it handled. Registering the repo and putting someone on it.
mgr    ◂ Shipped — PR is up: reviewed the diff myself, returns 200 with a JSON
         body, no auth. Want me to add a smoke test while I'm in there?
```

You never see the worktrees, the supervisor, the model routing, or a single `mm`
command unless you ask. The manager's full runtime operating contract (including
its voice) is [`docs/manager-runtime.md`](docs/manager-runtime.md).

### What the manager runs for you (reference)

You don't type these — the manager does — but the control plane is a plain CLI,
so everything is inspectable and scriptable if you want it:

```bash
mm doctor                              # harnesses, capability-matrix drift, schema
mm setup --non-interactive --manager-provider claude --worker-provider codex
mm tools install                       # download/verify pinned companions (setup does this)
mm tools status                        # treehouse / lavish-axi / gh-axi availability
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
