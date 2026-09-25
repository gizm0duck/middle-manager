"""A worker's plan is reviewed, and every claim in it fact-checked, before it proceeds.

On 2026-09-25 a worker's plan claimed a provider write skipped a consent gate; the
manager relayed the claim and asked the user to rule on it, and the gate did not
apply there. Separately, issue #102: a bare ``mm resume`` after request-changes
sent the worker nothing, and two resumed workers exited doing nothing.
"""

from __future__ import annotations

import json
import time

import pytest

from middle_manager import board, plan_review, progress, repos
from middle_manager.cli import main
from middle_manager.review import record_review
from middle_manager.state import init_db, store
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer

CHECKED = "resume sends no findings => src/middle_manager/supervisor/core.py:852 read"


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
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows]


def _finished_task(client, source_repo) -> int:
    """A dispatched task whose first turn is over, so a resume is a fresh launch."""
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="plan first", instructions="")
    _wait_status(client, resp["task_id"], {"worker_done"})
    return resp["task_id"]


def _planned_task(client, source_repo) -> int:
    task_id = _finished_task(client, source_repo)
    progress.record(task_id, phase="plan", note="add a table.\nClaims: core.py:852 sends nothing")
    return task_id


def _bare_task(title: str = "plan first") -> int:
    conn = init_db()
    run_id = store.create_run(conn, "review plans")
    task_id = store.add_task(conn, run_id=run_id, title=title)
    store.set_task_status(conn, task_id, "worker_done")
    return task_id


# --------------------------------------------------------------------------- #
# mm review plan
# --------------------------------------------------------------------------- #


def test_a_plan_review_with_nothing_checked_is_refused(mm_home, capsys) -> None:
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="the approach")

    assert main(["review", "plan", str(task_id), "--approve"]) == 1
    assert "--checked" in capsys.readouterr().err
    assert plan_review.reviews_for(init_db(), task_id) == []


def test_a_checked_entry_without_evidence_is_refused(mm_home, capsys) -> None:
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="the approach")

    assert main(["review", "plan", str(task_id), "--checked", "it is fine", "--approve"]) == 1
    assert "<evidence>" in capsys.readouterr().err
    assert main(["review", "plan", str(task_id), "--checked", "it is fine =>", "--approve"]) == 1


def test_a_task_with_no_plan_note_has_nothing_to_review(mm_home, capsys) -> None:
    task_id = _bare_task()
    assert main(["review", "plan", str(task_id), "--checked", CHECKED, "--approve"]) == 1
    assert "no plan note" in capsys.readouterr().err


def test_request_changes_needs_a_findings_file(mm_home, tmp_path, capsys) -> None:
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="the approach")
    args = ["review", "plan", str(task_id), "--checked", CHECKED, "--request-changes"]

    assert main(args) == 1
    assert main([*args, "--findings", str(tmp_path / "missing.md")]) == 1
    findings = tmp_path / "findings.md"
    findings.write_text("the gate only applies to hosted-agent writes\n", encoding="utf-8")
    assert main([*args, "--findings", str(findings)]) == 0


def test_the_review_is_bound_to_the_newest_plan_note(mm_home, capsys) -> None:
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="first approach")
    second = progress.record(task_id, phase="plan", note="second approach")

    assert main(["review", "plan", str(task_id), "--checked", CHECKED, "--approve"]) == 0
    row = plan_review.reviews_for(init_db(), task_id)[0]
    assert row["progress_event_id"] == second
    assert json.loads(row["checked"]) == [
        {
            "claim": "resume sends no findings",
            "evidence": "src/middle_manager/supervisor/core.py:852 read",
        }
    ]
    out = capsys.readouterr().out
    assert "## Manager fact-check" in out
    assert "| resume sends no findings | src/middle_manager/supervisor/core.py:852 read |" in out
    assert _events(task_id, "plan_reviewed")[-1]["progress_event_id"] == second


