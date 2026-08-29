"""Hermetic test: a blocked fake worker resumes and completes (checkpoint steer)."""

from __future__ import annotations

import time

import pytest

from middle_manager import repos
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


def _wait_status(client, task_id, want, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status == want:
            return True
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never reached {want}")


def test_blocked_then_resume_completes(server, source_repo) -> None:
    srv, client = server
    added = repos.add_repo(source_repo)

    resp = client.dispatch_task(
        repo=added.name, title="needs input", instructions="ASK: which endpoint?"
    )
    task_id = resp["task_id"]
    _wait_status(client, task_id, "blocked")

    # Steering a terminal (blocked) task resumes it with the answer.
    steer = client.steer_task(task_id, "use /v2/users")
    assert steer["ok"]
    assert steer["mode"] == "checkpoint"

    _wait_status(client, task_id, "worker_done")
    rs = client.run_status(resp["run_id"])
    kinds = [a["kind"] for a in rs["actionable"]]
    assert "worker_done" in kinds
