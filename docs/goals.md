# Middle Manager goals

## Purpose

Middle Manager is a lightweight, local framework for directing autonomous software work across one or more Git repositories.

Its purpose is to let a user work through one trusted, high-capability manager agent while smaller, less expensive agents perform bounded repository-specific tasks. The manager remains responsible for understanding the objective, coordinating workstreams, resolving routine uncertainty, preserving prior decisions, reviewing the finished work, and delivering reviewed pull requests.

The framework should make multi-agent development feel like delegating to a capable engineering organization—not operating a collection of terminals.

## User promise

A user should be able to provide a meaningful objective and then step away. Middle Manager should continue until the objective is complete or a genuinely critical decision requires the user.

The user should not need to:

- repeatedly relay messages between agents;
- monitor workers for completion;
- restate decisions whose relevant context has not changed;
- choose a model for every task;
- manually coordinate related changes across repositories;
- inspect every intermediate implementation step;
- recover agent state after a dropped connection; or
- remember which commit was actually reviewed before a pull request was opened.

## Primary goals

### 1. Provide one accountable manager

The configured manager is the user's sole liaison and the final reasoning authority for a run. It should:

- translate an objective into explicit acceptance criteria and a dependency-aware plan;
- identify the repositories and workstreams involved;
- delegate implementation rather than becoming the default implementation worker;
- keep repository teams aligned with the objective;
- answer worker questions whenever existing decisions or repository facts are sufficient;
- coordinate contracts and delivery order across repositories;
- review the exact final changes and supporting evidence; and
- open pull requests only after that review passes.

### 2. Minimize model cost without sacrificing completion

Model economics should be a first-class product feature.

During setup, the user chooses the manager provider, model, and reasoning level, then chooses a worker provider and maximum worker model and reasoning level. The manager should select the smallest eligible worker profile likely to complete each task and escalate only when evidence shows that a stronger profile is needed.

The runtime must enforce the configured ceiling. It must never silently use the manager's expensive model as a worker or exceed the user's worker policy.

The goal is minimum expected cost for a correct result, not merely the cheapest individual model call.

### 3. Support Claude and Codex as peers

Claude Code and Codex should both be able to act as the manager or as repository workers, including mixed-provider runs.

Provider-specific CLI flags, event streams, permissions, session identifiers, interruption behavior, and resume behavior should remain inside adapters. The rest of the framework should operate on one provider-neutral task, question, result, usage, and lifecycle protocol.

Setup and reconfiguration should offer only harnesses that are installed and usable on the user's machine, with clear repair guidance for unavailable choices.

### 4. Make multi-repository work coherent

Middle Manager should treat a multi-repository objective as one coordinated run rather than several unrelated agent sessions.

It should understand dependency edges, shared contracts, rollout order, and linked pull requests. Workers should remain isolated to their assigned repository and task, while the manager keeps the broader product outcome coherent.

### 5. Be autonomous within explicit authority

The manager should autonomously perform normal, reversible software-development work within the objective and configured authority. This includes inspection, decomposition, worktree creation, delegation, testing, bounded retries, rework, commits, and opening an approved pull request.

It should interrupt the user only for matters such as:

- genuine product or direction uncertainty;
- conflicting requirements;
- material scope expansion;
- credentials or new access;
- destructive, irreversible, or production actions;
- changes to model, reasoning, retry, or spend ceilings; and
- merging, unless the user later grants an explicit standing merge policy.

Prompt instructions alone are not sufficient protection. Authority, model ceilings, lifecycle transitions, and delivery gates should be enforced by deterministic code.

### 6. Preserve decisions without making them timeless

When the user resolves an escalation, Middle Manager should record the decision, its scope, rationale, provenance, inputs, and validity conditions. Equivalent questions should reuse a still-valid decision instead of interrupting the user again.

A one-off decision must not silently become a permanent preference. Before reuse, the manager should consider whether relevant requirements, repository contracts, dependent decisions, authority, or time-sensitive facts have materially changed. If they have, it should explain the changed premise and ask only for the necessary delta.

