"""Dispatch refuses a base, remote or gate a worker cannot trust (issue #95).

About 40% of the worker reflections in performance assessment 6 named a gate or
environment false start: a gate command the worker's allowlist denied, a `make`
target missing at base, a lease cut at the wrong commit, and a stale local origin
that made a correct starting commit look missing (task 229). Each is checked here
against temp git repos and fake forge URLs; nothing reaches the network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import namedtuple

import pytest

from conftest import make_git_repo
from middle_manager import cli, preflight, repos
from middle_manager.state import init_db, store
from middle_manager.supervisor import core
from middle_manager.supervisor.client import SupervisorClient
from middle_manager.supervisor.server import SupervisorServer

Usage = namedtuple("Usage", "total used free")


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path, name: str, text: str) -> str:
    with open(f"{path}/{name}", "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))


@pytest.fixture
def sent(monkeypatch):
    """A stand-in supervisor that records what the CLI would have dispatched."""
    received: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            received.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "mm/task-7-abc"}

    import middle_manager.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    return received


@pytest.fixture
def untouchable(monkeypatch):
    class Untouchable:
        def __init__(self, *a, **k):
            raise AssertionError("a refused preflight must not reach the supervisor")

    import middle_manager.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", Untouchable)


@pytest.fixture
def claude_tools(monkeypatch):
    """The Claude worker's allowlist, without reading this machine's config."""
    tools = ["Read", "Edit", "Bash(git:*)", "Bash(uv:*)", "Bash(make:*)", "Bash(node:*)"]
    import middle_manager.providers.claude as claude_mod

    monkeypatch.setattr(claude_mod, "effective_allowed_tools", lambda: (tools, "test"))
    return tools


@pytest.fixture
def registered(mm_home, source_repo):
    return repos.add_repo(source_repo)


def _dispatch(repo: str, *extra: str, brief_text: str | None = None, tmp=None) -> int:
    argv = ["dispatch", "--repo", repo, "--title", "t"]
    if brief_text is not None:
        brief = tmp / "brief.md"
        brief.write_text(brief_text)
        argv += ["--brief", str(brief)]
    else:
        argv += ["--instructions", "go"]
    return cli.main([*argv, *extra])


def _set_gate(repo: str, gate: str) -> None:
    store.update_repo_fields(init_db(), repo, local_gate=gate)


def _events(task_id: int, kind: str) -> list[dict]:
    rows = init_db().execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    )
    return [json.loads(row["payload"]) for row in rows]


# --------------------------------------------------------------------------- #
# G1: the base clone's origin and the registered forge name one repository
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:acme/widget.git",
        "ssh://git@github.com/acme/widget",
        "https://github.com/Acme/widget.git/",
    ],
)
def test_ssh_and_https_spellings_of_one_github_repo_pass_the_remote_check(origin) -> None:
    preflight.check_remote(origin, "https://github.com/acme/widget")


def test_an_origin_naming_a_different_github_repo_is_refused_before_any_task(
    registered, untouchable, capsys
) -> None:
    _git(registered.local_path, "remote", "set-url", "origin", "git@github.com:acme/other.git")
    store.update_repo_fields(init_db(), registered.name, forge_url="https://github.com/acme/widget")

    rc = _dispatch(registered.name)

    err = capsys.readouterr().err
    assert rc == 1
    assert "git@github.com:acme/other.git" in err
    assert "https://github.com/acme/widget" in err
    assert "mm repo add <path> --forge-url" in err
    assert init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_a_local_path_origin_is_a_supported_registration_and_passes_the_remote_check(
    source_repo,
) -> None:
    """The manager's ruling: a path origin with the forge on a second remote is fine."""
    preflight.check_remote(source_repo, "https://github.com/acme/widget")


# --------------------------------------------------------------------------- #
# G2: the starting commit the brief names is on the remote after a fetch
# --------------------------------------------------------------------------- #


def test_a_reachable_starting_commit_passes_and_travels_to_the_supervisor(
    registered, sent, tmp_path
) -> None:
    head = _git(registered.local_path, "rev-parse", "HEAD")

    rc = _dispatch(
        registered.name, brief_text=f"# Do it\n\nyou must see `{head[:7]}`.\n", tmp=tmp_path
    )

    assert rc == 0
    assert sent["starting_sha"] == head
    assert sent["expect_base"] == head


