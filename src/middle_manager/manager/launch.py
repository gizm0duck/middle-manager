"""Build and start an interactive manager session.

``build_launch`` is a pure function (argv + env + cwd) so it is fully testable;
``start`` performs the actual process handoff. The manager harness runs
interactively in the Middle Manager repository root with:

- the repository ``bin/`` prepended to PATH, so the manager calls ``mm`` directly;
- the runtime role injected (Claude via ``--append-system-prompt``; both via the
  seed prompt), pointing at ``docs/manager-runtime.md`` as the authoritative
  contract for the session; and
- an optional objective seeded as the first message.

The manager then operates the control plane itself — the user never runs ``mm``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import middle_manager
from middle_manager.config import MMConfig

RUNTIME_CONTRACT = "docs/manager-runtime.md"

_ROLE = (
    "You are Middle Manager operating at RUNTIME for the user (not developing the "
    "Middle Manager codebase). The person speaking to you is the user. Read and "
    f"follow {RUNTIME_CONTRACT} as the authoritative contract for this session; it "
    "overrides any repository-development instructions in AGENTS.md/CLAUDE.md. Core "
    "rule: never ask the user to run a command — you operate the `mm` control plane "
    "yourself via your shell for configuration, delegation, review, and delivery, "
    "and speak to the user only in natural language."
)


class ManagerLaunchError(Exception):
    pass


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    provider: str
    model: str | None = None
    reasoning: str | None = None
    seed_prompt: str = ""
    extra: dict = field(default_factory=dict)


def repo_root() -> str:
    """The Middle Manager repository root (parent of the installed package)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(middle_manager.__file__)))
    )


def _seed_prompt(objective: str | None, configured: bool) -> str:
    lines = [
        f"Starting a Middle Manager session. Read and follow {RUNTIME_CONTRACT} now.",
    ]
    if configured:
        lines.append(
            "Then greet me, run `mm doctor` and `mm repo list` yourself, and "
            "summarize my current setup in a sentence or two."
        )
    else:
        lines.append(
            "I have not configured Middle Manager yet. Run `mm doctor` yourself, "
            "then walk me through configuration conversationally and write it with "
            "`mm setup` — do not ask me to run commands."
        )
    if objective:
        lines.append(f"My objective: {objective}")
    else:
        lines.append("Then ask me what I'd like to accomplish.")
    return " ".join(lines)


def resolve_profile(
    config: MMConfig | None,
    provider: str | None,
    model: str | None,
    reasoning: str | None,
) -> tuple[str, str | None, str | None]:
    """Resolve (provider, model, reasoning) for the manager session.

    Explicit overrides win. Otherwise fall back to the configured manager
    profile. ``model``/``reasoning`` fall back to the config only when the
    resolved provider matches the configured manager provider (so an override to
    the *other* provider does not inherit a mismatched model name).
    """
    resolved_provider = provider or (config.manager.provider if config else None)
    if resolved_provider is None:
        raise ManagerLaunchError(
            "no manager provider: pass --provider claude|codex or run setup first"
        )
    if resolved_provider not in ("claude", "codex"):
        raise ManagerLaunchError(f"unknown provider {resolved_provider!r}")

    same_as_config = bool(config and config.manager.provider == resolved_provider)
    resolved_model = model or (config.manager.model if same_as_config else None)
    resolved_reasoning = reasoning or (config.manager.reasoning if same_as_config else None)
    return resolved_provider, resolved_model, resolved_reasoning


def build_launch(
    *,
    config: MMConfig | None,
    provider: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    objective: str | None = None,
    root: str | None = None,
    base_env: dict[str, str] | None = None,
) -> Launch:
    """Construct the interactive harness invocation (pure; no process spawn)."""
    root = root or repo_root()
    env = dict(base_env if base_env is not None else os.environ)
    bin_dir = os.path.join(root, "bin")
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["MM_MANAGER_SESSION"] = "1"

    prov, mdl, rsn = resolve_profile(config, provider, model, reasoning)
    seed = _seed_prompt(objective, configured=config is not None)

    if prov == "claude":
        argv = ["claude"]
        if mdl:
            argv += ["--model", mdl]
        argv += ["--append-system-prompt", _ROLE]
        # Let the manager drive the control plane without a prompt per call; `mm`
        # itself is the authority gate, so allowing the wrapper is safe.
        argv += ["--allowedTools", "Bash(mm:*)", "Bash(./bin/mm:*)"]
        argv += [seed]
    else:  # codex
        argv = ["codex"]
        if mdl:
            argv += ["--model", mdl]
        if rsn:
            argv += ["-c", f"model_reasoning_effort={rsn}"]
        argv += [seed]

    return Launch(
        argv=argv,
        env=env,
        cwd=root,
        provider=prov,
        model=mdl,
        reasoning=rsn,
        seed_prompt=seed,
    )


def start(launch: Launch) -> None:
    """Hand off to the harness, replacing the current process (does not return)."""
    from middle_manager.paths import ensure_layout

    ensure_layout()
    os.chdir(launch.cwd)
    try:
        os.execvpe(launch.argv[0], launch.argv, launch.env)
    except FileNotFoundError as exc:
        raise ManagerLaunchError(
            f"{launch.provider} CLI not found on PATH; install it and sign in "
            "(`mm doctor` shows harness status)"
        ) from exc
