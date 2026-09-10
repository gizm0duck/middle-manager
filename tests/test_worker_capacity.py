"""Hermetic admission tests for the active worker execution bound."""

from __future__ import annotations

import threading
import time

import pytest

from middle_manager import repos
from middle_manager.config import MMConfig, WorkerCeiling, save_config
from middle_manager.state import init_db, store
from middle_manager.supervisor.core import Supervisor, SupervisorError
from middle_manager.supervisor.runner import RunnerGuardian
from middle_manager.supervisor.server import SupervisorServer


def _bounded_config(limit: int = 1) -> None:
    save_config(
        MMConfig(
            worker=WorkerCeiling("codex", "gpt-5.6-sol", "xhigh", "gpt-5.6-sol", "high", limit)
        )
    )


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for worker state")


def _live(task_id: int) -> bool:
    return bool(store.live_runners_for_task(init_db(), task_id))


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _event_count(task_id: int, kind: str) -> int:
    row = (
        init_db()
        .execute("SELECT COUNT(*) FROM events WHERE task_id = ? AND kind = ?", (task_id, kind))
        .fetchone()
    )
    return int(row[0])


def test_full_capacity_refuses_dispatch_before_task_lease_or_runner_creation(
    mm_home, source_repo
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    first = supervisor.dispatch_task(
        repo=added.name, title="hold the slot", instructions="HOLD:0.5", provider="fake"
    )
    _wait_for(lambda: _live(first["task_id"]))
    conn = init_db()
    before = (
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0],
    )

    with pytest.raises(SupervisorError, match="capacity is full"):
        supervisor.dispatch_task(repo=added.name, title="must wait", provider="fake")

    after = (
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0],
    )
    assert after == before
    _wait_for(lambda: not _live(first["task_id"]))
    admitted = supervisor.dispatch_task(repo=added.name, title="now admitted", provider="fake")
    _wait_for(lambda: _status(admitted["task_id"]) == "worker_done")


def test_two_simultaneous_requests_for_the_last_slot_admit_at_most_one(
    mm_home, source_repo
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    barrier = threading.Barrier(3)
    results: list[dict] = []
    errors: list[str] = []

    def dispatch(title: str) -> None:
        barrier.wait()
        try:
            results.append(
                supervisor.dispatch_task(
                    repo=added.name, title=title, instructions="HOLD:0.5", provider="fake"
                )
            )
        except SupervisorError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=dispatch, args=(f"candidate {i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 1
    assert len(errors) == 1 and "capacity is full" in errors[0]
    assert init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    _wait_for(lambda: not _live(results[0]["task_id"]))


def test_resume_at_capacity_and_duplicate_live_resume_have_no_side_effects(
    mm_home, source_repo
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    stopped = supervisor.dispatch_task(
        repo=added.name, title="needs input", instructions="ASK: which path?", provider="fake"
    )
    _wait_for(lambda: _status(stopped["task_id"]) == "blocked")
    blocker = supervisor.dispatch_task(
        repo=added.name, title="occupy capacity", instructions="HOLD:0.5", provider="fake"
    )
    _wait_for(lambda: _live(blocker["task_id"]))

    before_status = _status(stopped["task_id"])
    before_events = _event_count(stopped["task_id"], "resumed")
    with pytest.raises(SupervisorError, match="capacity is full"):
        supervisor.resume_task(stopped["task_id"], "use /v2")
    assert _status(stopped["task_id"]) == before_status
    assert _event_count(stopped["task_id"], "resumed") == before_events

    blocker_events = _event_count(blocker["task_id"], "resumed")
    with pytest.raises(SupervisorError, match="already has a live execution"):
        supervisor.resume_task(blocker["task_id"], "duplicate")
    assert _event_count(blocker["task_id"], "resumed") == blocker_events
    assert len(store.live_runners_for_task(init_db(), blocker["task_id"])) == 1
    _wait_for(lambda: not _live(blocker["task_id"]))


def test_new_supervisor_counts_persisted_live_runner_until_reconciled(mm_home, source_repo) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    first_supervisor = Supervisor()
    first = first_supervisor.dispatch_task(
        repo=added.name, title="survive restart", instructions="HOLD:0.5", provider="fake"
    )
    _wait_for(lambda: _live(first["task_id"]))

    restarted = Supervisor()
    with pytest.raises(SupervisorError, match="capacity is full"):
        restarted.dispatch_task(repo=added.name, title="not yet", provider="fake")
    _wait_for(lambda: not _live(first["task_id"]))


def test_one_mm_home_refuses_a_second_live_supervisor(mm_home) -> None:
    first = SupervisorServer()
    first.start_background()
    try:
        with pytest.raises(RuntimeError, match="one supervisor per MM_HOME"):
            SupervisorServer().start_background()
    finally:
        first.stop()


def test_slot_releases_after_worker_error_and_launch_failure(
    mm_home, source_repo, monkeypatch
) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    failed = supervisor.dispatch_task(
        repo=added.name, title="provider error", instructions="FAIL", provider="fake"
    )
    _wait_for(lambda: _status(failed["task_id"]) == "failed")

    real_run = RunnerGuardian.run
    calls = 0

    def fail_first_launch(self, spec, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            self.runner_id = "failed-launch"
            store.register_runner(
                init_db(),
                runner_id=self.runner_id,
                task_id=spec.task_id,
                provider=self.adapter.name,
            )
            raise OSError("launch failed")
        return real_run(self, spec, **kwargs)

    monkeypatch.setattr(RunnerGuardian, "run", fail_first_launch)
    launch_failed = supervisor.dispatch_task(repo=added.name, title="launch error", provider="fake")
    _wait_for(lambda: _status(launch_failed["task_id"]) == "failed")
    admitted = supervisor.dispatch_task(repo=added.name, title="after failures", provider="fake")
    _wait_for(lambda: _status(admitted["task_id"]) == "worker_done")


def test_checkpoint_auto_resume_reacquires_a_released_slot(mm_home, source_repo) -> None:
    _bounded_config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(
        repo=added.name, title="checkpoint", instructions="HOLD:0.3", provider="fake"
    )
    _wait_for(lambda: _live(task["task_id"]))
    response = supervisor.steer_task(task["task_id"], "apply after checkpoint")
    assert response["mode"] == "checkpoint_pending"
    _wait_for(lambda: _event_count(task["task_id"], "resumed") == 1)
    _wait_for(lambda: _status(task["task_id"]) == "worker_done")
