"""GitHub delivery, gated by the exact-HEAD review.

Delivery is the only path that leaves the machine, so it is the most conservative
step: it refuses unless an approved review is bound to the worktree's current
head. Push uses plain git (works against any origin); PR creation uses ``gh``
(preferred: ``gh-axi`` when present) and is skippable for local fixtures.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from middle_manager.review import ReviewError, head_sha, is_approved_at_head
from middle_manager.state import init_db, store


class DeliveryError(Exception):
    pass


@dataclass
class DeliveryResult:
    task_id: int
    branch: str
    head_sha: str
    pushed: bool
    pr_url: str | None
    note: str


def _run(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _pr_tool() -> str | None:
    for tool in ("gh-axi", "gh"):
        if shutil.which(tool):
            return tool
    return None


def deliver(
    task_id: int,
    *,
    push: bool = True,
    open_pr: bool = True,
    remote: str = "origin",
    base: str | None = None,
) -> DeliveryResult:
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise DeliveryError(f"task {task_id} not found")
    worktree = task["worktree_path"]
    branch = task["branch"]
    if not worktree or not branch:
        raise DeliveryError("task has no worktree/branch to deliver")

    # The gate: never ship work that was not reviewed at its current head.
    approved, reason = is_approved_at_head(task_id)
    if not approved:
        raise DeliveryError(f"refusing to deliver: {reason}")

    head = head_sha(worktree)
    pushed = False
    if push:
        proc = _run(["git", "push", remote, f"HEAD:{branch}"], cwd=worktree)
        if proc.returncode != 0:
            raise DeliveryError(f"git push failed: {proc.stderr.strip()}")
        pushed = True

    pr_url: str | None = None
    note = "pushed" if pushed else "no-op"
    if open_pr:
        tool = _pr_tool()
        if tool is None:
            note = "pushed; no gh/gh-axi found, PR not opened"
        else:
            argv = [
                tool,
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                task["title"],
                "--body",
                f"Automated delivery of task {task_id} ({head[:8]}).",
            ]
            if base:
                argv += ["--base", base]
            proc = _run(argv, cwd=worktree)
            if proc.returncode == 0:
                pr_url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None
                note = "pushed; PR opened"
            else:
                note = f"pushed; PR creation failed: {proc.stderr.strip()[:200]}"

    store.set_task_status(conn, task_id, "delivered")
    store.append_event(
        conn,
        kind="delivered",
        payload={"task_id": task_id, "branch": branch, "head_sha": head, "pr_url": pr_url},
        run_id=task["run_id"],
        task_id=task_id,
    )
    return DeliveryResult(task_id, branch, head, pushed, pr_url, note)


__all__ = ["DeliveryError", "DeliveryResult", "deliver", "ReviewError"]