def test_an_unreachable_starting_commit_is_refused_naming_the_sha_and_remote(
    registered, untouchable, tmp_path, capsys
) -> None:
    rc = _dispatch(registered.name, brief_text="# Do it\n\nyou must see `deadbee`.\n", tmp=tmp_path)

    err = capsys.readouterr().err
    assert rc == 1
    assert "deadbee" in err
    assert "git fetch origin" in err
    assert "--accept-preflight base" in err


def test_a_brief_with_no_starting_commit_and_no_base_skips_the_starting_commit_check(
    registered, sent, tmp_path
) -> None:
    rc = _dispatch(registered.name, brief_text="# Do it\n\nNo commit named.\n", tmp=tmp_path)

    assert rc == 0
    assert sent["starting_sha"] is None


def test_a_base_branch_missing_everywhere_is_refused(registered, untouchable, capsys) -> None:
    rc = _dispatch(registered.name, "--base", "no-such-branch")

    err = capsys.readouterr().err
    assert rc == 1
    assert "'no-such-branch'" in err
    assert "origin" in err


@pytest.fixture
def stale_local_origin(mm_home, tmp_path):
    """A repo registered from a local checkout that is behind its forge (task 229).

    The forge has a commit the local checkout never fetched. The starting commit
    and the intended base must come from the forge, never from the stale origin.
    """
    local = make_git_repo(tmp_path / "local")
    forge = tmp_path / "forge"
    subprocess.run(["git", "clone", "-q", local, str(forge)], check=True)
    _git(forge, "config", "receive.denyCurrentBranch", "ignore")
    ahead = _commit(forge, "new.txt", "only on the forge\n")
    added = repos.add_repo(local, forge_url=str(forge))
    return added, ahead


def test_a_starting_commit_only_on_the_forge_is_found_there_not_in_the_stale_origin(
    stale_local_origin, sent, tmp_path
) -> None:
    added, ahead = stale_local_origin

    rc = _dispatch(added.name, brief_text=f"# Do it\n\nyou must see `{ahead}`.\n", tmp=tmp_path)

    assert rc == 0
    assert sent["starting_sha"] == ahead
    assert sent["expect_base"] == ahead, "the intended base is the forge's head, not origin's"


# --------------------------------------------------------------------------- #
# G3: the lease sits on the intended base, or no worker runs
# --------------------------------------------------------------------------- #


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


@pytest.fixture
def spawned(monkeypatch):
    """Every provider adapter the supervisor builds; a refused lease builds none."""
    built: list[str] = []
    real = core._adapter_for

    def recording(provider):
        built.append(provider)
        return real(provider)

    monkeypatch.setattr(core, "_adapter_for", recording)
    return built


