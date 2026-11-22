"""`mm setup` and `mm config` flows.

Setup is scriptable: with ``--non-interactive`` and flags (or an
``overrides`` dict) it writes the config without prompting, which is how tests
and the setup skill drive it. Without those, it prompts, offering only detected,
authenticated harnesses.
"""

from __future__ import annotations

from middle_manager.config import (
    FORGES,
    REASONING_LEVELS,
    Authority,
    ConfigError,
    ForgePolicy,
    ManagerProfile,
    MMConfig,
    WorkerCeiling,
    load_config,
    save_config,
)
from middle_manager.memory import ensure_memory_layout
from middle_manager.paths import config_path, ensure_layout
from middle_manager.setup.discovery import discover, usable_harnesses
from middle_manager.state.db import init_db


def _choose(prompt: str, options: list[str], default: str) -> str:
    opts = "/".join(options)
    while True:
        raw = input(f"{prompt} [{opts}] ({default}): ").strip() or default
        if raw in options:
            return raw
        print(f"  please choose one of: {opts}")


def _forge_policy(kind: str, twg_path: str | None) -> ForgePolicy:
    """The forge to configure. Bitbucket needs a ``twg`` it can actually run."""
    if kind not in FORGES:
        raise ConfigError(f"forge must be one of {FORGES}")
    policy = ForgePolicy(kind=kind, twg_path=twg_path or "")
    if kind == "bitbucket":
        from middle_manager import forge

        found = forge._executable(policy.twg_path) if policy.twg_path else None
        if policy.twg_path and found is None:
            raise ConfigError(f"--twg-path {policy.twg_path} is not an executable")
        if found is None and forge.twg_bin() is None:
            raise ConfigError(f"forge bitbucket needs the TWG CLI: {forge.INSTALL_HINT}")
    return policy


def build_config(overrides: dict) -> MMConfig:
    """Construct and validate a config from an overrides dict (fail-closed)."""
    report = discover()
    usable = usable_harnesses(report)
    if not usable:
        raise ConfigError(
            "no authenticated Claude/Codex harness detected; run `mm doctor` and "
            "sign in before setup"
        )

    manager_provider = overrides.get("manager_provider", recommended_provider(usable))
    if manager_provider not in usable:
        raise ConfigError(f"manager provider {manager_provider!r} is not usable; usable: {usable}")
    # Workers run on the manager's harness unless the user asks to mix them.
    worker_provider = overrides.get("worker_provider", manager_provider)
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
            default_model=overrides.get(
                "worker_default_model",
                overrides.get("worker_max_model", _default_worker_model(worker_provider)),
            ),
            default_reasoning=overrides.get(
                "worker_default_reasoning", overrides.get("worker_max_reasoning", "medium")
            ),
            max_concurrent=overrides.get("worker_max_concurrent", WorkerCeiling.max_concurrent),
        ),
        cost_posture=overrides.get("cost_posture", "lean"),
        authority=Authority(
            merge=bool(overrides.get("allow_merge", False)),
        ),
        forge=_forge_policy(overrides.get("forge", "github"), overrides.get("twg_path")),
    )
    cfg.validate()
    return cfg


def recommended_provider(usable: list[str]) -> str:
    """The harness a new installation should run on: Claude when it's signed in."""
    return "claude" if "claude" in usable else usable[0]


def _default_model(provider: str) -> str:
    """The manager's model, and the hard worker ceiling, for a new installation."""
    return {"claude": "opus", "codex": "gpt-5-codex"}.get(provider, "unknown")


def _default_worker_model(provider: str) -> str:
    """The model a worker gets when its task names none.

    On Claude that is Sonnet under an Opus ceiling: routine work runs on the cheaper
    model, and the router still escalates heavy briefs up to Opus.
    """
    return {"claude": "sonnet"}.get(provider, _default_model(provider))


def run_setup(non_interactive: bool = False, overrides: dict | None = None) -> MMConfig:
    ensure_layout()
    init_db()
    ensure_memory_layout()
    overrides = dict(overrides or {})

    if not non_interactive:
        report = discover()
        usable = usable_harnesses(report)
        if not usable:
            raise ConfigError("no authenticated Claude/Codex harness detected; run `mm doctor`")
        print("Detected, authenticated harnesses:", ", ".join(usable))
        if "claude" in usable:
            print("Recommended: Claude Opus as the manager, Claude Sonnet for workers.")
        overrides.setdefault(
            "manager_provider", _choose("Manager provider", usable, recommended_provider(usable))
        )
        overrides.setdefault(
            "manager_reasoning",
            _choose("Manager reasoning", list(REASONING_LEVELS), "high"),
        )
        overrides.setdefault(
            "worker_provider", _choose("Worker provider", usable, overrides["manager_provider"])
        )
        overrides.setdefault(
            "worker_max_reasoning",
            _choose("Max worker reasoning", list(REASONING_LEVELS), "medium"),
        )
        if "forge" not in overrides:
            overrides["forge"] = _choose(
                "Code host (github uses gh; bitbucket uses twg bb)", list(FORGES), "github"
            )

    cfg = build_config(overrides)
    save_config(cfg)
    return cfg


def config_models(overrides: dict) -> MMConfig:
    """`mm config models` — rerun discovery and change either profile."""
    # Only an absent file means first-time construction. An existing but invalid
    # config may hold authority and custom policy that must not be replaced by a
    # newly built default config; surface the error and leave its bytes untouched.
    path = config_path()
    current = load_config(path) if path.exists() else None
    if current is None:
        cfg = build_config(overrides)
    else:
        report = discover()
        usable = usable_harnesses(report)
        manager_provider = overrides.get("manager_provider", current.manager.provider)
        worker_provider = overrides.get("worker_provider", current.worker.provider)
        for role, provider in (("manager", manager_provider), ("worker", worker_provider)):
            if provider not in usable:
                raise ConfigError(f"{role} provider {provider!r} is not usable; usable: {usable}")

        current.manager.provider = manager_provider
        current.worker.provider = worker_provider
        for key, target, attr in (
            ("manager_model", current.manager, "model"),
            ("manager_reasoning", current.manager, "reasoning"),
            ("worker_max_model", current.worker, "max_model"),
            ("worker_max_reasoning", current.worker, "max_reasoning"),
            ("worker_default_model", current.worker, "default_model"),
            ("worker_default_reasoning", current.worker, "default_reasoning"),
            ("worker_max_concurrent", current.worker, "max_concurrent"),
        ):
            if key in overrides:
                setattr(target, attr, overrides[key])
        if "cost_posture" in overrides:
            current.cost_posture = overrides["cost_posture"]
        current.validate()
        cfg = current
    save_config(cfg)
    return cfg


def config_forge(kind: str | None, twg_path: str | None) -> MMConfig:
    """`mm config forge` — choose GitHub (gh) or Bitbucket (twg) for an existing install."""
    cfg = load_config()
    cfg.forge = _forge_policy(
        kind or cfg.forge.kind, cfg.forge.twg_path if twg_path is None else twg_path
    )
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
