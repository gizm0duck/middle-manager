# Middle Manager technical plan

The product purpose, success criteria, and non-goals are defined in [`goals.md`](goals.md). This document describes how to realize them.

## Product promise

Middle Manager is a local, single-user agent command center for work across multiple Git repositories. The user talks to one high-capability manager. The manager decomposes work, routes bounded tasks to lower-cost repository workers, resolves routine uncertainty, coordinates dependencies across repositories, reviews the exact final diff, and opens a pull request only after that review passes.

The user should be interrupted only for decisions that materially change product direction, authority, security, cost ceilings, or irreversible outcomes.

## Recommended v1

Build an agent-home repository plus a small Python control-plane CLI named `mm`, with Treehouse, `gh-axi`, and Lavish as pinned, managed companion tools.

- `mm setup` first probes installed and authenticated Claude Code and Codex harnesses. It offers only usable harnesses, explains unavailable choices, and provides exact install/login guidance.
- It then asks for the manager provider, model, and reasoning effort from the detected choices.
- It then asks for the worker provider, maximum worker model, and maximum worker reasoning effort.
- The manager selects the smallest eligible worker model and reasoning level for each task. The runtime enforces the configured ceiling.
- The same settings remain easy to change later through `mm config`, including focused `mm config models` and `mm config authority` flows.
- `mm start` launches either Claude Code or Codex as the interactive manager with the configured manager profile.
- Workers run non-interactively through provider adapters in isolated Treehouse-leased Git worktrees.
- A single local supervisor process records events and keeps worker jobs independent of the manager's current turn.
- Claude and Codex project hooks call the same provider-neutral lifecycle commands, so an active run continues without model-powered polling.
- SQLite stores canonical lifecycle state; task packets, worker results, review bundles, and event streams remain inspectable files.
- For plans, architecture, comparisons, release reviews, and other decisions that benefit from a visual surface, the manager creates a Lavish HTML artifact and stays in its feedback loop until the user sends and ends the session. Annotations, document chat, selected text, and whiteboard edits become durable run events and decisions.
- GitHub is the only forge in v1, and bundled `gh-axi` is the manager-facing GitHub interface. The manager may push and open a PR after review, but never merge without explicit user authority.

Use Python 3.12+ and the standard library for the control plane (`argparse`, `asyncio`, `sqlite3`, `subprocess`, `tomllib`, and `json`). Keep Python runtime package dependencies at zero. Treehouse, `gh-axi`, and Lavish are explicit, version-pinned companion CLIs rather than hidden library dependencies. Use Ruff and pytest only for development.

## Architecture

The system has five layers:

1. **Manager agent** — the only user-facing intelligence. It plans, delegates, resolves worker questions, coordinates repositories, reviews work, and decides whether evidence is sufficient.
2. **Control plane (`mm`)** — deterministic policy enforcement. It owns model ceilings, Treehouse lease lifecycle, process lifecycle, task state, structured events, usage accounting, and the exact-HEAD review gate.
3. **Provider adapters** — Claude Code and Codex command construction, capability probing, streaming-event normalization, usage extraction, resume behavior, and interruption.
4. **Managed tool adapters** — Treehouse provides reusable, durably leased worktrees; `gh-axi` provides token-efficient GitHub operations; Lavish provides local HTML review surfaces and a structured human-feedback channel. Their versions and capabilities are probed just like model harnesses.
5. **Repository workspaces** — read-only base clones plus isolated task worktrees. Workers may edit, test, and commit only in their assigned worktree. They may not push, open PRs, merge, or operate on other repositories.

Avoid a persistent repository-lead agent in v1. Repository teams should be declarative roles (`scout`, `implementer`, `tester`, and `reviewer`) backed by ephemeral sessions. Durable repository knowledge belongs in scoped state, not in a long-running model conversation.

### Worker communication and supervision

Do not use tmux as the worker transport in v1. Tmux is valuable in Firstmate because it standardizes interactive, long-lived shell sessions across a broader fleet. Middle Manager has a narrower contract: Claude and Codex workers both expose non-interactive structured-output modes and explicit session resume identifiers, so an extra terminal protocol would add state and parsing without improving the normal path.

