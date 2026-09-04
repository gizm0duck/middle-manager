"""GitHub delivery, gated by the exact-HEAD review.

Delivery is the only path that leaves the machine, so it is the most conservative
step: it refuses unless an approved review is bound to the worktree's current
head. Push uses plain git (works against any origin); PR creation uses ``gh``
(preferred: ``gh-axi`` when present) and is skippable for local fixtures.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from middle_manager.companions import companion_bin
from middle_manager.review import ReviewError, head_sha, is_approved_at_head
from middle_manager.state import init_db, store


class DeliveryError(Exception):
    pass


_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}\Z")


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
        resolved = companion_bin(tool)
        if resolved:
            return resolved
    return None


def _forge_for_task(conn, task) -> tuple[str, str | None]:
    """The remote to push through and the ``owner/name`` to open the PR against.

    A repo registered from a local path has a local ``origin``: pushing there
    reaches nobody and there is no forge to open a pull request on. The
    registration now carries the forge URL, and the base clone gets a ``forge``
    remote when its ``origin`` is not it — so delivery pushes and opens the pull
    request where the work is actually reviewed.
    """
    from middle_manager import repos

    repo_id = task["repo_id"] if "repo_id" in task.keys() else None  # noqa: SIM118 - sqlite3.Row
    if not repo_id:
        return "origin", None
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "origin", None
    try:
        return repos.upstream_remote(row), repos.forge_slug(row["forge_url"])
    except repos.RepoError:
        return "origin", repos.forge_slug(row["forge_url"])


def deliver(
    task_id: int,
    *,
    push: bool = True,
    open_pr: bool = True,
    remote: str | None = None,
    base: str | None = None,
) -> DeliveryResult:
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise DeliveryError(f"task {task_id} not found")
    forge_remote, forge_slug = _forge_for_task(conn, task)
    remote = remote or forge_remote
    worktree = task["worktree_path"]
    branch = task["branch"]
    if not worktree or not branch:
        raise DeliveryError("task has no worktree/branch to deliver")
    if base is None and "stacked_on" in task.keys():  # noqa: SIM118 - sqlite3.Row: `in` scans values
        # A task dispatched with --base opens its PR against that branch unless
        # the caller says otherwise; stacked PRs no longer depend on remembering.
        base = task["stacked_on"] or None

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
            if forge_slug:
                # The worktree's `origin` may be a local clone; name the forge
                # explicitly so `gh` opens the pull request in the right place.
                argv += ["--repo", forge_slug]
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
        payload={
            "task_id": task_id,
            "branch": branch,
            "head_sha": head,
            "pr_url": pr_url,
            "remote": remote,
            "forge": forge_slug,
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return DeliveryResult(task_id, branch, head, pushed, pr_url, note)


def record_merged(task_id: int, merged_sha: str, *, note: str | None = None) -> DeliveryResult:
    """Record delivery against a commit that is already merged upstream. Pushes nothing.

    When the manager merges a task's PR on GitHub outside ``mm deliver``, the task
    stays ``worker_done``: the heartbeat keeps listing it as work awaiting the
    manager, and the worktree is never eligible for reclamation. There is nothing
    left to push and no review gate to satisfy — the commit shipped — so this is
    pure bookkeeping: it names the merge commit and closes the task out.

    A squash or rebase merge produces a *new* commit that the task's worktree has
    never seen, so the SHA is validated by shape only and taken at its word.
    """
    if not _SHA_RE.match((merged_sha or "").strip()):
        raise DeliveryError(
            f"--merged wants the commit SHA the work landed as; {merged_sha!r} is not one "
            "(7-40 hex characters)"
        )
    merged_sha = merged_sha.strip().lower()
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise DeliveryError(f"task {task_id} not found")
    branch = task["branch"] or ""
    store.set_task_status(conn, task_id, "delivered")
    store.append_event(
        conn,
        kind="delivered",
        payload={
            "task_id": task_id,
            "branch": branch,
            "head_sha": merged_sha,
            "pr_url": None,
            "merged_outside_mm": True,
            "note": note or "recorded against an already-merged commit; nothing was pushed",
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    return DeliveryResult(
        task_id=task_id,
        branch=branch,
        head_sha=merged_sha,
        pushed=False,
        pr_url=None,
        note=f"recorded as delivered at {merged_sha[:8]} (already merged; nothing pushed)",
    )


__all__ = [
    "DeliveryError",
    "DeliveryResult",
    "ReviewError",
    "deliver",
    "record_merged",
]
