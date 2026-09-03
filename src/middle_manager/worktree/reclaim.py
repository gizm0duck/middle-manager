"""Worktree inventory and reclamation (``mm worktree list|prune``).

`mm dispatch` refuses below a 5 GB free-space floor, but nothing ever gave the
slots back: on 2026-09-02 twenty gigabytes sat in the worktrees of tasks that had
already been delivered (16 backend slots, 13 frontend), and the only way to see
or reclaim them was `du` plus a hand-written `git worktree remove`.

Reclamation is deliberately conservative. A worktree is removed only when its
task is over **and** the checkout holds nothing that exists nowhere else:

- no uncommitted changes (``git status --porcelain`` is empty), and
- no commits missing from a remote (``git rev-list HEAD --not --remotes`` is 0).

Anything else is skipped and listed with the reason, so a full disk never costs
work that was never pushed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from middle_manager.state import init_db, store
from middle_manager.worktree.lease import Lease, LeaseError, LeaseManager

# A task in one of these is over: nothing will run in its worktree again.
RECLAIMABLE_STATUSES = ("delivered", "closed", "cancelled")


@dataclass
class WorktreeEntry:
    """One leased slot, with everything needed to decide whether it can go."""

    lease_id: str
    path: str
    repo: str | None
    task_id: int | None
    task_status: str | None
    branch: str
    exists: bool = True
    dirty: bool = False
    unpushed_commits: int = 0
    size_bytes: int = 0
    reclaimable: bool = False
    reason: str = ""
    backend: str = "git"
    repo_path: str = ""

    @property
    def slot(self) -> str:
        """The pool slot name — the last path segment, which is what `du` shows."""
        return Path(self.path).name


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip()


def directory_size(path: str) -> int:
    """Bytes on disk under ``path``. Uses ``du`` and falls back to a walk."""
    proc = subprocess.run(["du", "-sk", path], capture_output=True, text=True, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return int(proc.stdout.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _inspect(entry: WorktreeEntry) -> WorktreeEntry:
    """Fill in the disk facts and decide whether this slot can be reclaimed."""
    if not Path(entry.path).exists():
        entry.exists = False
        entry.reclaimable = True
        entry.reason = "worktree directory is already gone; only the lease record remains"
        return entry

    entry.size_bytes = directory_size(entry.path)
    rc, out = _git(["status", "--porcelain"], entry.path)
    if rc != 0:
        entry.reason = "not a readable git worktree; leaving it alone"
        return entry
    entry.dirty = bool(out.strip())

    rc, out = _git(["rev-list", "--count", "HEAD", "--not", "--remotes"], entry.path)
    entry.unpushed_commits = int(out) if rc == 0 and out.isdigit() else -1

    if entry.task_status not in RECLAIMABLE_STATUSES:
        entry.reason = f"task is {entry.task_status or 'unknown'}, not finished with"
        return entry
    if entry.dirty:
        entry.reason = "uncommitted changes in the worktree"
        return entry
    if entry.unpushed_commits != 0:
        count = "unknown" if entry.unpushed_commits < 0 else entry.unpushed_commits
        entry.reason = f"{count} commit(s) exist only here — nothing on a remote holds them"
        return entry
    entry.reclaimable = True
    entry.reason = f"task {entry.task_status}, clean, every commit is on a remote"
    return entry


def list_worktrees(repo: str | None = None) -> list[WorktreeEntry]:
    """Every active lease, newest first, with its task, cleanliness, and size."""
    conn = init_db()
    rows = conn.execute(
        """
        SELECT l.id AS lease_id, l.worktree_path, l.branch, l.task_id, l.backend,
               r.name AS repo_name, r.local_path AS repo_path, t.status AS task_status
        FROM leases l
        LEFT JOIN repos r ON r.id = l.repo_id
        LEFT JOIN tasks t ON t.id = l.task_id
        WHERE l.status = 'active'
        ORDER BY l.created_at DESC
        """
    ).fetchall()
    entries = []
    for row in rows:
        if repo is not None and row["repo_name"] != repo:
            continue
        entries.append(
            _inspect(
                WorktreeEntry(
                    lease_id=row["lease_id"],
                    path=row["worktree_path"],
                    repo=row["repo_name"],
                    task_id=row["task_id"],
                    task_status=row["task_status"],
                    branch=row["branch"],
                    backend=row["backend"],
                    repo_path=row["repo_path"] or "",
                )
            )
        )
    return entries


def reclaimable_bytes(repo: str | None = None) -> int:
    """How much `mm worktree prune` would free right now. Never raises."""
    try:
        return sum(e.size_bytes for e in list_worktrees(repo) if e.reclaimable)
    except Exception:  # noqa: BLE001 - an inventory failure must not break a caller
        return 0


def prune(repo: str | None = None, *, dry_run: bool = False) -> dict:
    """Remove the worktrees of finished tasks that hold nothing unique.

    Returns what went, what stayed and why, and the bytes involved. ``dry_run``
    changes nothing on disk or in the database.
    """
    conn = init_db()
    entries = list_worktrees(repo)
    removed: list[dict] = []
    skipped: list[dict] = []
    for entry in entries:
        record = {
            "lease": entry.lease_id,
            "task_id": entry.task_id,
            "task_status": entry.task_status,
            "path": entry.path,
            "branch": entry.branch,
            "repo": entry.repo,
            "size_bytes": entry.size_bytes,
            "reason": entry.reason,
        }
        if not entry.reclaimable:
            record["dirty"] = entry.dirty
            record["unpushed_commits"] = entry.unpushed_commits
            skipped.append(record)
            continue
        if dry_run:
            removed.append(record)
            continue
        lease = Lease(
            id=entry.lease_id,
            repo_id=None,
            task_id=entry.task_id,
            branch=entry.branch,
            worktree_path=entry.path,
            base_sha=None,
            backend=entry.backend,
        )
        try:
            # The branch is kept: every commit on it is already on a remote, and a
            # kept branch costs nothing while a deleted one cannot be undone.
            LeaseManager(lease.backend).release(
                lease, repo_path=entry.repo_path, remove_branch=False
            )
        except LeaseError as exc:
            record["reason"] = f"could not remove: {exc}"
            skipped.append(record)
            continue
        removed.append(record)
        task = store.get_task(conn, entry.task_id) if entry.task_id else None
        store.append_event(
            conn,
            kind="worktree_pruned",
            payload=record,
            run_id=task["run_id"] if task else None,
            task_id=entry.task_id,
        )

    return {
        "dry_run": dry_run,
        "repo": repo,
        "removed": removed,
        "skipped": skipped,
        "reclaimed_bytes": sum(r["size_bytes"] for r in removed),
        "held_bytes": sum(r["size_bytes"] for r in skipped),
    }


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"
