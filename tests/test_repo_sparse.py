"""Sparse checkout for large repositories: `mm repo add --sparse` and its worktrees.

Real git in temp dirs throughout: the point is what ends up on disk. A sparse repo's
base clone and every worktree leased from it hold only the chosen directories (plus
root files); a repo registered without ``--sparse`` behaves byte-for-byte as before.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from middle_manager import cli, repos
from middle_manager.state import init_db, store
from middle_manager.worktree import LeaseManager
from middle_manager.worktree import lease as lease_mod


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def big_repo(tmp_path) -> str:
    """A source repo with three top-level directories and a root file."""
    path = tmp_path / "big"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@t")
    for name in ("a", "b", "c"):
        (path / name / "deep").mkdir(parents=True)
        (path / name / "deep" / "f.txt").write_text(name)
    (path / "README.md").write_text("root\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return str(path)


def _entries(path) -> set[str]:
    return {name for name in os.listdir(path) if name != ".git"}


def test_add_sparse_materializes_only_the_cone_in_the_base_clone(mm_home, big_repo) -> None:
    added = repos.add_repo(big_repo, sparse=["a"])

    assert _entries(added.local_path) == {"README.md", "a"}
    assert added.sparse_paths == ["a"]
    assert _git(added.local_path, "sparse-checkout", "list") == "a"
    row = store.get_repo(init_db(), added.name)
    assert json.loads(row["sparse_paths"]) == ["a"]
    assert repos.sparse_paths_of(row) == ["a"]
    assert any("git sparse-checkout add" in note for note in added.notes)


def test_bare_sparse_is_root_files_only(mm_home, big_repo) -> None:
    added = repos.add_repo(big_repo, sparse=[])

    assert _entries(added.local_path) == {"README.md"}
    assert repos.sparse_paths_of(store.get_repo(init_db(), added.name)) == []


def test_git_lease_of_a_sparse_repo_is_sparse_and_clean(mm_home, big_repo) -> None:
    added = repos.add_repo(big_repo, sparse=["a"])
    mgr = LeaseManager(backend="git")

    lease = mgr.acquire(repo_path=added.local_path, task_id=None, sparse_paths=["a"])

    assert lease.sparse_warning is None
    assert _entries(lease.worktree_path) == {"README.md", "a"}
    assert os.path.exists(os.path.join(lease.worktree_path, "a", "deep", "f.txt"))
    assert mgr.head_sha(lease) == lease.base_sha
    assert mgr.is_dirty(lease) is False
    # The worker can widen its own checkout.
    _git(lease.worktree_path, "sparse-checkout", "add", "b")
    assert _entries(lease.worktree_path) == {"README.md", "a", "b"}
    assert mgr.is_dirty(lease) is False
    mgr.release(lease, repo_path=added.local_path)


def test_git_lease_on_an_existing_branch_is_sparse(mm_home, big_repo) -> None:
    added = repos.add_repo(big_repo, sparse=["c"])
    _git(added.local_path, "branch", "mm/task-7-kept")
    mgr = LeaseManager(backend="git")

    lease = mgr.acquire(
        repo_path=added.local_path, task_id=None, branch="mm/task-7-kept", sparse_paths=["c"]
    )

    assert lease.branch == "mm/task-7-kept"
    assert _entries(lease.worktree_path) == {"README.md", "c"}
    assert mgr.is_dirty(lease) is False


def test_set_sparse_widens_and_then_turns_off(mm_home, big_repo) -> None:
    added = repos.add_repo(big_repo, sparse=["a"])

    widened = repos.set_sparse(added.name, ["a", "b"])
    assert widened.sparse_paths == ["a", "b"]
    assert _entries(added.local_path) == {"README.md", "a", "b"}
    assert "sparse: a, b" in widened.describe()

    full = repos.set_sparse(added.name, None)
    assert full.sparse_paths is None
    assert _entries(added.local_path) == {"README.md", "a", "b", "c"}
    assert store.get_repo(init_db(), added.name)["sparse_paths"] is None


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../b", "./a", ""])
def test_sparse_directories_must_stay_inside_the_repo(bad) -> None:
    with pytest.raises(repos.RepoError):
        repos.normalize_sparse_paths([bad])


def test_normalize_strips_slashes_and_dedupes() -> None:
    assert repos.normalize_sparse_paths(["a/", "a", "src/app/"]) == ["a", "src/app"]
    assert repos.normalize_sparse_paths(None) is None
    assert repos.normalize_sparse_paths([]) == []


def test_a_sparse_apply_that_fails_is_a_warning_never_silent(
    mm_home, big_repo, monkeypatch
) -> None:
    added = repos.add_repo(big_repo, sparse=["a"])
    monkeypatch.setattr(repos, "apply_sparse", lambda path, paths: "git refused")

    lease = LeaseManager(backend="git").acquire(
        repo_path=added.local_path, task_id=None, sparse_paths=["a"]
    )

    assert lease.sparse_warning and "git refused" in lease.sparse_warning
    assert "sees more of the repository" in lease.sparse_warning


def test_verify_sparse_catches_a_full_checkout(mm_home, big_repo) -> None:
    added = repos.add_repo(big_repo)
    assert "not enabled" in repos.verify_sparse(added.local_path, ["a"])


def test_pooled_worktree_follows_the_repo_setting_each_lease(mm_home, big_repo) -> None:
    """The treehouse path: a reused worktree is made sparse, or made full again."""
    added = repos.add_repo(big_repo)
    lease = LeaseManager(backend="git").acquire(repo_path=added.local_path, task_id=None)
    path = lease.worktree_path

    assert lease_mod._treehouse_sparse(path, ["b"]) is None
    assert _entries(path) == {"README.md", "b"}
    # The same pooled worktree, leased next for a full-checkout repo.
    assert lease_mod._treehouse_sparse(path, None) is None
    assert _entries(path) == {"README.md", "a", "b", "c"}
    # A full one stays untouched.
    assert lease_mod._treehouse_sparse(path, None) is None


def test_non_sparse_add_and_lease_argv_are_unchanged(mm_home, big_repo, monkeypatch) -> None:
    clone_calls: list[list[str]] = []
    real_git = repos._git

    def spy_git(args, cwd=None):
        if args and args[0] == "clone":
            clone_calls.append(list(args))
        return real_git(args, cwd=cwd)

    monkeypatch.setattr(repos, "_git", spy_git)
    added = repos.add_repo(big_repo)
    assert clone_calls == [["clone", "--quiet", big_repo, added.local_path]]
    assert added.sparse_paths is None
    assert store.get_repo(init_db(), added.name)["sparse_paths"] is None
    assert _entries(added.local_path) == {"README.md", "a", "b", "c"}

    lease_calls: list[list[str]] = []
    real_lease_git = lease_mod._git

    def spy_lease_git(args, cwd):
        lease_calls.append(list(args))
        return real_lease_git(args, cwd)

    monkeypatch.setattr(lease_mod, "_git", spy_lease_git)
    lease = LeaseManager(backend="git").acquire(repo_path=added.local_path, task_id=None)
    assert lease_calls == [
        ["rev-parse", "HEAD"],
        ["worktree", "add", "-q", "-b", lease.branch, lease.worktree_path, "HEAD"],
    ]
    assert lease.sparse_warning is None


def test_migration_adds_the_column_to_an_existing_database(mm_home) -> None:
    conn = init_db()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
    assert "sparse_paths" in columns


def test_cli_parses_sparse_flags() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["repo", "add", "u", "--sparse", "a", "b"]).sparse == ["a", "b"]
    assert parser.parse_args(["repo", "add", "u", "--sparse"]).sparse == []
    assert parser.parse_args(["repo", "add", "u"]).sparse is None
    rset = parser.parse_args(["repo", "set", "r", "--no-sparse"])
    assert rset.no_sparse is True and rset.sparse is None


def test_cli_repo_add_sparse_and_list(mm_home, big_repo, capsys) -> None:
    assert cli.main(["repo", "add", big_repo, "--sparse", "a"]) == 0
    out = capsys.readouterr().out
    assert "sparse: a" in out

    assert cli.main(["repo", "list"]) == 0
    assert "[sparse: a]" in capsys.readouterr().out

    assert cli.main(["repo", "set", "big", "--sparse", "a", "c"]) == 0
    assert "checkout: sparse: a, c" in capsys.readouterr().out
    assert cli.main(["repo", "set", "big", "--sparse", "a", "--no-sparse"]) != 0
