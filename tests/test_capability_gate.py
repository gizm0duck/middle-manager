"""Hermetic tests for the provider capability gate (fail-closed)."""

from __future__ import annotations

import json

from middle_manager.providers import capability


def _write(path, providers) -> str:
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    capability._load.cache_clear()
    return str(path)


def test_missing_file_is_all_false(tmp_path) -> None:
    caps = capability.capabilities_for("claude", path=str(tmp_path / "none.json"))
    assert all(v is False for v in caps.values())


def test_reads_recorded_flags(tmp_path) -> None:
    p = _write(
        tmp_path / "cap.json",
        {
            "claude": {
                "cli_version": "2.1.251",
                "capabilities": {
                    "resume_after_sigint_tool": True,
                    "resume_after_sigkill": True,
                    "session_survives_process_exit": True,
                },
            }
        },
    )
    caps = capability.capabilities_for("claude", path=p)
    assert caps["resume_after_sigint_tool"] is True
    assert caps["mid_process_steer"] is False  # absent -> false
    assert capability.allows_interrupt_steer("claude", path=p) is True


def test_version_drift_disables_record(tmp_path) -> None:
    p = _write(
        tmp_path / "cap.json",
        {
            "codex": {
                "cli_version": "0.150.1",
                "capabilities": {
                    "resume_after_sigint_tool": True,
                    "resume_after_sigkill": True,
                    "session_survives_process_exit": True,
                },
            }
        },
    )
    # A different installed version must not trust the old probe row.
    assert capability.allows_interrupt_steer("codex", cli_version="0.200.0", path=p) is False
    assert capability.allows_interrupt_steer("codex", cli_version="0.150.1", path=p) is True


def test_committed_matrix_matches_m0(tmp_path) -> None:
    # The committed matrix records the real M0 outcome for these versions.
    capability._load.cache_clear()
    assert capability.allows_interrupt_steer("claude", cli_version="2.1.251") is True
    assert capability.allows_interrupt_steer("codex", cli_version="0.150.1") is True
    # Codex is single-shot: mid-process steer stays false even so.
    assert capability.capabilities_for("codex")["mid_process_steer"] is False
