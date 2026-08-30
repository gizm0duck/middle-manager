"""Worker health: is every in-flight team member alive, and have we heard from them?

The runner already records everything a worker says (every stream event lands in the
``events`` table) and the pid of its process. Health turns that into a verdict per
in-flight task without a model call:

- ``alive``  — process up and it spoke within the quiet threshold;
- ``quiet``  — process up but silent for longer than the threshold (probably stuck:
  a hung tool, a wedged prompt, a worker waiting on something nobody will give it);
- ``dead``   — no live runner process for an in-flight task (crash, kill, machine
  restart) — ``mm reconcile`` turns this into ``needs_recovery``.

The supervisor polls this on its maintenance tick and raises an actionable
``worker_quiet`` event the first time a worker goes quiet (and again only after it
has been heard from and gone quiet a second time), so ``mm run`` / ``mm wait`` surface
it like any other actionable moment. ``mm health`` prints the same view on demand.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from middle_manager.state import init_db, store

IN_FLIGHT = ("requested", "in_progress")
DEFAULT_QUIET_MINUTES = 15
DEFAULT_PLAN_MINUTES = 10


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def last_heard(conn: sqlite3.Connection, task_id: int) -> datetime | None:
    """When the worker last produced anything — its newest event, else the task row."""
    row = conn.execute(
        "SELECT MAX(created_at) AS at FROM events WHERE task_id = ?", (task_id,)
    ).fetchone()
    heard = _parse(row["at"] if row else None)
    if heard is None:
        task = store.get_task(conn, task_id)
        heard = _parse(task["updated_at"]) if task else None
    return heard


def _latest_runner(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runners WHERE task_id = ? ORDER BY started_at DESC LIMIT 1", (task_id,)
    ).fetchone()


def check(
    conn: sqlite3.Connection | None = None,
    *,
    quiet_after: timedelta | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One health entry per in-flight task, oldest silence first."""
    conn = conn or init_db()
    quiet_after = quiet_after or timedelta(minutes=DEFAULT_QUIET_MINUTES)
    now = now or datetime.now(UTC)
    repos = {r["id"]: r["name"] for r in store.list_repos(conn)}
    marks = ",".join("?" for _ in IN_FLIGHT)
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", IN_FLIGHT
    ).fetchall()

    out: list[dict[str, Any]] = []
    for task in rows:
        runner = _latest_runner(conn, task["id"])
        pid = runner["pid"] if runner else None
        alive = _pid_alive(pid)
        heard = last_heard(conn, task["id"])
        silent = (now - heard) if heard else None
        if not alive:
            verdict = "dead"
        elif silent is not None and silent > quiet_after:
            verdict = "quiet"
        else:
            verdict = "alive"
        out.append(
            {
                "task_id": task["id"],
                "title": task["title"],
                "repo": repos.get(task["repo_id"]),
                "status": task["status"],
                "provider": task["provider"],
                "run_id": task["run_id"],
                "pid": pid,
                "process_alive": alive,
                "last_heard": heard.isoformat(timespec="seconds") if heard else None,
                "silent_seconds": int(silent.total_seconds()) if silent else None,
                "verdict": verdict,
            }
        )
    out.sort(key=lambda e: -(e["silent_seconds"] or 0))
    return out


def humanize(seconds: int | None) -> str:
    if seconds is None:
        return "never heard from"
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def describe(entry: dict[str, Any]) -> str:
    where = f" ({entry['repo']})" if entry.get("repo") else ""
    silence = humanize(entry["silent_seconds"])
    head = f'task {entry["task_id"]} "{entry["title"]}"{where}'
    if entry["verdict"] == "dead":
        return f"{head}: DEAD — no runner process; run `mm reconcile`, then `mm resume`"
    if entry["verdict"] == "quiet":
        return (
            f"{head}: QUIET for {silence} — process up but silent; peek at its progress "
            "log, then `mm steer`/`mm resume` or `mm reconcile` if it's wedged"
        )
    return f"{head}: alive (heard {silence} ago)"


