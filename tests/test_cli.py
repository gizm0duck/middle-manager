"""Hermetic tests for the mm CLI dispatch."""

from __future__ import annotations

import subprocess

import pytest

from middle_manager import cli


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
