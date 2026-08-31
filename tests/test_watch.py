"""The heartbeat: one relayable line of team state per tick, never silent."""

from __future__ import annotations

import io

import pytest

from middle_manager import board, cli, watch
from middle_manager.state import init_db, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return init_db()


def test_tick_reports_owed_tasks_new_events_and_the_ledger(home) -> None:
    conn = home
    run_id = store.create_run(conn, "ship the thing")
    done = store.add_task(conn, run_id=run_id, title="finished, awaiting review")
    store.set_task_status(conn, done, "worker_done")
    board.add("review task", task_id=done)

    baseline = watch.max_event_id(conn)
    store.append_event(conn, kind="worker_done", payload={}, run_id=run_id, task_id=done)
    store.append_event(conn, kind="worker_item.completed", payload={}, run_id=run_id, task_id=done)
    store.append_event(conn, kind="error", payload={"summary": "x"}, run_id=run_id, task_id=done)

    snap = watch.tick(conn, since_event_id=baseline)
    assert [r["id"] for r in snap["needs_me"]] == [done]
    assert snap["new_events"] == {"worker_done": 1, "error": 1}  # provider chatter excluded
    assert snap["open_todos"] == 1
    assert snap["last_event_id"] > baseline

    line = watch.render(snap)
    assert "no workers in flight" in line
    assert f"needs me: t{done} worker_done" in line
    assert "new since last tick: error×1, worker_done×1" in line
    assert "open todos: 1" in line

    # A second tick from the new cursor reports nothing new — but still reports.
    again = watch.tick(conn, since_event_id=snap["last_event_id"])
    assert again["new_events"] == {}
    assert "new since last tick: none" in watch.render(again)


def test_run_once_prints_exactly_one_line_and_returns(home) -> None:
    out = io.StringIO()
    slept: list[float] = []
    rc = watch.run(interval=1, once=True, out=out, sleep=slept.append)
    assert rc == 0
    assert out.getvalue().count("\n") == 1
    assert out.getvalue().startswith("TEAM ")
    assert slept == []


def test_run_loops_on_the_interval_until_stopped(home) -> None:
    out = io.StringIO()
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(interval=300, out=out, sleep=fake_sleep)
    assert calls == [300, 300, 300]
    assert out.getvalue().count("TEAM ") == 3


def test_cli_watch_once(home, capsys) -> None:
    assert cli.main(["watch", "--once"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("TEAM ") and "no workers in flight" in line
    assert cli.main(["watch", "--once", "--json"]) == 0
    assert '"in_flight"' in capsys.readouterr().out
