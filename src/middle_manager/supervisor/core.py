"""Supervisor business logic (transport-independent).

Owns dispatch, status, actionable-wait, and reconcile. The socket server is a
thin transport around this class; tests drive it directly. Runners execute in
background threads; SQLite is the durable coordination point so state survives a
supervisor restart.
"""

from __future__ import annotations

import os
import threading
import time

from middle_manager import decisions
from middle_manager.config import ConfigError, load_config
from middle_manager.providers.base import TaskSpec
from middle_manager.providers.claude import ClaudeAdapter
from middle_manager.providers.codex import CodexAdapter
from middle_manager.providers.fake import FakeProvider
from middle_manager.router import CeilingError, WorkerProfile, choose_worker, enforce_ceiling
from middle_manager.schemas import SchemaError, validate_task
from middle_manager.state import init_db, store
from middle_manager.supervisor.runner import RunnerGuardian
from middle_manager.worktree import LeaseManager


class SupervisorError(Exception):
    pass


def _adapter_for(provider: str):
    if provider == "fake":
        return FakeProvider()
    if provider == "claude":
        return ClaudeAdapter()
    if provider == "codex":
        return CodexAdapter()
    raise SupervisorError(f"unknown provider {provider!r}")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Supervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._runners: dict[int, RunnerGuardian] = {}
        self._leases: dict[int, object] = {}
        # Question fingerprints already auto-answered per task; a fingerprint that
        # reappears after its stored answer is escalated instead of looping.
        self._auto_answered: dict[int, set[str]] = {}

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def dispatch_task(
        self,
        *,
        repo: str,
        title: str,
        instructions: str = "",
        provider: str = "fake",
        run_id: int | None = None,
        model: str | None = None,
        reasoning: str | None = None,
    ) -> dict:
        conn = init_db()
        repo_row = store.get_repo(conn, repo)
        if repo_row is None:
            raise SupervisorError(f"repo {repo!r} is not registered (mm repo add)")

        # Validate the task packet contract before doing any work.
        packet = {
            "title": title,
            "repo": repo,
            "instructions": instructions,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "run_id": run_id,
        }
        try:
            validate_task(packet)
        except SchemaError as exc:
            raise SupervisorError(f"invalid task packet: {exc}") from exc

        # Enforce the hard worker ceiling for real providers (runtime, not prompt).
        if provider in ("claude", "codex"):
            model, reasoning = self._resolve_and_enforce(provider, model, reasoning)

        if run_id is None:
            run_id = store.create_run(conn, title)
            store.set_run_status(conn, run_id, "running")

        task_id = store.add_task(
            conn,
            run_id=run_id,
            title=title,
            repo_id=repo_row["id"],
            provider=provider,
            model=model,
            reasoning=reasoning,
        )

        lease_mgr = LeaseManager()
        lease = lease_mgr.acquire(
            repo_path=repo_row["local_path"],
            repo_id=repo_row["id"],
            task_id=task_id,
        )
        store.update_task_fields(
            conn,
            task_id,
            base_sha=lease.base_sha,
            branch=lease.branch,
            worktree_path=lease.worktree_path,
            lease_id=lease.id,
        )
        store.append_event(
            conn,
            kind="dispatched",
            payload={"task_id": task_id, "title": title, "provider": provider, "lease": lease.id},
            run_id=run_id,
            task_id=task_id,
        )

        # Point the worker at this repo's shared memory (learnings + progress log) so
        # it uses and updates it — which is also how the manager follows progress
        # without interrupting. Seeding is idempotent; files already exist from add.
        from middle_manager import memory

        memory.seed_repo_memory(repo)

        spec = TaskSpec(
            task_id=task_id,
            title=title,
            instructions=instructions,
            worktree_path=lease.worktree_path,
            base_sha=lease.base_sha or "",
            provider=provider,
            model=model,
            reasoning=reasoning,
            run_id=run_id,
            memory_preamble=memory.worker_context(repo, task_id=task_id),
        )
        adapter = _adapter_for(provider)
        runner = RunnerGuardian(adapter)

        with self._lock:
            self._runners[task_id] = runner
            self._leases[task_id] = (lease, repo_row["local_path"])

        thread = threading.Thread(target=self._run_task, args=(runner, spec), daemon=True)
        with self._lock:
            self._threads[task_id] = thread
        thread.start()

        return {
            "run_id": run_id,
            "task_id": task_id,
            "lease_id": lease.id,
            "branch": lease.branch,
            "worktree_path": lease.worktree_path,
        }

    def _resolve_and_enforce(
        self, provider: str, model: str | None, reasoning: str | None
    ) -> tuple[str | None, str | None]:
        """Resolve worker profile and enforce the ceiling (runtime, not prompt).

        ``model=None`` means "use the provider account default": we never fabricate
        a model name, because an unsupported explicit model makes the CLI error
        (e.g. Codex on a ChatGPT account rejects an explicit gpt-5-codex). For a
        provider with a known-valid cheap tier (claude) we pick the smallest
        eligible model when none is given; otherwise we keep None.
        """
        try:
            config = load_config()
        except ConfigError as exc:
            raise SupervisorError(f"run `mm setup` first: {exc}") from exc

        if model is None and provider == "claude":
            model = choose_worker(config).model

        try:
            if model is not None:
                enforce_ceiling(config, WorkerProfile(provider, model, reasoning or "low"))
            elif reasoning is not None:
                # Enforce only the reasoning ceiling when the model is a default.
                enforce_ceiling(
                    config,
                    WorkerProfile(config.worker.provider, config.worker.max_model, reasoning),
                )
        except CeilingError as exc:
            raise SupervisorError(f"worker ceiling: {exc}") from exc
        return model, reasoning

    def resume_task(self, task_id: int, message: str | None = None) -> dict:
        """Resume a task's provider session (blocked-worker resume / checkpoint steer)."""
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None:
            raise SupervisorError(f"task {task_id} not found")
        sess = conn.execute(
            "SELECT * FROM sessions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        session_id = sess["provider_session_id"] if sess else None
        spec = TaskSpec(
            task_id=task_id,
            title=task["title"],
            instructions=message or "",
            worktree_path=task["worktree_path"] or "",
            base_sha=task["base_sha"] or "",
            provider=task["provider"] or "fake",
            model=task["model"],
            reasoning=task["reasoning"],
            run_id=task["run_id"],
            resume_session_id=session_id,
            steer_message=message,
        )
        adapter = _adapter_for(spec.provider)
        runner = RunnerGuardian(adapter)
        with self._lock:
            self._runners[task_id] = runner
        thread = threading.Thread(target=self._run_task, args=(runner, spec), daemon=True)
        with self._lock:
            self._threads[task_id] = thread
        store.append_event(
            conn,
            kind="resumed",
            payload={"task_id": task_id, "session_id": session_id, "message": message},
            run_id=task["run_id"],
            task_id=task_id,
        )
        thread.start()
        return {"task_id": task_id, "resumed_session": session_id}

    def steer_task(self, task_id: int, message: str) -> dict:
        """Steer a task.

        Interrupt steering is offered only when the provider/version proves it
        (capability-gated). Otherwise steering is checkpoint-at-completion: the
        message is applied by resuming the session once the current turn ends.
        """
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None:
            raise SupervisorError(f"task {task_id} not found")
        provider = task["provider"] or "fake"
        adapter = _adapter_for(provider)
        can_interrupt = getattr(adapter, "supports_interrupt_steer", lambda: False)()

        terminal = task["status"] in ("worker_done", "blocked", "failed")
        if terminal:
            return {"mode": "checkpoint", **self.resume_task(task_id, message)}

        if can_interrupt:
            with self._lock:
                runner = self._runners.get(task_id)
            if runner is not None:
                runner.interrupt()
            # After the interrupt lands, resume with the steer message.
            store.append_event(
                conn,
                kind="steer",
                payload={"task_id": task_id, "mode": "interrupt", "message": message},
                run_id=task["run_id"],
                task_id=task_id,
            )
            return {
                "mode": "interrupt",
                "task_id": task_id,
                "note": "interrupt delivered; resume applies the steer",
            }

        # Checkpoint steering: record the pending steer to apply at completion.
        store.append_event(
            conn,
            kind="steer",
            payload={"task_id": task_id, "mode": "checkpoint_pending", "message": message},
            run_id=task["run_id"],
            task_id=task_id,
        )
        return {
            "mode": "checkpoint_pending",
            "task_id": task_id,
            "note": "provider does not support mid-flight steer; queued for checkpoint",
        }

    def _run_task(self, runner: RunnerGuardian, spec: TaskSpec) -> None:
        try:
            result = runner.run(spec)
        except Exception as exc:  # noqa: BLE001 - record, never crash the supervisor
            conn = init_db()
            store.set_task_status(conn, spec.task_id, "failed")
            store.append_event(
                conn,
                kind="error",
                payload={"task_id": spec.task_id, "summary": f"runner crashed: {exc!r}"},
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
        else:
            if result.status == "blocked" and result.question:
                self._maybe_auto_answer(spec, result.question)
        finally:
            # This is a cheap deterministic due check, not a model call. It makes
            # run-count and failure-triggered reviews durable immediately.
            try:
                from middle_manager.assessments import ensure_due

                ensure_due()
            except Exception:  # noqa: BLE001 - maintenance never breaks worker completion
                pass

    def _maybe_auto_answer(self, spec: TaskSpec, question: str) -> None:
        """Resolve a blocking question from durable decisions, or leave it for a human.

        A routine question already answered by a scoped decision is resumed
        autonomously. A question that reappears after we applied its stored answer
        is left actionable so the human is not looped.
        """
        conn = init_db()
        fp = decisions.fingerprint(question)
        answered = self._auto_answered.setdefault(spec.task_id, set())
        if fp in answered:
            store.append_event(
                conn,
                kind="blocked",
                payload={
                    "task_id": spec.task_id,
                    "question": question,
                    "summary": "recurring question after auto-answer; escalating to human",
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            return
        context = _task_context(conn, spec.task_id)
        try:
            match = decisions.find_matching(
                conn, question, run_id=spec.run_id, task_id=spec.task_id, context=context
            )
        except decisions.StaleDecision as stale:
            # A prior answer exists but the premise changed: ask for the delta.
            store.append_event(
                conn,
                kind="blocked",
                payload={
                    "task_id": spec.task_id,
                    "question": question,
                    "summary": "premise changed since a prior decision; confirmation needed",
                    "prior_decision_id": stale.decision.id,
                    "prior_answer": stale.decision.answer,
                },
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
            return
        if match is None:
            return  # leave the `question` event actionable for a human
        answered.add(fp)
        store.append_event(
            conn,
            kind="auto_answered",
            payload={
                "task_id": spec.task_id,
                "question": question,
                "answer": match.answer,
                "decision_id": match.id,
                "scope": match.scope,
            },
            run_id=spec.run_id,
            task_id=spec.task_id,
        )
        self.resume_task(spec.task_id, match.answer)

    def answer_question(
        self,
        task_id: int,
        answer: str,
        *,
        scope: str = "run",
        rationale: str | None = None,
    ) -> dict:
        """Record the user's answer as a durable decision and resume the task."""
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None:
            raise SupervisorError(f"task {task_id} not found")
        question = _last_question(conn, task_id)
        if question is None:
            raise SupervisorError(f"task {task_id} has no pending question")
        decision_id = decisions.record_decision(
            conn,
            question=question,
            answer=answer,
            scope=scope,
            run_id=task["run_id"],
            task_id=task_id,
            rationale=rationale,
            context=_task_context(conn, task_id),
        )
        resumed = self.resume_task(task_id, answer)
        return {"task_id": task_id, "decision_id": decision_id, **resumed}

    # ------------------------------------------------------------------ #
    # Status and wait
    # ------------------------------------------------------------------ #
    def task_status(self, task_id: int) -> dict:
        conn = init_db()
        row = store.get_task(conn, task_id)
        if row is None:
            raise SupervisorError(f"task {task_id} not found")
        return dict(row)

    def run_status(self, run_id: int) -> dict:
        conn = init_db()
        run = store.get_run(conn, run_id)
        if run is None:
            raise SupervisorError(f"run {run_id} not found")
        tasks = [dict(t) for t in store.list_tasks(conn, run_id)]
        actionable = [
            {"seq": e["seq"], "kind": e["kind"], "payload": e["payload"]}
            for e in store.actionable_events(conn, run_id)
        ]
        return {
            "run": dict(run),
            "tasks": tasks,
            "actionable": actionable,
            "usage": store.usage_totals(conn, run_id),
        }

    def _all_terminal(self, run_id: int) -> bool:
        conn = init_db()
        tasks = store.list_tasks(conn, run_id)
        if not tasks:
            return False
        terminal = {"worker_done", "blocked", "failed", "delivered", "cancelled"}
        return all(t["status"] in terminal for t in tasks)

    def wait_actionable(self, run_id: int, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            conn = init_db()
            actionable = store.actionable_events(conn, run_id)
            if actionable or self._all_terminal(run_id):
                return {
                    "run_id": run_id,
                    "actionable": [
                        {"seq": e["seq"], "kind": e["kind"], "payload": e["payload"]}
                        for e in actionable
                    ],
                    "all_terminal": self._all_terminal(run_id),
                }
            if time.monotonic() > deadline:
                return {
                    "run_id": run_id,
                    "actionable": [],
                    "all_terminal": False,
                    "timed_out": True,
                }
            time.sleep(0.1)

    # ------------------------------------------------------------------ #
    # Reconcile
    # ------------------------------------------------------------------ #
    def reconcile(self) -> dict:
        """Find half-alive runners and mark them for recovery (fail-closed)."""
        conn = init_db()
        findings = []
        for row in store.live_runners(conn):
            pid = row["pid"]
            if not _pid_alive(pid):
                # Process is gone but no result was recorded: never assume success.
                store.update_runner(conn, row["id"], status="orphaned")
                task = store.get_task(conn, row["task_id"])
                if task and task["status"] in ("in_progress", "requested", "dispatched"):
                    store.set_task_status(conn, row["task_id"], "needs_recovery")
                    store.append_event(
                        conn,
                        kind="error",
                        payload={
                            "task_id": row["task_id"],
                            "summary": "runner process vanished without a result; "
                            "marked needs_recovery",
                        },
                        run_id=task["run_id"] if task else None,
                        task_id=row["task_id"],
                    )
                findings.append(
                    {
                        "runner": row["id"],
                        "task_id": row["task_id"],
                        "action": "orphaned->needs_recovery",
                    }
                )
        return {"reconciled": findings}

    def release_task_lease(self, task_id: int, *, remove_branch: bool = False) -> None:
        with self._lock:
            entry = self._leases.pop(task_id, None)
        if entry is None:
            return
        lease, repo_path = entry
        LeaseManager(lease.backend).release(lease, repo_path=repo_path, remove_branch=remove_branch)

    def shutdown(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.interrupt()


def _task_context(conn, task_id: int) -> str | None:
    """A compact premise fingerprint for a task's decisions.

    Uses the task's repo base SHA: if the repository state a decision was made
    under has moved, the decision is treated as premise-changed on reuse. This is
    a conservative proxy for "relevant repository facts changed".
    """
    row = conn.execute("SELECT base_sha FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    base = row["base_sha"]
    return f"repo@{base[:12]}" if base else None


def _last_question(conn, task_id: int) -> str | None:
    import json

    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind IN ('question','blocked') "
        "ORDER BY seq DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("question")
    except (ValueError, KeyError, TypeError):
        return None
