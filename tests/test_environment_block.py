"""The environment facts a worker needs come from repo config, rendered at dispatch.

Forty-eight of 102 worker reflections between 2026-09-01 and 09-07 lost a cycle to
the environment, not the task: a pre-push hook running the full suite, an evidence
directory under /private/tmp the worker's shell could not reach, a shared Postgres
container recreated by a sibling worktree, a full-suite run killed by the tool
timeout. The recipe existed in notes; nothing put it in front of the worker.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from middle_manager import cli, compose, environment, progress, repos, review
from middle_manager.config import MMConfig, WorkerCeiling, save_config
from middle_manager.providers.base import TaskSpec
from middle_manager.providers.claude import ClaudeAdapter
from middle_manager.providers.codex import CodexAdapter
from middle_manager.providers.command_rules import HEADING as RULES_HEADING
from middle_manager.providers.command_rules import command_rules
from middle_manager.state import init_db, store
from middle_manager.state.db import _column_names
from middle_manager.supervisor import autocommit
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer

# --------------------------------------------------------------------------- #
# Repo config
# --------------------------------------------------------------------------- #


def test_schema_gains_the_environment_columns_on_fresh_and_existing_databases(mm_home) -> None:
    conn = init_db()
    assert set(environment.REPO_COLUMNS) <= _column_names(conn, "repos")
    for col in environment.REPO_COLUMNS:
        conn.execute(f"ALTER TABLE repos DROP COLUMN {col}")
    conn.commit()
    conn = init_db()
    assert set(environment.REPO_COLUMNS) <= _column_names(conn, "repos")
    init_db()  # a second run must not fail on the now-present columns


def test_repo_set_records_the_environment_and_shows_it(mm_home, source_repo, capsys) -> None:
    added = repos.add_repo(source_repo)
    assert cli.main(["repo", "set", added.name]) == 0
    out = capsys.readouterr().out
    assert "compose stack: none declared" in out
    assert "evidence directory: .mm-evidence" in out

    assert (
        cli.main(
            [
                "repo",
                "set",
                added.name,
                "--compose-stack",
                "docker-compose.test.yml",
                "--db-port-base",
                "54000",
                "--push-hook-runs-full-suite",
                "yes",
                "--local-gate",
                "make test-backend",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "compose stack: docker-compose.test.yml; database port from 54000 + task id" in out
    assert "push hook runs the full suite: yes" in out
    assert "local gate: make test-backend; full suite owner: ci" in out

    env = environment.for_repo(init_db().execute("SELECT * FROM repos").fetchone())
    assert env.compose_file == "docker-compose.test.yml"
    assert env.db_port_base == 54000
    assert env.push_hook_runs_full_suite is True
    assert env.local_gate == "make test-backend"

    # `no` and an empty string clear a setting rather than storing the word.
    assert (
        cli.main(
            [
                "repo",
                "set",
                added.name,
                "--compose-stack",
                "no",
                "--push-hook-runs-full-suite",
                "no",
                "--local-gate",
                "",
            ]
        )
        == 0
    )
    env = environment.for_repo(init_db().execute("SELECT * FROM repos").fetchone())
    assert env.compose_stack is None
    assert env.push_hook_runs_full_suite is False
    assert env.local_gate is None


def test_repo_set_refuses_a_port_base_that_cannot_hold_a_task(mm_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    with pytest.raises(repos.RepoError, match="db-port-base"):
        repos.set_settings(added.name, db_port_base="70000")
    with pytest.raises(repos.RepoError, match="yes or no"):
        repos.set_settings(added.name, push_hook_runs_full_suite="maybe")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _env(**kwargs) -> environment.RepoEnvironment:
    return environment.RepoEnvironment(repo="backend", **kwargs)


def test_the_block_for_a_repo_without_a_compose_stack_names_evidence_and_the_gate() -> None:
    text = environment.render(
        _env(local_gate="make test-backend"), task_id=12, evidence_path="/wt/12/.mm-evidence"
    )
    assert text.startswith(f"## {environment.HEADING}")
    assert "`/wt/12/.mm-evidence/`" in text
    assert "Never write receipts under `/private/tmp`" in text
    assert "`mm review show` lists that directory" in text
    assert "Local gate: `make test-backend`" in text
    assert "The full suite belongs to CI" in text
    assert "tool timeout" in text
    assert "Private database stack" not in text
    assert "COMPOSE_PROJECT_NAME" not in text
    assert "Push hook" not in text


def test_the_block_for_a_repo_with_a_compose_stack_carries_the_override_recipe() -> None:
    text = environment.render(
        _env(compose_stack="docker-compose.test.yml", db_port_base=54000),
        task_id=12,
        evidence_path="/wt/12/.mm-evidence",
        compose_project="task_12",
        db_port=54012,
    )
    assert "compose project `task_12`" in text
    assert "host port **54012**" in text
    assert "`make <target> COMPOSE_PROJECT_NAME=task_12 DB_PORT=54012`" in text
    assert "`docker compose -p task_12 -f docker-compose.test.yml up -d`" in text
    assert "root `.env`" in text
    assert "`COMPOSE_PROJECT_NAME=task_12`, `DB_PORT=54012`" in text
    assert "Never run `docker compose down` on any other project name" in text
    assert "delivered or closed" in text


def test_a_repo_can_name_the_variable_its_makefile_reads_the_port_from() -> None:
    text = environment.render(
        _env(compose_stack="yes", db_port_base=55000, db_port_variable="PAPAYA_DB_PORT"),
        task_id=165,
        evidence_path="/wt/165/.mm-evidence",
        compose_project="task_165",
        db_port=55165,
    )
    assert "host port **55165** (`PAPAYA_DB_PORT`)" in text
    assert "`make <target> COMPOSE_PROJECT_NAME=task_165 PAPAYA_DB_PORT=55165`" in text
    assert "`COMPOSE_PROJECT_NAME=task_165`, `PAPAYA_DB_PORT=55165`" in text
    assert "DB_PORT=55165`" not in text.replace("PAPAYA_DB_PORT=55165`", "")


def test_the_push_hook_line_routes_the_push_through_the_harness() -> None:
    text = environment.render(
        _env(push_hook_runs_full_suite=True),
        task_id=12,
        evidence_path="/wt/12/.mm-evidence",
        branch="mm/task-12-abc",
    )
    assert "pre-push hook runs the full suite, so do not push" in text
    assert "replaces the push line in the command rules above" in text
    assert "Stop at the code-level gates" in text
    assert '`mm progress 12 --phase done --note "..."`' in text
    assert "`git rev-parse HEAD`" in text
    assert "pushes `mm/task-12-abc` on your behalf" in text
    assert "`mm task push 12`" in text


# --------------------------------------------------------------------------- #
# Port derivation
# --------------------------------------------------------------------------- #


def test_ports_are_deterministic_and_unique_per_task() -> None:
    assert environment.derive_port(54000, 12) == 54012
    assert environment.derive_port(54000, 12) == environment.derive_port(54000, 12)
    ports = [environment.derive_port(54000, task_id) for task_id in range(1, 1000)]
    assert len(set(ports)) == len(ports)
    assert all(54000 < port <= 65535 for port in ports)
    assert environment.derive_port(None, 12) is None


def test_a_port_past_the_top_of_the_range_wraps_rather_than_overflowing() -> None:
    port = environment.derive_port(65000, 5000)
    assert 65000 <= port <= 65535
    assert port == environment.derive_port(65000, 5000)


# --------------------------------------------------------------------------- #
# Dispatch-time assignment
# --------------------------------------------------------------------------- #


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def leased(mm_home, source_repo, tmp_path):
    """A registered repo with a compose stack, and a task on a linked worktree."""
    added = repos.add_repo(source_repo)
    repos.set_settings(added.name, compose_stack="yes", db_port_base="54000")
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build it", repo_id=repo_row["id"])
    worktree = tmp_path / "wt"
    _git(repo_row["local_path"], "worktree", "add", "-q", str(worktree), "-b", f"mm/task-{task_id}")
    store.update_task_fields(
        conn,
        task_id,
        worktree_path=str(worktree),
        branch=f"mm/task-{task_id}",
        repo_id=repo_row["id"],
    )
    return conn, repo_row, task_id, worktree


def test_prepare_records_the_stack_with_the_task_env_teardown_reads(leased) -> None:
    conn, repo_row, task_id, worktree = leased
    prepared = environment.prepare(
        conn, repo_row, task_id=task_id, worktree=str(worktree), branch=f"mm/task-{task_id}"
    )
    assert prepared.compose_project == f"task_{task_id}"
    assert prepared.db_port == 54000 + task_id
    assert compose.project_for(task_id, conn=conn) == f"task_{task_id}"
    assert store.get_task_env(conn, task_id, environment.DB_PORT_KEY) == str(54000 + task_id)
    rows = {row["key"]: row["source"] for row in store.task_env(conn, task_id)}
    assert rows == {"compose_project": "dispatch", "db_port": "dispatch"}
    assert f"compose project `task_{task_id}`" in prepared.block
    assert prepared.evidence_path == str(worktree / ".mm-evidence")


def test_prepare_leaves_a_repo_without_a_stack_alone_but_still_pins_evidence(
    mm_home, source_repo, tmp_path
) -> None:
    added = repos.add_repo(source_repo)
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build it", repo_id=repo_row["id"])
    prepared = environment.prepare(
        conn, repo_row, task_id=task_id, worktree=str(tmp_path / "nowhere"), branch=None
    )
    assert prepared.compose_project is None
    assert store.task_env(conn, task_id) == []
    assert ".mm-evidence/" in prepared.block
    assert "Private database stack" not in prepared.block


def test_the_evidence_directory_is_excluded_for_every_worktree_of_the_clone(leased) -> None:
    conn, repo_row, task_id, worktree = leased
    receipts = worktree / ".mm-evidence"
    receipts.mkdir()
    (receipts / "gate.log").write_text("ok\n")
    assert ".mm-evidence" in _git(worktree, "status", "--porcelain", "--untracked-files=all")

    assert environment.ensure_excluded(worktree, ".mm-evidence")
    assert _git(worktree, "status", "--porcelain", "--untracked-files=all") == ""
    # The base clone shares the exclude, and a second call adds nothing.
    assert environment.ensure_excluded(worktree, ".mm-evidence")
    exclude = Path(_git(worktree, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = worktree / exclude
    assert exclude.read_text().count("/.mm-evidence/") == 1


def test_ensure_excluded_shrugs_at_a_directory_that_is_not_a_worktree(tmp_path) -> None:
    assert environment.ensure_excluded(tmp_path / "missing", ".mm-evidence") is False


def test_the_auto_commit_holds_the_evidence_directory_back(tmp_path) -> None:
    assert autocommit.is_excluded(".mm-evidence/gate.log", worktree=str(tmp_path))
    assert not autocommit.is_excluded("src/app.py", worktree=str(tmp_path))


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #

BLOCK = f"## {environment.HEADING}\n\n- **Evidence directory: `/wt/.mm-evidence/`**\n"


def _spec(provider="claude", **kwargs):
    return TaskSpec(
        task_id=7,
        title="add the endpoint",
        instructions="THE BRIEF BODY",
        worktree_path="/wt",
        base_sha="abc1234",
        provider=provider,
        branch="mm/task-7-abc",
        **kwargs,
    )


def test_command_rules_append_the_block_for_claude_and_hand_it_alone_to_codex() -> None:
    claude = command_rules("claude", "mm/task-7-abc", environment=BLOCK)
    assert claude.index(RULES_HEADING) < claude.index(environment.HEADING)
    assert "git push origin HEAD:mm/task-7-abc" in claude
    codex = command_rules("codex", "mm/task-7-abc", environment=BLOCK)
    assert codex.startswith(f"## {environment.HEADING}")
    assert RULES_HEADING not in codex
    assert command_rules("codex", "mm/task-7-abc") == ""
    assert command_rules("claude", "mm/task-7-abc", environment=None) == command_rules(
        "claude", "mm/task-7-abc"
    )


def test_a_claude_worker_prompt_places_the_block_between_the_rules_and_the_brief() -> None:
    prompt = ClaudeAdapter().worker_prompt(_spec(environment=BLOCK, memory_preamble="MEMORY"))
    assert (
        prompt.index(RULES_HEADING)
        < prompt.index(environment.HEADING)
        < prompt.index("MEMORY")
        < prompt.index("THE BRIEF BODY")
    )


def test_a_codex_worker_prompt_carries_the_block_and_nothing_else_extra() -> None:
    prompt = CodexAdapter().worker_prompt(_spec(provider="codex", environment=BLOCK))
    assert prompt.startswith(f"## {environment.HEADING}")
    assert RULES_HEADING not in prompt
    assert prompt.index(environment.HEADING) < prompt.index("THE BRIEF BODY")
    assert CodexAdapter().worker_prompt(_spec(provider="codex")) == "THE BRIEF BODY"


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


def test_a_dispatched_claude_brief_carries_the_block_for_this_task(
    server, source_repo, monkeypatch
) -> None:
    srv, _client = server
    save_config(MMConfig(worker=WorkerCeiling("claude", "sonnet", "medium")))
    added = repos.add_repo(source_repo)
    repos.set_settings(
        added.name,
        compose_stack="yes",
        db_port_base="54000",
        push_hook_runs_full_suite="yes",
        local_gate="make test-backend",
    )
    captured: list[TaskSpec] = []
    monkeypatch.setattr(srv.supervisor, "_run_task", lambda runner, spec: captured.append(spec))
    result = srv.supervisor.dispatch_task(
        repo=added.name, title="do it", instructions="THE BRIEF BODY", provider="claude"
    )
    spec = captured[0]
    task_id = result["task_id"]
    assert result["compose_project"] == f"task_{task_id}"
    assert result["db_port"] == 54000 + task_id
    assert result["evidence_path"] == str(Path(spec.worktree_path) / ".mm-evidence")

    prompt = ClaudeAdapter().worker_prompt(spec)
    assert prompt.index(RULES_HEADING) < prompt.index(environment.HEADING)
    assert prompt.index(environment.HEADING) < prompt.index("THE BRIEF BODY")
    assert f"COMPOSE_PROJECT_NAME=task_{task_id} DB_PORT={54000 + task_id}" in prompt
    assert f"{spec.worktree_path}/.mm-evidence/" in prompt
    assert "Local gate: `make test-backend`" in prompt
    assert f"pushes `{spec.branch}` on your behalf" in prompt

    conn = init_db()
    assert compose.project_for(task_id, conn=conn) == f"task_{task_id}"
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM events").fetchall()]
    assert "environment_assigned" in kinds


def test_two_dispatches_to_one_repo_get_different_stacks(server, source_repo, monkeypatch) -> None:
    srv, _client = server
    save_config(MMConfig(worker=WorkerCeiling("claude", "sonnet", "medium")))
    added = repos.add_repo(source_repo)
    repos.set_settings(added.name, compose_stack="yes", db_port_base="54000")
    monkeypatch.setattr(srv.supervisor, "_run_task", lambda runner, spec: None)
    first = srv.supervisor.dispatch_task(repo=added.name, title="a", provider="claude")
    second = srv.supervisor.dispatch_task(repo=added.name, title="b", provider="claude")
    assert first["compose_project"] != second["compose_project"]
    assert first["db_port"] != second["db_port"]


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


def test_review_show_lists_the_evidence_directory_even_when_the_report_did_not(
    mm_home, source_repo, tmp_path, monkeypatch, capsys
) -> None:
    from types import SimpleNamespace

    added = repos.add_repo(source_repo)
    conn = init_db()
    repo_row = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build it", repo_id=repo_row["id"])
    worktree = tmp_path / "wt"
    receipts = worktree / ".mm-evidence"
    receipts.mkdir(parents=True)
    (receipts / "gate.log").write_text("ok\n")
    store.update_task_fields(
        conn, task_id, worktree_path=str(worktree), branch="mm/task-1-abc", base_sha="a" * 40
    )
    progress.record(task_id, phase="done", note="Done; receipts filed.", conn=conn)
    monkeypatch.setattr(
        review,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=1, diffstat="1 file"
        ),
    )
    assert cli.main(["review", "show", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "captures and receipts named in the worker's reports:" in out
    assert f"{receipts} — directory, 1 item(s)" in out
    assert "gate.log" in out
