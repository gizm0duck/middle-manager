"""Shallow base clones: `mm repo add --depth N`.

A huge repository's full history can be the expensive part even when the checkout
is sparse: on 2026-11-18 a partial clone of a large monorepo ran 18
minutes and failed, while the latest commit plus a sparse checkout took under a
minute and a half. Real git in temp dirs where the point is what lands on disk.
"""

from __future__ import annotations

import subprocess

import pytest

from middle_manager import cli, repos
from middle_manager.worktree import LeaseManager


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def deep_repo(tmp_path) -> str:
    """A source repo with three commits and two top-level directories."""
    path = tmp_path / "deep"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@t")
    for n in range(3):
        for name in ("a", "b"):
            (path / name).mkdir(exist_ok=True)
            (path / name / "f.txt").write_text(f"{name}{n}")
        _git(path, "add", ".")
        _git(path, "commit", "-qm", f"commit {n}")
    return str(path)


def _spy_clones(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real_git, real_git_ok = repos._git, repos._git_ok

    def spy_git(args, cwd=None):
        if args and args[0] == "clone":
            calls.append(list(args))
        return real_git(args, cwd=cwd)

    def spy_git_ok(args, cwd=None):
        if args and args[0] == "clone":
            calls.append(list(args))
        return real_git_ok(args, cwd=cwd)

    monkeypatch.setattr(repos, "_git", spy_git)
    monkeypatch.setattr(repos, "_git_ok", spy_git_ok)
    return calls


def test_depth_is_passed_to_a_full_checkout_clone(mm_home, deep_repo, monkeypatch) -> None:
    calls = _spy_clones(monkeypatch)
    added = repos.add_repo(deep_repo, depth=1)

    assert calls == [["clone", "--quiet", "--depth", "1", deep_repo, added.local_path]]
    assert any("shallow: the last 1 commit(s)" in note for note in added.notes)


def test_depth_is_passed_to_a_sparse_clone(mm_home, deep_repo, monkeypatch) -> None:
    calls = _spy_clones(monkeypatch)
    added = repos.add_repo(deep_repo, sparse=["a"], depth=1)

    assert calls[0] == [
        "clone",
        "--quiet",
        *repos.SPARSE_CLONE_FLAGS,
        "--depth",
        "1",
        deep_repo,
        added.local_path,
    ]
    assert any("shallow" in note for note in added.notes)


def test_the_fallback_keeps_the_depth(mm_home, deep_repo, monkeypatch) -> None:
    """A refused partial clone must not quietly download the full history instead."""
    calls = _spy_clones(monkeypatch)
    real_git_ok = repos._git_ok

    def refuse_partial(args, cwd=None):
        if args and args[0] == "clone" and "--filter=blob:none" in args:
            calls.append(list(args))
            return 1, "", "fatal: the remote end hung up unexpectedly"
        return real_git_ok(args, cwd=cwd)

    monkeypatch.setattr(repos, "_git_ok", refuse_partial)
    added = repos.add_repo(deep_repo, sparse=["a"], depth=1)

    assert calls[-1] == [
        "clone",
        "--quiet",
        "--sparse",
        "--depth",
        "1",
        deep_repo,
        added.local_path,
    ]
    assert any("cloned the last 1 commit(s) with a sparse" in note for note in added.notes)
    assert not any("full history" in note for note in added.notes)


def test_a_file_url_clone_is_really_shallow_and_leases_work(
    mm_home, deep_repo, monkeypatch
) -> None:
    # Local paths ignore --depth (git clones them by hardlink); a file:// URL does not.
    url = f"file://{deep_repo}"
    monkeypatch.setattr(repos, "_resolve_source", lambda source: source)
    added = repos.add_repo(url, name="deep", forge_url=url, sparse=["a"], depth=1)

    assert _git(added.local_path, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(added.local_path, "rev-list", "--count", "HEAD") == "1"

    lease = LeaseManager(backend="git").acquire(
        repo_path=added.local_path, task_id=None, sparse_paths=["a"]
    )
    assert lease.sparse_warning is None
    assert _git(lease.worktree_path, "rev-parse", "HEAD") == _git(
        added.local_path, "rev-parse", "HEAD"
    )


@pytest.mark.parametrize("bad", [0, -1])
def test_depth_must_be_one_or_more(mm_home, deep_repo, bad) -> None:
    with pytest.raises(repos.RepoError, match="--depth wants a number of commits"):
        repos.add_repo(deep_repo, depth=bad)


def test_no_depth_leaves_the_clone_argv_unchanged(mm_home, deep_repo, monkeypatch) -> None:
    calls = _spy_clones(monkeypatch)
    added = repos.add_repo(deep_repo)
    assert calls == [["clone", "--quiet", deep_repo, added.local_path]]
    assert not any("shallow" in note for note in added.notes)


def test_cli_parses_depth() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["repo", "add", "u", "--depth", "1"]).depth == 1
    assert parser.parse_args(["repo", "add", "u"]).depth is None


def test_fetches_against_a_shallow_clone_stay_shallow(mm_home, deep_repo, monkeypatch) -> None:
    url = f"file://{deep_repo}"
    monkeypatch.setattr(repos, "_resolve_source", lambda source: source)
    shallow = repos.add_repo(url, name="deep", forge_url=url, depth=1)
    full = repos.add_repo(deep_repo, name="full")

    assert repos.shallow_fetch_args(shallow.local_path) == ["--depth", "1"]
    assert repos.shallow_fetch_args(full.local_path) == []

    _git(deep_repo, "commit", "-q", "--allow-empty", "-m", "commit 3")
    result = repos.sync_repo("deep")
    assert result.base_sha == _git(deep_repo, "rev-parse", "HEAD")
    assert _git(shallow.local_path, "rev-parse", "--is-shallow-repository") == "true"
    # The new tip, joined to the commit the clone already held, and nothing older.
    assert _git(shallow.local_path, "rev-list", "--count", "HEAD") == "2"
    assert repos.shallow_roots(shallow.local_path) == {shallow.base_sha}


def test_a_shallow_base_clone_with_local_commits_is_not_moved(
    mm_home, deep_repo, monkeypatch
) -> None:
    url = f"file://{deep_repo}"
    monkeypatch.setattr(repos, "_resolve_source", lambda source: source)
    shallow = repos.add_repo(url, name="deep", forge_url=url, depth=1)
    _git(shallow.local_path, "config", "user.name", "t")
    _git(shallow.local_path, "config", "user.email", "t@t")
    _git(shallow.local_path, "commit", "-q", "--allow-empty", "-m", "local work")
    local = _git(shallow.local_path, "rev-parse", "HEAD")

    _git(deep_repo, "commit", "-q", "--allow-empty", "-m", "remote work")
    with pytest.raises(repos.RepoError, match="may hold local commits"):
        repos.sync_repo("deep")
    assert _git(shallow.local_path, "rev-parse", "HEAD") == local


# --------------------------------------------------------------------------- #
# Pushing and delivering from a shallow clone (2026-11-20)
# --------------------------------------------------------------------------- #


def _shallow_clone(deep_repo, tmp_path) -> str:
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{deep_repo}", str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.name", "t")
    _git(clone, "config", "user.email", "t@t")
    return str(clone)


