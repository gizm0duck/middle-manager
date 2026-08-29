"""Hermetic tests for config load/validate/save."""

from __future__ import annotations

import pytest

from middle_manager.config import (
    Authority,
    ConfigError,
    ManagerProfile,
    MMConfig,
    WorkerCeiling,
    load_config,
    save_config,
)


def test_round_trip(tmp_path) -> None:
    path = tmp_path / "config.toml"
    cfg = MMConfig(
        manager=ManagerProfile("claude", "opus", "high"),
        worker=WorkerCeiling("codex", "gpt-5-codex", "medium"),
        cost_posture="lean",
        authority=Authority(merge=False),
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.manager.provider == "claude"
    assert loaded.worker.max_model == "gpt-5-codex"
    assert loaded.authority.merge is False
    assert loaded.cost_posture == "lean"


def test_missing_config_raises(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_invalid_reasoning_rejected() -> None:
    cfg = MMConfig(manager=ManagerProfile("claude", "opus", "ludicrous"))
    with pytest.raises(ConfigError):
        cfg.validate()


def test_invalid_provider_rejected() -> None:
    cfg = MMConfig(worker=WorkerCeiling("gemini", "x", "low"))
    with pytest.raises(ConfigError):
        cfg.validate()


def test_merge_defaults_off() -> None:
    assert Authority().merge is False
