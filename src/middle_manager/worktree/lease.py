"""Worktree lease lifecycle.

A lease is an identity-bound, isolated Git worktree branched from a repository's
base clone. Workers only ever touch their leased worktree.

Two backends implement the same ``Lease`` contract:

- ``git`` — the always-available reference backend using plain ``git worktree``
  under ``.mm/worktree-pools/``.
- ``treehouse`` — the pinned companion (https://github.com/kunchenguid/treehouse),
  which maintains a pool of pre-warmed, reusable worktrees. We use its
  non-interactive lease interface: ``treehouse get --lease --json`` durably
  reserves a pooled worktree and prints ``{path, lease_id, ...}``; ``treehouse
  return <path> --force --if-lease-id <id>`` releases it.

Backend selection: an explicit ``backend`` argument wins; otherwise ``auto``
consults ``MM_LEASE_BACKEND`` and then prefers ``treehouse`` when its binary is on
PATH, falling back to ``git``. Either way the lease is recorded in SQLite so
recovery and reconcile can find and clean up orphans.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from middle_manager.companions import companion_bin, has_companion
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
    #: Set when a sparse repository's worktree could not be made sparse: the worker
    #: then has more of the repository than it should, and dispatch says so.
    sparse_warning: str | None = None


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LeaseError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
# Treehouse backend helpers (pure argv/JSON so they are unit-testable)
# --------------------------------------------------------------------------- #


def _th_acquire_argv(holder: str) -> list[str]:
    return ["treehouse", "get", "--lease", "--json", "--lease-holder", holder]


def _th_return_argv(path: str, lease_id: str) -> list[str]:
    return ["treehouse", "return", path, "--force", "--if-lease-id", lease_id]


def _parse_lease_json(text: str) -> dict:
    """Parse ``treehouse get --lease --json`` stdout into a lease dict."""
    try:
        data = json.loads(text.strip())
    except (ValueError, TypeError) as exc:
        raise LeaseError(f"could not parse treehouse lease JSON: {text!r}") from exc
    if "path" not in data or "lease_id" not in data:
        raise LeaseError(f"treehouse lease JSON missing path/lease_id: {data!r}")
    return data


def _run_treehouse(argv: list[str], cwd: str) -> str:
    # Prefer the provisioned copy under .mm/tools/bin; fall back to PATH. The argv
    # builders stay pure (argv[0] == "treehouse") so only the executable is swapped.
    resolved = companion_bin(argv[0]) or argv[0]
    proc = subprocess.run(
        [resolved, *argv[1:]], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        # Update banners go to stderr; only surface it on failure.
        raise LeaseError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def resolve_backend(backend: str | None) -> str:
    """Resolve a backend name. Explicit wins; else env; else auto-detect.

    ``MM_LEASE_BACKEND`` lets the runtime prefer git (e.g. the hermetic test
    suite) or force treehouse without code changes.
    """
    if backend not in (None, "auto"):
        if backend not in ("git", "treehouse"):
            raise LeaseError(f"unknown lease backend {backend!r}")
        return backend
    env = os.environ.get("MM_LEASE_BACKEND")
    if env in ("git", "treehouse"):
        return env
    if env not in (None, "", "auto"):
        raise LeaseError(f"unknown MM_LEASE_BACKEND {env!r}")
    return "treehouse" if has_companion("treehouse") else "git"


class LeaseManager:
    """Acquire and release worktree leases against a repository base clone."""

    def __init__(self, backend: str | None = None) -> None:
        self.backend = resolve_backend(backend)

    def acquire(
        self,
        *,
        repo_path: str,
        repo_id: int | None = None,
        task_id: int | None = None,
        branch_prefix: str = "mm",
        branch: str | None = None,
        sparse_paths: list[str] | None = None,
    ) -> Lease:
        """Lease a fresh worktree; a fresh branch unless ``branch`` names the task's own.

        ``branch`` is for a task getting its worktree *back* (issue #58): the
        pull request, the remote, and every record already carry that name, so
        the rebuilt checkout is put on it — the existing local branch when the
        base clone still has one, a new branch at the clone's head otherwise.
        The caller moves it to the right commit afterwards.

        ``sparse_paths`` (a sparse repository's cone directories) makes the worktree
        sparse on exactly those; None is a full checkout, exactly as before. A
        worktree that could not be made sparse is still handed out, with
        ``sparse_warning`` saying why.
        """
        if self.backend == "treehouse":
            return self._acquire_treehouse(
                repo_path, repo_id, task_id, branch_prefix, branch, sparse_paths
            )
        return self._acquire_git(repo_path, repo_id, task_id, branch_prefix, branch, sparse_paths)

    def _acquire_git(
        self,
        repo_path: str,
        repo_id: int | None,
        task_id: int | None,
        branch_prefix: str,
        branch: str | None,
        sparse_paths: list[str] | None = None,
    ) -> Lease:
        lease_id = uuid.uuid4().hex[:12]
        pool = worktree_pools_dir()
        pool.mkdir(parents=True, exist_ok=True)
        worktree_path = str(pool / lease_id)

        base_sha = _git(["rev-parse", "HEAD"], cwd=repo_path)
        # A sparse repository's worktree is created empty, made sparse, and only
        # then checked out, so a large repository is never materialized in full.
        no_checkout = ["--no-checkout"] if sparse_paths is not None else []
        if branch and _branch_exists(repo_path, branch):
            _git(["worktree", "add", "-q", *no_checkout, worktree_path, branch], cwd=repo_path)
        else:
            branch = branch or f"{branch_prefix}/task-{task_id or 'adhoc'}-{lease_id}"
            _git(
                ["worktree", "add", "-q", *no_checkout, "-b", branch, worktree_path, "HEAD"],
                cwd=repo_path,
            )
        warning = None
        if sparse_paths is not None:
            warning = _make_sparse(worktree_path, sparse_paths)
            # The empty worktree's index and files come from HEAD, inside the cone.
            _git(["read-tree", "-mu", "HEAD"], cwd=worktree_path)
        lease = self._record(lease_id, repo_id, task_id, branch, worktree_path, base_sha)
        lease.sparse_warning = warning
        return lease

    def _acquire_treehouse(
        self,
        repo_path: str,
        repo_id: int | None,
        task_id: int | None,
        branch_prefix: str,
        branch: str | None,
        sparse_paths: list[str] | None = None,
    ) -> Lease:
        holder = f"mm-task-{task_id or 'adhoc'}"
        out = _run_treehouse(_th_acquire_argv(holder), cwd=repo_path)
        data = _parse_lease_json(out)
        worktree_path = data["path"]
        lease_id = data["lease_id"]
        base_sha = _git(["rev-parse", "HEAD"], cwd=worktree_path)
        # Treehouse hands out a pooled checkout; give the task its own branch so
        # delivery can push HEAD:<branch> exactly as with the git backend.
        if branch and _branch_exists(worktree_path, branch):
            _git(["switch", branch], cwd=worktree_path)
        else:
            branch = branch or f"{branch_prefix}/task-{task_id or 'adhoc'}-{lease_id[:12]}"
            _git(["switch", "-c", branch], cwd=worktree_path)
        # A pooled worktree is reused across tasks and repo settings, so it follows
        # the repository's current sparse setting on every lease. git (2.36+) copies
        # the base clone's sparse patterns into a new worktree, but a reused one
        # keeps whatever it had, so the setting is applied (or removed) each time.
        warning = _treehouse_sparse(worktree_path, sparse_paths)
        lease = self._record(lease_id, repo_id, task_id, branch, worktree_path, base_sha)
        lease.sparse_warning = warning
        return lease

    def _record(
        self,
        lease_id: str,
        repo_id: int | None,
        task_id: int | None,
        branch: str,
        worktree_path: str,
        base_sha: str,
    ) -> Lease:
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
        if lease.backend == "treehouse":
            self._release_treehouse(lease)
        else:
            self._release_git(lease, repo_path, remove_branch)
        conn = init_db()
        store.release_lease(conn, lease.id)

    def _release_git(self, lease: Lease, repo_path: str, remove_branch: bool) -> None:
        try:
            _git(["worktree", "remove", "--force", lease.worktree_path], cwd=repo_path)
        except LeaseError:
            _git(["worktree", "prune"], cwd=repo_path)
        if remove_branch:
            with_suppressed_branch_delete(repo_path, lease.branch)

    def _release_treehouse(self, lease: Lease) -> None:
        # Guarded by the lease id so we never return someone else's worktree.
        # cwd is irrelevant to `treehouse return <path>`; use the worktree itself.
        cwd = lease.worktree_path if Path(lease.worktree_path).exists() else os.getcwd()
        with contextlib.suppress(LeaseError):
            _run_treehouse(_th_return_argv(lease.worktree_path, lease.id), cwd=cwd)

    def head_sha(self, lease: Lease) -> str:
        if not Path(lease.worktree_path).exists():
            raise LeaseError(f"worktree {lease.worktree_path} is missing")
        return _git(["rev-parse", "HEAD"], cwd=lease.worktree_path)

    def is_dirty(self, lease: Lease) -> bool:
        out = _git(["status", "--porcelain"], cwd=lease.worktree_path)
        return bool(out.strip())


def _make_sparse(worktree_path: str, sparse_paths: list[str]) -> str | None:
    """Make a worktree sparse; the reason it is not, as a dispatch warning, or None."""
    from middle_manager.repos import apply_sparse

    failed = apply_sparse(worktree_path, sparse_paths)
    if failed:
        return (
            f"this repository is sparse but worktree {worktree_path} is not: {failed}. "
            "The worker sees more of the repository than configured."
        )
    return None


def _treehouse_sparse(worktree_path: str, sparse_paths: list[str] | None) -> str | None:
    """Bring a pooled worktree in line with its repository's sparse setting."""
    if sparse_paths is not None:
        return _make_sparse(worktree_path, sparse_paths)
    enabled = subprocess.run(
        ["git", "-C", worktree_path, "config", "--get", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if enabled != "true":
        return None
    from middle_manager.repos import disable_sparse

    failed = disable_sparse(worktree_path)
    if failed:
        return (
            f"pooled worktree {worktree_path} is still sparse from an earlier task, but this "
            f"repository is a full checkout: {failed}. The worker is missing files."
        )
    return None


def _branch_exists(repo_path: str, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def with_suppressed_branch_delete(repo_path: str, branch: str) -> None:
    """Delete a lease branch, ignoring the not-found case."""
    with contextlib.suppress(LeaseError):
        _git(["branch", "-D", branch], cwd=repo_path)