def test_review_show_prints_the_plan_review_record(server, source_repo, capsys) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)

    assert main(["review", "show", str(task_id)]) == 0
    assert f"run `mm review plan {task_id}` first" in capsys.readouterr().out

    assert main(["review", "plan", str(task_id), "--checked", CHECKED, "--approve"]) == 0
    capsys.readouterr()
    assert main(["review", "show", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "## Manager fact-check" in out
    assert "not yet delivered" in out
    assert "mm review plan" not in out.split("## Manager fact-check")[0]


# --------------------------------------------------------------------------- #
# The gate on resume and steer
# --------------------------------------------------------------------------- #


def test_resume_and_steer_are_refused_on_an_unreviewed_plan(server, source_repo) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)
    resumes_before = len(_events(task_id, "resumed"))

    resumed = client.resume_task(task_id)
    assert not resumed["ok"]
    assert f"run `mm review plan {task_id}` first" in resumed["error"]
    assert "\n" not in resumed["error"], "the refusal is one line"

    steered = client.steer_task(task_id, "go on")
    assert not steered["ok"]
    assert f"run `mm review plan {task_id}` first" in steered["error"]

    assert len(_events(task_id, "resumed")) == resumes_before, "nothing was launched"


def test_the_cli_resume_prints_the_one_line_refusal(server, source_repo, capsys) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)

    assert main(["resume", str(task_id), "--verify-seconds", "0"]) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("resume failed:")
    assert f"mm review plan {task_id}" in err
    assert main(["steer", str(task_id), "--message", "go"]) == 1
    assert f"mm review plan {task_id}" in capsys.readouterr().err


def test_force_unreviewed_records_its_reason(server, source_repo) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)

    assert client.resume_task(task_id, "go", force_unreviewed="  ")["ok"] is False, (
        "a blank reason is no reason"
    )
    resp = client.resume_task(task_id, "go", force_unreviewed="the user said ship it tonight")
    assert resp["ok"]
    forced = _events(task_id, "plan_review_forced")
    assert forced[-1]["reason"] == "the user said ship it tonight"
    assert forced[-1]["via"] == "resume"
    assert forced[-1]["progress_event_id"] is not None


def test_a_reviewed_plan_opens_the_gate(server, source_repo) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)
    plan_review.record(task_id, checked=[CHECKED], verdict="approved")
    assert client.resume_task(task_id)["ok"]


def test_a_new_plan_note_needs_its_own_review(mm_home) -> None:
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="first")
    plan_review.record(task_id, checked=[CHECKED], verdict="approved")
    conn = init_db()
    assert plan_review.refusal(conn, task_id) is None
    progress.record(task_id, phase="plan", note="revised")
    assert plan_review.refusal(conn, task_id) is not None


def test_a_done_note_never_trips_the_gate(mm_home) -> None:
    """The fake worker files `--phase done` (fake_worker.py); only a plan is gated."""
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="the approach")
    progress.record(task_id, phase="done", note="finished")
    conn = init_db()
    assert plan_review.refusal(conn, task_id) is None
    assert plan_review.all_awaiting(conn) == []


def test_a_task_that_never_planned_is_not_gated(server, source_repo) -> None:
    srv, client = server
    task_id = _finished_task(client, source_repo)
    assert client.resume_task(task_id, "carry on")["ok"]
    assert not _events(task_id, "plan_review_forced")


# --------------------------------------------------------------------------- #
# What the worker hears on the next resume
# --------------------------------------------------------------------------- #


def test_an_approval_delivers_its_message_on_a_bare_resume(server, source_repo) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)
    plan_review.record(
        task_id, checked=[CHECKED], verdict="approved", message="also cite fake_worker.py"
    )

    assert client.resume_task(task_id)["ok"]
    resumed = _events(task_id, "resumed")[-1]
    assert "APPROVED" in resumed["message"]
    assert "also cite fake_worker.py" in resumed["message"]
    assert "core.py:852 read" in resumed["message"], "the worker sees what was checked"
    row = plan_review.reviews_for(init_db(), task_id)[0]
    assert row["delivered_at"] is not None
    assert resumed["plan_reviews_delivered"] == [row["id"]]

    _wait_status(client, task_id, {"worker_done"})
    assert client.resume_task(task_id, "next thing")["ok"]
    assert _events(task_id, "resumed")[-1]["message"] == "next thing", "delivered once"


