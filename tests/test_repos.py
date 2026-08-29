"""Hermetic tests for repository registration (offline, local git only)."""

from __future__ import annotations

import subprocess

import pytest

from middle_manager import repos


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


def _make_source_repo(path) -> str:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=path, check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.name", "t")
    run("config", "user.email", "t@t")
    (path / "file.txt").write_text("hello\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return str(path)


def test_derive_name() -> None:
    assert repos.derive_name("https://github.com/acme/widgets.git") == "widgets"
    assert repos.derive_name("/home/me/projects/thing/") == "thing"


def test_add_list_sync(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    assert added.name == "source"
    assert added.default_branch == "main"
    assert added.base_sha

    listed = repos.list_repos()
    assert len(listed) == 1
    assert listed[0]["name"] == "source"

    # Add a new commit to the source, then sync.
    subprocess.run(["git", "-C", source, "commit", "--allow-empty", "-qm", "next"], check=True)
    new_sha = repos.sync_repo("source")
    assert new_sha


def test_duplicate_registration_rejected(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    repos.add_repo(source)
    with pytest.raises(repos.RepoError):
        repos.add_repo(source)
