---
name: setup-middle-manager
description: >-
  Shared first-run setup and repair guidance for Middle Manager, visible to both
  Codex and Claude. Use when installing, configuring, diagnosing, or repairing a
  Middle Manager agent home. The deterministic install/version/auth probes live
  in `mm`; this skill owns guidance and judgment, not shell mechanics.
user-invocable: true
---

# setup-middle-manager

Middle Manager separates mechanics from judgment. The `mm` control plane owns the
deterministic install, version, and auth probes. This skill interprets their
output, explains exactly what will change, asks for approval when required, and
hands authentication steps back to the user.

## First run

1. Run `./bin/install`. It verifies Git, uv, Python 3.12+, and warns about Node
   and `gh`. It is idempotent and safe to rerun after a partial failure.
2. Run `mm doctor` (or `mm doctor --json`). Read the harness, requirement, and
   companion-tool sections.
3. If a harness is installed but unauthenticated, hand the exact login step to
   the user: `claude auth login` or `codex login`. Never authenticate on their
   behalf.
4. When at least one harness is authenticated, run `mm setup`. Offer only the
   harnesses `mm doctor` reports as usable. When Claude is usable, recommend
   Claude Opus as the manager and Claude Sonnet as the worker default under an
   Opus ceiling; with only Codex, recommend Codex. That is what `mm setup` writes
   when no provider is named, and workers stay on the manager's harness unless
   the user asks to mix. Setup collects the manager profile
   and the hard worker ceiling and writes `.mm/config.toml`. It also asks for the
   code host: `github` (`gh`, the default) or `bitbucket` (Atlassian's `twg bb`).
   Choose Bitbucket only when the user asks for it (`--forge bitbucket`, plus
   `--twg-path` when `twg` is not on `PATH` or in `~/.local/bin`); then `mm doctor`
   shows whether Bitbucket access is ready, and if not, hand the user `twg login`
   and `twg setup bitbucket`. Never handle their tokens.
5. Register work with `mm repo add <url-or-path>`, then confirm with
   `mm repo list` and `mm status`.

## Repair

- Missing requirement: name it, explain what installs it, and ask before running
  anything that changes the system.
- Invalid config: `mm doctor` reports the validation error. Re-run `mm setup` or
  `mm config models` / `mm config authority` to fix the specific field.
- A harness that disappeared or lost auth: `mm doctor` shows it as unavailable.
  Guide re-authentication rather than silently switching providers.

## Boundaries

- Deterministic probes and state changes belong to `mm`, not to shell commands
  you type by hand.
- Do not enable dangerous permission-bypass modes.
- Merge authority stays off unless the user explicitly grants it via
  `mm config authority --allow-merge`.
