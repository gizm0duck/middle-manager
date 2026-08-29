"""Thin data-access helpers over the SQLite schema.

Kept deliberately small: milestone M1 needs repos, runs, tasks, and the event
stream. Later milestones extend this module rather than reaching into SQL from
across the codebase.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #


def add_repo(
    conn: sqlite3.Connection,
    *,
    name: str,
    origin: str,
    local_path: str,
    default_branch: str | None,
    base_sha: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO repos (name, origin, local_path, default_branch, base_sha, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, origin, local_path, default_branch, base_sha, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_repo(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE name = ?", (name,)).fetchone()


def list_repos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM repos ORDER BY name").fetchall())


def update_repo_base_sha(conn: sqlite3.Connection, name: str, base_sha: str) -> None:
    conn.execute("UPDATE repos SET base_sha = ? WHERE name = ?", (base_sha, name))
    conn.commit()


# --------------------------------------------------------------------------- #
# Runs and tasks
# --------------------------------------------------------------------------- #


def create_run(conn: sqlite3.Connection, objective: str) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO runs (objective, status, created_at, updated_at) VALUES (?,?,?,?)",
        (objective, "requested", now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), run_id),
    )
    conn.commit()


def add_task(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    title: str,
    repo_id: int | None = None,
    role: str = "implementer",
    provider: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO tasks
            (run_id, repo_id, role, title, status, provider, model, reasoning,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?)
        """,
        (run_id, repo_id, role, title, provider, model, reasoning, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    )


def set_task_status(conn: sqlite3.Connection, task_id: int, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), task_id),
    )
    conn.commit()


def update_task_fields(conn: sqlite3.Connection, task_id: int, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), _now(), task_id]
    conn.execute(f"UPDATE tasks SET {cols}, updated_at = ? WHERE id = ?", values)
    conn.commit()


def add_dependency(conn: sqlite3.Connection, task_id: int, depends_on: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?, ?)",
        (task_id, depends_on),
    )
    conn.commit()


def dependencies_of(conn: sqlite3.Connection, task_id: int) -> list[int]:
    rows = conn.execute("SELECT depends_on FROM task_deps WHERE task_id = ?", (task_id,)).fetchall()
    return [int(r[0]) for r in rows]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def next_seq(conn: sqlite3.Connection, run_id: int | None) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id IS ?",
        (run_id,),
    ).fetchone()
    return int(row[0]) + 1


def append_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict,
    run_id: int | None = None,
    task_id: int | None = None,
) -> int:
    seq = next_seq(conn, run_id)
    cur = conn.execute(
        """
        INSERT INTO events (run_id, task_id, seq, kind, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, task_id, seq, kind, json.dumps(payload), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def events_after(conn: sqlite3.Connection, run_id: int | None, after_seq: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM events WHERE run_id IS ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
    )


# Event kinds that require manager or human attention (used by `mm wait`). A
# completed worker is actionable because the manager must review it next.
ACTIONABLE_KINDS = (
    "worker_done",
    "question",
    "blocked",
    "review_requested",
    "run_done",
    "error",
)


def actionable_events(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in ACTIONABLE_KINDS)
    return list(
        conn.execute(
            f"SELECT * FROM events WHERE run_id = ? AND kind IN ({marks}) ORDER BY seq",
            (run_id, *ACTIONABLE_KINDS),
        ).fetchall()
    )


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #


def add_lease(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    repo_id: int | None,
    task_id: int | None,
    branch: str,
    worktree_path: str,
    base_sha: str | None,
    backend: str,
) -> None:
    conn.execute(
        """
        INSERT INTO leases
            (id, repo_id, task_id, branch, worktree_path, base_sha, backend,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (lease_id, repo_id, task_id, branch, worktree_path, base_sha, backend, _now()),
    )
    conn.commit()


def release_lease(conn: sqlite3.Connection, lease_id: str) -> None:
    conn.execute(
        "UPDATE leases SET status = 'released', released_at = ? WHERE id = ?",
        (_now(), lease_id),
    )
    conn.commit()


def active_leases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM leases WHERE status = 'active' ORDER BY created_at"))


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


def register_runner(
    conn: sqlite3.Connection,
    *,
    runner_id: str,
    task_id: int,
    provider: str,
    pid: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runners (id, task_id, pid, provider, status, started_at, heartbeat_at)
        VALUES (?, ?, ?, ?, 'starting', ?, ?)
        """,
        (runner_id, task_id, pid, provider, _now(), _now()),
    )
    conn.commit()


def update_runner(conn: sqlite3.Connection, runner_id: str, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), runner_id]
    conn.execute(f"UPDATE runners SET {cols} WHERE id = ?", values)
    conn.commit()


def get_runner(conn: sqlite3.Connection, runner_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runners WHERE id = ?", (runner_id,)).fetchone()


def live_runners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM runners WHERE status IN ('starting','running') ORDER BY started_at"
        )
    )


# --------------------------------------------------------------------------- #
# Sessions and usage
# --------------------------------------------------------------------------- #


def upsert_session(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    provider: str,
    provider_session_id: str | None,
    status: str = "active",
) -> int:
    now = _now()
    existing = conn.execute(
        "SELECT id FROM sessions WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO sessions
                (task_id, provider, provider_session_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, provider, provider_session_id, status, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE sessions SET provider_session_id = ?, status = ?, updated_at = ? WHERE id = ?",
        (provider_session_id, status, now, existing["id"]),
    )
    conn.commit()
    return int(existing["id"])


def record_usage(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    task_id: int | None,
    provider: str,
    model: str | None,
    reasoning: str | None,
    input_tokens: int,
    output_tokens: int,
) -> None:
    conn.execute(
        """
        INSERT INTO usage
            (run_id, task_id, provider, model, reasoning, input_tokens,
             output_tokens, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, task_id, provider, model, reasoning, input_tokens, output_tokens, _now()),
    )
    conn.commit()


def usage_by_profile(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT provider, model, reasoning,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COUNT(*) AS calls
        FROM usage WHERE run_id = ?
        GROUP BY provider, model, reasoning
        ORDER BY provider, model
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "provider": r["provider"],
            "model": r["model"],
            "reasoning": r["reasoning"],
            "input_tokens": int(r["input"]),
            "output_tokens": int(r["output"]),
            "calls": int(r["calls"]),
        }
        for r in rows
    ]


def usage_totals(conn: sqlite3.Connection, run_id: int) -> dict:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output
        FROM usage WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return {"input_tokens": int(row["input"]), "output_tokens": int(row["output"])}
