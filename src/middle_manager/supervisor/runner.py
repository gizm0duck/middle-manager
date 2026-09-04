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
from dataclasses import dataclass, field

from middle_manager.providers.base import ProviderAdapter, TaskSpec, WorkerResult
from middle_manager.state import init_db, store
from middle_manager.supervisor import autocommit
from middle_manager.supervisor.spool import EventSpool


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


@dataclass
class Finalized:
    """The outcome of the end-of-task auto-commit."""

    head_sha: str | None = None
    committed: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def _finalize_worktree(spec: TaskSpec, summary: str) -> Finalized:
    """Ensure completed work is committed; report what was staged and what was not.

    Workers may or may not commit. The manager owns the reviewable commit, so a
    completed task with a dirty worktree is committed here with a generated
    message — but only for the paths that belong on the branch. Evidence and
    receipt directories a worker wrote for the review are held back (see
    ``supervisor.autocommit``); they stay in the worktree and are named in the
    task's event, never silently swept into the commit.
    """
    if not spec.worktree_path:
        return Finalized()
    committed, excluded = autocommit.stage(spec.worktree_path)
    if committed:
        _git(
            [
                "-c",
                "user.name=Middle Manager",
                "-c",
                "user.email=manager@mm.local",
                "commit",
                "-qm",
                f"task {spec.task_id}: {summary or spec.title}",
            ],
            cwd=spec.worktree_path,
        )
    head = _git(["rev-parse", "HEAD"], cwd=spec.worktree_path)
    return Finalized(head_sha=head or None, committed=committed, excluded=excluded)


_TERMINAL_STATUS = {"completed": "worker_done", "blocked": "blocked", "failed": "failed"}


def worker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a worker process: it can call ``mm`` from its worktree.

    ``mm`` resolves ``.mm`` from the cwd unless ``MM_HOME`` is set, and a worker's cwd
    is its leased worktree — so pin ``MM_HOME`` to this instance and put the repo's
    ``bin/`` first on PATH. That is what makes ``mm progress`` work for workers.
    """
    from middle_manager.manager.launch import repo_root
    from middle_manager.paths import mm_home

    env = dict(base if base is not None else os.environ)
    env["MM_HOME"] = str(mm_home())
    bin_dir = os.path.join(repo_root(), "bin")
    env["PATH"] = os.pathsep.join([bin_dir, env.get("PATH", "")])
    return env


class RunnerGuardian:
    def __init__(self, adapter: ProviderAdapter) -> None:
        self.adapter = adapter
        self._proc: subprocess.Popen | None = None
        self._interrupt = threading.Event()
        # Set when this runner's session was retired mid-flight (a resume or a
        # steer's interrupt started a newer session for the same task).
        self.superseded = False

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
        env = worker_env(self.adapter.child_env() if hasattr(self.adapter, "child_env") else None)

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
        session_seen: str | None = spec.resume_session_id
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
                session_seen = ev.session_id
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

        # A runner superseded by a resume (or by a steer's interrupt) is no longer
        # the task's session. Its exit — usually a nonzero code from the interrupt
        # that ended it — must not rewrite the task's status or raise an actionable
        # error, or a live resumed worker reads as `failed` (2026-09-01, codex).
        session_id = result.session_id or session_seen
        if store.is_runner_superseded(conn, runner_id):
            self.superseded = True
            store.update_runner(
                conn,
                runner_id,
                status="superseded",
                exit_code=exit_code if exit_code is not None else -1,
                result_recorded=1,
            )
            store.append_event(
                conn,
                kind="runner_superseded",
                payload={
                    "task_id": spec.task_id,
                    "runner": runner_id,
                    "session_id": session_id,
                    "exit_code": exit_code,
                    "worker_status": result.status,
                    "summary": result.summary,
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            self._proc = None
            return result

        # A turn ending is not evidence that the gate was finished. Judge it on the
        # evidence *before* the auto-commit below, so the supervisor's own safety
        # commit is never what makes the worker look like it left work unpushed.
        from middle_manager import turn_end

        verdict = turn_end.StopVerdict()
        if result.status == "completed":
            verdict = turn_end.why_stopped(conn, spec.task_id)

        task_status = _TERMINAL_STATUS.get(result.status, "failed")
        if verdict.stopped:
            task_status = turn_end.WORKER_STOPPED
        store.set_task_status(conn, spec.task_id, task_status)

        # For a completed task, guarantee a reviewable commit and record head.
        finalized = None
        if result.status == "completed" and not result.head_sha:
            finalized = _finalize_worktree(spec, result.summary)
            result.head_sha = finalized.head_sha
            # Say out loud what the auto-commit staged and what it held back, so a
            # missing file is a recorded decision rather than a surprise at review.
            store.append_event(
                conn,
                kind="autocommit",
                payload={
                    "task_id": spec.task_id,
                    "head_sha": finalized.head_sha,
                    "committed": finalized.committed,
                    "excluded": finalized.excluded,
                    "summary": (
                        f"auto-committed {len(finalized.committed)} path(s); "
                        f"left {len(finalized.excluded)} out of the commit"
                        + (f": {', '.join(finalized.excluded)}" if finalized.excluded else "")
                    ),
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )

        # Emit an actionable event for the manager/human loop. Every terminal event
        # carries the session it came from so a late arrival can be attributed.
        if verdict.stopped:
            store.append_event(
                conn,
                kind=turn_end.WORKER_STOPPED,
                payload={
                    "task_id": spec.task_id,
                    "head_sha": result.head_sha,
                    "session_id": session_id,
                    "summary": verdict.summary,
                    "reasons": verdict.reasons,
                    "unpushed": verdict.unpushed,
                    "phase": verdict.phase,
                    "background_command": verdict.background_command,
                    "resume_message": verdict.resume_message(),
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
        elif result.status == "completed":
            store.append_event(
                conn,
                kind="worker_done",
                payload={
                    "task_id": spec.task_id,
                    "head_sha": result.head_sha,
                    "summary": result.summary,
                    "session_id": session_id,
                    "excluded_from_commit": finalized.excluded if finalized else [],
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
        elif result.status == "blocked":
            store.append_event(
                conn,
                kind="question",
                payload={
                    "task_id": spec.task_id,
                    "question": result.question,
                    "session_id": session_id,
                },
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
                    "session_id": session_id,
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
