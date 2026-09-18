"""What a re-dispatch owes the worker about the attempt before it.

A worker's reflection on the first delivered connected-agent task (issue #79):
the rejection of the prior attempt in the same slot "was recorded in repo notes
only while I was already mid-task". A brief written from the contract alone
sends the second worker down the first one's path. So a dispatch whose
objective a closed, failed or cancelled task in the same repository already
carried is a *re-dispatch*: the brief lint asks for a ``## Prior attempt``
section, and the environment block names the prior task, how it ended, and why,
whether or not the brief did.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from middle_manager.state import store

#: A prior task in one of these is over; the new dispatch is trying again.
ENDED_STATUSES = ("closed", "failed", "cancelled")

#: The task_env key under which dispatch records the brief file it was given.
#: Briefs are named by their place in the plan (b7-cli-browser-signin.md) and a
#: re-dispatch revises and reuses the file, so the path outlives a retitled brief.
BRIEF_PATH_KEY = "brief_path"


@dataclass(frozen=True)
class PriorAttempt:
    task_id: int
    title: str
    status: str
    ended_at: str | None
    reason: str | None
    brief_path: str | None

    def describe(self) -> str:
        when = f" on {self.ended_at[:10]}" if self.ended_at else ""
        why = f": {self.reason}" if self.reason else ""
        return f'task {self.task_id} "{self.title}" ended {self.status}{when}{why}'


def _normalise(title: str | None) -> str:
    return " ".join((title or "").split()).strip().lower()


def _resolved(path: str | None) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def record_brief_path(conn: sqlite3.Connection, task_id: int, brief_path: str | None) -> None:
    """Remember which brief file a task was dispatched from, for the next attempt."""
    resolved = _resolved(brief_path)
    if resolved:
        store.set_task_env(conn, task_id, BRIEF_PATH_KEY, resolved, source="dispatch")


def find(
    conn: sqlite3.Connection,
    repo_id,
    title: str,
    *,
    explicit: int | None = None,
    brief_path: str | None = None,
) -> list[sqlite3.Row]:
    """Ended tasks in this repository that carried this piece of work, newest first.

    Three ways to be the same piece of work, any one enough: ``explicit`` names
    a task by id; ``brief_path`` is the file the prior task was dispatched from
    (the plan-position name survives a retitled revision); the title matches.
    """
    rows: list[sqlite3.Row] = []
    seen: set[int] = set()
    if explicit is not None:
        row = store.get_task(conn, explicit)
        if row is not None:
            rows.append(row)
            seen.add(int(row["id"]))
    if not repo_id:
        return rows
    wanted_title = _normalise(title)
    wanted_path = _resolved(brief_path)
    if not wanted_title and not wanted_path:
        return rows
    marks = ",".join("?" for _ in ENDED_STATUSES)
    for row in conn.execute(
        f"SELECT * FROM tasks WHERE repo_id = ? AND status IN ({marks}) ORDER BY id DESC",
        (repo_id, *ENDED_STATUSES),
    ).fetchall():
        task_id = int(row["id"])
        if task_id in seen:
            continue
        same_file = bool(wanted_path) and (
            store.get_task_env(conn, task_id, BRIEF_PATH_KEY) == wanted_path
        )
        if same_file or (wanted_title and _normalise(row["title"]) == wanted_title):
            rows.append(row)
            seen.add(task_id)
    return rows


def summarize(conn: sqlite3.Connection, task: sqlite3.Row) -> PriorAttempt:
    """How a prior task ended and why, from its own events."""
    from middle_manager.preflight import archived_brief_path

    reason = None
    ended_at = None
    row = conn.execute(
        "SELECT payload, created_at FROM events WHERE task_id = ? AND kind IN "
        "('task_closed', 'error', 'status_set') ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if row is not None:
        ended_at = row["created_at"]
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = {}
        reason = payload.get("reason") or payload.get("summary") or payload.get("note")
    brief_path = None
    repo = conn.execute("SELECT name FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    if repo is not None:
        path = archived_brief_path(repo["name"], int(task["id"]))
        if path.is_file():
            brief_path = str(path)
    return PriorAttempt(
        task_id=int(task["id"]),
        title=task["title"],
        status=task["status"],
        ended_at=ended_at or task["updated_at"],
        reason=reason,
        brief_path=brief_path,
    )


def for_dispatch(
    conn: sqlite3.Connection,
    repo_id,
    title: str,
    *,
    explicit: int | None = None,
    brief_path: str | None = None,
) -> PriorAttempt | None:
    """The newest prior attempt to tell the worker about, or None for a first try."""
    rows = find(conn, repo_id, title, explicit=explicit, brief_path=brief_path)
    return summarize(conn, rows[0]) if rows else None
