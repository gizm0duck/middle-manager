"""Dispatch preflight: the checks a manager kept in its head, moved into the tool.

Two dispatches were lost on 2026-08-30/31 to conditions ``mm`` could have caught
before creating a task: a brief read from a file that no longer existed (the
worker started with empty instructions) and a worktree checkout that died on a
full disk. ``mm dispatch --brief <file>`` now refuses an empty brief, refuses to
dispatch onto a nearly full disk, and archives the brief it sent next to the
instance state so the exact packet a worker received is always recoverable.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from middle_manager.paths import mm_home

MIN_FREE_GB_ENV = "MM_MIN_FREE_GB"
DEFAULT_MIN_FREE_GB = 5.0


class PreflightError(RuntimeError):
    """A dispatch was refused before any task state was created."""


def read_brief(path: str | Path) -> str:
    """Return the brief's text, refusing a missing or empty file plainly."""
    brief_path = Path(path).expanduser()
    if not brief_path.is_file():
        raise PreflightError(
            f"brief file not found: {brief_path} — nothing was dispatched. "
            "Write the brief to a durable location (for example .mm/briefs/<repo>/) first."
        )
    text = brief_path.read_text(encoding="utf-8")
    if not text.strip():
        raise PreflightError(
            f"brief file is empty: {brief_path} — refusing to dispatch a worker with no "
            "instructions (this is how an empty-brief worker was sent out on 2026-08-30)."
        )
    return text


def min_free_gb() -> float:
    """The free-space floor a dispatch requires, in gigabytes (env-overridable)."""
    raw = os.environ.get(MIN_FREE_GB_ENV)
    if not raw:
        return DEFAULT_MIN_FREE_GB
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MIN_FREE_GB


def check_disk(path: str | Path | None = None, *, floor_gb: float | None = None) -> float:
    """Return free gigabytes at ``path`` (the instance root by default) or refuse.

    A worker needs room for a worktree checkout and a dependency install — several
    gigabytes on a real repository. Dispatching below the floor produces a
    half-created checkout that also blocks the pool until someone removes it.
    """
    target = Path(path) if path is not None else mm_home()
    probe = target if target.exists() else target.parent
    usage = shutil.disk_usage(probe)
    free_gb = usage.free / 1e9
    floor = min_free_gb() if floor_gb is None else floor_gb
    if free_gb < floor:
        raise PreflightError(
            f"only {free_gb:.1f} GB free on the volume holding {probe} (floor {floor:g} GB; "
            f"set {MIN_FREE_GB_ENV} to change it) — free space before dispatching; a "
            "worktree checkout plus a dependency install needs several GB."
        )
    return free_gb


def archive_brief(repo: str, task_id: int, text: str) -> Path:
    """Keep the exact brief a task received under the instance's briefs directory."""
    target_dir = mm_home() / "briefs" / repo
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"task-{task_id}.md"
    target.write_text(text, encoding="utf-8")
    return target