Corrections should supersede earlier records without erasing history.

### 7. Survive interruptions and remain steerable

The manager should be able to steer workers while work is in progress. Routine guidance should be delivered at a safe checkpoint; urgent guidance should use controlled interruption, worktree reconciliation, and a tested resume or recovery path.

A dropped manager connection must not stop workers. A supervisor restart should not lose live task events. A machine restart should preserve enough durable state to reconstruct obligations safely.

The framework must never infer that a task succeeded merely because a process disappeared. When exact provider-session continuation cannot be proven safe, it should start a recovery worker with a compact task packet, worktree diff, checkpoint, and open obligations.

### 8. Make state durable, inspectable, and token-efficient

SQLite and append-only run artifacts should be the source of truth for repositories, tasks, dependencies, decisions, sessions, events, usage, reviews, and delivery state.

Model context should contain compact task packets, relevant decisions, normalized results, and actionable events—not full transcripts or unbounded command output. Durable state should help the system recover and learn how the user works without turning every past conversation into prompt baggage.

Users should be able to inspect, correct, supersede, archive, or forget stored knowledge.

### 9. Make complex review collaborative

For plans, architecture, comparisons, reports, and connected decisions, the manager should be able to create a Lavish HTML review surface.

Annotations, selected-text comments, document chat, attachments, layout feedback, and whiteboard edits should return to the manager as durable feedback events. Applied feedback should link to the resulting decision or task change. The manager should continue the document loop until the user ends it, then consume the final feedback exactly once.

Lavish should improve comprehension when visual structure helps, not become ceremony for trivial updates.

### 10. Keep the framework lightweight and local

Middle Manager should remain understandable and operable from a local repository. Its core should be a small control plane with minimal runtime dependencies, a clear README, an idempotent installer, and an agent-facing setup/repair skill.

Treehouse, `gh-axi`, and `lavish-axi` should be reproducible managed dependencies with pinned versions, integrity metadata, retained notices, and explicit upgrades. Their source trees should not be copied into this repository.

Tmux may be offered later as an observability convenience, but it should not be required for identity, messaging, recovery, or lifecycle state.

## Definition of success

Middle Manager succeeds when a new user can:

1. install it and configure available Claude/Codex harnesses without hand-editing files;
2. choose an expensive manager profile and a hard worker ceiling;
3. register one or more repositories and provide a plain-language objective;
4. observe the manager delegate work to appropriately sized workers;
5. leave the run unattended through normal implementation, testing, rework, and cross-repository coordination;
6. answer only genuinely critical questions;
7. have those answers reused while their context remains valid;
8. steer active work or recover from a dropped connection without losing task history;
9. review complex material through Lavish when useful; and
10. receive manager-reviewed pull requests whose approvals match their exact head commits.

The framework should also make its behavior auditable: the user can see which model was selected, why it escalated, what decisions were reused, what evidence was reviewed, how much usage was incurred, and which authority enabled each delivery action.

## Non-goals for v1

Middle Manager v1 is not intended to be:

- a general-purpose multi-agent social or messaging platform;
- a replacement for GitHub, Claude Code, Codex, Treehouse, `gh-axi`, or Lavish;
- a hosted multi-user service;
- a custom desktop application or chat TUI;
- a Windows product;
- a multi-forge abstraction;
- a system for autonomous production operations;
- a fleet of persistent repository-lead personalities;
- dependent on tmux or another terminal multiplexer;
- an automatic merge bot; or
- a system that learns hidden user preferences from incidental behavior.

These boundaries preserve the central goal: a small, reliable management layer for economical, autonomous, reviewable software delivery across repositories.

## Product principles

When goals compete, use these priorities:

1. **Correctness and user authority over speed.**
2. **A completed objective over local worker activity.**
3. **The smallest capable worker over the cheapest isolated call.**
4. **Durable evidence over conversational memory.**
5. **Explicit scoped decisions over inferred preferences.**
6. **Safe recovery over pretending continuity.**
7. **Manager accountability over worker autonomy.**
8. **A lightweight, inspectable system over feature breadth.**
