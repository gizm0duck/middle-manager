"""Preflight a brief against the worker's command surface and the slot's history (issue #79)."""

from __future__ import annotations

import json
import time

import pytest

from middle_manager import brief_lint, cli, environment, lifecycle, prior_attempts, repos
from middle_manager.brief_lint import allowlist_findings, literal_commands
from middle_manager.config import default_claude_allowed_tools
from middle_manager.providers.fake import FakeProvider
from middle_manager.state import init_db, store
from middle_manager.supervisor import core
from middle_manager.supervisor.core import Supervisor

ALLOWED = default_claude_allowed_tools()

BRIEF = """# Package the CLI

## Goals

`mm-client --help` works from a wheel. Contract §2.

## Intent

Users install one thing.

## In scope

`packaging/` and its tests. Pre-authorised adjacent changes: the pyproject entry.

## Out of scope

No new commands.

## Steps

Unpack the fixture with `unzip fixtures/sample.zip -d /tmp/x`, then:

```
chmod +x bin/mm-client
git add bin/mm-client && git commit -m "exec bit"
./bin/mm progress 7 --phase plan --note "planned"
```

Run `make test` and `uv run pytest -q`.
"""


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #


def test_literal_commands_are_read_from_fences_and_command_like_inline_code() -> None:
    found = [c for _line, c in literal_commands(BRIEF)]
    assert "unzip fixtures/sample.zip -d /tmp/x" in found
    assert "chmod +x bin/mm-client" in found
    # A compound line is split: each piece is matched on its own.
    assert "git add bin/mm-client" in found and 'git commit -m "exec bit"' in found
    assert './bin/mm progress 7 --phase plan --note "planned"' in found
    assert "make test" in found and "uv run pytest -q" in found
    # Identifiers in backticks are not commands.
    assert not any(c.startswith("mm-client --help") for c in found)
    assert not any(c.startswith("packaging/") for c in found)


def test_allowlist_findings_name_the_denied_command_and_its_substitute() -> None:
    found = allowlist_findings(BRIEF, ALLOWED)
    heads = {f.message.split("`")[3] for f in found}
    assert heads == {"unzip", "chmod"}
    by_head = {f.message.split("`")[3]: f for f in found}
    assert "git update-index --chmod=+x <file>" in by_head["chmod"].message
    assert "python3 -m zipfile -e" in by_head["unzip"].message
    assert (
        by_head["unzip"].line
        == BRIEF.splitlines().index(
            "Unpack the fixture with `unzip fixtures/sample.zip -d /tmp/x`, then:"
        )
        + 1
    )
    # Covered commands, with or without a path, are not reported.
    assert not any(h in heads for h in ("git", "make", "uv", "mm"))


def test_a_bare_bash_pattern_allows_everything_and_unknowns_get_generic_advice() -> None:
    assert allowlist_findings(BRIEF, ["Read", "Bash"]) == []
    (finding,) = allowlist_findings("Run `brew install jq` first.\n", ["Bash(git:*)"])
    assert "`brew` is not covered" in finding.message
    assert "let the manager do it" in finding.message


def test_lint_brief_asks_for_the_pre_authorised_line_under_in_scope() -> None:
    without = BRIEF.replace(" Pre-authorised adjacent changes: the pyproject entry.", "")
    (finding,) = [f for f in brief_lint.lint_brief(without) if "pre-authorised" in f.message]
    assert finding.line == BRIEF.splitlines().index("## In scope") + 1
    assert "Outside scope, required to build" in finding.message
    assert not any("pre-authorised" in f.message for f in brief_lint.lint_brief(BRIEF))
    # Either spelling, anywhere in the brief, satisfies it.
    spelled = without.replace(
        "## Out of scope", "Pre-authorized adjacent changes: none.\n\n## Out of scope"
    )
    assert not any("pre-authorised" in f.message for f in brief_lint.lint_brief(spelled))


def test_cli_brief_lint_checks_the_allowlist_for_claude_only(tmp_path, mm_home, capsys) -> None:
    path = tmp_path / "b.md"
    path.write_text(BRIEF)
    assert cli.main(["brief", "lint", str(path)]) == 1
    out = capsys.readouterr().out
    assert "claude allowlist: `chmod +x bin/mm-client`" in out
    assert cli.main(["brief", "lint", str(path), "--provider", "codex"]) == 0
    assert "no findings" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "brief",
                "lint",
                str(path),
                "--provider",
                "codex",
                "--prior-attempt",
                "task 3 ended closed: wrong table",
            ]
        )
        == 1
    )
    assert "no Prior attempt section" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Prior attempts
