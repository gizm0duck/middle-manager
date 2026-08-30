"""Hermetic tests for worker health: alive / quiet / dead, and the supervisor poller."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from middle_manager import health
from middle_manager.cli import main
from middle_manager.config import MMConfig, save_config
from middle_manager.state import init_db, store
from middle_manager.supervisor.server import SupervisorServer


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


def _in_flight_task(conn, *, title="build", pid=4242, heard_ago: timedelta | None = None):
    run_id = store.create_run(conn, title)
    task_id = store.add_task(conn, run_id=run_id, title=title)
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id=f"r{task_id}", task_id=task_id, provider="claude")
    store.update_runner(conn, f"r{task_id}", pid=pid, status="running")
    if heard_ago is not None:
        at = (datetime.now(UTC) - heard_ago).isoformat()
        conn.execute(
            "INSERT INTO events (run_id, task_id, seq, kind, payload, created_at) "
            "VALUES (?, ?, 1, 'worker_text', '{}', ?)",
            (run_id, task_id, at),
        )
        conn.commit()
    return task_id


def test_verdicts(mm_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: pid == 4242)
    alive = _in_flight_task(conn, title="alive", heard_ago=timedelta(minutes=1))
    quiet = _in_flight_task(conn, title="quiet", heard_ago=timedelta(minutes=40))
    dead = _in_flight_task(conn, title="dead", pid=1, heard_ago=timedelta(minutes=2))

    entries = {e["task_id"]: e for e in health.check(conn, quiet_after=timedelta(minutes=15))}

    assert entries[alive]["verdict"] == "alive"
    assert entries[quiet]["verdict"] == "quiet"
    assert entries[quiet]["silent_seconds"] >= 40 * 60
    assert entries[dead]["verdict"] == "dead"
    assert "QUIET for 40m" in health.describe(entries[quiet])
    assert "DEAD" in health.describe(entries[dead])


def test_last_heard_falls_back_to_task_row(mm_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    task_id = _in_flight_task(conn)  # no events at all
    (entry,) = health.check(conn, quiet_after=timedelta(minutes=15))
    assert entry["task_id"] == task_id
    assert entry["verdict"] == "alive"
    assert entry["last_heard"] is not None


def test_flag_quiet_workers_once_per_episode(mm_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    task_id = _in_flight_task(conn, heard_ago=timedelta(minutes=30))
    flagged: set[int] = set()
    threshold = timedelta(minutes=15)

    first = health.flag_quiet_workers(conn, quiet_after=threshold, already_flagged=flagged)
    assert [e["task_id"] for e in first] == [task_id]
    assert flagged == {task_id}
    # Second tick: still quiet, but already flagged — no duplicate event.
    assert health.flag_quiet_workers(conn, quiet_after=threshold, already_flagged=flagged) == []
    events = conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,)).fetchall()
    assert [e["kind"] for e in events].count("worker_quiet") == 1

    # The flagged event is actionable, so `mm run`/`mm wait` surface it.
    run_id = store.get_task(conn, task_id)["run_id"]
    assert any(e["kind"] == "worker_quiet" for e in store.actionable_events(conn, run_id))

    # The worker speaks again → the episode ends and a relapse is flagged anew.
    store.append_event(conn, kind="worker_text", payload={}, run_id=run_id, task_id=task_id)
    assert health.flag_quiet_workers(conn, quiet_after=threshold, already_flagged=flagged) == []
    assert flagged == set()


def test_supervisor_tick_polls_health(mm_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    task_id = _in_flight_task(conn, heard_ago=timedelta(hours=1))
    server = SupervisorServer(socket_path=str(mm_home / "t.sock"))

    server._tick_health()

    kinds = [
        r["kind"] for r in conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,))
    ]
    assert "worker_quiet" in kinds
    assert server._quiet_flagged == {task_id}


def test_config_health_threshold_and_cli(mm_home, monkeypatch, capsys) -> None:
    save_config(MMConfig())
    assert health.quiet_threshold() == timedelta(minutes=15)
    assert main(["config", "health", "--quiet-minutes", "5"]) == 0
    assert "5m of silence" in capsys.readouterr().out
    assert health.quiet_threshold() == timedelta(minutes=5)

    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    _in_flight_task(conn, heard_ago=timedelta(minutes=10))
    assert main(["health"]) == 1  # quiet under the 5m threshold → non-zero
    assert "QUIET" in capsys.readouterr().out
    assert main(["health", "--quiet-minutes", "30"]) == 0
    assert "alive" in capsys.readouterr().out


def test_health_with_no_workers(mm_home, capsys) -> None:
    init_db()
    assert main(["health"]) == 0
    assert "no workers in flight" in capsys.readouterr().out