def flag_quiet_workers(
    conn: sqlite3.Connection,
    *,
    quiet_after: timedelta,
    already_flagged: set[int],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Raise one actionable ``worker_quiet`` event per quiet episode.

    ``already_flagged`` is the caller's memory of which tasks were flagged; a task is
    removed from it as soon as it is heard from again so a relapse is flagged anew.
    Returns the entries flagged on this call.
    """
    flagged: list[dict[str, Any]] = []
    entries = check(conn, quiet_after=quiet_after, now=now)
    seen = {e["task_id"] for e in entries}
    for stale in [t for t in already_flagged if t not in seen]:
        already_flagged.discard(stale)  # task left in-flight; forget it
    for e in entries:
        if e["verdict"] != "quiet":
            already_flagged.discard(e["task_id"])
            continue
        if e["task_id"] in already_flagged:
            continue
        already_flagged.add(e["task_id"])
        store.append_event(
            conn,
            kind="worker_quiet",
            payload={
                "task_id": e["task_id"],
                "silent_seconds": e["silent_seconds"],
                "last_heard": e["last_heard"],
                "summary": (
                    f"no output from the worker for {humanize(e['silent_seconds'])}; "
                    "it may be stuck — check its progress log and steer, resume, or reconcile"
                ),
            },
            run_id=e["run_id"],
            task_id=e["task_id"],
        )
        flagged.append(e)
    return flagged


def quiet_threshold() -> timedelta:
    """The configured quiet threshold, defaulting when there is no config yet."""
    try:
        from middle_manager.config import load_config

        minutes = load_config().health.quiet_minutes
    except Exception:  # noqa: BLE001 - health must work before setup
        minutes = DEFAULT_QUIET_MINUTES
    return timedelta(minutes=minutes)


def plan_grace() -> timedelta:
    """How long a worker may run before a missing plan report is flagged."""
    try:
        from middle_manager.config import load_config

        minutes = load_config().health.plan_minutes
    except Exception:  # noqa: BLE001 - health must work before setup
        minutes = DEFAULT_PLAN_MINUTES
    return timedelta(minutes=minutes)


def flag_missing_plans(
    conn: sqlite3.Connection,
    *,
    grace: timedelta,
    already_flagged: set[int],
    now: datetime | None = None,
) -> list[int]:
    """Raise one actionable ``plan_missing`` event per task that never posted a plan.

    Workers are told to report ``mm progress <id> --phase plan`` before implementing.
    A task that has been ``in_progress`` longer than ``grace`` with no plan report is
    flagged once so the manager peeks and steers early instead of reviewing a finished
    diff that went the wrong way.
    """
    now = now or datetime.now(UTC)
    flagged: list[int] = []
    rows = conn.execute("SELECT * FROM tasks WHERE status = 'in_progress' ORDER BY id").fetchall()
    live = {row["id"] for row in rows}
    for stale in [t for t in already_flagged if t not in live]:
        already_flagged.discard(stale)
    for task in rows:
        if task["id"] in already_flagged:
            continue
        if store.has_progress_phase(conn, task["id"], "plan"):
            already_flagged.add(task["id"])  # satisfied; never flag
            continue
        started = _parse(task["updated_at"])
        runner = _latest_runner(conn, task["id"])
        if runner is not None and _parse(runner["started_at"]):
            started = _parse(runner["started_at"])
        if started is None or now - started < grace:
            continue
        already_flagged.add(task["id"])
        store.append_event(
            conn,
            kind="plan_missing",
            payload={
                "task_id": task["id"],
                "running_seconds": int((now - started).total_seconds()),
                "summary": (
                    f"worker has run {humanize(int((now - started).total_seconds()))} without "
                    "posting a plan (`mm progress --phase plan`); peek at it and steer early"
                ),
            },
            run_id=task["run_id"],
            task_id=task["id"],
        )
        flagged.append(task["id"])
    return flagged
