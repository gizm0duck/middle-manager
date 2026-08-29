"""`mm setup` and `mm config` flows.

Setup is scriptable: with ``--non-interactive`` and flags (or an
``overrides`` dict) it writes the config without prompting, which is how tests
and the setup skill drive it. Without those, it prompts, offering only detected,
authenticated harnesses.
"""

from __future__ import annotations

from middle_manager.config import (
    REASONING_LEVELS,
    Authority,
    ConfigError,
    ManagerProfile,
    MMConfig,
    WorkerCeiling,
    load_config,
    save_config,
)
from middle_manager.paths import ensure_layout
from middle_manager.setup.discovery import discover, usable_harnesses
from middle_manager.state.db import init_db


def _choose(prompt: str, options: list[str], default: str) -> str:
    opts = "/".join(options)
    while True:
        raw = input(f"{prompt} [{opts}] ({default}): ").strip() or default
        if raw in options:
            return raw
        print(f"  please choose one of: {opts}")


def build_config(overrides: dict) -> MMConfig:
    """Construct and validate a config from an overrides dict (fail-closed)."""
    report = discover()
    usable = usable_harnesses(report)
    if not usable:
        raise ConfigError(
            "no authenticated Claude/Codex harness detected; run `mm doctor` and "
            "sign in before setup"
        )

    manager_provider = overrides.get("manager_provider", usable[0])
    if manager_provider not in usable:
        raise ConfigError(f"manager provider {manager_provider!r} is not usable; usable: {usable}")
    worker_provider = overrides.get("worker_provider", usable[-1])
    if worker_provider not in usable:
        raise ConfigError(f"worker provider {worker_provider!r} is not usable; usable: {usable}")

    cfg = MMConfig(
        manager=ManagerProfile(
            provider=manager_provider,
            model=overrides.get("manager_model", _default_model(manager_provider)),
            reasoning=overrides.get("manager_reasoning", "high"),
        ),
        worker=WorkerCeiling(
            provider=worker_provider,
            max_model=overrides.get("worker_max_model", _default_model(worker_provider)),
            max_reasoning=overrides.get("worker_max_reasoning", "medium"),
        ),
        cost_posture=overrides.get("cost_posture", "lean"),
        authority=Authority(
            merge=bool(overrides.get("allow_merge", False)),
        ),
    )
    cfg.validate()
    return cfg


def _default_model(provider: str) -> str:
    return {"claude": "opus", "codex": "gpt-5-codex"}.get(provider, "unknown")


def run_setup(non_interactive: bool = False, overrides: dict | None = None) -> MMConfig:
    ensure_layout()
    init_db()
    overrides = dict(overrides or {})

    if not non_interactive:
        report = discover()
        usable = usable_harnesses(report)
        if not usable:
            raise ConfigError("no authenticated Claude/Codex harness detected; run `mm doctor`")
        print("Detected, authenticated harnesses:", ", ".join(usable))
        overrides.setdefault("manager_provider", _choose("Manager provider", usable, usable[0]))
        overrides.setdefault(
            "manager_reasoning",
            _choose("Manager reasoning", list(REASONING_LEVELS), "high"),
        )
        overrides.setdefault("worker_provider", _choose("Worker provider", usable, usable[-1]))
        overrides.setdefault(
            "worker_max_reasoning",
            _choose("Max worker reasoning", list(REASONING_LEVELS), "medium"),
        )

    cfg = build_config(overrides)
    save_config(cfg)
    return cfg


def config_models(overrides: dict) -> MMConfig:
    """`mm config models` — rerun discovery and change either profile."""
    try:
        current = load_config()
    except ConfigError:
        current = None
    merged = dict(overrides)
    if current is not None:
        merged.setdefault("manager_provider", current.manager.provider)
        merged.setdefault("manager_model", current.manager.model)
        merged.setdefault("manager_reasoning", current.manager.reasoning)
        merged.setdefault("worker_provider", current.worker.provider)
        merged.setdefault("worker_max_model", current.worker.max_model)
        merged.setdefault("worker_max_reasoning", current.worker.max_reasoning)
        merged.setdefault("cost_posture", current.cost_posture)
    cfg = build_config(merged)
    save_config(cfg)
    return cfg


def config_authority(overrides: dict) -> MMConfig:
    """`mm config authority` — adjust standing authority, e.g. merge policy."""
    cfg = load_config()
    auth = cfg.authority
    for key in (
        "clone_fetch",
        "create_worktrees",
        "commit",
        "push_branches",
        "open_pr",
        "merge",
    ):
        if key in overrides:
            setattr(auth, key, bool(overrides[key]))
    save_config(cfg)
    return cfg