def test_a_lease_behind_the_forge_is_refused_released_and_no_worker_runs(
    stale_local_origin, server, spawned, tmp_path, capsys
) -> None:
    added, ahead = stale_local_origin
    stale = _git(added.local_path, "rev-parse", "HEAD")

    rc = _dispatch(
        added.name,
        "--provider",
        "fake",
        brief_text=f"# Do it\n\nyou must see `{ahead}`.\n",
        tmp=tmp_path,
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "dispatch refused" in err and "mm repo sync" in err
    conn = init_db()
    task = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert task["status"] == "failed"
    [refusal] = _events(task["id"], "preflight_refused")
    assert refusal["check"] == "lease"
    assert (refusal["expected"], refusal["actual"]) == (ahead, stale)
    statuses = {row["status"] for row in conn.execute("SELECT status FROM leases")}
    assert statuses == {"released"}
    assert spawned == []


def test_a_lease_at_the_wrong_commit_is_refused_by_the_supervisor_directly(
    registered, server, spawned
) -> None:
    _srv, client = server

    resp = client.dispatch_task(
        repo=registered.name, title="t", instructions="go", expect_base="0" * 40
    )

    assert not resp.get("ok")
    assert "dispatch refused" in resp["error"]
    assert spawned == []


# --------------------------------------------------------------------------- #
# G4: a Claude worker can run every segment of the recorded local gate
# --------------------------------------------------------------------------- #


def test_a_node_test_gate_passes_with_the_node_allowlist_entry(source_repo) -> None:
    preflight.check_gate(
        "node --test npm/test/*.test.js",
        ["Read", "Bash(node:*)"],
        local_path=source_repo,
        ref="HEAD",
    )


def test_an_inline_assignment_in_the_gate_is_refused(source_repo) -> None:
    with pytest.raises(preflight.PreflightError, match="inline assignment `CI=1`") as info:
        preflight.check_gate(
            "uv run ruff check . && CI=1 make x",
            ["Bash(uv:*)", "Bash(make:*)"],
            local_path=source_repo,
            ref="HEAD",
        )
    assert info.value.check == "gate"


def test_a_gate_command_off_the_allowlist_names_the_entry_it_lacks(source_repo) -> None:
    with pytest.raises(preflight.PreflightError, match=r"needs `Bash\(npm:\*\)`"):
        preflight.check_gate("npm test", ["Bash(uv:*)"], local_path=source_repo, ref="HEAD")


def test_a_make_target_missing_at_base_is_refused_and_a_present_one_passes(source_repo) -> None:
    _commit(source_repo, "Makefile", "lint:\n\truff check .\n\ntest lint-all: lint\n\tpytest\n")
    tools = ["Bash(make:*)"]

    preflight.check_gate("make lint && make -j2 test", tools, local_path=source_repo, ref="HEAD")
    with pytest.raises(preflight.PreflightError, match="make target `harness-check`"):
        preflight.check_gate(
            "make lint; make harness-check", tools, local_path=source_repo, ref="HEAD"
        )


def test_a_claude_dispatch_with_a_missing_make_target_is_refused(
    registered, claude_tools, untouchable, capsys
) -> None:
    _set_gate(registered.name, "make harness-check")

    rc = _dispatch(registered.name, "--provider", "claude")

    err = capsys.readouterr().err
    assert rc == 1
    assert "make harness-check" in err and "--accept-preflight gate" in err


def test_a_codex_dispatch_skips_the_gate_check(registered, sent) -> None:
    _set_gate(registered.name, "CI=1 make harness-check")

    rc = _dispatch(registered.name, "--provider", "codex")

    assert rc == 0
    assert sent["accepted_preflight"] == []


# --------------------------------------------------------------------------- #
# G5: each check is waved through only by name, with a reason, on the record
# --------------------------------------------------------------------------- #


def test_accepting_the_gate_check_lets_the_dispatch_through_with_the_reason(
    registered, claude_tools, sent, capsys
) -> None:
    _set_gate(registered.name, "make harness-check")

    rc = _dispatch(
        registered.name,
        "--provider",
        "claude",
        "--accept-preflight",
        "gate",
        "--reason",
        "target lands in the parent PR",
    )

    assert rc == 0
    [accepted] = sent["accepted_preflight"]
    assert accepted["check"] == "gate"
    assert "harness-check" in accepted["overridden"]
    assert sent["preflight_reason"] == "target lands in the parent PR"
    assert "preflight gate accepted" in capsys.readouterr().out


def test_an_accepted_check_is_recorded_as_an_event_on_the_task(registered, server) -> None:
    _srv, client = server

    resp = client.dispatch_task(
        repo=registered.name,
        title="t",
        instructions="go",
        accepted_preflight=[{"check": "gate", "overridden": "no make target"}],
        preflight_reason="target lands in the parent PR",
    )

    assert resp.get("ok"), resp
    [event] = _events(resp["task_id"], "preflight_accepted")
    assert event["check"] == "gate"
    assert event["reason"] == "target lands in the parent PR"
    assert event["overridden"] == "no make target"


def test_an_accepted_lease_mismatch_starts_the_worker_and_records_what_it_waved_through(
    registered, server
) -> None:
    _srv, client = server

    resp = client.dispatch_task(
        repo=registered.name,
        title="t",
        instructions="go",
        expect_base="0" * 40,
        accepted_preflight=[{"check": "lease", "overridden": None}],
        preflight_reason="known stale clone",
    )

    assert resp.get("ok"), resp
    [event] = _events(resp["task_id"], "preflight_accepted")
    assert event["check"] == "lease" and "intended base" in event["overridden"]


def test_an_unknown_check_name_is_an_argument_error(registered) -> None:
    with pytest.raises(SystemExit) as info:
        _dispatch(registered.name, "--accept-preflight", "everything", "--reason", "x")
    assert info.value.code == 2


def test_accepting_a_check_without_a_reason_is_refused(registered, untouchable, capsys) -> None:
    rc = _dispatch(registered.name, "--accept-preflight", "gate")

    assert rc == 2
    assert "needs --reason" in capsys.readouterr().err
