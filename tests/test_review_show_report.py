"""`mm review show` puts the worker's own report, in full, above the diff."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from conftest import make_git_repo
from middle_manager import cli, progress
from middle_manager.state import init_db, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return init_db()


_MIGRATION = '''"""a revision"""

revision = "{revision}"
down_revision = "{down_revision}"


def upgrade() -> None:
    pass
'''


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _worktree_adding_a_migration(path, *, revision: str, down_revision: str) -> tuple[str, str]:
    """A worktree whose diff against its base adds exactly one Alembic revision."""
    make_git_repo(path)
    base = _git(path, "rev-parse", "HEAD")
    migration = path / "backend" / "alembic" / "versions" / f"{revision}.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(_MIGRATION.format(revision=revision, down_revision=down_revision))
    _git(path, "add", ".")
    _git(path, "commit", "-qm", f"add revision {revision}")
    return str(path), base


def _two_tasks_each_adding_a_migration(conn, tmp_path, *, first_parent: str, second_parent: str):
    repo_id = store.add_repo(
        conn,
        name="backend",
        origin=str(tmp_path / "origin"),
        local_path=str(tmp_path / "clone"),
        default_branch="main",
        base_sha=None,
    )
    run_id = store.create_run(conn, "two migrations")
    ids = []
    for index, (title, revision, parent) in enumerate(
        (
            ("add the events table", "aaa", first_parent),
            ("add the sessions table", "bbb", second_parent),
        )
    ):
        task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id)
        worktree, base = _worktree_adding_a_migration(
            tmp_path / f"worker-{index}", revision=revision, down_revision=parent
        )
        store.update_task_fields(
            conn, task_id, worktree_path=worktree, base_sha=base, branch=f"mm/task-{task_id}-abc"
        )
        store.set_task_status(conn, task_id, "in_progress")
        ids.append(task_id)
    return ids


def test_review_show_prints_the_full_latest_report_first(home, monkeypatch, capsys) -> None:
    conn = home
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    long_note = "Verification:\n" + "\n".join(f"- check {i}: PASS" for i in range(40))
    progress.record(task_id, phase="implement", note="halfway", conn=conn)
    progress.record(task_id, phase="done", note=long_note, conn=conn)

    import middle_manager.review as review_mod

    monkeypatch.setattr(
        review_mod,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=2, diffstat="2 files"
        ),
    )
    assert cli.main(["review", "show", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "worker report (done, 2 report(s)" in out, out
    assert out.index("worker report (done, 2 report(s)") < out.index("2 files")
    assert "- check 39: PASS" in out  # the whole note, not a truncated line
    assert f"mm progress {task_id}" in out


def test_review_show_flags_two_unmerged_migrations_off_the_same_parent(
    home, tmp_path, capsys
) -> None:
    """The two-heads condition itself: both diffs exist and both name one down_revision."""
    first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="base"
    )
    assert cli.main(["review", "show", str(second)]) == 0
    out = capsys.readouterr().out
    assert "MIGRATION COLLISION" in out, out
    assert f'task {first} "add the events table"' in out
    assert "backend/alembic/versions/aaa.py" in out
    assert "backend/alembic/versions/bbb.py" in out
    assert "'base'" in out
    assert out.index("MIGRATION COLLISION") < out.index(f"task {second}: ")


def test_review_show_says_nothing_when_the_two_migrations_have_different_parents(
    home, tmp_path, capsys
) -> None:
    _first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="aaa"
    )
    assert cli.main(["review", "show", str(second)]) == 0
    assert "MIGRATION COLLISION" not in capsys.readouterr().out


def test_review_show_states_the_merge_order_for_a_stacked_layer(home, monkeypatch, capsys) -> None:
    """Task 92: 'a line about merge order would have saved me discovering it at the end'."""
    conn = home
    run_id = store.create_run(conn, "stack")
    bottom = store.add_task(conn, run_id=run_id, title="bottom layer")
    store.update_task_fields(conn, bottom, branch="mm/task-1-aaa")
    top = store.add_task(conn, run_id=run_id, title="top layer")
    store.update_task_fields(
        conn, top, branch="mm/task-2-bbb", stacked_on="mm/task-1-aaa", stacked_on_task=bottom
    )

    import middle_manager.review as review_mod

    monkeypatch.setattr(
        review_mod,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=1, diffstat="1 file"
        ),
    )
    assert cli.main(["review", "show", str(top)]) == 0
    out = capsys.readouterr().out
    assert "stack: layer 2 of 2; its pull request targets mm/task-1-aaa" in out, out
    assert f'task {bottom} "bottom layer" (branch mm/task-1-aaa, unmerged)' in out
    assert f"then this task {top}" in out
    assert out.index("stack: layer") < out.index(f"task {top}: ")

    # The bottom layer has no parent: nothing to sequence, so nothing is printed.
    assert cli.main(["review", "show", str(bottom)]) == 0
    assert "stack: layer" not in capsys.readouterr().out
