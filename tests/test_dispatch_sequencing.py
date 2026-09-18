"""Dispatch refuses overlapping in-flight work and stacking onto an empty parent (issue #97).

Two briefs touched one server module concurrently off a pinned starting commit,
and the overlap check (issue #61) only printed a suggestion the manager missed on
the next dispatch. A child was also stacked on a parent whose branch had no
commits yet, so it started from — and recorded — the default branch as its base
(2026-09-17). Both are now refused before any task state exists, unless waved
through with ``--accept-preflight overlap|empty-parent --reason ...``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import namedtuple

import pytest

from middle_manager import cli, overlap, repos
from middle_manager.state import init_db, store
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer
from middle_manager.worktree import LeaseManager

ACCEPT_OVERLAP = [{"check": "overlap", "overridden": None}]


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


@pytest.fixture
def registered(server, source_repo):
    return repos.add_repo(source_repo)


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path, name: str) -> str:
    with open(f"{path}/{name}", "w", encoding="utf-8") as fh:
        fh.write(name + "\n")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


def _in_progress_task(repo_name: str, title: str, *, touches: list[str] | None = None) -> int:
    """A task still being worked on: a real lease worktree whose branch has no commits yet."""
    conn = init_db()
    repo_row = store.get_repo(conn, repo_name)
    run_id = store.create_run(conn, title)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_row["id"])
    lease = LeaseManager().acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=task_id
    )
    store.update_task_fields(
        conn,
        task_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        base_sha=lease.base_sha,
        lease_id=lease.id,
    )
    store.set_task_status(conn, task_id, "in_progress")
    overlap.record_touches(conn, task_id, touches or [])
    return task_id


def _task(task_id: int):
    return store.get_task(init_db(), task_id)


def _task_count() -> int:
    return init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _events(task_id: int, kind: str) -> list[dict]:
    rows = init_db().execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    )
    return [json.loads(row["payload"]) for row in rows]


def _wait_terminal(client, task_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in {"worker_done", "blocked", "failed"}:
            return status
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never finished")


def _dispatch(client, repo: str, touches: str, **kwargs) -> dict:
    return client.dispatch_task(
        repo=repo, title="next change", instructions=f"# Next\n\nTouches: {touches}\n", **kwargs
    )


# --------------------------------------------------------------------------- #
# G1: overlapping in-flight work is refused
# --------------------------------------------------------------------------- #


def test_an_overlapping_dispatch_is_refused_naming_the_sibling_and_the_stack_on_to_use(
    server, registered
) -> None:
    _srv, client = server
    sibling = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    before = _task_count()

    resp = _dispatch(client, registered.name, "src/serve.py, tests/test_serve.py")

    assert not resp["ok"]
    error = resp["error"]
    assert error.startswith("dispatch refused:")
    assert f'task {sibling} "rework serve"' in error
    assert "src/serve.py" in error
    assert f"--stack-on {sibling}" in error
    assert "--accept-preflight overlap" in error
    assert "Nothing was refused" not in error
    assert _task_count() == before, "a refused dispatch must not create a task row"


def test_stacking_on_the_overlapping_task_passes(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    _commit(_task(parent)["worktree_path"], "serve.txt")

    resp = _dispatch(client, registered.name, "src/serve.py", stack_on=parent)

    assert resp["ok"], resp
    assert resp["overlap_advisory"] is None
    _wait_terminal(client, resp["task_id"])


def test_stacking_on_another_task_is_refused_for_the_overlapping_sibling(
    server, registered
) -> None:
    _srv, client = server
    sibling = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    parent = _in_progress_task(registered.name, "docs", touches=["docs/serve.md"])
    _commit(_task(parent)["worktree_path"], "docs.txt")

    resp = _dispatch(client, registered.name, "src/serve.py", stack_on=parent)

    assert not resp["ok"]
    assert f"--stack-on {sibling}" in resp["error"]
    assert f"--stack-on {parent}" not in resp["error"]


def test_an_accepted_overlap_passes_and_records_what_it_waved_through(server, registered) -> None:
    _srv, client = server
    sibling = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])

    resp = _dispatch(
        client,
        registered.name,
        "src/serve.py",
        accepted_preflight=ACCEPT_OVERLAP,
        preflight_reason="x",
    )

    assert resp["ok"], resp
    assert f"--stack-on {sibling}" in resp["overlap_advisory"]
    [event] = _events(resp["task_id"], "preflight_accepted")
    assert event["check"] == "overlap"
    assert event["reason"] == "x"
    assert f'task {sibling} "rework serve"' in event["overridden"]
    _wait_terminal(client, resp["task_id"])


@pytest.mark.parametrize("status", ["delivered", "merged", "closed"])
def test_a_task_no_longer_in_flight_never_causes_an_overlap_refusal(
    server, registered, status
) -> None:
    _srv, client = server
    finished = _in_progress_task(registered.name, "rework serve", touches=["src/serve.py"])
    store.set_task_status(init_db(), finished, status)

    resp = _dispatch(client, registered.name, "src/serve.py")

    assert resp["ok"], resp
    assert resp["overlap_advisory"] is None
    _wait_terminal(client, resp["task_id"])


# --------------------------------------------------------------------------- #
# G2: a stack parent with nothing on its branch is refused
# --------------------------------------------------------------------------- #


def test_stacking_on_a_parent_with_no_commits_beyond_its_base_is_refused(
    server, registered
) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    before = _task_count()

    resp = _dispatch(client, registered.name, "src/child.py", stack_on=parent)

    assert not resp["ok"]
    error = resp["error"]
    assert f'task {parent} "parent layer"' in error
    assert "no commits beyond its own base" in error
    assert "first push" in error
    assert "--accept-preflight empty-parent" in error
    assert _task_count() == before


def test_base_naming_an_in_flight_task_s_empty_lease_branch_is_refused(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")

    resp = _dispatch(client, registered.name, "src/child.py", base=_task(parent)["branch"])

    assert not resp["ok"]
    assert "no commits beyond its own base" in resp["error"]


def test_a_parent_commit_in_its_lease_worktree_is_enough_to_stack_on(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    head = _commit(_task(parent)["worktree_path"], "parent.txt")

    resp = _dispatch(client, registered.name, "src/child.py", stack_on=parent)

    assert resp["ok"], resp
    assert _task(resp["task_id"])["base_sha"] == head
    _wait_terminal(client, resp["task_id"])


def test_a_parent_commit_on_the_forge_is_enough_to_stack_on(server, registered) -> None:
    """Pushed, then the lease worktree moved back to base: only the forge has it."""
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")
    row = _task(parent)
    head = _commit(row["worktree_path"], "parent.txt")
    _git(row["worktree_path"], "push", "-q", "origin", f"HEAD:{row['branch']}")
    _git(row["worktree_path"], "reset", "-q", "--hard", row["base_sha"])

    resp = _dispatch(client, registered.name, "src/child.py", stack_on=parent)

    assert resp["ok"], resp
    assert _task(resp["task_id"])["base_sha"] == head
    _wait_terminal(client, resp["task_id"])


def test_an_accepted_empty_parent_passes_and_records_the_event(server, registered) -> None:
    _srv, client = server
    parent = _in_progress_task(registered.name, "parent layer")

    resp = _dispatch(
        client,
        registered.name,
        "src/child.py",
        stack_on=parent,
        accepted_preflight=[{"check": "empty-parent", "overridden": None}],
        preflight_reason="parent is a placeholder",
    )

    assert resp["ok"], resp
    [event] = _events(resp["task_id"], "preflight_accepted")
    assert event["check"] == "empty-parent"
    assert "no commits beyond its own base" in event["overridden"]
    _wait_terminal(client, resp["task_id"])


# --------------------------------------------------------------------------- #
# G3: the CLI override joins the fixed set
# --------------------------------------------------------------------------- #

Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize("check", ["overlap", "empty-parent"])
def test_the_cli_accepts_the_sequencing_checks_by_name_and_needs_a_reason(
    mm_home, source_repo, monkeypatch, check
) -> None:
    received: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            received.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "mm/task-7-abc"}

    import middle_manager.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    added = repos.add_repo(source_repo)
    argv = ["dispatch", "--repo", added.name, "--title", "t", "--instructions", "go"]

    assert cli.main([*argv, "--accept-preflight", check]) == 2
    assert not received

    assert cli.main([*argv, "--accept-preflight", check, "--reason", "x"]) == 0
    assert {"check": check, "overridden": None} in received["accepted_preflight"]
    assert received["preflight_reason"] == "x"