Run one repo-local `mm supervisor` daemon plus a small `mm runner` guardian for each active task. The CLI talks to the supervisor through a permission-restricted Unix domain socket under `.mm/run/`; each runner owns its provider child process group, captures stdout/stderr and provider JSON events, writes an append-only spool, and maintains its own reconnectable control socket. The supervisor normalizes those events into SQLite. Commands such as dispatch, steer, answer, interrupt, and inspect are idempotent requests carrying run/task identity and an operation ID. Workers do not talk directly to one another or to the manager process. A blocked worker returns a structured question; the manager answers it through the control plane, which resumes that worker's exact provider session.

After dispatching available tasks, the manager calls `mm wait --run <id> --until-actionable`. This is a completion-aware blocking subscription over the supervisor socket, not model-powered polling. It returns a compact event batch when a worker asks a question, finishes, fails, exhausts budget, or unlocks dependent work. The manager handles the transition and waits again until the run is terminal. If the interactive manager or wait call dies, workers continue under the supervisor; the next SessionStart hook reconstructs active obligations from disk and reconnects.

An optional later `mm observe --tmux` integration may open human-friendly panes for logs and worktrees, but tmux must never own task identity, message delivery, or lifecycle state. The system should work unchanged in a headless shell with no tmux installed.

### In-flight steering and dropped-connection recovery

The manager can steer an active task through `mm task steer <task-id> --message <guidance>` with two modes:

- **Checkpoint steering (default):** persist the guidance immediately and deliver it after the current provider tool/action boundary. The worker continues in the same provider session with the new constraint, and the result records which checkpoint consumed it.
- **Interrupt steering:** for urgent corrections, request a controlled interrupt of the provider process group, wait for the current tool subprocess to exit when possible, reconcile the worktree, and resume the exact provider session with the guidance. If an adapter cannot prove that an interrupted session is resumable, start a fresh worker with a compact recovery packet instead of pretending continuity.

Priorities, cancellation, acceptance criteria, dependencies, and task scope are control-plane state, so the manager can also pause, cancel, split, or replace work without depending on provider chat semantics. Every steering command is versioned against the task contract; a late command cannot overwrite a newer one, and a worker result based on an obsolete contract version is routed to rework rather than accepted.

Recovery is layered:

1. **Manager or `mm wait` disconnect:** the supervisor and task runners continue. Reconnecting returns events after the caller's last acknowledged sequence number.
2. **Supervisor restart:** live task runners keep provider pipes and spools. The new supervisor discovers their sockets, verifies task/run identity and process start tokens, replays unacknowledged events, and resumes supervision.
3. **Runner or provider CLI failure:** inspect the persisted provider session ID, task contract version, worktree Git status/diff, last completed event, and validation state. Resume the exact session only when the adapter's compatibility test says it is safe; otherwise dispatch a recovery worker with those compact facts.
4. **Network loss:** classify provider transport failures as retryable, apply bounded backoff without spending model turns, and resume from the same durable worktree/session when connectivity returns.
5. **Machine restart:** no process survival is assumed. `mm reconcile` reconstructs active obligations from SQLite and run spools, verifies every worktree before acting, and asks the manager to resume or replace incomplete sessions. It never marks a task complete from a missing process alone.

The adapter test suite must verify normal resume, interruption during a model turn, interruption during a tool command, duplicate command delivery, event replay, and CLI-version incompatibility. Until a provider/version passes those tests, its capability record should expose only checkpoint-at-completion steering and fresh-session recovery, not claim mid-flight continuation.

## Setup and model policy

The first-run setup is a core product experience, not a config-file exercise. It begins with environment discovery: executable path, CLI version, authentication state, and supported model/effort capabilities for Claude Code and Codex. Uninstalled or unauthenticated harnesses remain visible as unavailable explanations, but cannot be selected until their probe passes.

It should collect:

