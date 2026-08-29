"""Hermetic tests for discovery parsing and the scriptable setup flow."""

from __future__ import annotations

import pytest

from middle_manager.config import ConfigError, load_config
from middle_manager.setup import discovery, wizard


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


def _fake_report(usable: list[str]):
    def make(name: str) -> dict:
        return {
            "name": name,
            "kind": "harness",
            "path": f"/usr/bin/{name}",
            "version": "1.0.0",
            "authenticated": name in usable,
            "available": name in usable,
            "detail": "",
        }

    return {
        "harnesses": [make("claude"), make("codex")],
        "requirements": [],
        "companions": [],
    }


def test_usable_harnesses_filters_unauthenticated() -> None:
    report = _fake_report(["claude"])
    assert discovery.usable_harnesses(report) == ["claude"]


def test_setup_non_interactive_writes_config(mm_home, monkeypatch) -> None:
    report = _fake_report(["claude", "codex"])
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    cfg = wizard.run_setup(
        non_interactive=True,
        overrides={
            "manager_provider": "claude",
            "worker_provider": "codex",
            "worker_max_reasoning": "medium",
        },
    )
    assert cfg.manager.provider == "claude"
    assert cfg.worker.provider == "codex"
    # Persisted and reloadable.
    reloaded = load_config()
    assert reloaded.worker.provider == "codex"


def test_setup_rejects_unusable_worker(mm_home, monkeypatch) -> None:
    report = _fake_report(["claude"])  # codex not authenticated
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    with pytest.raises(ConfigError):
        wizard.build_config({"manager_provider": "claude", "worker_provider": "codex"})


def test_config_authority_toggles_merge(mm_home, monkeypatch) -> None:
    report = _fake_report(["claude", "codex"])
    monkeypatch.setattr(wizard, "discover", lambda: report)
    monkeypatch.setattr(wizard, "usable_harnesses", lambda r: discovery.usable_harnesses(r))
    wizard.run_setup(non_interactive=True, overrides={"manager_provider": "claude"})
    assert load_config().authority.merge is False
    wizard.config_authority({"merge": True})
    assert load_config().authority.merge is True
