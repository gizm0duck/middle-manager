"""Did the worker finish, or did its turn just end?

A Claude worker started the full backend suite, the tool's foreground cap sent it
to the background, the worker ended its turn to wait for it, and the harness read
that turn ending as ``worker_done``: the backgrounded suite was killed with the
session, the branch was never pushed, and no done note was ever filed (task 103,
2026-09-04; task 104 had the same shape with browser smoke retries). The manager
only noticed because the newest progress report said "test" and nothing had been
pushed.

A turn ending is not evidence of completion. This module asks for the evidence:

1. the newest progress note says the worker reached ``done``;
2. the branch holds nothing the remote does not already have;
3. the session's last tool call was not a backgrounded command, which dies with
   the session that started it.

Any of those failing means the task stopped mid-gate — a real state
(``worker_stopped``) with a named reason and a resume that says what was cut
short, rather than a finished task nobody looks at again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from middle_manager.state import store

WORKER_STOPPED = "worker_stopped"


@dataclass
class StopVerdict:
    """Whether the turn ended mid-gate, and the reasons in the manager's words."""

    stopped: bool = False
    reasons: list[str] = field(default_factory=list)
    unpushed: int = 0
    phase: str | None = None
    background_command: str | None = None

    @property
    def summary(self) -> str:
        if not self.stopped:
            return "the worker finished the gate"
        return "worker stopped before done: " + "; ".join(self.reasons)

    def resume_message(self) -> str:
        """The default `mm resume` message: what was cut short, and what to do now."""
        return (
            "Your turn ended before the work was finished: "
            + "; ".join(self.reasons)
            + ". Pick it back up in this worktree: run the authoritative verification "
            "suite in the foreground (never as a background task — a backgrounded "
            "command dies with the session), file your `mm progress <task_id> --phase "
            "done --note ...` report, and push your branch."
        )


def _latest_phase(conn, task_id: int) -> str | None:
    row = store.latest_progress(conn, task_id)
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("phase")
    except (TypeError, ValueError):
        return None


def _tool_calls(payload: object) -> list[dict]:
    """Every tool call in one recorded provider event, in the order they appear.

    Providers wrap tool calls differently — Claude nests ``tool_use`` blocks in an
    assistant message's content — so this walks the payload rather than assuming a
    shape, and accepts anything carrying a name and an input object.
    """
    found: list[dict] = []
    if isinstance(payload, dict):
        is_call = payload.get("type") == "tool_use" or (
            "name" in payload and isinstance(payload.get("input"), dict)
        )
        if is_call and isinstance(payload.get("input"), dict):
            found.append(payload)
        for value in payload.values():
            found.extend(_tool_calls(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_tool_calls(item))
    return found


def last_background_command(conn, task_id: int) -> str | None:
    """The command of the session's last tool call, when that call was backgrounded.

    A backgrounded command is owned by the session that started it; when the turn
    ends, it is killed. If that was the last thing the worker did, whatever it was
    waiting for never finished.
    """
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind LIKE 'worker_%' ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        calls = _tool_calls(payload)
        if not calls:
            continue
        last = calls[-1]
        params = last.get("input") or {}
        if params.get("run_in_background") or params.get("background"):
            return str(params.get("command") or last.get("name") or "a backgrounded command")
        return None
    return None


def why_stopped(conn, task_id: int) -> StopVerdict:
    """Judge a finished turn on evidence. Call it before any supervisor auto-commit.

    The auto-commit is the supervisor's own safety net, not the worker's work, so
    it must not be what makes a task look like it has unpushed commits.
    """
    from middle_manager import stacks

    task = store.get_task(conn, task_id)
    verdict = StopVerdict()
    if task is None:
        return verdict

    verdict.phase = _latest_phase(conn, task_id)
    if verdict.phase != "done":
        verdict.stopped = True
        verdict.reasons.append(
            "no done note was ever filed"
            if verdict.phase is None
            else f"the latest progress note is {verdict.phase!r}, not a done note"
        )

    unpushed = stacks.unpushed_commits(conn, task)
    verdict.unpushed = unpushed
    if unpushed > 0:
        verdict.stopped = True
        verdict.reasons.append(
            f"the branch has {unpushed} commit(s) no remote holds — the work was never pushed"
        )

    command = last_background_command(conn, task_id)
    if command:
        verdict.background_command = command
        verdict.stopped = True
        verdict.reasons.append(
            f"the last thing the session did was background `{command[:120]}`, which was "
            "killed when the turn ended"
        )
    return verdict


def latest_stop_verdict(conn, task_id: int) -> StopVerdict | None:
    """Rebuild the recorded verdict for a task, for `mm task` and the default resume."""
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, WORKER_STOPPED),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return StopVerdict(
        stopped=True,
        reasons=list(payload.get("reasons") or []),
        unpushed=int(payload.get("unpushed") or 0),
        phase=payload.get("phase"),
        background_command=payload.get("background_command"),
    )