# --------------------------------------------------------------------------- #


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out")


def _closed_task(added, title: str, reason: str) -> int:
    supervisor = Supervisor()
    resp = supervisor.dispatch_task(repo=added.name, title=title, provider="fake")
    _wait_for(lambda: store.get_task(init_db(), resp["task_id"])["status"] == "worker_done")
    lifecycle.close_task(resp["task_id"], reason)
    return resp["task_id"]


def test_find_matches_an_ended_task_by_title_or_by_explicit_id(mm_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    prior = _closed_task(added, "Package the CLI", "under-delivered: no tests")
    conn = init_db()
    repo_id = store.get_repo(conn, added.name)["id"]
    [found] = prior_attempts.find(conn, repo_id, "  package THE cli ")
    assert int(found["id"]) == prior
    assert prior_attempts.find(conn, repo_id, "something else") == []
    [explicit] = prior_attempts.find(conn, repo_id, "renamed objective", explicit=prior)
    assert int(explicit["id"]) == prior

    summary = prior_attempts.summarize(conn, found)
    assert summary.status == "closed"
    assert summary.reason == "under-delivered: no tests"
    assert summary.brief_path is None  # nothing was archived for a --title dispatch
    assert summary.describe().startswith(f'task {prior} "Package the CLI" ended closed on ')
    assert summary.describe().endswith(": under-delivered: no tests")


def test_a_live_or_delivered_task_with_the_same_title_is_not_a_prior_attempt(
    mm_home, source_repo
) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    resp = supervisor.dispatch_task(repo=added.name, title="Package the CLI", provider="fake")
    _wait_for(lambda: store.get_task(init_db(), resp["task_id"])["status"] == "worker_done")
    conn = init_db()
    repo_id = store.get_repo(conn, added.name)["id"]
    assert prior_attempts.find(conn, repo_id, "Package the CLI") == []
    store.set_task_status(conn, resp["task_id"], "delivered")
    assert prior_attempts.find(conn, repo_id, "Package the CLI") == []


def test_the_environment_block_names_the_prior_attempt() -> None:
    prior = prior_attempts.PriorAttempt(
        task_id=155,
        title="Package the CLI",
        status="closed",
        ended_at="2026-09-09T12:00:00+00:00",
        reason="under-delivered: no tests",
        brief_path="/x/.mm/briefs/client/task-155.md",
    )
    env = environment.RepoEnvironment(repo="client")
    block = environment.render(
        env, task_id=159, evidence_path="/wt/.mm-evidence", prior_attempt=prior
    )
    line = [ln for ln in block.splitlines() if ln.startswith("- **Prior attempt:")][0]
    assert (
        'task 155 "Package the CLI" ended closed on 2026-09-09: under-delivered: no tests' in line
    )
    assert "/x/.mm/briefs/client/task-155.md" in line
    assert "what this attempt does differently" in line
    assert "Prior attempt" not in environment.render(env, task_id=1, evidence_path="/e")


def test_a_re_dispatch_carries_the_prior_attempt_into_the_worker_block(
    mm_home, source_repo, monkeypatch
) -> None:
    added = repos.add_repo(source_repo)
    prior = _closed_task(added, "Package the CLI", "under-delivered: no tests")

    supervisor = Supervisor()
    captured = []
    monkeypatch.setattr(core, "_adapter_for", lambda provider: FakeProvider())

    def capture(runner, spec, execution=None):
        captured.append(spec)
        supervisor._release(execution)

    monkeypatch.setattr(supervisor, "_run_task", capture)
    resp = supervisor.dispatch_task(repo=added.name, title="Package the CLI", provider="fake")
    assert resp["prior_attempt"] == prior
    _wait_for(lambda: len(captured) == 1)
    assert f"Prior attempt: task {prior}" in captured[0].environment
    assert "under-delivered: no tests" in captured[0].environment
    conn = init_db()
    assert store.get_task(conn, resp["task_id"])["prior_attempt_task"] == prior
    noted = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = 'prior_attempt_noted'",
        (resp["task_id"],),
    ).fetchone()
    assert json.loads(noted["payload"])["prior_task"] == prior

    # A first attempt at a new objective carries nothing.
    fresh = supervisor.dispatch_task(repo=added.name, title="Something new", provider="fake")
    assert fresh["prior_attempt"] is None
    _wait_for(lambda: len(captured) == 2)
    assert "Prior attempt" not in captured[1].environment


