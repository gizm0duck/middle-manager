"""A turn that ended mid-gate is not `worker_done`.

Task 103 (2026-09-04): the Claude worker started the full backend suite, the
tool's foreground cap sent it to the background, the worker ended its turn to
wait for it, and the harness read that as `worker_done`. The suite died with the
session, the branch was never pushed, and no done note was ever filed. Task 104
had the same shape with browser smoke retries. The manager only noticed because
`mm review show` had a "test" note as the newest report and nothing was pushed.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from middle_manager import repos, turn_end
from middle_manager.state import init_db, store
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


def _wait_status(client, task_id, wanted, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.task_status(task_id)["task"]["status"]
        if last in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached {wanted} (last {last})")


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _dispatch(client, source_repo, instructions: str) -> dict:
    added = repos.add_repo(source_repo)
    return client.dispatch_task(repo=added.name, title="run the gate", instructions=instructions)


# --------------------------------------------------------------------------- #
# The compliant worker still finishes
# --------------------------------------------------------------------------- #


def test_a_worker_that_pushed_and_filed_a_done_note_is_done(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "")
    _wait_status(client, resp["task_id"], {"worker_done"})
    assert not _events(resp["task_id"], turn_end.WORKER_STOPPED)


# --------------------------------------------------------------------------- #
# Each trigger, on its own
# --------------------------------------------------------------------------- #


def test_no_done_note_stops_the_task_with_that_reason(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE")
    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})

    stopped = _events(resp["task_id"], turn_end.WORKER_STOPPED)[-1]
    assert "no done note was ever filed" in stopped["summary"]
    assert not _events(resp["task_id"], "worker_done"), "a stopped turn is never also done"


def test_a_note_that_never_reached_done_stops_the_task(server, source_repo) -> None:
    """The incident's own shape: the newest report said `test`, and the turn ended."""
    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE")
    from middle_manager import progress

    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})
    progress.record(resp["task_id"], phase="test", note="running the suite")
    conn = init_db()
    verdict = turn_end.why_stopped(conn, resp["task_id"])
    assert verdict.stopped
    assert "'test', not a done note" in verdict.reasons[0]


def test_unpushed_commits_stop_the_task_even_with_a_done_note(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "NOPUSH")
    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})

    stopped = _events(resp["task_id"], turn_end.WORKER_STOPPED)[-1]
    assert stopped["phase"] == "done", "the worker did file its report; it just never pushed"
    assert stopped["unpushed"] >= 1
    assert "never pushed" in stopped["summary"]


def test_a_backgrounded_last_command_stops_the_task(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "BACKGROUND")
    _wait_status(client, resp["task_id"], {turn_end.WORKER_STOPPED})

    stopped = _events(resp["task_id"], turn_end.WORKER_STOPPED)[-1]
    assert stopped["background_command"] == "make test"
    assert "killed when the turn ended" in stopped["summary"]
    assert stopped["phase"] == "done" and stopped["unpushed"] == 0, (
        "the other two signals were clean: the backgrounded command alone is enough"
    )


def test_a_cascaded_branch_is_not_mistaken_for_unpushed_work(server, source_repo) -> None:
    """A rebase upstream renames every commit; that is not work the worker withheld."""
    from middle_manager import stacks

    srv, client = server
    resp = _dispatch(client, source_repo, "")
    _wait_status(client, resp["task_id"], {"worker_done"})
    task = client.task_status(resp["task_id"])["task"]

    def _git(path, *args):
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    # GitHub's cascade: main moves, the branch is rebased onto it upstream.
    with open(f"{source_repo}/moved.txt", "w", encoding="utf-8") as fh:
        fh.write("the layer below merged\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved")
    cascade = f"{source_repo}-cascade"
    _git(source_repo, "worktree", "add", "-q", cascade, task["branch"])
    _git(cascade, "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "-q", "main")
    _git(source_repo, "worktree", "remove", "--force", cascade)

    conn = init_db()
    row = store.get_task(conn, resp["task_id"])
    assert stacks.unpushed_commits(conn, row) == 0
    assert turn_end.why_stopped(conn, resp["task_id"]).stopped is False


# --------------------------------------------------------------------------- #
# What the manager sees, and what a bare resume sends
# --------------------------------------------------------------------------- #


def test_run_and_task_snapshots_name_the_reason(server, source_repo, capsys) -> None:
    from middle_manager.cli import main

    srv, client = server
    resp = _dispatch(client, source_repo, "NODONE")
    task_id, run_id = resp["task_id"], resp["run_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})

    assert main(["run", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert "worker_stopped" in out
    assert "no done note was ever filed" in out, "the kind alone is not actionable"

    assert main(["task", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "worker_stopped" in out
    assert "no done note was ever filed" in out
    assert "mm resume" in out


def test_a_bare_resume_tells_the_worker_what_was_cut_short(server, source_repo) -> None:
    srv, client = server
    resp = _dispatch(client, source_repo, "NOPUSH")
    task_id = resp["task_id"]
    _wait_status(client, task_id, {turn_end.WORKER_STOPPED})

    assert client.resume_task(task_id)["ok"]
    resumed = _events(task_id, "resumed")[-1]
    message = resumed["message"]
    assert message, "a bare resume must not hand the worker silence"
    assert "commit(s) no remote holds" in message
    assert "foreground" in message and "background" in message
    assert "push your branch" in message

    # An explicit message still wins.
    _wait_status(client, task_id, {"worker_done", turn_end.WORKER_STOPPED})
    client.resume_task(task_id, "do this instead")
    assert _events(task_id, "resumed")[-1]["message"] == "do this instead"


def test_worker_stopped_is_a_real_lifecycle_status() -> None:
    from middle_manager.lifecycle import TASK_STATUSES, TERMINAL_STATUSES

    assert turn_end.WORKER_STOPPED in TASK_STATUSES
    assert turn_end.WORKER_STOPPED not in TERMINAL_STATUSES, "a stopped turn still needs finishing"
    assert turn_end.WORKER_STOPPED in store.ACTIONABLE_KINDS