1. Which detected, authenticated harness should run the manager.
2. Manager model and reasoning effort supported by that harness.
3. Which detected, authenticated harness should run workers.
4. Maximum worker model.
5. Maximum worker reasoning effort.
6. Default cost posture: lean, balanced, or quality-biased. Lean is recommended.
7. Standing repository authority: clone/fetch, create worktrees, commit, push branches, and open reviewed PRs. Merge remains off by default.
8. Managed tool location and update policy for Treehouse, `gh-axi`, and Lavish.

The generated worker ladder is an ordered set of provider-specific model/effort profiles from smallest to the configured ceiling. Every dispatch records the chosen profile and a short reason.

Routing is capability-minimizing:

1. Classify the task's shape, risk, ambiguity, repository breadth, and required tools.
2. Choose the smallest profile expected to meet the task contract.
3. Start at low reasoning for deterministic inspection and narrow edits; increase reasoning only for ambiguity, architectural coupling, or failed evidence.
4. Escalate only after a concrete mismatch: repeated failure, unresolved ambiguity, inadequate test diagnosis, or a task that cannot be safely decomposed further.
5. Never exceed the worker provider/model/reasoning ceiling. If the task genuinely requires more, split it or ask the user to change the ceiling.
6. Never silently substitute the manager's expensive model for a worker.

Provider catalogs change. Each adapter should probe installed CLI capabilities during `mm setup`, `mm config models`, and `mm doctor`, persist the detected CLI version, validate configured flags, and use a small versioned model ladder that the user can inspect and override. Unknown models should require explicit ranking rather than an assumed cost/capability position. If a configured harness disappears or loses authentication, `mm start` fails with a focused repair path rather than silently switching providers.

## Bundled companion tools

“Bundled” should mean reproducible and locally managed, not three copied source trees living inside Middle Manager.

- Commit a `tools.lock` file containing the supported Treehouse, `gh-axi`, and `lavish-axi` versions, source coordinates, platform artifacts, and checksums.
- During `mm setup`, provision each tool into the gitignored `.mm/tools/<tool>/<version>/` directory and verify the artifact before activation.
- Install Treehouse from its platform-specific release binary. Its Go implementation and lease-oriented JSON interface make it a clean standalone dependency.
- Install `gh-axi` as an isolated pinned npm package, using the user's installed Node runtime and authenticated official `gh` CLI. Do not require a global npm install.
- Install `lavish-axi` as an isolated pinned npm package. Its current package requires Node 22+, so `bin/install` must validate the version once and use the same local Node package prefix for both npm companions.
- Commit third-party notices and upstream MIT license texts. `mm tools status` reports active, bundled, and available versions; `mm tools update` performs an explicit lock-file-approved upgrade.
- Allow an advanced path override for development, but default to the managed copies so behavior does not depend on an unrelated global version.

Treehouse integration should use its automation-safe lease contract. For each task, the control plane runs `treehouse get --lease --lease-holder <run:task> --json`, records `path`, `lease_id`, `lease_holder`, and base branch, creates the task branch in that leased worktree, and releases it only through an identity-checked `treehouse return` after landed-work safety is proven. Configure a Middle Manager-owned Treehouse root by default so no global user configuration is mutated; allow opting into an existing Treehouse root later.

`gh-axi` should be the GitHub tool described in manager and worker instructions because its compact output saves tokens and its errors include agent-oriented recovery guidance. Workers still receive no forge credentials. Only the control plane invokes GitHub mutations after policy gates pass. Because upstream `gh-axi` explicitly optimizes output for agent comprehension rather than a stable imperative parser, safety-critical postconditions should be verified through structured official `gh` JSON/API reads instead of brittle parsing of agent-oriented output.

### Lavish document and feedback loop

Lavish is the manager's review surface when relationships, tradeoffs, layout, or several related decisions would be materially harder to evaluate in terminal prose. It is not mandatory for trivial status updates or one-line questions.

The manager follows a stateful protocol:

