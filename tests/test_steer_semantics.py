"""Steering reaches the worker, or says plainly why it cannot — never vanishes."""

from __future__ import annotations

import json
import time

import pytest

from middle_manager import repos
from middle_manager.state import init_db, store
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer


@pytest.fixture
def server(mm_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


def _wait_status(client, task_id, wanted, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in wanted:
            return status
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never reached {wanted}")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_steering_a_delivered_task_resumes_it_instead_of_queueing(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="ship it")["task_id"]
    _wait_status(client, task_id, {"worker_done"})

    # Delivered tasks used to fall through to the "queued for checkpoint" branch —
    # a checkpoint that never comes once the turn has ended.
    conn = init_db()
    store.set_task_status(conn, task_id, "delivered")

    resp = client.steer_task(task_id, "one more thing")
    assert resp["mode"] == "resume"
    assert "no live worker turn" in resp["note"]
    resumed = _events(task_id, "resumed")
    assert resumed and resumed[-1]["message"] == "one more thing"
    assert not [e for e in _events(task_id, "steer") if e.get("mode") == "checkpoint_pending"]


def test_queued_checkpoint_steer_is_applied_when_the_turn_ends(server, source_repo, monkeypatch):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = client.dispatch_task(repo=added.name, title="long turn")["task_id"]
    _wait_status(client, task_id, {"worker_done"})
    conn = init_db()
    run_id = store.get_task(conn, task_id)["run_id"]

    # Seed the situation the fix covers: a steer queued while the turn was live.
    store.append_event(
        conn,
        kind="steer",
        payload={"task_id": task_id, "mode": "checkpoint_pending", "message": "apply me"},
        run_id=run_id,
        task_id=task_id,
    )
    delivered: list[tuple[int, str | None]] = []
    core = srv.supervisor
    monkeypatch.setattr(
        core, "resume_task", lambda tid, msg=None, **kw: delivered.append((tid, msg))
    )

    from middle_manager.providers.base import TaskSpec

    core._apply_pending_steer(
        TaskSpec(
            task_id=task_id,
            title="long turn",
            instructions="",
            worktree_path="",
            base_sha="",
            provider="fake",
            run_id=run_id,
        )
    )
    assert delivered == [(task_id, "apply me")]
    assert len(_events(task_id, "steer_applied")) == 1

    # Exactly once: a second checkpoint finds nothing pending.
    core._apply_pending_steer(
        TaskSpec(
            task_id=task_id,
            title="long turn",
            instructions="",
            worktree_path="",
            base_sha="",
            provider="fake",
            run_id=run_id,
        )
    )
    assert delivered == [(task_id, "apply me")]
