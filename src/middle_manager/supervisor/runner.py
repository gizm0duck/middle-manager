"""Per-task runner guardian.

Owns exactly one worker subprocess: spawns it in its own process group, streams
its normalized event stream to the spool and SQLite, and records a fail-closed
result. It never infers success from a missing process — a worker that exits
without a result event is a failure to be reconciled, not a silent success.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import uuid
from collections.abc import Callable

from middle_manager.providers.base import ProviderAdapter, TaskSpec, WorkerResult
from middle_manager.state import init_db, store
from middle_manager.supervisor.spool import EventSpool

_TERMINAL_STATUS = {"completed": "worker_done", "blocked": "blocked", "failed": "failed"}


class RunnerGuardian:
    def __init__(self, adapter: ProviderAdapter) -> None:
        self.adapter = adapter
        self._proc: subprocess.Popen | None = None
        self._interrupt = threading.Event()

    def interrupt(self) -> None:
        """Request a cooperative stop of the running worker."""
        self._interrupt.set()
        proc = self._proc
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), self.adapter.interrupt_signal())

    def run(
        self,
        spec: TaskSpec,
        *,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> WorkerResult:
        conn = init_db()
        runner_id = uuid.uuid4().hex[:12]
        store.register_runner(
            conn, runner_id=runner_id, task_id=spec.task_id, provider=self.adapter.name
        )
        spool = EventSpool(spec.run_id, spec.task_id)

        argv = self.adapter.resume(spec) if spec.resume_session_id else self.adapter.start(spec)
        env = None
        if hasattr(self.adapter, "child_env"):
            env = self.adapter.child_env()

        self._proc = subprocess.Popen(
            argv,
            cwd=spec.worktree_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        store.update_runner(conn, runner_id, pid=self._proc.pid, status="running")
        store.set_task_status(conn, spec.task_id, "in_progress")

        events = []
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            ev = self.adapter.parse_event(raw)
            if ev is None:
                continue
            events.append(ev)
            spool.append(ev.kind, ev.raw)
            store.append_event(
                conn,
                kind=f"worker_{ev.kind}",
                payload=ev.raw,
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            if ev.session_id:
                store.upsert_session(
                    conn,
                    task_id=spec.task_id,
                    provider=self.adapter.name,
                    provider_session_id=ev.session_id,
                )
                store.update_runner(conn, runner_id, session_id=ev.session_id)
            if on_event:
                on_event(ev.kind, ev.raw)

        stderr = self._proc.stderr.read() if self._proc.stderr else ""
        self._proc.wait()
        exit_code = self._proc.returncode

        result = self.adapter.result(events, exit_code)
        if result.usage is not None:
            store.record_usage(
                conn,
                run_id=spec.run_id,
                task_id=spec.task_id,
                provider=result.usage.provider,
                model=result.usage.model or spec.model,
                reasoning=result.usage.reasoning or spec.reasoning,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )

        task_status = _TERMINAL_STATUS.get(result.status, "failed")
        store.set_task_status(conn, spec.task_id, task_status)

        # Emit an actionable event for the manager/human loop.
        if result.status == "completed":
            store.append_event(
                conn,
                kind="worker_done",
                payload={
                    "task_id": spec.task_id,
                    "head_sha": result.head_sha,
                    "summary": result.summary,
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
        elif result.status == "blocked":
            store.append_event(
                conn,
                kind="question",
                payload={"task_id": spec.task_id, "question": result.question},
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
        else:
            store.append_event(
                conn,
                kind="error",
                payload={
                    "task_id": spec.task_id,
                    "summary": result.summary,
                    "exit_code": exit_code,
                    "stderr": stderr[-2000:],
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )

        store.update_runner(
            conn,
            runner_id,
            status="exited",
            exit_code=exit_code if exit_code is not None else -1,
            result_recorded=1,
        )
        self._proc = None
        return result