1. Run `lavish-axi design` and every applicable playbook before producing the HTML artifact.
2. Write the portable source to `.mm/runs/<run-id>/artifacts/<artifact-id>.html`, open it through the managed Lavish binary, and store the canonical path and session state.
3. Start `lavish-axi poll <file>` only as an attached foreground operation or a supervisor-owned completion-aware job with a verified manager wake path. Never use detached fire-and-forget polling. A stopped poll can be restarted because queued feedback remains available.
4. Normalize returned annotations, selected-text comments, document chat, layout findings, attachments, and whiteboard summaries into `artifact_feedback` events. Read bounded summaries first and only load large DOM snapshots, images, or Excalidraw files when needed.
5. Apply the user's feedback, record any resulting decision with provenance back to the artifact and feedback event, update the HTML, send a concise `--agent-reply`, and poll again.
6. Treat “Send & End” as the terminal review event. Consume its final feedback exactly once and do not reopen a user-ended session unless the user explicitly asks.

The control plane should expose small wrappers such as `mm artifact open`, `mm artifact poll`, `mm artifact reply`, `mm artifact status`, and `mm artifact end`. The wrappers use the bundled CLI but also write session lifecycle, feedback application, and decision links to SQLite. The manager contract should explicitly recognize artifact chat and comments as user input subject to the same authority boundaries as direct chat: product direction is durable and authoritative, while destructive actions, new credentials, and scope expansion still require the normal explicit checks.

## Installation and getting started

Ship both a deterministic installer and an agent-facing setup skill.

`bin/install` is the canonical macOS/Linux bootstrap. It should be small, idempotent, and safe to rerun. It verifies Python 3.12+, Git, Node 22+, and the official `gh` CLI; creates the local Python environment and `mm` entry point; provisions the pinned Treehouse, `gh-axi`, and Lavish copies; verifies checksums and licenses; and then launches or points to `mm setup`. It never installs or logs into a model provider without explicit user consent.

`.agents/skills/setup-middle-manager/SKILL.md` is a shared setup/repair skill visible to both Codex and Claude. A thin `.claude/skills` compatibility link can expose the same skill without duplicating instructions. The skill runs structured `mm doctor` and `mm setup --resume` commands, interprets missing requirements, explains exactly what will be installed or changed, asks for approval only when required, and hands authentication steps back to the user. Deterministic install/version/auth probes stay in `mm`; the skill owns guidance and judgment, not shell mechanics.

The root `README.md` should make the happy path obvious above the fold:

1. Requirements and supported platforms.
2. Clone the repository.
3. Run `./bin/install`.
4. Run `mm setup` and choose among detected, authenticated Claude/Codex harnesses.
5. Add a repository with `mm repo add <url-or-owner/repo>`.
6. Start the configured manager with `mm start`.
7. Give it a real objective in plain language.

The README should also include: what the installer puts under `.mm/`; the manager-versus-worker model policy; the autonomy and merge boundary; one same-provider and one cross-provider example; how to inspect progress; how to resume after restart; `mm config` examples; `mm tools status/update`; `mm doctor`; how a Lavish review opens, how feedback returns to the manager, and how to resume or end one; memory inspection/forget commands; uninstall/cleanup; and links to deeper architecture, provider, artifact, and safety docs. A user should not need to read `AGENTS.md` to learn how to operate the product.

## Claude and Codex adapter contract

Both adapters implement the same operations: `probe`, `start`, `resume`, `steer`, `interrupt`, `reconcile`, `parse_event`, `parse_usage`, and `result`.

Codex workers use non-interactive JSONL execution, structured output schemas, explicit workspace-write sandboxing, model selection, and `model_reasoning_effort`. The adapter persists the thread ID and resumes with the specific thread rather than relying on “last session.”

Claude workers use print mode with stream JSON, a JSON result schema, explicit model and effort flags, automatic/controlled permissions, and a persisted session ID for follow-ups.

Do not use either provider's dangerous permission-bypass mode by default. Workers should inherit only the repository worktree and intentionally configured environment. Forge credentials stay unavailable to workers.

