"""Hermetic tests for repository registration (offline, local git only).

The sync tests pin the thing that made every un-based dispatch start from stale
code: sync recorded the base clone's local ``HEAD``, which nothing ever moved, so
a base clone registered weeks earlier kept handing workers an old commit.
"""

from __future__ import annotations

import subprocess

import pytest

from middle_manager import repos


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


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


def _commit_on_origin(source: str, message: str) -> str:
    subprocess.run(["git", "-C", source, "commit", "--allow-empty", "-qm", message], check=True)
    return _git(source, "rev-parse", "HEAD")


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
    _commit_on_origin(source, "next")
    res = repos.sync_repo("source")
    assert res.base_sha


def test_duplicate_registration_rejected(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    repos.add_repo(source)
    with pytest.raises(repos.RepoError):
        repos.add_repo(source)


# --------------------------------------------------------------------------- #
# Sync fast-forwards the base clone rather than re-recording its stale HEAD
# --------------------------------------------------------------------------- #


def test_sync_fast_forwards_the_base_clone_to_the_remote_tip(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    clone = added.local_path

    tip = _commit_on_origin(source, "moved on without us")
    res = repos.sync_repo("source")

    assert res.fast_forwarded is True
    assert res.base_sha == tip
    assert res.previous_sha == added.base_sha
    # The clone's own branch moved too — a worker leased from it starts at the tip.
    assert _git(clone, "rev-parse", "HEAD") == tip
    assert _git(clone, "rev-parse", "main") == tip
    assert repos.list_repos()[0]["base_sha"] == tip


def test_sync_is_a_no_op_when_the_base_clone_is_already_current(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    res = repos.sync_repo("source")
    assert res.fast_forwarded is False
    assert res.already_current is True
    assert res.base_sha == added.base_sha


def test_sync_refuses_a_dirty_base_clone_and_names_the_paths(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    clone = added.local_path
    (tmp_path / "source" / "file.txt").write_text("hello again\n")
    _commit_on_origin(source, "remote moved")

    # An edit to a tracked file and an untracked file: both must stop the sync.
    with open(f"{clone}/file.txt", "w", encoding="utf-8") as fh:
        fh.write("edited by hand\n")
    with open(f"{clone}/scratch.txt", "w", encoding="utf-8") as fh:
        fh.write("notes\n")

    with pytest.raises(repos.RepoError) as exc:
        repos.sync_repo("source")
    message = str(exc.value)
    assert "file.txt" in message
    assert "scratch.txt" in message
    # Nothing moved: the refusal changes no state.
    assert repos.list_repos()[0]["base_sha"] == added.base_sha
    assert _git(clone, "rev-parse", "HEAD") == added.base_sha


def test_sync_refuses_rather_than_rewriting_a_base_clone_that_has_diverged(
    tmp_path, mm_home
) -> None:
    """A base clone holding its own commits is never reset — that would destroy them."""
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    clone = added.local_path
    subprocess.run(
        ["git", "-C", clone, "-c", "user.name=t", "-c", "user.email=t@t"]
        + ["commit", "--allow-empty", "-qm", "local only"],
        check=True,
    )
    local_only = _git(clone, "rev-parse", "HEAD")
    _commit_on_origin(source, "remote moved")

    with pytest.raises(repos.RepoError) as exc:
        repos.sync_repo("source")
    assert "fast-forward" in str(exc.value)
    assert _git(clone, "rev-parse", "HEAD") == local_only


def test_sync_reports_a_stray_mm_directory_and_clears_it_on_request(tmp_path, mm_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    added = repos.add_repo(source)
    stray = tmp_path / ".mm" / "repos" / "source" / ".mm"
    (stray / "repos").mkdir(parents=True)

    res = repos.sync_repo("source")
    assert res.stray_mm is True
    assert res.stray_mm_removed is False
    assert any(".mm" in note and "--clean-stray-mm" in note for note in res.notes)
    assert stray.exists(), "reporting must not delete anything on its own"
    # The stray directory is not treated as a dirty base clone.
    assert repos.dirty_paths(added.local_path) == []

    res = repos.sync_repo("source", clean_stray_mm=True)
    assert res.stray_mm is True
    assert res.stray_mm_removed is True
    assert not stray.exists()


def test_sync_of_an_unregistered_repo_says_so(mm_home) -> None:
    with pytest.raises(repos.RepoError) as exc:
        repos.sync_repo("nope")
    assert "not registered" in str(exc.value)


def test_repo_sync_cli_reports_the_fast_forward(tmp_path, mm_home, capsys) -> None:
    from middle_manager.cli import main

    source = _make_source_repo(tmp_path / "source")
    repos.add_repo(source)
    tip = _commit_on_origin(source, "remote moved")

    assert main(["repo", "sync", "source"]) == 0
    out = capsys.readouterr().out
    assert "fast-forwarded main" in out
    assert tip[:8] in out
