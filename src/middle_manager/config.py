"""Configuration model for Middle Manager (``.mm/config.toml``).

Holds the manager profile, the hard worker ceiling, cost posture, and standing
repository authority. The runtime enforces these; prompts never do. Validation
is fail-closed: an unusable config raises rather than silently degrading.

The control plane keeps zero runtime package dependencies, so TOML is read with
the stdlib ``tomllib`` and written with a small purpose-built serializer for the
known, flat-ish schema.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from middle_manager.paths import config_path

REASONING_LEVELS = ("low", "medium", "high", "xhigh")
COST_POSTURES = ("lean", "balanced", "quality")
PROVIDERS = ("claude", "codex")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class ManagerProfile:
    provider: str = "claude"
    model: str = "opus"
    reasoning: str = "high"


@dataclass
class WorkerCeiling:
    provider: str = "codex"
    max_model: str = "gpt-5-codex"
    max_reasoning: str = "medium"


@dataclass
class Authority:
    clone_fetch: bool = True
    create_worktrees: bool = True
    commit: bool = True
    push_branches: bool = True
    open_pr: bool = True
    merge: bool = False  # merge stays off by default


@dataclass
class ToolsPolicy:
    location: str = ".mm/tools"
    update: str = "explicit"  # explicit | manual


@dataclass
class MMConfig:
    manager: ManagerProfile = field(default_factory=ManagerProfile)
    worker: WorkerCeiling = field(default_factory=WorkerCeiling)
    cost_posture: str = "lean"
    authority: Authority = field(default_factory=Authority)
    tools: ToolsPolicy = field(default_factory=ToolsPolicy)

    def validate(self) -> None:
        if self.manager.provider not in PROVIDERS:
            raise ConfigError(f"manager.provider must be one of {PROVIDERS}")
        if self.worker.provider not in PROVIDERS:
            raise ConfigError(f"worker.provider must be one of {PROVIDERS}")
        if self.manager.reasoning not in REASONING_LEVELS:
            raise ConfigError(f"manager.reasoning must be one of {REASONING_LEVELS}")
        if self.worker.max_reasoning not in REASONING_LEVELS:
            raise ConfigError(f"worker.max_reasoning must be one of {REASONING_LEVELS}")
        if self.cost_posture not in COST_POSTURES:
            raise ConfigError(f"cost_posture must be one of {COST_POSTURES}")
        for name in ("model",):
            if not getattr(self.manager, name):
                raise ConfigError(f"manager.{name} must be set")
        if not self.worker.max_model:
            raise ConfigError("worker.max_model must be set")

    def to_dict(self) -> dict:
        return {
            "manager": asdict(self.manager),
            "worker": asdict(self.worker),
            "cost_posture": self.cost_posture,
            "authority": asdict(self.authority),
            "tools": asdict(self.tools),
        }


def _from_dict(data: dict) -> MMConfig:
    cfg = MMConfig(
        manager=ManagerProfile(**data.get("manager", {})),
        worker=WorkerCeiling(**data.get("worker", {})),
        cost_posture=data.get("cost_posture", "lean"),
        authority=Authority(**data.get("authority", {})),
        tools=ToolsPolicy(**data.get("tools", {})),
    )
    return cfg


def load_config(path: Path | None = None) -> MMConfig:
    p = path or config_path()
    if not p.exists():
        raise ConfigError(
            f"no config at {p}; run `mm setup` to create the manager profile and worker ceiling"
        )
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    cfg = _from_dict(data)
    cfg.validate()
    return cfg


def save_config(cfg: MMConfig, path: Path | None = None) -> Path:
    cfg.validate()
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_dumps_toml(cfg.to_dict()), encoding="utf-8")
    return p


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dumps_toml(data: dict) -> str:
    """Serialize the known config shape: top-level scalars then [tables]."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_scalar(value)}")
    for table, values in tables.items():
        lines.append("")
        lines.append(f"[{table}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_scalar(value)}")
    return "\n".join(lines) + "\n"