Tracked Claude and Codex Stop/SessionStart hooks should call provider-neutral `mm hook stop` and `mm hook session-start` commands. The hooks report active work, request another manager turn only when an actionable event exists, and reconstruct a compact active-run summary after restart or compaction.

## Task packets and question flow

Every worker receives a bounded task packet containing:

- objective and task role;
- acceptance criteria and explicit non-goals;
- assigned repository, base SHA, branch, and worktree;
- relevant user decisions and repository conventions;
- dependencies on other tasks or repositories;
- allowed tools and validation commands;
- provider/model/reasoning profile;
- question and completion schemas.

Workers return a structured result with status, summary, files changed, validations run, risks, assumptions, and open questions. Raw transcripts are retained for diagnosis but are never injected into the manager by default.

Worker questions always go to the manager. The manager follows this order:

1. Answer from the task contract, a still-valid prior user decision, or current repository knowledge.
2. If the choice is reversible and within the objective, choose a sensible default, record the assumption, and resume the worker.
3. Re-scope, split, or re-route the task when the worker is overloaded.
4. Ask the user only for genuine product/direction ambiguity, new authority, secrets or access, destructive/irreversible action, conflicting requirements, or a required worker ceiling increase.

Every user escalation produces a structured decision record rather than disappearing into chat. The record contains the question, answer, rationale when provided, scope (global, repository, product area, run, or task), provenance, author, timestamp, the facts and repository SHAs it relied on, explicit validity conditions, and whether the user marked it as standing policy or a one-off choice. The manager links the answer to the blocked task and resumes it without asking again.

Before reusing a prior decision, the control plane evaluates a compact context fingerprint. Relevant invalidators include changed requirements, changed dependent decisions, repository or contract changes in the files/SHAs named by the decision, elapsed time for time-sensitive facts, changed authority or model ceilings, and an explicit user correction. A still-valid matching decision is reused and cited in the event log. A materially changed fingerprint marks it `review_required`; the manager summarizes what changed and asks only for the delta instead of relitigating the entire history. Superseded records remain inspectable and linked to their replacement. One-off decisions never silently become global preferences.

## Lifecycle and autonomy

A run is a dependency graph of repository tasks:

`requested -> planned -> ready -> running -> worker_done -> manager_review -> approved -> PR_open -> complete`

Side paths are `blocked_manager`, `blocked_user`, `rework`, `retryable_failure`, and `failed`.

The manager may autonomously:

- clone and fetch registered repositories;
- inspect repository history and configuration;
- decompose and sequence work across repositories;
- acquire identity-bound Treehouse leases and create task branches inside them;
- select workers below the configured ceiling;
- run tests, retries, and targeted rework;
- make reversible implementation assumptions within the stated objective;
- commit worker changes;
- push task branches and open a PR after the review gate passes.

The manager must ask before:

- changing product direction or resolving genuinely ambiguous requirements;
- exceeding a configured provider/model/reasoning or spend ceiling;
- destructive data changes, force pushes, history rewrites, credential changes, or production actions;
- broadening scope beyond the user's objective;
- merging a PR, unless a later explicit standing merge policy is added.

## Review gate

The manager must review work before a PR is opened. A cheap reviewer can supply additional evidence, but it cannot replace the manager's review.

`mm review bundle` produces a compact bundle containing the objective, acceptance criteria, base/head SHAs, diff summary, full diff path, test evidence, dependency state, worker assumptions, questions, risks, and usage. The manager records `approved` or `changes_required` plus findings.

`mm pr open` refuses unless an approved review is bound to the current head SHA. Any new commit invalidates the approval. Workers never receive forge credentials and cannot bypass this gate.

For multi-repository work, the review also verifies API/contract compatibility, rollout order, linked PRs, and whether dependent changes are safe to open independently.

## Durable state and memory

Canonical state lives under a gitignored `.mm/` directory:

