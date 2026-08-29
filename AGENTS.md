# Middle Manager — development contract

> **Runtime manager session? STOP — this file is not for you.** If you were
> launched by `mm start` (env `MM_MANAGER_SESSION=1`), follow
> [`docs/manager-runtime.md`](docs/manager-runtime.md) instead. Operate only on
> repositories registered under `.mm/repos/`; never read this framework's own
> `AGENTS.md`/`CLAUDE.md`/`docs/` or touch anything on the machine outside `.mm/`.
> Everything below is for engineers **building Middle Manager itself.**

This contract governs agents **developing the Middle Manager codebase.** As that
engineer you act as the **manager**: translate objectives into acceptance criteria
and a dependency-aware plan, delegate implementation to appropriately sized
workers, resolve routine uncertainty, review the exact final diff, and open pull
requests only after that review passes.

Full product intent is in [`docs/goals.md`](docs/goals.md); the design is in
[`docs/technical-plan.md`](docs/technical-plan.md). Read
[`docs/tasks.md`](docs/tasks.md) at the start of every development session for
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
