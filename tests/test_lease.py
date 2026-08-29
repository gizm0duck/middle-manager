"""Hermetic tests for worktree leases (git backend)."""

from __future__ import annotations

import os

from middle_manager.state import init_db, store
from middle_manager.worktree import LeaseManager


def test_acquire_and_release(mm_home, source_repo) -> None:
    mgr = LeaseManager(backend="git")
    # task_id=None keeps this a pure lease test (no FK to a task row).
    lease = mgr.acquire(repo_path=source_repo, task_id=None)
    assert os.path.isdir(lease.worktree_path)
    assert lease.branch.startswith("mm/task-adhoc-")
    assert lease.base_sha

    conn = init_db()
    assert len(store.active_leases(conn)) == 1

    # The worktree is a real checkout of the base commit.
    assert os.path.exists(os.path.join(lease.worktree_path, "README.md"))
    assert mgr.head_sha(lease) == lease.base_sha
    assert mgr.is_dirty(lease) is False

    mgr.release(lease, repo_path=source_repo)
    assert not os.path.isdir(lease.worktree_path)
    assert len(store.active_leases(conn)) == 0