- `config.toml` — manager profile, worker ceiling, cost posture, and authority.
- `tools.lock` — active managed Treehouse, `gh-axi`, and `lavish-axi` versions and integrity metadata.
- `state.db` — repositories, runs, versioned task contracts, dependencies, scoped and versioned decisions, worker sessions, runner identities, acknowledged event sequences, artifact sessions, feedback applications, reviews, usage, and lifecycle events.
- `repos/` — base clones.
- `tools/` — gitignored, locally provisioned companion tools.
- `worktree-pools/` — Middle Manager-owned Treehouse root; task state records the exact lease identity and path.
- `runs/<run-id>/` — plan, task packets, normalized worker results, Lavish artifact sources, review bundles, and append-only event JSONL.
- `memory/` — inspectable exports of user preferences, scoped decisions, and repository facts.

Memory has provenance, scope, and validity. Store explicit user preferences and decisions, confirmed repository conventions, task outcomes, and corrections. A decision is not a timeless string: retain its inputs, context fingerprint, validity conditions, reuse history, and supersession chain. Do not convert incidental chat behavior or a one-off answer into durable preference automatically. Provide `mm memory list`, `mm memory show`, `mm memory forget`, `mm decision history`, and `mm decision review`.

Startup context is a bounded query, not a dump: active runs, unresolved or freshness-invalidated decisions, changed repository SHAs, and only the still-relevant memory records for the current repositories and objective.

## Token and cost efficiency

- Use deterministic Git, process, state, and validation commands instead of model calls.
- Wake the manager on state transitions, not polling intervals.
- Pass task packets and normalized results instead of conversation transcripts.
- Reuse a worker session only for bounded follow-up on the same task; start fresh after context becomes noisy.
- Persist usage from provider event streams by run, task, provider, model, and reasoning level.
- Cache repository facts and invalidate them by relevant file or base-SHA changes.
- Default to the smallest profile and log every escalation.
- Set optional per-run spend and retry budgets; pause rather than silently exceed them.

## Keep and omit from Firstmate

Keep the single user liaison, explicit authority, isolated worktrees, event-driven supervision, durable state, fail-closed teardown, and evidence-backed delivery.

Omit tmux as a required transport, multiple terminal backends, remote second managers, social relay, voice, Middle Manager self-update machinery, multi-forge support, arbitrary harnesses, and a large shell-script protocol surface. One Python supervisor, one Unix-socket control protocol, two provider adapters, three pinned companion tools, and GitHub-only delivery are enough for v1.

## Suggested repository shape

- `AGENTS.md` — concise manager operating contract.
- `CLAUDE.md` — imports or points to the same contract.
- `README.md` — five-minute install, first repo, first objective, Lavish review loop, reconfiguration, status/recovery, and safety boundaries.
- `.claude/settings.json` and `.codex/hooks.json` — thin lifecycle hooks.
- `.agents/skills/setup-middle-manager/SKILL.md` — shared first-run and repair guidance for either manager harness.
- `bin/install` — idempotent macOS/Linux bootstrap into the repo-local environment.
- `pyproject.toml` — package and `mm` entry point.
- `tools.lock` and `THIRD_PARTY_NOTICES.md` — pinned companion tools, checksums, and license attribution.
- `src/middle_manager/` — CLI, setup/config, state, supervisor, task runner, router, Treehouse, GitHub/`gh-axi`, Lavish artifacts, review, memory, and provider adapters.
- `schemas/` — task, worker-result, review, and event JSON schemas.
- `templates/` — manager and worker prompts.
- `tests/` — unit tests plus fake-provider integration tests and opt-in live adapter tests.
- `docs/` — architecture, setup, provider support, and safety model.

## Delivery phases

### Phase 0 — provider capability probes (done)

Before the kernel, measure the installed Claude/Codex CLIs, since interrupted
provider turns and half-alive processes are the largest technical uncertainty.
The `middle_manager.probes` harness spawns each CLI in isolated git fixtures,
applies SIGINT/SIGKILL at model-turn and tool-command boundaries, attempts resume
by explicit session id, and records a fail-closed capability matrix
(`docs/provider-capabilities.md`, schema in
`schemas/provider-capability.schema.json`). Adapters (Phase 2) may claim
mid-flight interrupt steering only for a provider/version whose matching probe
flags are true; otherwise they fall back to checkpoint-at-completion steering
plus a fresh-session recovery packet. Re-probe on any CLI version change.

