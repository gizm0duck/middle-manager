"""A task may rebuild only while its recorded lease is still live.

On 2026-09-06 four Claude workers were cut off by a usage limit and marked
failed. Rebuilding a task after its lease was returned later let a post-turn push
use a pool slot belonging to another task. Released leases are terminal identities:
the task must be re-dispatched instead of resumed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from middle_manager import lifecycle, repos
from middle_manager.state import init_db, store
from middle_manager.supervisor.core import Supervisor, SupervisorError


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _events(task_id: int, kind: str) -> list[dict]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows]


def test_a_failed_task_whose_pristine_lease_was_released_is_not_resumed(
    mm_home, source_repo
) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(
        repo=added.name, title="cut off early", instructions="FAIL", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "failed")
    _wait_for(lambda: not Path(task["worktree_path"]).exists())
    before = store.get_task(init_db(), task_id)
    assert before["worktree_path"] == task["worktree_path"]  # the record still names it

    with pytest.raises(SupervisorError, match="lease_released event"):
        supervisor.resume_task(task_id)
    assert _status(task_id) == "failed"
    assert _events(task_id, "worktree_rebuilt") == []


def test_a_task_with_pushed_work_is_not_rebuilt_after_its_lease_is_released(
    mm_home, source_repo
) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(repo=added.name, title="pushed then lost", provider="fake")
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "worker_done")
    row = store.get_task(init_db(), task_id)
    work = Path(row["worktree_path"], f"mm-fake-{task_id}.txt")
    assert work.exists()

    # The worker was then judged failed and its slot taken, local branch and all.
    store.set_task_status(init_db(), task_id, "failed")
    supervisor.release_task_lease(task_id, remove_branch=True)
    assert not Path(row["worktree_path"]).exists()

    with pytest.raises(SupervisorError, match="no live worktree lease"):
        supervisor.resume_task(task_id, "one more line")
    assert _status(task_id) == "failed"


def test_resume_refuses_plainly_when_the_recorded_worktree_is_gone(mm_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(
        repo=added.name, title="orphan", instructions="FAIL", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "failed")
    _wait_for(lambda: not Path(task["worktree_path"]).exists())
    conn = init_db()
    conn.execute("UPDATE tasks SET repo_id = NULL WHERE id = ?", (task_id,))
    conn.commit()
    try:
        supervisor.resume_task(task_id)
    except Exception as exc:  # noqa: BLE001
        assert "no live worktree lease" in str(exc)
    else:
        raise AssertionError("resume should have refused")
    assert _status(task_id) == "failed"


def test_lifecycle_release_then_resume_is_refused(mm_home, source_repo) -> None:
    """The by-hand path records the release and makes the former task non-resumable."""
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(repo=added.name, title="by hand", provider="fake")
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "worker_done")
    lifecycle.release_task_lease(task_id, reason="test")
    supervisor._leases.pop(task_id, None)
    assert not Path(task["worktree_path"]).exists()

    with pytest.raises(SupervisorError, match="lease_released event"):
        supervisor.resume_task(task_id, "again")
    assert _events(task_id, "worktree_rebuilt") == []
