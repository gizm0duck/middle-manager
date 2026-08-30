"""Hermetic tests for the mm CLI dispatch."""

from __future__ import annotations

import json
import subprocess

import pytest

from middle_manager import cli
from middle_manager.config import MMConfig, load_config, save_config


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


def test_version(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert "middle-manager" in capsys.readouterr().out


def test_status_without_setup(mm_home, capsys) -> None:
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "config:" in out


def test_repo_add_and_list_via_cli(tmp_path, mm_home, capsys) -> None:
    source = tmp_path / "src"
    source.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "t"],
        ["config", "user.email", "t@t"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    (source / "f").write_text("x")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "i"], check=True)

    assert cli.main(["repo", "add", str(source)]) == 0
    capsys.readouterr()
    assert cli.main(["repo", "list"]) == 0
    assert "src" in capsys.readouterr().out


def test_assessment_policy_is_easy_to_reconfigure(mm_home, capsys) -> None:
    save_config(MMConfig())
    assert (
        cli.main(
            [
                "config",
                "assessments",
                "--completed-runs",
                "8",
                "--max-days",
                "21",
                "--disabled",
            ]
        )
        == 0
    )
    capsys.readouterr()
    policy = load_config().assessments
    assert policy.enabled is False
    assert policy.completed_runs == 8
    assert policy.max_days == 21


def test_assessment_cli_round_trip(mm_home, capsys) -> None:
    assert cli.main(["assessment", "tick", "--force", "--json"]) == 0
    cycle_id = json.loads(capsys.readouterr().out)["id"]
    actions = json.dumps(
        [
            {
                "description": "Review worker plans earlier.",
                "observation": "Late feedback caused rework.",
                "likely_cause": "The manager waited for completion.",
                "baseline": "No early checks.",
                "target": "Check every multi-step task once.",
                "measurement": "Early checks per multi-step task.",
            }
        ]
    )
    assert (
        cli.main(
            [
                "assessment",
                "complete",
                str(cycle_id),
                "--summary",
                "Delivery is sound; feedback should land earlier.",
                "--strength",
                "Kept the work moving.",
                "--weakness",
                "Caught one issue late.",
                "--action-json",
                actions,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "assessment",
                "align",
                str(cycle_id),
                "--decision",
                "approved",
                "--notes",
                "Run it for the next cycle.",
            ]
        )
        == 0
    )
    assert "aligned" in capsys.readouterr().out
