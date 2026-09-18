"""The supervisor and the watch run detached from the harness; resume verifies liveness (issue #80).

The harness reclaimed the supervisor and `mm watch` under memory pressure on
2026-09-09, orphaning three live workers. A detached process has its own
session and no harness-tracked parent. These tests spawn the real CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import middle_manager
from middle_manager import cli, daemon, liveness, repos
from middle_manager.state import init_db, store
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.core import Supervisor

_SRC = os.path.dirname(os.path.dirname(middle_manager.__file__))


def _mm(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, MM_HOME=str(home), PYTHONPATH=_SRC)
    return subprocess.run(
        [sys.executable, "-m", "middle_manager", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _ppid(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or -1)


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def home(mm_home):
    mm_home.mkdir(parents=True, exist_ok=True)
    yield mm_home
    # Never leave a detached process behind, whatever the test did.
    for name in ("supervisor", "watch"):
        daemon.stop(name, grace=2.0)


def test_supervisor_start_detaches_reports_ready_and_is_not_our_child(home) -> None:
    started = _mm(home, "supervisor", "start")
    assert started.returncode == 0, started.stderr
    assert "supervisor started detached (pid " in started.stdout
    pid = daemon.running_pid("supervisor")
    assert pid is not None and f"(pid {pid})" in started.stdout
    assert SupervisorClient().ping()["pid"] == pid
    # Its own session (the intermediate child led it and exited), reparented
    # away from the CLI that started it and from this test's session.
    assert _ppid(pid) != os.getpid()
    assert os.getsid(pid) != os.getsid(os.getpid())
    assert (home / "run" / "supervisor.log").exists()
    assert "(detached)" in (home / "run" / "supervisor.log").read_text()

    status = _mm(home, "supervisor", "status")
    assert f"supervisor up (pid {pid}) (detached)" in status.stdout
    assert "log:" in status.stdout

    # A second start is refused by the owner lock and changes nothing.
    again = _mm(home, "supervisor", "start")
    assert again.returncode == 1
    assert "refusing to start" in again.stderr and "already owns" in again.stderr
    assert daemon.running_pid("supervisor") == pid
    assert SupervisorClient().ping()["pid"] == pid

    stopped = _mm(home, "supervisor", "stop")
    assert stopped.returncode == 0
    _wait_for(lambda: daemon.running_pid("supervisor") is None)
    assert not (home / "run" / "supervisor.pid").exists()


def test_serve_detach_is_the_same_as_start(home) -> None:
    started = _mm(home, "supervisor", "serve", "--detach")
    assert started.returncode == 0, started.stderr
    pid = daemon.running_pid("supervisor")
    assert pid is not None and SupervisorClient().ping()["pid"] == pid
    assert daemon.stop("supervisor") == pid


def test_watch_detaches_writes_ticks_to_its_log_and_stops(home) -> None:
    started = _mm(home, "watch", "--detach", "--interval", "0.2")
    assert started.returncode == 0, started.stderr
    assert "watch started detached (pid " in started.stdout
    pid = daemon.running_pid("watch")
    assert pid is not None
    log = home / "run" / "watch.log"
    _wait_for(lambda: log.exists() and "TEAM " in log.read_text())

    again = _mm(home, "watch", "--detach")
    assert again.returncode == 0 and f"already running (pid {pid})" in again.stdout

    stopped = _mm(home, "watch", "--stop")
    assert stopped.returncode == 0 and f"watch stopped (pid {pid})" in stopped.stdout
    _wait_for(lambda: daemon.running_pid("watch") is None)
    assert "no detached watch was running" in _mm(home, "watch", "--stop").stdout


def test_a_body_that_refuses_leaves_nothing_running(home) -> None:
    def body(ready) -> None:
        ready("refused: not today")

    with pytest.raises(daemon.DetachError, match="not today"):
        daemon.detach("probe", body, ready_timeout=5)
    _wait_for(lambda: daemon.running_pid("probe") is None)


# --------------------------------------------------------------------------- #
# Liveness after resume
# --------------------------------------------------------------------------- #


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def test_liveness_sees_a_live_worker_a_finished_one_and_nothing(mm_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    held = supervisor.dispatch_task(
        repo=added.name, title="hold", instructions="HOLD:1.5", provider="fake"
    )
    seen = liveness.verify(held["task_id"], timeout=5)
    assert seen.verdict == "alive" and seen.pid
    assert "worker alive" in seen.describe(held["task_id"])
    _wait_for(lambda: _status(held["task_id"]) == "worker_done")

    done = liveness.verify(held["task_id"], timeout=1)
    assert done.verdict == "finished" and done.task_status == "worker_done"

    conn = init_db()
    run_id = store.create_run(conn, "ghost")
    ghost = store.add_task(conn, run_id=run_id, title="ghost")
    store.set_task_status(conn, ghost, "in_progress")
    missing = liveness.verify(ghost, timeout=0.3)
    assert missing.verdict == "missing"
    assert "This is an incident" in missing.describe(ghost)


def test_cli_resume_reports_liveness_and_fails_when_no_worker_appears(
    mm_home, source_repo, monkeypatch, capsys
) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    blocked = supervisor.dispatch_task(
        repo=added.name, title="asks", instructions="ASK: which?", provider="fake"
    )
    task_id = blocked["task_id"]
    _wait_for(lambda: _status(task_id) == "blocked")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def resume_task(self, tid, message=None, **kwargs):
            return {"ok": True, **supervisor.resume_task(tid, message)}

    import middle_manager.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    assert cli.main(["resume", str(task_id), "--message", "HOLD:1"]) == 0
    out = capsys.readouterr().out
    assert f"task {task_id}: worker alive (pid " in out
    _wait_for(lambda: _status(task_id) == "worker_done")

    # A resume whose worker never appears is an incident, not a success.
    class GhostClient(FakeClient):
        def resume_task(self, tid, message=None, **kwargs):
            return {"ok": True, "resumed_session": None, "status": "in_progress"}

    monkeypatch.setattr(client_mod, "SupervisorClient", GhostClient)
    conn = init_db()
    run_id = store.create_run(conn, "ghost")
    ghost = store.add_task(conn, run_id=run_id, title="ghost")
    assert cli.main(["resume", str(ghost), "--verify-seconds", "0.3"]) == 1
    assert "no live worker process seen after the resume" in capsys.readouterr().err
    assert cli.main(["resume", str(ghost), "--verify-seconds", "0"]) == 0


def test_status_names_a_dead_worker_as_an_incident(mm_home, capsys) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "r")
    task_id = store.add_task(conn, run_id=run_id, title="died")
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id="dead", task_id=task_id, provider="fake")
    store.update_runner(conn, "dead", pid=2**22 + 4242, status="running")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"INCIDENT: 1 worker process(es) gone with no result — task(s) {task_id}" in out
