"""Lifecycle hook ingestion.

Provider harnesses can be configured to call ``mm hook <event>`` at turn
boundaries (e.g. Claude Code Stop / SessionStart). The hook reads a JSON payload
on stdin, records a durable lifecycle event against the task/run, and prints a
JSON acknowledgement. This is the supervision seam for interactive managers; the
per-task runner already normalizes worker streams without hooks.
"""

from __future__ import annotations

import json
from typing import Any

from middle_manager.state import init_db, store

LIFECYCLE_EVENTS = ("session_start", "stop", "notification", "pre_tool", "post_tool")


def _normalize(event: str) -> str:
    return event.strip().lower().replace("-", "_")


def handle_hook(event: str, payload: dict[str, Any]) -> dict:
    """Record a lifecycle hook and return an acknowledgement dict."""
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
    return {"ok": True, "event": normalized, "run_id": run_id, "task_id": task_id}


def handle_hook_stdin(event: str, raw: str) -> dict:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw[:2000]}
    if not isinstance(payload, dict):
        payload = {"_raw": raw[:2000]}
    return handle_hook(event, payload)
