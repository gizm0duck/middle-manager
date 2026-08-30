"""Lease lifecycle around dispatch and worker failure.

Two regressions seen in a real session on 2026-08-30:

- a lease that could not be acquired left a ``requested`` task row with no runner
  and the CLI printed ``dispatch failed: None``;
- workers that failed instantly (a rejected model name) kept their treehouse
  leases forever, so the pool filled with dead slots and later dispatches failed.
"""

from __future__ import annotations

import os
import time

import pytest

from middle_manager import repos
from middle_manager.state import init_db, store
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer
from middle_manager.worktree import LeaseError, LeaseManager


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


def _wait_terminal(client, task_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    terminal = {"worker_done", "blocked", "failed"}
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in terminal:
            return status
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _wait_event(conn, task_id, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND kind = ?", (task_id, kind)
        ).fetchone()
        if row:
            return True
        time.sleep(0.05)
    return False


def _lease_row(conn, task_id):
    return conn.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()


def test_dispatch_marks_task_failed_when_no_lease(server, source_repo, monkeypatch) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)

    def boom(self, **kwargs):
        raise LeaseError("treehouse get failed: pool exhausted")

    monkeypatch.setattr(LeaseManager, "acquire", boom)

    resp = client.dispatch_task(repo=added.name, title="never starts")
    assert resp["ok"] is False
    assert "no worktree lease" in resp["error"]
    assert "pool exhausted" in resp["error"]

    conn = init_db()
    task = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert task["status"] == "failed", "a task with no worktree must not linger as requested"
    err = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'error'", (task["id"],)
    ).fetchone()
    assert err is not None and "pool exhausted" in err["payload"]
    assert store.active_leases(conn) == []


def test_failed_pristine_worker_releases_lease(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)

    resp = client.dispatch_task(repo=added.name, title="doomed", instructions="FAIL")
    assert resp["ok"]
    task_id = resp["task_id"]
    worktree = resp["worktree_path"]

    assert _wait_terminal(client, task_id) == "failed"
    conn = init_db()
    assert _wait_event(conn, task_id, "lease_released")

    lease = _lease_row(conn, task_id)
    assert lease["status"] == "released"
    assert not os.path.exists(worktree), "a pristine failed worktree is handed back"
    assert store.active_leases(conn) == []


def test_failed_dirty_worker_keeps_lease(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)

    resp = client.dispatch_task(repo=added.name, title="half done", instructions="DIRTY_FAIL")
    assert resp["ok"]
    task_id = resp["task_id"]
    worktree = resp["worktree_path"]

    assert _wait_terminal(client, task_id) == "failed"
    # Give the post-run hook a moment; it must decide to keep this one.
    conn = init_db()
    assert not _wait_event(conn, task_id, "lease_released", timeout=1.0)

    lease = _lease_row(conn, task_id)
    assert lease["status"] == "active"
    assert os.path.exists(os.path.join(worktree, "mm-fake-partial.txt")), (
        "uncommitted work is preserved for inspection and resume"
    )