@pytest.fixture
def fake_client(monkeypatch):
    sent: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            sent.update(kwargs)
            return {"ok": True, "task_id": 99, "run_id": 3, "branch": "mm/task-99-abc"}

    import middle_manager.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    return sent


def test_dispatch_lint_asks_for_a_prior_attempt_section_on_a_re_dispatch(
    mm_home, source_repo, tmp_path, fake_client, capsys
) -> None:
    added = repos.add_repo(source_repo)
    prior = _closed_task(added, "Package the CLI", "under-delivered: no tests")
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF.replace("/tmp/x", "x"))

    rc = cli.main(["dispatch", "--repo", added.name, "--brief", str(brief), "--provider", "codex"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no Prior attempt section" in err
    assert f"task {prior}" in err and "under-delivered: no tests" in err
    assert fake_client["prior_attempt"] is None  # detection was by title; nothing explicit

    fake_client.clear()
    with_section = BRIEF.replace(
        "## Steps",
        "## Prior attempt\n\nTask closed for no tests; this one ships them first.\n\n## Steps",
    )
    brief.write_text(with_section)
    rc = cli.main(
        ["dispatch", "--repo", added.name, "--brief", str(brief), "--provider", "codex", "--strict"]
    )
    assert rc == 0, capsys.readouterr().err
    assert "Prior attempt" not in capsys.readouterr().err

    # --strict refuses a re-dispatch that says nothing about the last try.
    brief.write_text(BRIEF)
    rc = cli.main(
        [
            "dispatch",
            "--repo",
            added.name,
            "--brief",
            str(brief),
            "--provider",
            "codex",
            "--strict",
            "--prior-attempt",
            str(prior),
        ]
    )
    assert rc == 1
    assert "no Prior attempt section" in capsys.readouterr().err


def test_a_retitled_revision_of_the_same_brief_file_is_a_prior_attempt(
    mm_home, source_repo, tmp_path, fake_client, capsys
) -> None:
    """Briefs are named by plan position (b7-cli-browser-signin.md) and a re-dispatch
    revises the file; the title changes, the file does not."""
    added = repos.add_repo(source_repo)
    brief = tmp_path / "b7-cli-browser-signin.md"
    brief.write_text(BRIEF.replace("/tmp/x", "x"))
    supervisor = Supervisor()
    first = supervisor.dispatch_task(
        repo=added.name,
        title="Package the CLI",
        instructions=brief.read_text(),
        provider="fake",
        brief_path=str(brief),
    )
    _wait_for(lambda: store.get_task(init_db(), first["task_id"])["status"] == "worker_done")
    conn = init_db()
    assert store.get_task_env(conn, first["task_id"], prior_attempts.BRIEF_PATH_KEY) == str(
        brief.resolve()
    )
    lifecycle.close_task(first["task_id"], "wrong packaging root")

    repo_id = store.get_repo(conn, added.name)["id"]
    # Same file, new title: found by the file, not the title.
    [found] = prior_attempts.find(conn, repo_id, "Ship the CLI as a wheel", brief_path=str(brief))
    assert int(found["id"]) == first["task_id"]
    # A different file with a different title is a first attempt.
    assert (
        prior_attempts.find(conn, repo_id, "Ship the CLI as a wheel", brief_path="/x/other.md")
        == []
    )

    revised = brief.read_text().replace("# Package the CLI", "# Ship the CLI as a wheel")
    brief.write_text(revised)
    rc = cli.main(["dispatch", "--repo", added.name, "--brief", str(brief), "--provider", "codex"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no Prior attempt section" in err and "wrong packaging root" in err
    assert fake_client["brief_path"] == str(brief.resolve())
    assert fake_client["title"] == "Ship the CLI as a wheel"
