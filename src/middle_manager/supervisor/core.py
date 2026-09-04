"""Supervisor business logic (transport-independent).

Owns dispatch, status, actionable-wait, and reconcile. The socket server is a
thin transport around this class; tests drive it directly. Runners execute in
background threads; SQLite is the durable coordination point so state survives a
supervisor restart.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time

from middle_manager import decisions
from middle_manager.config import ConfigError, load_config
from middle_manager.providers.base import TaskSpec
from middle_manager.providers.claude import ClaudeAdapter, effective_allowed_tools
from middle_manager.providers.codex import CodexAdapter
from middle_manager.providers.fake import FakeProvider
from middle_manager.router import CeilingError, WorkerProfile, choose_worker, enforce_ceiling
from middle_manager.schemas import SchemaError, validate_task
from middle_manager.state import init_db, store
from middle_manager.supervisor.runner import RunnerGuardian
from middle_manager.worktree import Lease, LeaseError, LeaseManager


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


def _start_from_branch(worktree_path: str, branch: str, remote: str = "origin") -> str:
    """Move a fresh worktree onto ``<remote>/<branch>`` and return the resulting HEAD.

    ``remote`` is the repo's forge remote. For a repo registered from a local
    path, ``origin`` is that local clone and the branch a stack is built on lives
    on the forge instead.
    """
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", worktree_path, *args], capture_output=True, text=True, check=False
        )

    fetched = git("fetch", "--quiet", remote, branch)
    if fetched.returncode != 0:
        raise SupervisorError(
            f"cannot start from {branch!r}: git fetch {remote} {branch} failed: "
            f"{fetched.stderr.strip()[:200]}"
        )
    reset = git("reset", "--hard", "--quiet", "FETCH_HEAD")
    if reset.returncode != 0:
        raise SupervisorError(
            f"cannot start from {branch!r}: git reset --hard {remote}/{branch} failed: "
            f"{reset.stderr.strip()[:200]}"
        )
    head = git("rev-parse", "HEAD")
    return head.stdout.strip()


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
        provider: str | None = None,
        run_id: int | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        base: str | None = None,
        stack_on: int | None = None,
    ) -> dict:
        # A caller that names no provider gets the configured worker provider. It
        # used to get `fake`, which on 2026-09-04 sent a real task to a stub worker
        # that pushed a branch to a live GitHub remote (issue #49). Imported here,
        # not at module scope, so the hermetic suite can substitute its own default.
        from middle_manager.config import default_worker_provider

        provider = provider or default_worker_provider()
        conn = init_db()
        repo_row = store.get_repo(conn, repo)
        if repo_row is None:
            raise SupervisorError(f"repo {repo!r} is not registered (mm repo add)")

        # A stack is a chain of tasks. Naming the task this one builds on derives
        # the starting branch from its lease, so the manager never has to look up
        # (or misremember) a branch name.
        if stack_on is not None:
            parent = store.get_task(conn, stack_on)
            if parent is None:
                raise SupervisorError(f"cannot stack on task {stack_on}: no such task")
            if not parent["branch"]:
                raise SupervisorError(
                    f"cannot stack on task {stack_on}: it has no branch yet "
                    "(it never got a worktree, so there is nothing to build on)"
                )
            if base and base != parent["branch"]:
                raise SupervisorError(
                    f"--stack-on {stack_on} means starting from {parent['branch']!r}, but "
                    f"--base says {base!r}; pass one or the other"
                )
            base = parent["branch"]

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
        if provider == "claude":
            # A Claude worker with no allowed tools has no shell: it cannot run the
            # suite, commit, or even report progress. That used to happen silently
            # whenever the supervisor was started without MM_CLAUDE_ALLOWED_TOOLS.
            allowed, source = effective_allowed_tools()
            if not allowed:
                raise SupervisorError(
                    f"claude worker would have no tools at all ({source} is empty) — it could "
                    "not run a command, commit, or report progress. Restore the profile with "
                    "`mm config claude --reset`, or set one with "
                    "`mm config claude --allowed-tools 'Read,Edit,Bash(git:*),...'`."
                )

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

        # The task row exists first because the lease's branch name carries the
        # task id. If no worktree can be leased, the row must not linger as a
        # `requested` task with no runner: mark it failed with the reason, and
        # surface that reason to the caller instead of a bare "dispatch failed".
        lease_mgr = LeaseManager()
        try:
            lease = lease_mgr.acquire(
                repo_path=repo_row["local_path"],
                repo_id=repo_row["id"],
                task_id=task_id,
            )
        except LeaseError as exc:
            store.set_task_status(conn, task_id, "failed")
            store.append_event(
                conn,
                kind="error",
                payload={"task_id": task_id, "summary": f"no worktree lease: {exc}"},
                run_id=run_id,
                task_id=task_id,
            )
            raise SupervisorError(f"task {task_id}: no worktree lease: {exc}") from exc
        base_sha = lease.base_sha
        if base:
            # Stacked work starts from the exact tip of the branch it builds on, not
            # from the repo's base. A worker cannot be redirected after it starts,
            # and a wrong starting commit cost a dispatch on 2026-08-30.
            try:
                from middle_manager import repos as _repos

                base_sha = _start_from_branch(
                    lease.worktree_path, base, _repos.upstream_remote(repo_row)
                )
            except SupervisorError as exc:
                store.set_task_status(conn, task_id, "failed")
                store.append_event(
                    conn,
                    kind="error",
                    payload={"task_id": task_id, "summary": str(exc)},
                    run_id=run_id,
                    task_id=task_id,
                )
                with self._lock:
                    self._leases[task_id] = (lease, repo_row["local_path"])
                with contextlib.suppress(Exception):
                    self.release_task_lease(task_id, remove_branch=True)
                raise
        store.update_task_fields(
            conn,
            task_id,
            base_sha=base_sha,
            branch=lease.branch,
            worktree_path=lease.worktree_path,
            lease_id=lease.id,
            stacked_on=base,
            stacked_on_task=stack_on,
        )
        store.append_event(
            conn,
            kind="dispatched",
            payload={
                "task_id": task_id,
                "title": title,
                "provider": provider,
                "lease": lease.id,
                "stacked_on": base,
                "stacked_on_task": stack_on,
            },
            run_id=run_id,
            task_id=task_id,
        )

        # Give the worktree whatever head start this repo has configured — a reused
        # virtualenv from the base clone, a provision command — before the worker
        # starts. Opt-in per repo; a repo with nothing configured does nothing here,
        # and a hook that fails costs the head start, never the dispatch.
        from middle_manager.worktree.provision import provision_worktree

        provision_worktree(
            repo_row,
            lease.worktree_path,
            task_id=task_id,
            run_id=run_id,
            conn=conn,
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
            base_sha=base_sha or "",
            provider=provider,
            model=model,
            reasoning=reasoning,
            run_id=run_id,
            branch=lease.branch,
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
        # A task whose turn ended mid-gate is resumed with what was cut short, not
        # with silence: a bare resume used to hand the worker no reason at all, and
        # a worker that does not know what it left unfinished repeats the ending.
        if message is None:
            from middle_manager import turn_end

            recorded = turn_end.latest_stop_verdict(conn, task_id)
            if recorded is not None and task["status"] == turn_end.WORKER_STOPPED:
                message = recorded.resume_message()
        # A cascade may have rewritten this task's branch while it was stopped.
        # Resuming a worker into a worktree that is behind its own remote ends in
        # a force-push over the cascade, so the worktree is moved onto the remote
        # first — or the resume is refused when both sides hold commits.
        from middle_manager import stacks

        try:
            stacks.sync_worktree_with_remote(task_id)
        except stacks.StackError as exc:
            raise SupervisorError(str(exc)) from exc
        # Any runner still attached to the old session is retired here: it is no
        # longer the task's worker, and its (usually nonzero) exit must not land on
        # the task after this resume. Then say the truth about the task *now* —
        # it is being worked again, not `failed` from the session we just replaced.
        superseded = store.supersede_runners(conn, task_id, reason="resume")
        store.set_task_status(conn, task_id, "in_progress")
        from middle_manager.config import default_worker_provider

        spec = TaskSpec(
            task_id=task_id,
            title=task["title"],
            instructions=message or "",
            worktree_path=task["worktree_path"] or "",
            base_sha=task["base_sha"] or "",
            provider=task["provider"] or default_worker_provider(),
            model=task["model"],
            reasoning=task["reasoning"],
            run_id=task["run_id"],
            branch=task["branch"],
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
            payload={
                "task_id": task_id,
                "session_id": session_id,
                "message": message,
                "superseded_runners": superseded,
                "status": "in_progress",
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        thread.start()
        return {
            "task_id": task_id,
            "resumed_session": session_id,
            "status": "in_progress",
            "superseded_runners": superseded,
        }

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

        # A steer only reaches a worker that is actually mid-turn. A finished,
        # delivered, blocked, or failed task — or one whose runner process has
        # already exited — has no turn to interrupt and no checkpoint coming, so
        # the only way the message can land is by resuming the session with it.
        # (Queueing it "for the checkpoint" of a turn that already ended is how a
        # steer silently vanished on 2026-08-31.)
        if not self._has_live_turn(conn, task):
            return {
                "mode": "resume",
                "note": "no live worker turn; resumed the session with the steer",
                **self.resume_task(task_id, message),
            }

        if can_interrupt:
            return self._interrupt_steer(conn, task, message)

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
            "note": (
                "provider does not support mid-flight steer; queued and applied "
                "automatically when the current turn ends"
            ),
        }

    # How long the auto-resume waits for an interrupted worker process to exit
    # before resuming anyway (the resume is by session id, so it is safe either way).
    INTERRUPT_DRAIN_SECONDS = 60.0

    def _interrupt_steer(self, conn, task, message: str) -> dict:
        """Interrupt the live turn and resume it with the steer, staying in_progress.

        The interrupted process exits nonzero and its adapter reports ``failed``.
        Before the signal goes out the runner is marked superseded, so that exit is
        recorded against the retired session instead of stamping the task — a steer
        is a redirection, not a failure (2026-09-02, task 78). The resume carrying
        the steer text is fired as soon as the old process is gone.
        """
        task_id = int(task["id"])
        with self._lock:
            runner = self._runners.get(task_id)
        superseded = store.supersede_runners(conn, task_id, reason="steer-interrupt")
        store.set_task_status(conn, task_id, "in_progress")
        if runner is not None:
            runner.interrupt()
        store.append_event(
            conn,
            kind="steer",
            payload={
                "task_id": task_id,
                "mode": "interrupt_resume",
                "message": message,
                "superseded_runners": superseded,
                "status": "in_progress",
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        thread = threading.Thread(
            target=self._resume_after_interrupt,
            args=(task_id, message, [r["runner"] for r in superseded]),
            daemon=True,
        )
        thread.start()
        return {
            "mode": "interrupt_resume",
            "task_id": task_id,
            "status": "in_progress",
            "superseded_runners": superseded,
            "note": (
                "interrupt delivered; the session is being resumed with the steer — "
                "the task stays in_progress"
            ),
        }

    def _resume_after_interrupt(self, task_id: int, message: str, runner_ids: list[str]) -> None:
        """Wait for the interrupted worker process to die, then resume with the steer."""
        deadline = time.monotonic() + self.INTERRUPT_DRAIN_SECONDS
        while time.monotonic() < deadline:
            conn = init_db()
            pending = False
            for runner_id in runner_ids:
                row = store.get_runner(conn, runner_id)
                if row is None or row["result_recorded"]:
                    continue
                if _pid_alive(row["pid"]):
                    pending = True
            if not pending:
                break
            time.sleep(0.1)
        try:
            self.resume_task(task_id, message)
        except Exception as exc:  # noqa: BLE001 - a failed resume is visible in events
            conn = init_db()
            task = store.get_task(conn, task_id)
            store.append_event(
                conn,
                kind="error",
                payload={
                    "task_id": task_id,
                    "summary": f"steer interrupt landed but the resume failed: {exc!r}",
                },
                run_id=task["run_id"] if task else None,
                task_id=task_id,
            )

    NO_LIVE_TURN_STATUSES = (
        "worker_done",
        "worker_stopped",
        "delivered",
        "closed",
        "cancelled",
        "blocked",
        "failed",
        "needs_recovery",
    )

    def _has_live_turn(self, conn, task) -> bool:
        """True only while a worker process is actually running a turn for the task."""
        if task["status"] in self.NO_LIVE_TURN_STATUSES:
            return False
        runner = conn.execute(
            "SELECT status FROM runners WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        return bool(runner) and runner["status"] in ("starting", "running")

    def _apply_pending_steer(self, spec: TaskSpec) -> None:
        """Deliver a checkpoint-queued steer now that the worker's turn has ended.

        ``steer_task`` promises "applied at the checkpoint"; this is that
        checkpoint. The newest ``checkpoint_pending`` steer not yet followed by a
        ``steer_applied`` event is resumed into the session exactly once.
        """
        conn = init_db()
        rows = conn.execute(
            "SELECT id, kind, payload FROM events WHERE task_id = ? "
            "AND kind IN ('steer', 'steer_applied') ORDER BY id DESC",
            (spec.task_id,),
        ).fetchall()
        pending: tuple[int, str] | None = None
        for row in rows:
            if row["kind"] == "steer_applied":
                break  # everything older was already delivered
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
            if payload.get("mode") == "checkpoint_pending":
                pending = (int(row["id"]), str(payload.get("message") or ""))
                break
        if pending is None:
            return
        event_id, message = pending
        store.append_event(
            conn,
            kind="steer_applied",
            payload={"task_id": spec.task_id, "steer_event": event_id},
            run_id=spec.run_id,
            task_id=spec.task_id,
        )
        with contextlib.suppress(Exception):  # a failed resume is visible in events
            self.resume_task(spec.task_id, message)

    def _run_task(self, runner: RunnerGuardian, spec: TaskSpec) -> None:
        try:
            result = runner.run(spec)
        except Exception as exc:  # noqa: BLE001 - record, never crash the supervisor
            conn = init_db()
            # Only stamp the task when this runner is still the task's session; a
            # crash in a session that a resume already replaced says nothing about
            # the worker now running.
            if not store.live_runners_for_task(conn, spec.task_id):
                store.set_task_status(conn, spec.task_id, "failed")
            store.append_event(
                conn,
                kind="error",
                payload={"task_id": spec.task_id, "summary": f"runner crashed: {exc!r}"},
                run_id=spec.run_id,
                task_id=spec.task_id,
            )
        else:
            if runner.superseded:
                # A retired session's ending is not this task's checkpoint.
                return
            if result.status == "blocked" and result.question:
                self._maybe_auto_answer(spec, result.question)
            else:
                self._apply_pending_steer(spec)
        # A task that failed without touching its worktree has nothing worth
        # keeping; hand the slot back so the pool cannot fill up with dead leases.
        with contextlib.suppress(Exception):  # lease hygiene never breaks worker completion
            self._release_lease_if_pristine(spec.task_id)
        # This is a cheap deterministic due check, not a model call. It makes
        # run-count and failure-triggered reviews durable immediately.
        try:
            from middle_manager.assessments import ensure_due

            ensure_due()
        except Exception:  # noqa: BLE001 - maintenance never breaks worker completion
            pass

    def _release_lease_if_pristine(self, task_id: int) -> None:
        """Release a failed task's lease when its worktree holds no work.

        A failed worker that never committed or edited anything (a rejected model
        name, a crashed launch) leaves a pristine worktree. Keeping that lease
        only starves later dispatches. A worktree with uncommitted edits or new
        commits is kept for inspection and ``mm resume``.
        """
        conn = init_db()
        task = store.get_task(conn, task_id)
        if task is None or task["status"] != "failed":
            return
        with self._lock:
            entry = self._leases.get(task_id)
        if entry is None:
            return
        lease, repo_path = entry
        assert isinstance(lease, Lease)
        mgr = LeaseManager(lease.backend)
        try:
            pristine = not mgr.is_dirty(lease) and mgr.head_sha(lease) == (lease.base_sha or "")
        except LeaseError:
            # Missing or unreadable worktree: nothing to preserve.
            pristine = True
        if not pristine:
            return
        try:
            self.release_task_lease(task_id, remove_branch=True)
        except LeaseError:
            return
        store.append_event(
            conn,
            kind="lease_released",
            payload={"task_id": task_id, "lease": lease.id, "reason": "failed, pristine"},
            run_id=task["run_id"],
            task_id=task_id,
        )

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
        terminal = {"worker_done", "blocked", "failed", "delivered", "closed", "cancelled"}
        return all(t["status"] in terminal for t in tasks)

    def wait_actionable(self, run_id: int, timeout: float = 30.0, after_seq: int = 0) -> dict:
        """Block until the run has actionable events newer than ``after_seq`` (a cursor).

        Without the cursor a caller keeps seeing the same finished worker forever;
        watchers keyed on status strings then produce false "done" ticks. Pass the
        highest ``seq`` already handled to hear only what is new.
        """
        deadline = time.monotonic() + timeout
        while True:
            conn = init_db()
            actionable = [
                e for e in store.actionable_events(conn, run_id) if int(e["seq"]) > after_seq
            ]
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
