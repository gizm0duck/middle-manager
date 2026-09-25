"""The manager's review of a worker's plan, and the gate that makes it happen.

A worker posts ``mm progress <id> --phase plan`` before it writes code. That note
is a set of claims about the codebase — "this path skips the consent gate", "this
function is only called from here" — and on 2026-09-25 one of them was false: the
manager relayed it and asked the user to rule on a premise nobody had checked.

So a plan is reviewed, and the review records what was checked. ``mm review plan``
takes one or more ``--checked "<claim> => <evidence>"`` entries, where the
evidence is what the manager ran or read, and exactly one verdict. The review is
bound to the plan note's event id. While a task's newest progress note is a plan
with no review, ``mm resume`` and ``mm steer`` refuse; ``--force-unreviewed
<reason>`` gets past that and leaves the reason on the record.

The review is also what the worker hears next: its message (on approval) or its
findings (on request-changes) ride the next resume, as do the findings of a code
review that requested changes (issue #102: a bare resume after request-changes
used to hand the worker nothing to do, and two workers exited doing nothing).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from middle_manager.state import init_db, store

VERDICTS = ("approved", "changes_requested")


class PlanReviewError(Exception):
    pass


def parse_checked(entry: str) -> dict[str, str]:
    """``"<claim> => <evidence>"`` as a dict; both halves are required."""
    claim, sep, evidence = entry.partition("=>")
    claim, evidence = claim.strip(), evidence.strip()
    if not sep or not claim or not evidence:
        raise PlanReviewError(
            f"--checked needs '<claim> => <evidence>' (what you ran or read), got {entry!r}"
        )
    return {"claim": claim, "evidence": evidence}


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}


def newest_plan_note(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    """The task's newest ``phase=plan`` progress event, or None."""
    return conn.execute(
        "SELECT * FROM events WHERE task_id = ? AND kind = 'worker_progress' "
        "AND json_extract(payload, '$.phase') = 'plan' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def reviews_for(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    """Every plan review of the task, newest first."""
    return list(
        conn.execute(
            "SELECT * FROM plan_reviews WHERE task_id = ? ORDER BY id DESC", (task_id,)
        ).fetchall()
    )


def record(
    task_id: int,
    *,
    checked: list[str],
    verdict: str,
    message: str = "",
    findings: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record a review of the task's newest plan note."""
    if not checked:
        raise PlanReviewError(
            "a plan review needs at least one --checked '<claim> => <evidence>': verify the "
            "plan's load-bearing claims before approving or relaying them"
        )
    entries = [parse_checked(c) for c in checked]
    if verdict not in VERDICTS:
        raise PlanReviewError(f"verdict must be one of {', '.join(VERDICTS)}")
    if verdict == "changes_requested" and not findings.strip():
        raise PlanReviewError("--request-changes needs --findings with something to change")
    conn = conn or init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise PlanReviewError(f"task {task_id} not found")
    note = newest_plan_note(conn, task_id)
    if note is None:
        raise PlanReviewError(f"task {task_id} has no plan note to review")
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO plan_reviews (task_id, progress_event_id, verdict, checked, message, "
        "findings, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            note["id"],
            verdict,
            json.dumps(entries),
            message.strip(),
            findings.strip(),
            now,
        ),
    )
    conn.commit()
    review_id = int(cur.lastrowid)
    store.append_event(
        conn,
        kind="plan_reviewed",
        payload={
            "task_id": task_id,
            "plan_review_id": review_id,
            "progress_event_id": note["id"],
            "verdict": verdict,
            "checked": len(entries),
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    row = conn.execute("SELECT * FROM plan_reviews WHERE id = ?", (review_id,)).fetchone()
    return {"id": review_id, "progress_event_id": note["id"], "verdict": verdict, "row": row}


def awaiting(conn: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    """The newest progress note when it is a plan nobody has reviewed; else None."""
    latest = store.latest_progress(conn, task_id)
    if latest is None or _payload(latest).get("phase") != "plan":
        return None
    reviewed = conn.execute(
        "SELECT 1 FROM plan_reviews WHERE task_id = ? AND progress_event_id = ? LIMIT 1",
        (task_id, latest["id"]),
    ).fetchone()
    if reviewed is not None:
        return None
    return {"task_id": task_id, "event_id": latest["id"], "at": latest["created_at"]}


def all_awaiting(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Every live task whose newest note is an unreviewed plan, oldest plan first."""
    from middle_manager.lifecycle import TERMINAL_STATUSES

    conn = conn or init_db()
    marks = ",".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        f"SELECT id, title FROM tasks WHERE status NOT IN ({marks}) ORDER BY id",
        TERMINAL_STATUSES,
    ).fetchall()
    out = []
    for row in rows:
        entry = awaiting(conn, row["id"])
        if entry is not None:
            out.append({**entry, "title": row["title"]})
    return sorted(out, key=lambda e: e["event_id"])


def describe_awaiting(entry: dict[str, Any]) -> str:
    return (
        f'task {entry["task_id"]} "{entry["title"]}": plan posted {entry["at"]} awaits review — '
        f"check its claims, then `mm review plan {entry['task_id']}`"
    )


def refusal(conn: sqlite3.Connection, task_id: int) -> str | None:
    """The one-line refusal for a resume or steer, or None when the plan gate is clear."""
    if awaiting(conn, task_id) is None:
        return None
    return (
        f"task {task_id}'s newest note is a plan with no review: run `mm review plan "
        f"{task_id}` first (or pass --force-unreviewed <reason>)"
    )


def require_reviewed(
    conn: sqlite3.Connection, task_id: int, *, force_reason: str | None, via: str
) -> None:
    """Refuse an unreviewed plan, or record why the manager went past it."""
    refused = refusal(conn, task_id)
    if refused is None:
        return
    reason = (force_reason or "").strip()
    if not reason:
        raise PlanReviewError(refused)
    entry = awaiting(conn, task_id) or {}
    task = store.get_task(conn, task_id)
    store.append_event(
        conn,
        kind="plan_review_forced",
        payload={
            "task_id": task_id,
            "progress_event_id": entry.get("event_id"),
            "via": via,
            "reason": reason,
            "summary": f"{via} went past an unreviewed plan: {reason}",
        },
        run_id=task["run_id"] if task is not None else None,
        task_id=task_id,
    )


# --------------------------------------------------------------------------- #
# What the next resume tells the worker
# --------------------------------------------------------------------------- #


@dataclass
class Feedback:
    text: str = ""
    plan_review_ids: list[int] = field(default_factory=list)
    review_ids: list[int] = field(default_factory=list)


def _checked(row: sqlite3.Row) -> list[dict[str, str]]:
    try:
        return list(json.loads(row["checked"] or "[]"))
    except (TypeError, ValueError):
        return []


def _plan_feedback(row: sqlite3.Row) -> str:
    lines = []
    if row["verdict"] == "approved":
        lines.append("Your manager reviewed your plan and APPROVED it. Proceed with it.")
        if row["message"]:
            lines.append(f"Manager's message: {row['message']}")
    else:
        lines.append(
            "Your manager reviewed your plan and REQUESTED CHANGES. Revise the plan to "
            "address these findings and post a new `--phase plan` note before implementing:"
        )
        lines.append(row["findings"] or "")
    checks = _checked(row)
    if checks:
        lines.append("Claims your manager checked:")
        lines.extend(f"- {c['claim']} => {c['evidence']}" for c in checks)
    return "\n".join(lines)


def undelivered(conn: sqlite3.Connection, task_id: int) -> Feedback:
    """Plan-review and code-review feedback the worker has not been sent yet."""
    parts: list[str] = []
    feedback = Feedback()
    pending = conn.execute(
        "SELECT * FROM plan_reviews WHERE task_id = ? AND delivered_at IS NULL ORDER BY id",
        (task_id,),
    ).fetchall()
    # The newest plan review speaks for the plan; older undelivered ones were
    # superseded by it and are marked delivered with it.
    if pending:
        parts.append(_plan_feedback(pending[-1]))
        feedback.plan_review_ids = [int(r["id"]) for r in pending]
    # Likewise only the newest code review speaks.
    review = conn.execute(
        "SELECT * FROM reviews WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if (
        review is not None
        and review["verdict"] == "changes_requested"
        and "delivered_at" in review.keys()  # noqa: SIM118 - sqlite3.Row: `in` scans values
        and review["delivered_at"] is None
    ):
        findings = (review["findings"] or "").strip() or "(no findings text was recorded)"
        parts.append(
            f"Your manager reviewed your work at {review['head_sha'][:8]} and REQUESTED "
            f"CHANGES. Address these findings, then finish as instructed:\n{findings}"
        )
        feedback.review_ids = [int(review["id"])]
    feedback.text = "\n\n".join(p for p in parts if p)
    return feedback


def compose_message(
    conn: sqlite3.Connection, task_id: int, message: str | None
) -> tuple[str | None, Feedback]:
    """The resume message with undelivered review feedback folded in front of it."""
    feedback = undelivered(conn, task_id)
    if not feedback.text:
        return message, feedback
    return (f"{feedback.text}\n\n{message}" if message else feedback.text), feedback


def mark_delivered(conn: sqlite3.Connection, feedback: Feedback) -> None:
    now = datetime.now(UTC).isoformat()
    for rid in feedback.plan_review_ids:
        conn.execute("UPDATE plan_reviews SET delivered_at = ? WHERE id = ?", (now, rid))
    for rid in feedback.review_ids:
        conn.execute("UPDATE reviews SET delivered_at = ? WHERE id = ?", (now, rid))
    conn.commit()


# --------------------------------------------------------------------------- #
# The record, printed
# --------------------------------------------------------------------------- #


def render(row: sqlite3.Row) -> str:
    """One plan review as a ``Manager fact-check`` block, ready for a PR body."""
    verdict = "approved" if row["verdict"] == "approved" else "changes requested"
    delivered = row["delivered_at"] or "not yet delivered"
    out = [
        "## Manager fact-check",
        "",
        f"Plan note (progress event {row['progress_event_id']}) reviewed {row['created_at']}: "
        f"{verdict}; delivered to the worker: {delivered}.",
        "",
        "| Claim | Evidence checked |",
        "| --- | --- |",
    ]
    for c in _checked(row):
        claim = c["claim"].replace("|", "\\|")
        evidence = c["evidence"].replace("|", "\\|")
        out.append(f"| {claim} | {evidence} |")
    if row["message"]:
        out += ["", f"Message: {row['message']}"]
    if row["findings"]:
        out += ["", "Findings:", "", row["findings"]]
    return "\n".join(out)
