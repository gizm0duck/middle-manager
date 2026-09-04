"""The heartbeat: one relayable line of team state per tick, never silent."""

from __future__ import annotations

import io
import json

import pytest

from middle_manager import board, cli, watch
from middle_manager.state import init_db, store


@pytest.fixture(autouse=True)
def no_real_forge(monkeypatch):
    """No test in this file may shell out: the forge is faked at its one seam.

    The default answer stands in for a machine with no ``gh`` at all, which is
    also the honest answer for every test that is not about pull requests.
    """
    monkeypatch.setattr(watch, "_run_gh", lambda args, cwd=None: (127, ""))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return init_db()


def delivered_task(conn, tmp_path, branch: str, *, status: str = "delivered") -> int:
    """A finished task whose work sits on a branch, with a worktree to run gh from."""
    run_id = store.create_run(conn, "ship the thing")
    task_id = store.add_task(conn, run_id=run_id, title=f"work on {branch}")
    worktree = tmp_path / f"wt-{branch.replace('/', '-')}"
    worktree.mkdir(parents=True, exist_ok=True)
    store.update_task_fields(conn, task_id, branch=branch, worktree_path=str(worktree))
    store.set_task_status(conn, task_id, status)
    return task_id


def fake_forge(monkeypatch, *, pr: dict | None, checks: list[dict] | None = None):
    """Answer `gh pr list` / `gh pr checks` from canned rows; record every call made."""
    calls: list[list[str]] = []

    def run_gh(args, cwd=None):
        calls.append(list(args))
        if args[1] == "list":
            return (0, json.dumps([pr] if pr else []))
        # gh exits non-zero whenever a check is failing or still running.
        bad = any(c.get("bucket") in ("fail", "pending") for c in checks or [])
        return (1 if bad else 0, json.dumps(checks or []))

    monkeypatch.setattr(watch, "_run_gh", run_gh)
    return calls


OPEN_PR = {
    "number": 779,
    "state": "OPEN",
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
    "baseRefName": "main",
}


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


def test_the_line_carries_a_green_pull_request_with_its_base(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/green")
    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])

    snap = watch.tick(conn)
    assert snap["prs"][0]["pr"] == 779
    assert snap["prs"][0]["ci"] == "pass"
    assert "prs: PR #779 -> main: ci pass, mergeable" in watch.render(snap)


def test_a_failing_check_is_named_so_the_manager_knows_what_broke(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/red")
    fake_forge(
        monkeypatch,
        pr={**OPEN_PR, "number": 555},
        checks=[{"name": "Unit tests", "bucket": "fail"}, {"name": "Lint", "bucket": "pass"}],
    )

    snap = watch.tick(conn)
    assert snap["prs"][0]["ci"] == "fail"
    assert "prs: PR #555 -> main: ci fail (Unit tests), mergeable" in watch.render(snap)


def test_checks_still_running_read_as_pending(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/running")
    fake_forge(
        monkeypatch,
        pr=OPEN_PR,
        checks=[{"name": "Unit tests", "bucket": "pending"}, {"name": "Lint", "bucket": "pass"}],
    )

    assert "prs: PR #779 -> main: ci pending, mergeable" in watch.render(watch.tick(conn))


def test_a_branch_behind_its_base_says_so(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/stale")
    fake_forge(
        monkeypatch,
        pr={**OPEN_PR, "mergeStateStatus": "BEHIND"},
        checks=[{"name": "Unit tests", "bucket": "pass"}],
    )

    assert "PR #779 -> main: ci pass, behind base" in watch.render(watch.tick(conn))


def test_without_gh_the_tick_says_unknown_rather_than_failing(home, tmp_path) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/no-gh")  # autouse fixture: gh is absent

    snap = watch.tick(conn)
    assert snap["prs"][0] == {
        "known": False,
        "pr": None,
        "ci": "unknown",
        "failing": [],
        "task_id": task_id,
        "branch": "harness/no-gh",
        "status": "delivered",
    }
    assert f"prs: t{task_id} ci: unknown" in watch.render(snap)


def test_a_branch_with_no_pull_request_yet_adds_no_noise(home, tmp_path, monkeypatch) -> None:
    conn = home
    task_id = delivered_task(conn, tmp_path, "harness/unopened", status="worker_done")
    fake_forge(monkeypatch, pr=None)

    line = watch.render(watch.tick(conn))
    assert "prs:" not in line
    assert f"needs me: t{task_id} worker_done" in line


def test_a_merged_pull_request_is_reported_without_asking_for_checks(
    home, tmp_path, monkeypatch
) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/landed")
    calls = fake_forge(monkeypatch, pr={**OPEN_PR, "state": "MERGED"})

    assert "prs: PR #779 -> main: merged" in watch.render(watch.tick(conn))
    assert [c[1] for c in calls] == ["list"]


def test_a_tick_asks_the_forge_once_per_branch(home, tmp_path, monkeypatch) -> None:
    conn = home
    first = delivered_task(conn, tmp_path, "harness/shared")
    # A second task delivered onto the same branch must not cost a second lookup.
    second = delivered_task(conn, tmp_path, "harness/shared", status="worker_done")
    calls = fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])

    snap = watch.tick(conn)
    assert [e["task_id"] for e in snap["prs"]] == [first, second]
    assert [c[1] for c in calls] == ["list", "checks"]


def test_the_line_names_what_flipped_since_the_last_tick(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/flip")
    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pending"}])

    first = watch.tick(conn)
    assert first["pr_changes"] == []  # nothing to compare the first tick against
    assert "changed:" not in watch.render(first)

    fake_forge(monkeypatch, pr=OPEN_PR, checks=[{"name": "Unit tests", "bucket": "pass"}])
    second = watch.tick(conn, previous_prs=watch.pr_index(first["prs"]))
    assert second["pr_changes"] == ["PR #779 ci pending -> pass"]
    assert "changed: PR #779 ci pending -> pass" in watch.render(second)

    third = watch.tick(conn, previous_prs=watch.pr_index(second["prs"]))
    assert third["pr_changes"] == []  # steady state stays quiet


def test_run_compares_each_tick_with_the_one_before_it(home, tmp_path, monkeypatch) -> None:
    conn = home
    delivered_task(conn, tmp_path, "harness/loop")
    rounds = iter(
        [
            [{"name": "Unit tests", "bucket": "pending"}],
            [{"name": "Unit tests", "bucket": "pass"}],
        ]
    )

    def run_gh(args, cwd=None):
        if args[1] == "list":
            return (0, json.dumps([OPEN_PR]))
        return (0, json.dumps(next(rounds)))

    monkeypatch.setattr(watch, "_run_gh", run_gh)

    out = io.StringIO()
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch.run(interval=300, out=out, sleep=fake_sleep)
    lines = out.getvalue().strip().splitlines()
    assert "ci pending" in lines[0] and "changed:" not in lines[0]
    assert "changed: PR #779 ci pending -> pass" in lines[1]
