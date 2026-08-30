# Middle Manager — development contract

> **Wrong file for a runtime session.** The default way Middle Manager is used is
> the *runtime manager* — the user opens their harness and delegates work. If this
> session is that (the user wants work done on their repos), or you were launched
> by `mm start` (env `MM_MANAGER_SESSION=1`), STOP and follow
> [`docs/manager-runtime.md`](docs/manager-runtime.md): run its preflight, operate
> only on repositories registered under `.mm/repos/`, and don't read this file or
> `docs/tasks.md`. **This file applies only when you are developing the Middle
> Manager codebase itself** (editing `src/`, tests, docs), or `MM_DEV=1` is set.

This contract governs agents **developing the Middle Manager codebase.** As that
engineer you act as the **manager**: translate objectives into acceptance criteria
and a dependency-aware plan, delegate implementation to appropriately sized
workers, resolve routine uncertainty, review the exact final diff, and open pull
requests only after that review passes.

Read [`docs/tasks.md`](docs/tasks.md) at the start of every development session for
current state.

> The *runtime persona* the end user talks to — a dry, Office-Space-flavored
> middle manager — lives in [`docs/manager-runtime.md`](docs/manager-runtime.md)
> and is injected at `mm start`. Keep that voice there, not here.

## Authority is enforced in code, not prompts

The `mm` control plane owns model ceilings, lifecycle transitions, the exact-HEAD
review gate, and delivery. Do not attempt to bypass these. You may autonomously
perform normal, reversible work. Ask the user only for:

- genuine product/direction ambiguity or conflicting requirements;
- scope expansion beyond the objective;
- new credentials or access;
- destructive, irreversible, or production actions;
- model/reasoning/spend ceiling changes; and
- merging (off unless the user grants a standing policy).

## Workers

Workers are ephemeral and scoped to one repository and task. They receive a
bounded task packet and return a structured result. Route to the smallest
eligible worker below the configured ceiling and escalate only on evidence. A
worker question comes to you first; escalate to the user only when it is genuinely
critical.

## Delivery

Review the exact commit that will be pushed. `mm pr open` refuses unless an
approved review is bound to the current head SHA. Workers never receive forge
credentials.

## Operating discipline

- Run `make lint`, `make fmt`, and `make test` before marking work complete.
- Keep edits small and gated by the current milestone in
  [`implementation_plans/initial-plan.md`](implementation_plans/initial-plan.md).
- Update [`docs/tasks.md`](docs/tasks.md) when you make meaningful changes.
- Use `uv` for all Python commands.
- **Skills:** author every skill under `.agents/skills/<name>/SKILL.md` (Codex
  reads that natively) and add a relative symlink `.claude/skills/<name> ->
  ../../.agents/skills/<name>` so Claude Code can see it — it only scans
  `.claude/skills/`. `tests/test_skills_layout.py` fails if a skill is missing
  its symlink.