def _advance(path, name: str) -> str:
    (path_obj := __import__("pathlib").Path(path) / name).write_text(name)
    _git(path, "add", str(path_obj))
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(path, "rev-parse", "HEAD")


def test_shallow_roots_names_the_clone_boundary(deep_repo, tmp_path) -> None:
    clone = _shallow_clone(deep_repo, tmp_path)

    assert repos.shallow_roots(clone) == {_git(clone, "rev-parse", "HEAD")}
    assert repos.shallow_roots(deep_repo) == set()


def test_a_push_from_a_shallow_clone_first_fetches_the_remote_tip(deep_repo, tmp_path) -> None:
    """The server's current tip must exist locally, or the push sends the whole tree."""
    clone = _shallow_clone(deep_repo, tmp_path)
    _advance(clone, "local.txt")
    moved_tip = _advance(deep_repo, "upstream.txt")  # main moves after the clone
    assert not repos._has_commit_locally(clone, moved_tip)

    fetched = repos.prepare_shallow_push(clone, "origin", ["main", "mm/task-1"])

    assert fetched == ["main"], "the branch not on the remote yet is skipped"
    assert repos._has_commit_locally(clone, moved_tip)
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true", "still shallow"
    # What `git push` builds from a shallow clone: a thin pack with the server's tip
    # as the boundary. Unrelated histories still exclude every object the tip holds.
    pack = subprocess.run(
        ["git", "-C", clone, "pack-objects", "--revs", "--thin", "--shallow", "--stdout", "-q"],
        input=f"HEAD\n^{moved_tip}\n".encode(),
        capture_output=True,
        check=True,
    ).stdout
    whole = subprocess.run(
        ["git", "-C", clone, "pack-objects", "--revs", "--stdout", "-q"],
        input=b"HEAD\n",
        capture_output=True,
        check=True,
    ).stdout

    def count(raw: bytes) -> int:  # a pack header holds its object count in bytes 8-12
        return int.from_bytes(raw[8:12], "big")

    assert count(pack) < count(whole), "the tip's objects are left out"
    assert count(pack) <= 5, "the new commit, its root tree, the changed blob, little else"


def test_preparing_a_push_is_a_no_op_when_nothing_is_missing(deep_repo, tmp_path) -> None:
    clone = _shallow_clone(deep_repo, tmp_path)

    assert repos.prepare_shallow_push(clone, "origin", ["main"]) == []
    assert repos.prepare_shallow_push(deep_repo, "origin", ["main"]) == [], "full clone"


