"""A dispatch with no base starts from the forge's current default branch.

On 2026-09-18 the first default dispatch into a repo after the lease check landed
was refused twice: the base clone was current, but the Treehouse pool slot it was
handed was parked at an older commit, and only ``--base``/``--stack-on`` ever moved
a fresh lease. Every earlier default dispatch had started stale and the worker
quietly repaired it. Here a pool slot is parked by hand after the real lease is
cut, the way a reused slot arrives.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time

import pytest

from middle_manager import repos
from middle_manager.state import init_db, store
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer
from middle_manager.worktree import LeaseManager


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path, name: str) -> str:
    with open(f"{path}/{name}", "w", encoding="utf-8") as fh:
        fh.write(f"{name}\n")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


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
    yield client
    srv.stop()


@pytest.fixture
def forge_two_ahead(mm_home, source_repo):
    """A registered repo whose forge's main is two commits past the parked commit.

    The base clone is synced to the forge, as it was in the incident: only the
    pool slot is stale.
    """
    parked = _git(source_repo, "rev-parse", "HEAD")
    added = repos.add_repo(source_repo)
    _commit(source_repo, "one.txt")
    head = _commit(source_repo, "two.txt")
    repos.sync_repo(added.name)
    assert _git(added.local_path, "rev-parse", "HEAD") == head
    return added, parked, head


@pytest.fixture
def park_slot(monkeypatch):
    """Hand out leases whose worktree is parked at a chosen commit, like a reused slot."""
    parked: dict = {}
    real = LeaseManager.acquire

    def acquire(self, **kwargs):
        lease = real(self, **kwargs)
        if "sha" not in parked:
            return lease
        _git(lease.worktree_path, "reset", "--hard", "--quiet", parked["sha"])
        return dataclasses.replace(lease, base_sha=parked["sha"])

    monkeypatch.setattr(LeaseManager, "acquire", acquire)
    return parked


def _dispatch(client, repo: str, **extra) -> dict:
    resp = client.dispatch_task(repo=repo, title="t", instructions="go", provider="fake", **extra)
    assert resp.get("ok"), resp
    return resp


def _task(task_id: int):
    return store.get_task(init_db(), task_id)


def _events(task_id: int, kind: str) -> list[dict]:
    rows = init_db().execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    )
    return [json.loads(row["payload"]) for row in rows]


def _contains(worktree: str, sha: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", worktree, "merge-base", "--is-ancestor", sha, "HEAD"], check=False
    )
    return proc.returncode == 0


def test_a_slot_parked_two_commits_behind_starts_at_the_forge_main_and_passes_the_lease_check(
    forge_two_ahead, park_slot, server
) -> None:
    added, parked, head = forge_two_ahead
    park_slot["sha"] = parked

    resp = _dispatch(server, added.name, expect_base=head)

    task = _task(resp["task_id"])
    assert task["base_sha"] == head, "base_sha records the forge's main, not the parked slot"
    assert task["stacked_on"] is None, "a default dispatch is not a stack"
    assert _contains(task["worktree_path"], head)
    assert _events(task["id"], "preflight_refused") == []
    assert _events(task["id"], "preflight_accepted") == []


def test_a_slot_parked_on_an_unrelated_branch_is_reset_to_the_forge_main(
    forge_two_ahead, park_slot, server
) -> None:
    added, parked, head = forge_two_ahead
    _git(added.local_path, "switch", "-q", "-c", "unrelated", parked)
    stray = _commit(added.local_path, "stray.txt")
    _git(added.local_path, "switch", "-q", "main")
    park_slot["sha"] = stray

    resp = _dispatch(server, added.name, expect_base=head)

    task = _task(resp["task_id"])
    assert task["base_sha"] == head
    assert _contains(task["worktree_path"], head)
    assert not _contains(task["worktree_path"], stray), "nothing from the parked branch"
    assert _events(task["id"], "preflight_refused") == []


def test_a_repo_with_no_forge_keeps_the_lease_where_it_was_cut(
    forge_two_ahead, park_slot, server
) -> None:
    added, parked, _head = forge_two_ahead
    store.update_repo_fields(init_db(), added.name, forge_url=None)
    park_slot["sha"] = parked

    resp = _dispatch(server, added.name)

    assert _task(resp["task_id"])["base_sha"] == parked
