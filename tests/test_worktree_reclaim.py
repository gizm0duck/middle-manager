"""`mm worktree list` / `mm worktree prune`: see the slots, and get them back.

The dispatch disk floor refused work on 2026-09-02 while 20 GB sat in the
worktrees of tasks that were already delivered, with no command to reclaim them.
Reclamation has to be conservative: a full disk must never cost work that only
exists in a leased checkout.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_git_repo
from middle_manager import preflight, repos
from middle_manager.cli import main
from middle_manager.state import init_db, store
from middle_manager.worktree.lease import LeaseManager
from middle_manager.worktree.reclaim import human_bytes, list_worktrees, prune, reclaimable_bytes


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(mm_home, source_repo):
    return repos.add_repo(source_repo)


def _lease_a_task(repo, *, status: str, title="work"):
    """Create a task in ``status`` holding a real leased worktree."""
    conn = init_db()
    run_id = store.create_run(conn, title)
    repo_row = store.get_repo(conn, repo.name)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_row["id"])
    lease = LeaseManager("git").acquire(
        repo_path=repo_row["local_path"], repo_id=repo_row["id"], task_id=task_id
    )
    store.update_task_fields(
        conn,
        task_id,
        branch=lease.branch,
        worktree_path=lease.worktree_path,
        lease_id=lease.id,
        base_sha=lease.base_sha,
    )
    store.set_task_status(conn, task_id, status)
    return task_id, lease


def _commit_in(worktree, name="local-only.txt"):
    Path(worktree, name).write_text("work that exists nowhere else\n")
    _git(worktree, "add", "-A")
    _git(worktree, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-qm", "local work")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# --------------------------------------------------------------------------- #
# mm worktree list
# --------------------------------------------------------------------------- #


def test_list_reports_slot_task_status_branch_state_and_size(repo):
    delivered_id, delivered = _lease_a_task(repo, status="delivered")
    working_id, working = _lease_a_task(repo, status="in_progress")
    Path(working.worktree_path, "scratch.txt").write_text("uncommitted\n")

    entries = {e.task_id: e for e in list_worktrees()}
    assert set(entries) == {delivered_id, working_id}

    done = entries[delivered_id]
    assert done.slot == Path(delivered.worktree_path).name
    assert done.task_status == "delivered"
    assert done.branch == delivered.branch
    assert done.dirty is False
    assert done.size_bytes > 0
    assert done.reclaimable is True

    live = entries[working_id]
    assert live.dirty is True
    assert live.reclaimable is False
    assert "in_progress" in live.reason


def test_list_filters_by_repo(mm_home, tmp_path):
    first = repos.add_repo(make_git_repo(tmp_path / "one"))
    second = repos.add_repo(make_git_repo(tmp_path / "two"))
    _lease_a_task(first, status="delivered")
    _lease_a_task(second, status="delivered")

    assert [e.repo for e in list_worktrees("one")] == ["one"]
    assert len(list_worktrees()) == 2


def test_list_prints_a_table(repo, capsys):
    _lease_a_task(repo, status="delivered")
    assert main(["worktree", "list"]) == 0
    out = capsys.readouterr().out
    assert "SLOT" in out and "prunable" in out


# --------------------------------------------------------------------------- #
# mm worktree prune
# --------------------------------------------------------------------------- #


def test_prune_removes_a_finished_clean_worktree_and_releases_its_lease(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert res["reclaimed_bytes"] > 0
    assert not Path(lease.worktree_path).exists()
    conn = init_db()
    assert conn.execute("SELECT status FROM leases WHERE id = ?", (lease.id,)).fetchone()[0] == (
        "released"
    )
    assert _events(task_id, "worktree_pruned")


def test_prune_keeps_the_branch_it_removed_the_worktree_for(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    repo_path = store.get_repo(init_db(), repo.name)["local_path"]
    prune()
    assert lease.branch in _git(repo_path, "branch", "--list", lease.branch)


def test_prune_skips_a_dirty_worktree_and_says_why(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    Path(lease.worktree_path, "unsaved.txt").write_text("not committed anywhere\n")

    res = prune()

    assert res["removed"] == []
    assert res["skipped"][0]["task_id"] == task_id
    assert res["skipped"][0]["dirty"] is True
    assert "uncommitted" in res["skipped"][0]["reason"]
    assert Path(lease.worktree_path).exists()


def test_prune_skips_a_worktree_holding_commits_no_remote_has(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    _commit_in(lease.worktree_path)

    res = prune()

    assert res["removed"] == []
    skipped = res["skipped"][0]
    assert skipped["unpushed_commits"] == 1
    assert "exist only here" in skipped["reason"]
    assert Path(lease.worktree_path).exists()


def test_prune_reclaims_a_worktree_once_its_commits_are_pushed(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    _commit_in(lease.worktree_path)
    _git(lease.worktree_path, "push", "--quiet", "origin", f"HEAD:{lease.branch}")

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert not Path(lease.worktree_path).exists()


def test_prune_leaves_unfinished_tasks_alone(repo):
    for status in ("in_progress", "worker_done", "blocked", "failed", "needs_recovery"):
        _lease_a_task(repo, status=status, title=status)

    res = prune()

    assert res["removed"] == []
    assert len(res["skipped"]) == 5
    assert all("not finished with" in s["reason"] for s in res["skipped"])


def test_prune_takes_every_terminal_status(repo):
    ids = [_lease_a_task(repo, status=s, title=s)[0] for s in ("delivered", "closed", "cancelled")]
    res = prune()
    assert sorted(r["task_id"] for r in res["removed"]) == sorted(ids)


def test_dry_run_changes_nothing_but_reports_the_bytes(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")

    res = prune(dry_run=True)

    assert res["dry_run"] is True
    assert [r["task_id"] for r in res["removed"]] == [task_id]
    assert res["reclaimed_bytes"] > 0
    assert Path(lease.worktree_path).exists(), "a dry run must not touch the disk"
    conn = init_db()
    assert conn.execute("SELECT status FROM leases WHERE id = ?", (lease.id,)).fetchone()[0] == (
        "active"
    )
    assert not _events(task_id, "worktree_pruned")


def test_prune_filters_by_repo(mm_home, tmp_path):
    first = repos.add_repo(make_git_repo(tmp_path / "one"))
    second = repos.add_repo(make_git_repo(tmp_path / "two"))
    _kept_id, kept = _lease_a_task(second, status="delivered")
    _lease_a_task(first, status="delivered")

    res = prune("one")

    assert [r["repo"] for r in res["removed"]] == ["one"]
    assert Path(kept.worktree_path).exists()


def test_prune_clears_a_lease_whose_worktree_is_already_gone(repo):
    task_id, lease = _lease_a_task(repo, status="delivered")
    repo_path = store.get_repo(init_db(), repo.name)["local_path"]
    subprocess.run(
        ["git", "-C", repo_path, "worktree", "remove", "--force", lease.worktree_path],
        check=True,
        capture_output=True,
    )

    res = prune()

    assert [r["task_id"] for r in res["removed"]] == [task_id]
    conn = init_db()
    assert conn.execute("SELECT status FROM leases WHERE id = ?", (lease.id,)).fetchone()[0] == (
        "released"
    )


def test_cli_prune_dry_run_reports_and_leaves_the_slot(repo, capsys):
    _task_id, lease = _lease_a_task(repo, status="delivered")
    assert main(["worktree", "prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would remove" in out
    assert Path(lease.worktree_path).exists()


# --------------------------------------------------------------------------- #
# The dispatch disk floor names the command and the space it would give back
# --------------------------------------------------------------------------- #


def test_disk_refusal_names_worktree_prune_and_what_it_would_reclaim(repo):
    _lease_a_task(repo, status="delivered")
    freeable = reclaimable_bytes()
    assert freeable > 0

    with pytest.raises(preflight.PreflightError) as exc:
        preflight.check_disk(floor_gb=10**9)

    message = str(exc.value)
    assert "mm worktree prune" in message
    assert human_bytes(freeable) in message
    assert "mm worktree list" in message


def test_disk_refusal_says_so_when_there_is_nothing_to_reclaim(repo):
    _lease_a_task(repo, status="in_progress")
    with pytest.raises(preflight.PreflightError) as exc:
        preflight.check_disk(floor_gb=10**9)
    assert "nothing to reclaim right now" in str(exc.value)
