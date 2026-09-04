"""`mm deliver` writes the pull request body from the brief, the reports, and the review."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from middle_manager import delivery, pr_body, preflight, progress, review
from middle_manager.state import init_db, store

BRIEF = """# Rebuild the review surface

## Gap (every delivery this week)

The pull request body said only that the delivery was automated, so a reviewer who
had not seen the brief had nothing to read.

## Proposal

Compose the body from what the run already holds.
"""

DONE_NOTE = """Rebuilt the surface. The panel now renders the worker's own report first.

## Outside scope, required to build

Bumped the linter pin; the old one could not parse the new syntax.

## Flagged, not done

The dark theme still has a contrast problem on the diffstat.
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    monkeypatch.delenv(pr_body.FOOTER_ENV, raising=False)
    return init_db()


@pytest.fixture
def task(home, tmp_path):
    conn = home
    repo_id = store.add_repo(
        conn,
        name="papaya",
        origin="git@example.com:acme/papaya.git",
        local_path=str(tmp_path / "clone"),
        default_branch="main",
        base_sha="a" * 40,
    )
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(
        conn, run_id=run_id, title="Rebuild the review surface", repo_id=repo_id
    )
    store.update_task_fields(
        conn,
        task_id,
        worktree_path=str(tmp_path / "wt"),
        branch="mm/task-1-abc",
        base_sha="a" * 40,
    )
    return SimpleNamespace(id=task_id, conn=conn)


# --------------------------------------------------------------------------- #
# The parts
# --------------------------------------------------------------------------- #


def test_first_section_takes_the_sub_section_after_the_opening_heading() -> None:
    section = pr_body.first_section(BRIEF)
    assert section.startswith("**Gap (every delivery this week)**")
    assert "nothing to read" in section
    assert "Proposal" not in section  # the next section of the same depth ends it


def test_first_section_takes_prose_written_straight_under_the_heading() -> None:
    section = pr_body.first_section("# Title\n\nThe reason, in prose.\n\n## Next\n\nMore.\n")
    assert section == "The reason, in prose."


def test_first_section_is_empty_when_the_heading_is_all_there_is() -> None:
    assert pr_body.first_section("# Title only\n") == ""


def test_named_section_quotes_the_body_verbatim_however_the_heading_is_written() -> None:
    assert "Bumped the linter pin" in pr_body.named_section(
        DONE_NOTE, "Outside scope, required to build"
    )
    bold = "Done.\n\n**Flagged, not done**\n\nThe contrast problem stands.\n"
    assert pr_body.named_section(bold, "Flagged, not done") == "The contrast problem stands."
    plain = "Done.\n\nFlagged, not done:\n\nOne thing left.\n"
    assert pr_body.named_section(plain, "Flagged, not done") == "One thing left."
    assert pr_body.named_section("nothing here", "Flagged, not done") == ""


# --------------------------------------------------------------------------- #
# The composed body
# --------------------------------------------------------------------------- #


def test_the_body_is_composed_from_the_brief_the_reports_and_the_approval(
    task, monkeypatch
) -> None:
    preflight.archive_brief("papaya", task.id, BRIEF)
    progress.record(task.id, phase="test", note="Full suite: 326 passed.", conn=task.conn)
    progress.record(task.id, phase="done", note=DONE_NOTE, conn=task.conn)
    monkeypatch.setattr(review, "head_sha", lambda _wt: "b" * 40)
    review.record_review(task.id, "approved", note="Opened both captures; spacing is right.")

    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)

    assert "## Why" in body
    assert "nothing to read" in body  # from the brief's first section
    assert "The panel now renders the worker's own report first." in body
    assert "Bumped the linter pin" in body
    assert "contrast problem" in body
    assert "Full suite: 326 passed." in body
    assert "Opened both captures; spacing is right." in body
    assert "Bottom of its stack" in body
    assert "mm/task-1-abc" in body
    # Plain references: no bare task identifier stands in for the work.
    assert f"task {task.id}" not in body
    assert "Automated delivery" not in body


def test_a_section_the_closing_report_missed_is_carried_from_an_earlier_report(
    task, monkeypatch
) -> None:
    progress.record(
        task.id,
        phase="implement",
        note="Halfway.\n\n## Flagged, not done\n\nThe migration needs a second pass.\n",
        conn=task.conn,
    )
    progress.record(task.id, phase="done", note="Finished the panel.", conn=task.conn)
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "Finished the panel." in body
    assert "**Flagged, not done**" in body
    assert "The migration needs a second pass." in body


def test_the_stack_section_names_the_branch_the_work_was_started_from(task) -> None:
    store.update_task_fields(task.conn, task.id, stacked_on="harness/first-layer")
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "`harness/first-layer`" in body
    assert "Merge that one first" in body


def test_a_task_with_no_brief_and_no_reports_still_produces_a_valid_body(task) -> None:
    body = pr_body.compose(task.id, head_sha="c" * 40, conn=task.conn)
    for heading in ("## Why", "## What", "## Verification", "## Stack"):
        assert heading in body
    assert "No brief was archived" in body
    assert '"Rebuild the review surface"' in body
    assert "filed no closing report" in body
    assert "Neither a verification report nor a note" in body
    assert body.endswith("\n")


def test_the_harness_footer_is_appended_when_the_environment_supplies_one(
    task, monkeypatch
) -> None:
    monkeypatch.setenv(pr_body.FOOTER_ENV, "Generated with a harness\n\nhttps://example/session")
    body = pr_body.compose(task.id, head_sha="c" * 40, conn=task.conn)
    assert body.rstrip().endswith("https://example/session")
    assert "Delivered by Middle Manager" in body


# --------------------------------------------------------------------------- #
# Delivery uses it
# --------------------------------------------------------------------------- #


def _fake_gh(monkeypatch, calls: list[list[str]]) -> None:
    def fake_run(argv, cwd=None):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="https://example/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")


def test_deliver_opens_the_pull_request_with_the_composed_body(task, monkeypatch) -> None:
    preflight.archive_brief("papaya", task.id, BRIEF)
    progress.record(task.id, phase="done", note=DONE_NOTE, conn=task.conn)
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)

    delivery.deliver(task.id)

    argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    body = argv[argv.index("--body") + 1]
    assert "## Why" in body
    assert "nothing to read" in body
    assert argv[argv.index("--title") + 1] == "Rebuild the review surface"


def test_a_body_file_and_a_title_override_everything(task, tmp_path, monkeypatch) -> None:
    preflight.archive_brief("papaya", task.id, BRIEF)
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)
    handwritten = tmp_path / "body.md"
    handwritten.write_text("Written by hand, and not to be improved upon.\n")

    delivery.deliver(task.id, title="A title I chose", body_file=str(handwritten))

    argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert argv[argv.index("--body") + 1] == "Written by hand, and not to be improved upon.\n"
    assert argv[argv.index("--title") + 1] == "A title I chose"