def test_a_plan_sent_back_delivers_its_findings(server, source_repo) -> None:
    srv, client = server
    task_id = _planned_task(client, source_repo)
    plan_review.record(
        task_id,
        checked=["write skips consent gate => the gate only guards hosted-agent writes"],
        verdict="changes_requested",
        findings="Drop the consent-gate change; it is not needed.",
    )

    assert client.resume_task(task_id, "and keep it small")["ok"]
    message = _events(task_id, "resumed")[-1]["message"]
    assert "REQUESTED CHANGES" in message
    assert "Drop the consent-gate change" in message
    assert message.endswith("and keep it small"), "an explicit message still goes along"


def test_a_bare_resume_after_request_changes_carries_the_findings(server, source_repo) -> None:
    """Issue #102."""
    srv, client = server
    task_id = _finished_task(client, source_repo)
    record_review(task_id, "changes_requested", "Rename the flag to --force-unreviewed.")

    assert client.resume_task(task_id)["ok"]
    resumed = _events(task_id, "resumed")[-1]
    assert resumed["message"], "a bare resume never hands the worker nothing"
    assert "Rename the flag to --force-unreviewed." in resumed["message"]
    assert resumed["reviews_delivered"]

    _wait_status(client, task_id, {"worker_done"})
    assert client.resume_task(task_id, "next")["ok"]
    assert _events(task_id, "resumed")[-1]["message"] == "next", "delivered once"


def test_an_approval_after_request_changes_sends_nothing_stale(server, source_repo) -> None:
    srv, client = server
    task_id = _finished_task(client, source_repo)
    record_review(task_id, "changes_requested", "old finding")
    record_review(task_id, "approved")

    assert client.resume_task(task_id, "just this")["ok"]
    assert _events(task_id, "resumed")[-1]["message"] == "just this"


# --------------------------------------------------------------------------- #
# Board and health
# --------------------------------------------------------------------------- #


def test_board_lists_plans_awaiting_review(mm_home) -> None:
    waiting = _bare_task("the waiting one")
    progress.record(waiting, phase="plan", note="approach")
    reviewed = _bare_task("the reviewed one")
    progress.record(reviewed, phase="plan", note="approach")
    plan_review.record(reviewed, checked=[CHECKED], verdict="approved")

    text = board.render_board(init_db())
    section = text.split("## Plans awaiting review")[1].split("##")[0]
    assert "the waiting one" in section
    assert f"mm review plan {waiting}" in section
    assert "the reviewed one" not in section


def test_health_lists_plans_awaiting_review(mm_home, capsys) -> None:
    waiting = _bare_task("the waiting one")
    progress.record(waiting, phase="plan", note="approach")

    main(["health"])
    assert "plan awaiting review" in capsys.readouterr().out
    main(["health", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [p["task_id"] for p in data["plans_awaiting_review"]] == [waiting]


def test_a_closed_task_is_not_awaiting_review(mm_home) -> None:
    task_id = _bare_task()
    progress.record(task_id, phase="plan", note="approach")
    store.set_task_status(init_db(), task_id, "closed")
    assert plan_review.all_awaiting() == []


# --------------------------------------------------------------------------- #
# The worker is told to list its claims
# --------------------------------------------------------------------------- #


def test_the_worker_is_told_to_list_claims_with_sources() -> None:
    from middle_manager.memory import worker_context
    from middle_manager.providers.command_rules import command_rules

    context = worker_context("repo", task_id=1)
    assert '"Claims:"' in context and "path:line" in context
    assert "evidence for its premise" in context
    rules = command_rules("claude", "b")
    assert "**Claims:**" in rules and "evidence for its premise" in rules
