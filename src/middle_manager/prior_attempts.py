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

from middle_manager.state import store

#: A prior task in one of these is over; the new dispatch is trying again.
ENDED_STATUSES = ("closed", "failed", "cancelled")


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


def find(
    conn: sqlite3.Connection, repo_id, title: str, *, explicit: int | None = None
) -> list[sqlite3.Row]:
    """Ended tasks in this repository that carried this objective, newest first.

    ``explicit`` names one by id regardless of title — the manager knows a
    re-dispatch when the title changed.
    """
    rows: list[sqlite3.Row] = []
    if explicit is not None:
        row = store.get_task(conn, explicit)
        if row is not None:
            rows.append(row)
    if not repo_id:
        return rows
    wanted = _normalise(title)
    if not wanted:
        return rows
    marks = ",".join("?" for _ in ENDED_STATUSES)
    for row in conn.execute(
        f"SELECT * FROM tasks WHERE repo_id = ? AND status IN ({marks}) ORDER BY id DESC",
        (repo_id, *ENDED_STATUSES),
    ).fetchall():
        if explicit is not None and int(row["id"]) == explicit:
            continue
        if _normalise(row["title"]) == wanted:
            rows.append(row)
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
    conn: sqlite3.Connection, repo_id, title: str, *, explicit: int | None = None
) -> PriorAttempt | None:
    """The newest prior attempt to tell the worker about, or None for a first try."""
    rows = find(conn, repo_id, title, explicit=explicit)
    return summarize(conn, rows[0]) if rows else None
