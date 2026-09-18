"""Is the worker actually running after a resume?

`mm resume` flips a task to in_progress before the new process exists. Nothing
then checked that the process came up, and on 2026-09-09 three resumes were
into a supervisor the harness had already killed (issue #80). So the resume
waits, briefly, and says what it saw: a live worker process, a worker that
already finished, or nothing — which is an incident, not a status.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from middle_manager.state import init_db, store

DEFAULT_VERIFY_SECONDS = 10.0
_FINISHED = ("worker_done", "worker_stopped", "blocked", "failed", "delivered")


@dataclass(frozen=True)
class Liveness:
    #: ``"alive"``, ``"finished"`` or ``"missing"``.
    verdict: str
    task_status: str | None
    pid: int | None
    runner_status: str | None

    def describe(self, task_id: int) -> str:
        if self.verdict == "alive":
            return f"task {task_id}: worker alive (pid {self.pid})"
        if self.verdict == "finished":
            return f"task {task_id}: worker already finished — status {self.task_status}"
        return (
            f"task {task_id}: no live worker process seen after the resume (task status "
            f"{self.task_status}, runner {self.runner_status or 'none'}). This is an incident: "
            "run `mm health`, then `mm reconcile`, and check the supervisor log before "
            "resuming again"
        )


def verify(task_id: int, *, timeout: float = DEFAULT_VERIFY_SECONDS, poll: float = 0.1) -> Liveness:
    """Wait up to ``timeout`` for a runner with a live pid, or a recorded result."""
    from middle_manager.health import _pid_alive

    deadline = time.monotonic() + timeout
    last: Liveness | None = None
    while True:
        conn = init_db()
        task = store.get_task(conn, task_id)
        status = task["status"] if task else None
        runner = conn.execute(
            "SELECT * FROM runners WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        pid = runner["pid"] if runner else None
        runner_status = runner["status"] if runner else None
        if runner is not None and runner_status == "running" and _pid_alive(pid):
            return Liveness("alive", status, pid, runner_status)
        if runner is not None and runner["result_recorded"] and status in _FINISHED:
            return Liveness("finished", status, pid, runner_status)
        last = Liveness("missing", status, pid, runner_status)
        if time.monotonic() >= deadline:
            return last
        time.sleep(poll)
