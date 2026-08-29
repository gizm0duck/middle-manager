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

from middle_manager.providers.base import TaskSpec
from middle_manager.providers.fake import FakeProvider
from middle_manager.state import init_db, store
from middle_manager.supervisor.runner import RunnerGuardian
from middle_manager.worktree import LeaseManager


class SupervisorError(Exception):
    pass


def _adapter_for(provider: str):
    if provider == "fake":
        return FakeProvider()
    raise SupervisorError(
        f"provider {provider!r} is not available in this milestone (M2 ships 'fake')"
    )


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

    def _run_task(self, runner: RunnerGuardian, spec: TaskSpec) -> None:
        try:
            runner.run(spec)
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
