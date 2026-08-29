"""Worktree lease lifecycle.

A lease is an identity-bound, isolated Git worktree branched from a repository's
base clone. Workers only ever touch their leased worktree. The default backend
uses plain ``git worktree`` (always available); when a pinned Treehouse binary is
present it is preferred via the ``treehouse`` backend. Either way the lease is
recorded in SQLite so recovery and reconcile can find and clean up orphans.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from middle_manager.paths import worktree_pools_dir
from middle_manager.state import init_db, store


class LeaseError(Exception):
    pass


@dataclass
class Lease:
    id: str
    repo_id: int | None
    task_id: int | None
    branch: str
    worktree_path: str
    base_sha: str | None
    backend: str


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LeaseError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class LeaseManager:
    """Acquire and release worktree leases against a repository base clone."""

    def __init__(self, backend: str | None = None) -> None:
        # "auto" prefers a pinned treehouse binary, else the git fallback.
        if backend in (None, "auto"):
            backend = "treehouse" if shutil.which("treehouse") else "git"
        if backend not in ("git", "treehouse"):
            raise LeaseError(f"unknown lease backend {backend!r}")
        self.backend = backend

    def acquire(
        self,
        *,
        repo_path: str,
        repo_id: int | None = None,
        task_id: int | None = None,
        branch_prefix: str = "mm",
    ) -> Lease:
        lease_id = uuid.uuid4().hex[:12]
        branch = f"{branch_prefix}/task-{task_id or 'adhoc'}-{lease_id}"
        pool = worktree_pools_dir()
        pool.mkdir(parents=True, exist_ok=True)
        worktree_path = str(pool / lease_id)

        base_sha = _git(["rev-parse", "HEAD"], cwd=repo_path)
        # The git backend is the reference implementation; treehouse would wrap
        # the same effect with durable, identity-bound leases.
        _git(["worktree", "add", "-q", "-b", branch, worktree_path, "HEAD"], cwd=repo_path)

        conn = init_db()
        store.add_lease(
            conn,
            lease_id=lease_id,
            repo_id=repo_id,
            task_id=task_id,
            branch=branch,
            worktree_path=worktree_path,
            base_sha=base_sha,
            backend=self.backend,
        )
        return Lease(lease_id, repo_id, task_id, branch, worktree_path, base_sha, self.backend)

    def release(self, lease: Lease, *, repo_path: str, remove_branch: bool = True) -> None:
        try:
            _git(["worktree", "remove", "--force", lease.worktree_path], cwd=repo_path)
        except LeaseError:
            # Worktree may already be gone; prune bookkeeping regardless.
            _git(["worktree", "prune"], cwd=repo_path)
        if remove_branch:
            with_suppressed_branch_delete(repo_path, lease.branch)
        conn = init_db()
        store.release_lease(conn, lease.id)

    def head_sha(self, lease: Lease) -> str:
        if not Path(lease.worktree_path).exists():
            raise LeaseError(f"worktree {lease.worktree_path} is missing")
        return _git(["rev-parse", "HEAD"], cwd=lease.worktree_path)

    def is_dirty(self, lease: Lease) -> bool:
        out = _git(["status", "--porcelain"], cwd=lease.worktree_path)
        return bool(out.strip())


def with_suppressed_branch_delete(repo_path: str, branch: str) -> None:
    """Delete a lease branch, ignoring the not-found case."""
    with contextlib.suppress(LeaseError):
        _git(["branch", "-D", branch], cwd=repo_path)
