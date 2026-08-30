"""Lifecycle hook ingestion.

Provider harnesses can be configured to call ``mm hook <event>`` at turn
boundaries (e.g. Claude Code Stop / SessionStart). The hook reads a JSON payload
on stdin, records a durable lifecycle event against the task/run, and prints a
JSON acknowledgement. This is the supervision seam for interactive managers; the
per-task runner already normalizes worker streams without hooks.
"""

from __future__ import annotations

import json
import re
from typing import Any

from middle_manager.state import init_db, store

LIFECYCLE_EVENTS = (
    "session_start",
    "stop",
    "session_end",
    "notification",
    "pre_tool",
    "post_tool",
)


def _normalize(event: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event.strip())
    return snake.lower().replace("-", "_")


def handle_hook(event: str, payload: dict[str, Any]) -> dict:
    """Record a lifecycle hook and return provider-compatible steering."""
    normalized = _normalize(event)
    conn = init_db()
    task_id = payload.get("task_id")
    run_id = payload.get("run_id")
    if task_id is not None and run_id is None:
        task = store.get_task(conn, int(task_id))
        run_id = task["run_id"] if task else None
    store.append_event(
        conn,
        kind=f"hook_{normalized}",
        payload={"event": normalized, **payload},
        run_id=int(run_id) if run_id is not None else None,
        task_id=int(task_id) if task_id is not None else None,
    )
    result: dict[str, Any] = {
        "ok": True,
        "event": normalized,
        "run_id": run_id,
        "task_id": task_id,
    }

    # Codex and Claude both accept additional context at session start and a
    # blocking continuation at Stop. The stop-active guard is supplied by the
    # harness and prevents the continuation from recursively triggering itself.
    if normalized in ("session_start", "stop"):
        from middle_manager import assessments

        context = assessments.hook_context(conn)
        cycle = assessments.latest_cycle(conn)
        if context and normalized == "session_start":
            result["additionalContext"] = context
            result["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        elif (
            context
            and cycle is not None
            and cycle["status"] == "ready"
            and normalized == "stop"
            and not payload.get("stop_hook_active", False)
        ):
            result.update({"decision": "block", "reason": context})
    return result


def handle_hook_stdin(event: str, raw: str) -> dict:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw[:2000]}
    if not isinstance(payload, dict):
        payload = {"_raw": raw[:2000]}
    return handle_hook(event, payload)