def test_the_carry_check_never_diffs_a_shallow_root(mm_home, monkeypatch) -> None:
    """A shallow root's patch is the whole repository; in a partial clone that means every blob."""
    from middle_manager import delivery
    from middle_manager.state import init_db, store

    conn = init_db()
    repo_id = store.add_repo(
        conn, name="r", origin="o", local_path="/r", default_branch="main", base_sha="root"
    )
    run_id = store.create_run(conn, "x")
    task_id = store.add_task(conn, run_id=run_id, title="delivering", repo_id=repo_id)
    sibling_id = store.add_task(conn, run_id=run_id, title="sibling", repo_id=repo_id)
    store.update_task_fields(conn, task_id, worktree_path="/wt", branch="b1", base_sha="root")
    store.update_task_fields(conn, sibling_id, branch="b2", base_sha="root")

    monkeypatch.setattr(repos, "shallow_roots", lambda _wt: {"root"})
    monkeypatch.setattr(delivery, "_resolved_ref", lambda _wt, *refs: next(r for r in refs if r))
    monkeypatch.setattr(delivery, "_git_lines", lambda _wt, *args: ["mine", "root"])
    monkeypatch.setattr(delivery, "_own_commits", lambda *a, **kw: {"theirs", "root"})
    diffed: list[set[str]] = []
    monkeypatch.setattr(delivery, "_patch_ids", lambda _wt, commits: diffed.append(commits) or {})

    task = store.get_task(conn, task_id)
    assert delivery.composition_tasks(conn, task, remote="origin", base="main", head="h") == []
    assert diffed and all("root" not in commits for commits in diffed)


# --------------------------------------------------------------------------- #
# Fetching a branch keeps the history the clone already holds
# --------------------------------------------------------------------------- #


def _task_branch(clone: str, branch: str, *names: str) -> str:
    _git(clone, "checkout", "-qb", branch)
    for name in names:
        _advance(clone, name)
    _git(clone, "push", "-q", "origin", f"HEAD:{branch}")
    return _git(clone, "rev-parse", "HEAD")


def test_a_branch_whose_tip_is_here_is_not_fetched_or_cut(deep_repo, tmp_path) -> None:
    """A depth fetch of a tip already here marks it shallow and ends its history there."""
    clone = _shallow_clone(deep_repo, tmp_path)
    _git(deep_repo, "config", "receive.denyCurrentBranch", "ignore")
    tip = _task_branch(clone, "mm/task-1", "one", "two")

    assert repos.fetch_branch(clone, "origin", "mm/task-1") == (tip, "")
    assert _git(clone, "rev-list", "--count", tip) == "3"
    assert repos.shallow_roots(clone) == {_git(clone, "rev-parse", f"{tip}~2")}


def test_a_stack_parent_that_moved_stays_joined_to_the_child(deep_repo, tmp_path) -> None:
    clone = _shallow_clone(deep_repo, tmp_path)
    _git(deep_repo, "config", "receive.denyCurrentBranch", "ignore")
    parent = _task_branch(clone, "mm/parent", "p1", "p2")
    _git(clone, "checkout", "-qb", "mm/child")
    _advance(clone, "child")

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "mm/parent", f"file://{deep_repo}", str(other)],
        check=True,
        capture_output=True,
    )
    moved = _advance(str(other), "p3")
    _git(other, "push", "-q", "origin", "HEAD:mm/parent")

    assert repos.fetch_branch(clone, "origin", "mm/parent") == (moved, "")
    assert _git(clone, "merge-base", "mm/child", moved) == parent
    assert _git(clone, "rev-list", "--count", f"{parent}..{moved}") == "1"


def test_a_branch_forked_below_the_boundary_is_not_fetched_whole(deep_repo, tmp_path) -> None:
    clone = _shallow_clone(deep_repo, tmp_path)
    _git(deep_repo, "checkout", "-qb", "old-fork", "HEAD~2")
    fork = _advance(deep_repo, "fork")
    _git(deep_repo, "checkout", "-q", "main")
    for n in range(repos.CONNECT_DEPTHS[0] + 4):
        _git(deep_repo, "commit", "-q", "--allow-empty", "-m", f"history {n}")

    assert repos.fetch_branch(clone, "origin", "old-fork") == (fork, "")
    # Its own commit and the two below it that the shallow clone never had, at most.
    assert int(_git(clone, "rev-list", "--count", fork)) <= repos.CONNECT_DEPTHS[0]


def test_a_full_clone_fetches_a_branch_plainly(deep_repo, tmp_path) -> None:
    clone = tmp_path / "full"
    subprocess.run(
        ["git", "clone", "-q", f"file://{deep_repo}", str(clone)], check=True, capture_output=True
    )
    head = _git(deep_repo, "rev-parse", "HEAD")
    assert repos.fetch_branch(str(clone), "origin", "main") == (head, "")
    assert repos.fetch_branch(str(clone), "origin", "nope")[0] is None


def test_reconnect_clears_only_marks_whose_parents_are_here(deep_repo, tmp_path) -> None:
    clone = _shallow_clone(deep_repo, tmp_path)
    root = _git(clone, "rev-parse", "HEAD")
    tip = _advance(clone, "local")
    shallow_file = __import__("pathlib").Path(clone) / ".git" / "shallow"
    shallow_file.write_text(f"{root}\n{tip}\n")

    assert repos.reconnect_shallow(clone) == [tip]
    assert repos.shallow_roots(clone) == {root}
    assert _git(clone, "rev-list", "--count", tip) == "2"
