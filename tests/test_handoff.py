"""Hermetic tests for `mm handoff`: a pickup prompt built from durable state, stored nowhere."""

from __future__ import annotations

import json

import pytest

from middle_manager import board, handoff, memory
from middle_manager.cli import main
from middle_manager.state import init_db, store


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


@pytest.fixture
def no_supervisor(monkeypatch):
    monkeypatch.setattr(
        handoff, "supervisor_status", lambda: {"reachable": False, "socket": "/nowhere"}
    )


@pytest.fixture
def supervisor_up(monkeypatch):
    monkeypatch.setattr(
        handoff, "supervisor_status", lambda: {"reachable": True, "pid": 1, "socket": "/s"}
    )


def _seed(conn):
    repo_id = store.add_repo(
        conn,
        name="demo",
        origin="/tmp/demo",
        local_path="/tmp/demo-clone",
        default_branch="main",
        base_sha="abc123",
    )
    run_id = store.create_run(conn, "ship the widget")
    store.set_run_status(conn, run_id, "running")
    running = store.add_task(conn, run_id=run_id, title="build widget", repo_id=repo_id)
    store.set_task_status(conn, running, "in_progress")
    done = store.add_task(conn, run_id=run_id, title="widget tests", repo_id=repo_id)
    store.set_task_status(conn, done, "worker_done")
    blocked = store.add_task(conn, run_id=run_id, title="widget docs", repo_id=repo_id)
    store.set_task_status(conn, blocked, "blocked")
    store.append_event(
        conn,
        kind="blocked",
        payload={"task_id": blocked, "question": "v1 or v2 endpoint?"},
        run_id=run_id,
        task_id=blocked,
    )
    old_run = store.create_run(conn, "old thing")
    shipped = store.add_task(conn, run_id=old_run, title="shipped", repo_id=repo_id)
    store.set_task_status(conn, shipped, "delivered")
    return {"run_id": run_id, "running": running, "done": done, "blocked": blocked, "old": old_run}


def test_collect_buckets_open_runs_only(mm_home, no_supervisor) -> None:
    conn = init_db()
    ids = _seed(conn)

    data = handoff.collect(conn)

    assert [r["id"] for r in data["open_runs"]] == [ids["run_id"]]
    run = data["open_runs"][0]
    assert [t["id"] for t in run["in_flight"]] == [ids["running"]]
    assert {t["id"] for t in run["needs_me"]} == {ids["done"], ids["blocked"]}
    blocked = next(t for t in run["needs_me"] if t["id"] == ids["blocked"])
    assert blocked["question"] == "v1 or v2 endpoint?"
    assert next(t for t in run["needs_me"] if t["id"] == ids["done"])["review"] == "not reviewed"
    assert data["supervisor"]["reachable"] is False
    assert [h["task_id"] for h in data["health"]] == [ids["running"]]
    assert data["health"][0]["verdict"] == "dead"  # no runner process was ever started


def test_warnings_cover_team_risks(mm_home, no_supervisor) -> None:
    conn = init_db()
    ids = _seed(conn)

    warnings = handoff.warnings_for(handoff.collect(conn))
    text = "\n".join(warnings)

    assert "Supervisor is not running" in text and f"task(s) {ids['running']}" in text
    assert "Worker process gone" in text
    assert 'is idle waiting on an answer: "v1 or v2 endpoint?"' in text
    assert "finished and waiting on review" in text
    assert "No next step is recorded" in text


def test_alive_worker_warning_when_supervisor_up(mm_home, supervisor_up, monkeypatch) -> None:
    conn = init_db()
    ids = _seed(conn)
    monkeypatch.setattr(handoff.health, "_pid_alive", lambda pid: True)
    store.register_runner(conn, runner_id="r1", task_id=ids["running"], provider="claude")
    store.update_runner(conn, "r1", pid=4242, status="running")
    store.append_event(
        conn, kind="worker_text", payload={}, run_id=ids["run_id"], task_id=ids["running"]
    )

    warnings = handoff.warnings_for(handoff.collect(conn))
    text = "\n".join(warnings)

    assert "1 worker(s) still running" in text
    assert "Supervisor is" not in text
    assert "Worker process gone" not in text


def test_prompt_is_self_contained_and_stores_nothing(mm_home, no_supervisor) -> None:
    conn = init_db()
    ids = _seed(conn)
    a = board.add("review task 2 and deliver", run_id=ids["run_id"], conn=conn)
    b = board.add("answer task 3: v2", conn=conn)
    c = board.add("confirm the endpoint", blocked_on="user", conn=conn)
    store.append_event(
        conn,
        kind="worker_progress",
        payload={"task_id": ids["running"], "phase": "implement", "note": "wiring it up"},
        run_id=ids["run_id"],
        task_id=ids["running"],
    )

    result = handoff.build_handoff(conn)
    prompt = result["prompt"]

    assert "Pick up where we left off" in prompt
    assert "mm supervisor status" in prompt and "mm reconcile" in prompt and "mm health" in prompt
    assert "mm todo list" in prompt
    assert f"- Next (todo ledger): #{a} review task 2 and deliver; #{b} answer task 3: v2" in prompt
    assert f"- Waiting (todo ledger): #{c} confirm the endpoint (waiting on user)" in prompt
    assert f'Run {ids["run_id"]} "ship the widget"' in prompt
    assert '"widget docs" (demo) — blocked · question: "v1 or v2 endpoint?"' in prompt
    assert '"build widget" (demo) — in_progress · phase implement ("wiring it up")' in prompt
    assert "Known risks at handoff" in prompt
    assert "No next step is recorded" not in prompt
    assert "greet me with one dry line" in prompt
    # Nothing written anywhere but the board projection the ledger already maintains.
    names = {p.name for p in memory.memory_dir().iterdir()}
    assert "handoff.md" not in names


def test_session_context_for_hooks(mm_home, no_supervisor) -> None:
    conn = init_db()
    assert handoff.render_session_context(handoff.collect(conn)) is None
    ids = _seed(conn)
    context = handoff.render_session_context(handoff.collect(conn))
    assert context.startswith("Middle Manager pickup context")
    assert f"Run {ids['run_id']}" in context
    assert "Attention:" in context


def test_handoff_with_nothing_open_still_returns_a_prompt(mm_home, no_supervisor) -> None:
    result = handoff.build_handoff()
    assert "Nothing in flight; nothing waiting on me." in result["prompt"]
    assert result["warnings"] == []
    assert "Known risks" not in result["prompt"]


def test_cli_handoff_prints_prompt_and_warnings(mm_home, no_supervisor, capsys) -> None:
    conn = init_db()
    _seed(conn)

    assert main(["handoff"]) == 0
    captured = capsys.readouterr()
    assert "Pick up where we left off" in captured.out
    assert captured.err.startswith("warning: ")

    assert main(["handoff", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"prompt", "warnings", "data"}
    assert payload["data"]["open_runs"][0]["objective"] == "ship the widget"
