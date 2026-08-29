"""Shared fixtures for hermetic tests."""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(autouse=True)
def _force_git_lease_backend(monkeypatch):
    """Keep the hermetic suite on the git lease backend.

    The treehouse binary may be installed on the dev machine; without this, the
    supervisor's auto-selected backend would mutate the global ~/.treehouse pool.
    The opt-in live test constructs ``LeaseManager(backend="treehouse")``
    explicitly, which overrides this env.
    """
    monkeypatch.setenv("MM_LEASE_BACKEND", "git")


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    """Point .mm state at an isolated temp dir for the test."""
    home = tmp_path / ".mm"
    monkeypatch.setenv("MM_HOME", str(home))
    return home


def make_git_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.name", "Tester")
    run("config", "user.email", "tester@example.com")
    (path / "README.md").write_text("# fixture\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return str(path)


@pytest.fixture
def source_repo(tmp_path):
    return make_git_repo(tmp_path / "source")
