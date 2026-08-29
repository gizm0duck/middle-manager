"""Hermetic tests for worker routing and ceiling enforcement."""

from __future__ import annotations

import pytest

from middle_manager.config import ManagerProfile, MMConfig, WorkerCeiling
from middle_manager.router import (
    CeilingError,
    WorkerProfile,
    choose_worker,
    enforce_ceiling,
)


def _config(max_model="sonnet", max_reasoning="medium"):
    return MMConfig(
        manager=ManagerProfile("claude", "opus", "high"),
        worker=WorkerCeiling("claude", max_model, max_reasoning),
    )


def test_choose_worker_picks_smallest() -> None:
    profile = choose_worker(_config())
    assert profile.model == "haiku"
    assert profile.reasoning == "low"


def test_enforce_rejects_model_over_ceiling() -> None:
    with pytest.raises(CeilingError):
        enforce_ceiling(_config("sonnet"), WorkerProfile("claude", "opus", "low"))


def test_enforce_rejects_reasoning_over_ceiling() -> None:
    with pytest.raises(CeilingError):
        enforce_ceiling(_config(max_reasoning="medium"), WorkerProfile("claude", "haiku", "high"))


def test_enforce_allows_at_ceiling() -> None:
    enforce_ceiling(_config("sonnet", "medium"), WorkerProfile("claude", "sonnet", "medium"))


def test_unknown_model_rejected_unless_ceiling() -> None:
    with pytest.raises(CeilingError):
        enforce_ceiling(_config("sonnet"), WorkerProfile("claude", "mystery", "low"))