### Phase 1 — kernel

Package skeleton, idempotent installer, shared setup skill, task-oriented README, installed/authenticated harness discovery, reconfiguration flows, managed Treehouse/`gh-axi`/Lavish provisioning, config validation, repo add/sync, SQLite state, supervisor/socket protocol, runner guardian, Treehouse lease lifecycle, and one fake provider. Acceptance: a new user can install, configure, add a fixture repo, open a feedback-capable artifact, and create/reconcile a run without hand-editing files.

### Phase 2 — real workers

Codex and Claude adapters, capability probes, JSON event normalization, usage accounting, task/result schemas, model ceiling enforcement, steering compatibility tests, and concurrent dependency-aware dispatch. Acceptance: manager on either provider can delegate to the other provider, steer it at a safe checkpoint, and resume a blocked worker.

### Phase 3 — autonomous completion

Supervisor, lifecycle hooks, manager question routing, bounded retry/escalation, durable Lavish feedback loop, exact-HEAD review gate, commit/push/PR flow, and cross-repo dependency checks. Acceptance: a two-repository change reaches reviewed PRs without user input unless a seeded critical decision is encountered, and a seeded artifact comment is applied and acknowledged before completion.

### Phase 4 — hardening and dogfood

Crash recovery, socket disconnect and event-replay tests, CLI drift tests, fake provider fixtures, end-to-end tests, usage reports, memory controls, doctor command, and concise documentation. Acceptance: killing and restarting the manager or supervisor does not lose obligations, live runner events, or open decisions.

## Delivery estimate

Assuming one experienced engineer working with coding agents, no Windows or custom TUI, and access to authenticated Claude, Codex, and GitHub test accounts:

- **First working demo: 3–5 working days.** Installer skeleton, detected model setup, one repository, one provider worker, Treehouse lease, durable task state, and a manual manager review path.
- **Usable end-to-end alpha: 10–15 working days.** Both providers, mixed-provider delegation, model ceilings and routing, supervisor/runner communication, multi-repository DAGs, `gh-axi` PR delivery, Lavish feedback, and the normal restart path.
- **Hardened v1: 20–30 working days, or roughly 4–6 calendar weeks.** Safe in-flight steering, provider-version compatibility fixtures, socket/event replay, process and machine-restart reconciliation, decision invalidation, supply-chain verification, failure injection, live integration tests, and polished documentation.

Approximate effort by phase is 4–6 days for the kernel, 5–8 for real provider workers, 5–7 for autonomous delivery, and 6–9 for hardening. Some documentation and test work overlaps implementation, but the recovery/steering matrix should not be compressed: interrupted provider turns and half-alive processes are the largest technical uncertainty. Review the estimate after the first single-provider vertical slice, when the installed Claude/Codex versions have passed real resume and interruption probes.

## Acceptance tests

