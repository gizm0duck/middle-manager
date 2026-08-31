"""Stacked work starts from the exact branch it builds on, and delivers against it."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from middle_manager import delivery, repos
from middle_manager.state import init_db, store
from middle_manager.state.db import _column_names
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


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _wait_terminal(client, task_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in {"worker_done", "blocked", "failed"}:
            return status
        time.sleep(0.1)
    raise AssertionError("task never finished")


def test_schema_gains_stacked_on_on_fresh_and_existing_databases(mm_home) -> None:
    conn = init_db()
    assert "stacked_on" in _column_names(conn, "tasks")
    # An older database without the column is migrated forward, idempotently.
    conn.execute("ALTER TABLE tasks DROP COLUMN stacked_on")
    conn.commit()
    assert "stacked_on" not in _column_names(conn, "tasks")
    conn = init_db()
    assert "stacked_on" in _column_names(conn, "tasks")
    init_db()  # a second run must not fail on the now-present column


def test_dispatch_with_base_starts_from_that_branch_and_records_it(server, source_repo) -> None:
    srv, client = server
    # A branch ahead of main in the origin the registered clone will fetch from.
    _git(source_repo, "switch", "-q", "-c", "stack/base")
    (Path(source_repo) / "stacked.txt").write_text("stacked work\n")
    _git(source_repo, "add", "stacked.txt")
    _git(source_repo, "commit", "-qm", "stacked base commit")
    stacked_tip = _git(source_repo, "rev-parse", "HEAD")
    _git(source_repo, "switch", "-q", "main")

    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="on the stack", base="stack/base")
    assert resp["ok"], resp
    task_id = resp["task_id"]
    _wait_terminal(client, task_id)

    task = client.task_status(task_id)["task"]
    assert task["base_sha"] == stacked_tip
    assert task["stacked_on"] == "stack/base"
    # The fake worker commits on top, so the tip is the worktree's ancestor, not its HEAD.
    subprocess.run(
        ["git", "-C", task["worktree_path"], "merge-base", "--is-ancestor", stacked_tip, "HEAD"],
        check=True,
    )
    assert _git(task["worktree_path"], "merge-base", "--is-ancestor", "main", stacked_tip) == ""
    # The worker's own branch still exists at that tip, ready for `mm deliver`.
    assert _git(task["worktree_path"], "rev-parse", "--abbrev-ref", "HEAD") == task["branch"]


def test_dispatch_with_an_unknown_base_fails_plainly_and_frees_the_slot(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="bad base", base="does/not-exist")
    assert not resp.get("ok")
    assert "cannot start from 'does/not-exist'" in resp["error"]
    conn = init_db()
    failed = conn.execute("SELECT status FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert failed["status"] == "failed"


def test_deliver_defaults_the_pr_base_to_the_recorded_stack(mm_home, monkeypatch) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "stacked")
    task_id = store.add_task(conn, run_id=run_id, title="t")
    store.update_task_fields(
        conn, task_id, worktree_path="/tmp/wt", branch="mm/task-9-abc", stacked_on="feature/base"
    )
    calls: list[list[str]] = []

    def fake_run(argv, cwd=None):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="https://example/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")

    res = delivery.deliver(task_id)
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "feature/base"
    assert res.pr_url == "https://example/pr/1"

    # An explicit base still wins.
    calls.clear()
    delivery.deliver(task_id, base="main")
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "main"
