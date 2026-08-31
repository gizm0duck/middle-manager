"""A heartbeat for the manager: one line of team state on a cadence.

The runtime contract asks the manager to check on every in-flight worker every
5–10 minutes and relay what it sees — even when nothing changed. This command is
the mechanism. Each tick prints one digest line: the in-flight tasks with their
liveness and current phase, the tasks waiting on the manager, the events that
arrived since the previous tick, and the size of the open ledger. A harness
monitor wakes the manager on every line, and the manager relays it. ``--once``
prints a single tick for scripts and tests.

It exists because on 2026-08-31 a finished worker sat unpushed and a steer went
undelivered while the manager waited, silently, on completion watchers that had
nothing to say — and the user had to ask whether anyone was watching.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from typing import Any, TextIO

from middle_manager import board, health
from middle_manager.state import init_db

DEFAULT_INTERVAL_SECONDS = 300.0

# Statuses that mean "the manager owes this task an action" right now.
NEEDS_ME = ("worker_done", "blocked", "needs_recovery")

# Event kinds that are provider chatter, not news the manager relays.
NOISE_PREFIXES = ("worker_item.", "worker_thread.", "worker_turn.", "hook_")


def _latest_phase(conn: sqlite3.Connection, task_id: int) -> str | None:
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'worker_progress' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("phase")
    except (TypeError, ValueError):
        return None


def max_event_id(conn: sqlite3.Connection | None = None) -> int:
    conn = conn or init_db()
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def tick(
    conn: sqlite3.Connection | None = None,
    *,
    since_event_id: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One snapshot of team state, plus the events that arrived since ``since_event_id``."""
    conn = conn or init_db()
    now = now or datetime.now(UTC)
    in_flight = health.check(conn, now=now)
    for entry in in_flight:
        entry["phase"] = _latest_phase(conn, entry["task_id"])
    marks = ",".join("?" for _ in NEEDS_ME)
    needs_me = [
        dict(r)
        for r in conn.execute(
            f"SELECT id, status, title FROM tasks WHERE status IN ({marks}) ORDER BY id",
            NEEDS_ME,
        ).fetchall()
    ]
    new_events: dict[str, int] = {}
    last_id = since_event_id
    for row in conn.execute(
        "SELECT id, kind FROM events WHERE id > ? ORDER BY id", (since_event_id,)
    ).fetchall():
        last_id = int(row["id"])
        kind = str(row["kind"])
        if kind.startswith(NOISE_PREFIXES):
            continue
        new_events[kind] = new_events.get(kind, 0) + 1
    return {
        "at": now.isoformat(timespec="seconds"),
        "in_flight": in_flight,
        "needs_me": needs_me,
        "new_events": new_events,
        "last_event_id": last_id,
        "open_todos": len(board.open_todos(conn)),
    }


def render(snapshot: dict[str, Any]) -> str:
    """The one line a harness monitor relays: what is running, what is owed, what is new."""
    when = snapshot["at"][11:16] + " UTC"
    if snapshot["in_flight"]:
        parts = []
        for e in snapshot["in_flight"]:
            piece = f"t{e['task_id']} {e.get('phase') or e['status']} {e['verdict']}"
            if e["verdict"] == "quiet" and e.get("silent_seconds"):
                piece += f" ({health.humanize(e['silent_seconds'])} silent)"
            parts.append(piece)
        in_flight = f"in flight {len(parts)}: " + "; ".join(parts)
    else:
        in_flight = "no workers in flight"
    needs = ", ".join(f"t{r['id']} {r['status']}" for r in snapshot["needs_me"]) or "none"
    new = ", ".join(f"{k}×{v}" for k, v in sorted(snapshot["new_events"].items())) or "none"
    return (
        f"TEAM {when} — {in_flight} | needs me: {needs} | "
        f"new since last tick: {new} | open todos: {snapshot['open_todos']}"
    )


def run(
    interval: float = DEFAULT_INTERVAL_SECONDS,
    *,
    once: bool = False,
    as_json: bool = False,
    out: TextIO | None = None,
    sleep=time.sleep,
) -> int:
    """Print a tick now and then every ``interval`` seconds until interrupted."""
    out = out or sys.stdout  # resolved per call so harness capture sees the ticks
    conn = init_db()
    # The first tick reports the current state, not the whole event history.
    since = max_event_id(conn)
    while True:
        snapshot = tick(conn, since_event_id=since)
        since = snapshot["last_event_id"]
        print(json.dumps(snapshot, default=str) if as_json else render(snapshot), file=out)
        out.flush()
        if once:
            return 0
        sleep(interval)
