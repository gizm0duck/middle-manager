"""Durable, scoped decisions and reuse.

Decisions the user (or manager) makes are recorded so the same routine question
is not asked twice. Each decision has a scope (task | run | global) and a
fingerprint of the normalized question, and can be superseded when context
changes. Autonomous question routing consults these before escalating.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

SCOPES = ("task", "run", "global")


def fingerprint(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.strip().lower())
    normalized = re.sub(r"[^\w\s?]", "", normalized)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass
class Decision:
    id: int
    question: str
    answer: str
    scope: str
    run_id: int | None
    task_id: int | None
    fingerprint: str


def record_decision(
    conn: sqlite3.Connection,
    *,
    question: str,
    answer: str,
    scope: str = "run",
    run_id: int | None = None,
    task_id: int | None = None,
    rationale: str | None = None,
    author: str = "user",
) -> int:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    from datetime import UTC, datetime

    fp = fingerprint(question)
    cur = conn.execute(
        """
        INSERT INTO decisions
            (run_id, task_id, question, answer, rationale, scope, author, standing,
             fingerprint, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id if scope != "global" else None,
            task_id if scope == "task" else None,
            question,
            answer,
            rationale,
            scope,
            author,
            1 if scope == "global" else 0,
            fp,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def find_matching(
    conn: sqlite3.Connection,
    question: str,
    *,
    run_id: int | None = None,
    task_id: int | None = None,
) -> Decision | None:
    """Return the most relevant non-superseded decision for a question.

    Precedence: global > run (same run) > task (same task). More specific and
    more recent wins.
    """
    fp = fingerprint(question)
    rows = conn.execute(
        "SELECT * FROM decisions WHERE fingerprint = ? AND superseded_by IS NULL ORDER BY id DESC",
        (fp,),
    ).fetchall()
    best: sqlite3.Row | None = None
    best_rank = -1
    rank = {"global": 3, "run": 2, "task": 1}
    for row in rows:
        scope = row["scope"]
        if scope == "run" and row["run_id"] != run_id:
            continue
        if scope == "task" and row["task_id"] != task_id:
            continue
        if rank[scope] > best_rank:
            best = row
            best_rank = rank[scope]
    if best is None:
        return None
    return Decision(
        id=best["id"],
        question=best["question"],
        answer=best["answer"],
        scope=best["scope"],
        run_id=best["run_id"],
        task_id=best["task_id"],
        fingerprint=best["fingerprint"],
    )


def supersede(conn: sqlite3.Connection, decision_id: int, by_id: int) -> None:
    conn.execute("UPDATE decisions SET superseded_by = ? WHERE id = ?", (by_id, decision_id))
    conn.commit()