1. Fresh setup detects installed and authenticated harnesses, offers only viable choices, and asks for manager provider/model/reasoning and worker provider/max-model/max-reasoning before any task can run.
2. `mm config models` reruns discovery and changes either profile without hand-editing configuration.
3. Setup provisions pinned Treehouse, `gh-axi`, and `lavish-axi` copies, verifies integrity, and uses them even when conflicting global versions exist.
4. `bin/install` is safe to rerun after a partial failure, and the setup skill resumes from the first incomplete step without reinstalling healthy tools.
5. A README-only user can reach a first managed task and knows how to reconfigure or diagnose it.
6. Every task records a Treehouse lease ID and holder; stale callers cannot release a later acquisition of the same worktree.
7. OpenAI manager with Claude workers and Claude manager with OpenAI workers both complete a fixture task.
8. No dispatch can exceed the configured worker ceiling, including retries and manual manager commands.
9. A simple task selects the smallest profile; a seeded failure escalates one step with a recorded reason.
10. A worker question is answered by the manager when prior decisions cover it and reaches the user only when classified critical.
11. Repeating an equivalent question under the same context reuses the prior user decision without interruption; changing a named requirement or relevant repository contract marks it for review and asks the user only about the changed premise.
12. A one-off answer remains task/run-scoped unless the user explicitly promotes it, and a corrected answer supersedes rather than destroys its predecessor.
13. Manager restart reconstructs active state without loading raw transcripts.
14. `mm wait --until-actionable` wakes the manager on a worker question or terminal transition without periodic model calls, and a disconnected wait never stops or duplicates a worker.
15. Checkpoint steering reaches the same worker session exactly once; urgent steering interrupts safely, reconciles the worktree, and either resumes a proven-compatible session or creates a compact recovery handoff.
16. Dropping the manager socket and restarting the supervisor does not lose the provider pipe: the task runner spools events, reconnects, and replays only unacknowledged sequence numbers.
17. A machine-restart fixture reconstructs an incomplete task from durable state and never treats a missing process as successful completion.
18. A cross-repository contract change respects dependency order and produces linked review evidence.
19. PR creation is refused before manager approval and after the approved head SHA changes; the live PR state is verified after `gh-axi` returns.
20. A Lavish review survives manager or poll restart, returns annotations and document chat as durable feedback events, records the applied decision, and consumes “Send & End” exactly once without reopening the session.
21. The full fixture run works without tmux installed; enabling an optional tmux observer does not alter persisted lifecycle state.
22. Usage and escalation history can be inspected by run and task.

## Main risks

- **CLI drift:** isolate every provider flag and event parser in adapter contract tests; store detected versions.
- **Companion-tool drift and supply chain:** pin Treehouse, `gh-axi`, and `lavish-axi`, verify artifacts, retain notices, and make upgrades explicit and reversible.
- **False cheapness:** a too-small model can cost more through retries. Route by expected total cost, not price per call alone.
- **Manager turn continuity:** use the same state commands behind both providers' hooks and keep state authoritative when hooks fail.
- **Half-alive workers:** give each task a runner guardian, process start token, heartbeat, append-only event sequence, and explicit reconciliation state; never infer success from a disconnected pipe.
- **Artifact feedback continuity:** keep polling attached to the supervisor's wake path, persist artifact lifecycle and normalized prompts, and make re-polling safe after interruption.
- **Runaway autonomy:** enforce authority and model ceilings in code, not prompts; set retry and spend budgets.
- **Stale memory:** scope facts, attach provenance and base SHAs, and archive invalidated records.
- **Decision overreach:** default answers to the narrowest justified scope, distinguish one-off choices from standing policies, and revalidate their context fingerprints before reuse.
- **Cross-repo races:** plan explicit dependency edges and bind reviews to exact SHAs.
- **Worker overconfidence:** require structured evidence and let only the manager approve delivery.

## Decisions resolved

The user accepted all seven recommended defaults on 2026-08-28. These are v1 product decisions, not timeless preferences: revisit an individual decision when its named premise materially changes and preserve any replacement as a superseding record.

1. **Worker pool:** one worker-provider ceiling in v1. The manager and workers may use different providers; independent simultaneous Claude/OpenAI worker ceilings are deferred.
2. **PR authority:** automatically push and open PRs after the configured manager approves the exact head. Merge still requires explicit user authority.
3. **Repository roles:** built-in `scout`, `implementer`, `tester`, and `reviewer` roles with per-repository overrides.
4. **Manager surface:** an agent-home repository launched through `mm start`; no custom chat/TUI in v1.
5. **Runtime:** Python 3.12+ on macOS and Linux; Windows is deferred.
6. **Companion tools:** pinned local provisioning for Treehouse, `gh-axi`, and `lavish-axi`, with sources, checksums, notices, and explicit upgrades; do not vendor their source trees or binaries into Git.
7. **Worker transport:** the supervisor/socket protocol with per-task runner guardians and `mm wait --until-actionable`; tmux is optional observability only.
