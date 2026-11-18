"""Bitbucket through Atlassian's ``twg bb``, chosen explicitly at setup; GitHub stays the default.

Every twg payload here is a real Bitbucket response (``tests/fixtures/bitbucket``,
captured 2026-11-17 from the public ``atlassian/forge-bitbucket-related-prs``
repository) inside twg's envelope. The error envelope is twg 1.4.5's real answer
with no Bitbucket token. No test calls the real ``twg``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from middle_manager import config, forge, gh_stack, repos, stack_merge, watch
from middle_manager.config import ConfigError, ForgePolicy, MMConfig, load_config, save_config
from middle_manager.setup import discovery, wizard

FIXTURES = Path(__file__).parent / "fixtures" / "bitbucket"
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents" / "skills" / "bitbucket-twg" / "scripts" / "twg_pr_state.py"
SLUG = "atlassian/forge-bitbucket-related-prs"

AUTH_ERROR = {
    "ok": False,
    "error": {
        "code": "AUTH_INVALID",
        "kind": "bitbucket",
        "mode": "bbc-token",
        "statusCode": 401,
        "message": "Bitbucket Cloud authentication required: no Bitbucket token configured.",
        "summary": "Your Bitbucket token is missing or invalid. Run `twg setup bitbucket` to set "
        "one up.",
        "remediation": {"command": "twg setup bitbucket", "env": ["TWG_BBC_TOKEN"]},
    },
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def data(name: str):
    return fixture(name)["data"]


@pytest.fixture
def mm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return tmp_path / ".mm"


@pytest.fixture
def bitbucket(mm_home):
    save_config(MMConfig(forge=ForgePolicy(kind="bitbucket")))
    return mm_home


class FakeTwg:
    """Answers ``run_twg`` from a table keyed by the twg argument list."""

    def __init__(self, answers: dict[tuple, object]) -> None:
        self.answers = answers
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, *, slug, cwd=None):
        self.calls.append((list(args), slug))
        for prefix, answer in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(answer, tuple):
                    code, payload = answer
                else:
                    code, payload = 0, answer
                return subprocess.CompletedProcess(args, code, json.dumps(payload), "")
        return subprocess.CompletedProcess(args, 1, json.dumps(AUTH_ERROR), "")


# --------------------------------------------------------------------------- #
# Configuration: GitHub unless explicitly set
# --------------------------------------------------------------------------- #


def test_default_config_is_github_and_writes_no_forge_table(mm_home) -> None:
    cfg = MMConfig()
    cfg.validate()
    assert "forge" not in cfg.to_dict()
    path = save_config(cfg)
    assert "[forge]" not in path.read_text(encoding="utf-8")
    assert forge.kind() == "github"


def test_no_config_at_all_is_github(mm_home) -> None:
    assert forge.kind() == "github"
    assert forge.tool_name() == "gh"


def test_bitbucket_round_trips(bitbucket) -> None:
    cfg = load_config()
    assert cfg.forge.kind == "bitbucket"
    assert cfg.to_dict()["forge"] == {"kind": "bitbucket", "twg_path": ""}
    assert forge.is_bitbucket()


def test_unknown_forge_is_refused() -> None:
    cfg = MMConfig(forge=ForgePolicy(kind="gitlab"))
    with pytest.raises(ConfigError, match="forge.kind"):
        cfg.validate()


def test_a_bitbucket_remote_never_switches_the_forge(mm_home) -> None:
    url = f"https://bitbucket.org/{SLUG}.git"
    assert forge.kind() == "github"
    assert repos.forge_slug(url) is None
    assert not repos.is_forge_url(url)
    assert repos.forge_slug("https://github.com/acme/widgets") == "acme/widgets"


def test_configured_bitbucket_reads_bitbucket_urls(bitbucket) -> None:
    for url in (
        f"https://bitbucket.org/{SLUG}.git",
        f"https://someone@bitbucket.org/{SLUG}",
        f"git@bitbucket.org:{SLUG}.git",
        f"ssh://git@bitbucket.org/{SLUG}.git",
    ):
        assert repos.forge_slug(url) == SLUG, url
    assert repos.is_forge_url(f"git@bitbucket.org:{SLUG}.git")


def _fake_report(usable):
    harness = {
        "kind": "harness",
        "path": "/usr/bin/x",
        "version": "1",
        "authenticated": True,
        "available": True,
        "detail": "",
    }
    return {
        "harnesses": [{**harness, "name": name} for name in usable],
        "requirements": [],
        "companions": [],
    }


def _executable(path: Path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_setup_records_bitbucket_with_a_twg_path(mm_home, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wizard, "discover", lambda: _fake_report(["claude"]))
    twg = _executable(tmp_path / "twg")
    cfg = wizard.run_setup(
        non_interactive=True,
        overrides={
            "manager_provider": "claude",
            "worker_provider": "claude",
            "forge": "bitbucket",
            "twg_path": twg,
        },
    )
    assert (cfg.forge.kind, cfg.forge.twg_path) == ("bitbucket", twg)
    assert load_config().forge.kind == "bitbucket"
    assert forge.twg_bin() == twg


def test_setup_defaults_to_github(mm_home, monkeypatch) -> None:
    monkeypatch.setattr(wizard, "discover", lambda: _fake_report(["claude"]))
    cfg = wizard.run_setup(
        non_interactive=True,
        overrides={"manager_provider": "claude", "worker_provider": "claude"},
    )
    assert cfg.forge == ForgePolicy()


def test_setup_refuses_bitbucket_without_twg(mm_home, monkeypatch) -> None:
    monkeypatch.setattr(wizard, "discover", lambda: _fake_report(["claude"]))
    monkeypatch.setattr(forge, "twg_bin", lambda: None)
    with pytest.raises(ConfigError, match="TWG CLI"):
        wizard.run_setup(
            non_interactive=True,
            overrides={
                "manager_provider": "claude",
                "worker_provider": "claude",
                "forge": "bitbucket",
            },
        )


def test_config_forge_switches_an_existing_install(mm_home, tmp_path, monkeypatch) -> None:
    save_config(MMConfig())
    twg = _executable(tmp_path / "twg")
    assert wizard.config_forge("bitbucket", twg).forge.kind == "bitbucket"
    assert wizard.config_forge("github", None).forge == ForgePolicy(kind="github", twg_path=twg)


def test_twg_resolution_order(bitbucket, tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    local = _executable(home / ".local" / "bin" / "twg")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert forge.twg_bin() == local  # the installer's spot, with a thin PATH
    on_path = tmp_path / "bin"
    on_path.mkdir()
    _executable(on_path / "twg")
    monkeypatch.setenv("PATH", str(on_path))
    assert forge.twg_bin() == str(on_path / "twg")
    pinned = _executable(tmp_path / "pinned-twg")
    cfg = load_config()
    cfg.forge.twg_path = pinned
    save_config(cfg)
    assert forge.twg_bin() == pinned


def test_discovery_requires_gh_by_default_and_twg_for_bitbucket(mm_home, monkeypatch) -> None:
    names = [r["name"] for r in discovery.discover()["requirements"]]
    assert "gh" in names and "twg" not in names
    save_config(MMConfig(forge=ForgePolicy(kind="bitbucket")))
    monkeypatch.setattr(forge, "twg_bin", lambda: None)
    twg = next(r for r in discovery.discover()["requirements"] if r["name"] == "twg")
    assert not twg["available"] and "twg setup bitbucket" in twg["detail"]
    assert "gh" not in [r["name"] for r in discovery.discover()["requirements"]]


def test_doctor_reads_only_twgs_bitbucket_flags(bitbucket, tmp_path, monkeypatch) -> None:
    twg = _executable(tmp_path / "twg")
    monkeypatch.setattr(forge, "twg_bin", lambda: twg)
    seen = {}
    answer = {
        "apiVersion": "v2",
        "command": "doctor",
        "data": {"bitbucket": {"ok": False, "status": "missing", "attempted": False}},
        "meta": {},
    }

    def run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    monkeypatch.setenv("TWG_AGENT_DEFAULTS", "1")
    monkeypatch.setattr(forge.subprocess, "run", run)
    status = forge.doctor_status()
    assert status == {
        "available": True,
        "path": twg,
        "bitbucket_ok": False,
        "bitbucket_status": "missing",
        "hint": forge.AUTH_HINT,
    }
    assert seen["argv"][1:] == ["--output", "json", "--output-summary", "none", "doctor"]
    assert "TWG_AGENT_DEFAULTS" not in seen["env"]
    from middle_manager.setup import doctor

    monkeypatch.setattr(doctor, "discover", lambda: _fake_report(["claude"]))
    text = doctor.render_text(doctor.collect())
    assert "Bitbucket access is missing; run `twg login`, then `twg setup bitbucket`" in text


# --------------------------------------------------------------------------- #
# Invocation and envelopes
# --------------------------------------------------------------------------- #


def test_argv_asks_for_the_bare_envelope_and_names_the_repo() -> None:
    argv = forge.twg_argv("/bin/twg", ["pull-requests", "get", "7"], slug=SLUG)
    assert argv == [
        "/bin/twg",
        "--output",
        "json",
        "--output-summary",
        "none",
        "bb",
        "pull-requests",
        "get",
        "7",
        "--repo",
        SLUG,
    ]


def test_run_twg_drops_agent_defaults(monkeypatch) -> None:
    monkeypatch.setenv("TWG_AGENT_DEFAULTS", "1")
    monkeypatch.setattr(forge, "twg_bin", lambda: "/bin/twg")
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(forge.subprocess, "run", run)
    forge.run_twg(["repo", "get"], slug=SLUG)
    assert "TWG_AGENT_DEFAULTS" not in seen["env"]


def test_missing_twg_is_a_failure_with_an_install_hint(monkeypatch) -> None:
    monkeypatch.setattr(forge, "twg_bin", lambda: None)
    proc = forge.run_twg(["repo", "get"], slug=SLUG)
    assert proc.returncode != 0
    assert "teamwork-graph.atlassian.com" in forge.error_detail(proc)


def test_envelope_success_and_error() -> None:
    assert forge.parse_envelope(json.dumps(fixture("repo_get.json")))["mainbranch"] == {
        "name": "main",
        "type": "branch",
    }
    assert forge.parse_envelope(json.dumps(AUTH_ERROR)) is None
    assert forge.parse_envelope("Error: not json") is None
    failed = subprocess.CompletedProcess([], 77, json.dumps(AUTH_ERROR), "Error: noise on stderr")
    detail = forge.error_detail(failed)
    assert detail.startswith("Your Bitbucket token is missing or invalid")
    assert detail.endswith("(run `twg setup bitbucket`)")
    assert "noise" not in detail
    assert "TWG_BBC_TOKEN" not in detail


# --------------------------------------------------------------------------- #
# Reading real Bitbucket objects
# --------------------------------------------------------------------------- #


def test_query_payload_is_a_list_of_pull_requests() -> None:
    rows = forge.pr_rows(data("query_open.json"))
    assert len(rows) == 3 and all(row["state"] == "OPEN" for row in rows)
    assert forge.pr_rows(data("get_237_statuses.json"))[0]["id"] == 237


def test_open_pull_request_normalizes() -> None:
    pr = forge.normalize_pr(data("get_237_statuses.json"))
    assert pr["number"] == 237
    assert pr["state"] == "OPEN"
    assert pr["baseRefName"] == "main"
    assert pr["headRefName"] == "renovate/node-26.x"
    assert pr["headRefOid"] == "49f3b57d59e8"
    assert pr["url"] == f"https://bitbucket.org/{SLUG}/pull-requests/237"
    assert pr["merged"] is False and pr["merge_commit"] is None


def test_merged_pull_request_carries_the_short_merge_hash() -> None:
    pr = forge.normalize_pr(data("get_244_statuses.json"))
    assert pr["state"] == "MERGED"
    assert pr["merged"] is True
    assert pr["merge_commit"] == "eb9abdfb6407"  # short: expanded before mm records it
    assert pr["mergedAt"]


def test_declined_is_closed() -> None:
    raw = copy.deepcopy(data("get_237_statuses.json"))
    raw["state"] = "DECLINED"
    assert forge.normalize_pr(raw)["state"] == "CLOSED"


def test_status_buckets() -> None:
    assert forge.status_bucket("SUCCESSFUL") == "pass"
    assert forge.status_bucket("FAILED") == "fail"
    assert forge.status_bucket("STOPPED") == "fail"
    assert forge.status_bucket("INPROGRESS") == "pending"
    assert forge.status_bucket("SOMETHING_NEW") == "pending"


def test_only_head_statuses_count() -> None:
    raw = copy.deepcopy(data("get_237_statuses.json"))
    assert forge.head_status_rows(raw) == [
        {"name": "renovate/stability-days", "bucket": "pass"},
        {"name": "Pipeline - default", "bucket": "pass"},
        {"name": "Pipeline - custom: trust-greenlight-pipeline", "bucket": "pass"},
    ]
    # A red build on an older push (synthetic, added to the real payload).
    old = copy.deepcopy(raw["_statuses"][1])
    old["state"], old["commit"]["hash"] = "FAILED", "0" * 40
    raw["_statuses"].append(old)
    assert all(row["bucket"] == "pass" for row in forge.head_status_rows(raw))


def test_blocking_merge_checks() -> None:
    rows = forge.blocking_rows(data("mergeability_237.json"))
    assert rows == [
        {"name": "merge check current_user_permission_check", "bucket": "fail"},
        {"name": "merge check minimum_approvals", "bucket": "fail"},
        {"name": "merge check minimum_default_reviewer_approvals", "bucket": "fail"},
    ]
    assert forge.git_mergeable(data("mergeability_237.json")) == "MERGEABLE"
    unknown = [{"type": "standard_merge_check", "status": "UNKNOWN", "blocking": True}]
    assert forge.blocking_rows(unknown)[0]["bucket"] == "pending"


def test_merge_result_in_both_shapes() -> None:
    pr = data("get_244_statuses.json")
    assert forge.merge_result_pr({"task_status": "SUCCESS", "merge_result": pr})["id"] == 244
    shaped = {"ok": True, "action": "merge", "status": "merged", "pullRequest": pr, "data": pr}
    assert forge.merge_result_pr(shaped)["id"] == 244
    assert forge.merge_result_pr({"task_status": "PENDING"}) is None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_expand_sha_turns_a_short_hash_into_the_full_one(tmp_path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    full = _git(repo, "rev-parse", "HEAD")
    assert forge.expand_sha(full[:12], str(repo)) == full
    assert forge.expand_sha("eb9abdfb6407", str(repo)) is None
    assert forge.expand_sha(None, str(repo)) is None


# --------------------------------------------------------------------------- #
# Operations, against a fake twg
# --------------------------------------------------------------------------- #


def test_lookup_open_branch(bitbucket, monkeypatch) -> None:
    fake = FakeTwg(
        {
            ("pull-requests", "query"): fixture("query_open.json")
            | {"data": [data("get_237_statuses.json")]},
            ("pull-requests", "get"): fixture("get_237_statuses.json"),
            ("pull-requests", "mergeability"): fixture("mergeability_237.json"),
        }
    )
    monkeypatch.setattr(forge, "run_twg", fake)
    monkeypatch.setattr(forge, "slug_from_checkout", lambda cwd: SLUG)
    entry = watch._lookup_pr("renovate/node-26.x", "/tmp")
    assert entry == {
        "known": True,
        "pr": 237,
        "base": "main",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "merge_state": "BLOCKED",
        "ci": "pass",
        "failing": [],
        "merged": False,
        "merge_commit": None,
    }
    assert fake.calls[0] == (
        [
            "pull-requests",
            "query",
            "--source",
            "renovate/node-26.x",
            "--state",
            "OPEN",
            "--limit",
            "1",
        ],
        SLUG,
    )
    assert fake.calls[1][0] == ["pull-requests", "get", "237", "--statuses"]


def test_lookup_merged_branch_expands_the_merge_commit(bitbucket, monkeypatch) -> None:
    merged = data("get_244_statuses.json")
    fake = FakeTwg(
        {
            ("pull-requests", "query", "--source", "b", "--state", "OPEN"): fixture(
                "query_open.json"
            )
            | {"data": []},
            ("pull-requests", "query", "--source", "b", "--state", "MERGED"): fixture(
                "query_merged.json"
            )
            | {"data": [merged]},
            ("pull-requests", "get"): fixture("get_244_statuses.json"),
        }
    )
    monkeypatch.setattr(forge, "run_twg", fake)
    monkeypatch.setattr(forge, "slug_from_checkout", lambda cwd: SLUG)
    monkeypatch.setattr(forge, "expand_sha", lambda short, cwd: short + "f" * 28)
    entry = forge.lookup("b", "/tmp")
    assert entry["state"] == "MERGED" and entry["merged"] is True
    assert entry["merge_commit"] == "eb9abdfb6407" + "f" * 28
    # A merge commit git cannot expand confirms nothing.
    monkeypatch.setattr(forge, "expand_sha", lambda short, cwd: None)
    entry = forge.lookup("b", "/tmp")
    assert entry["merged"] is False and entry["merge_commit"] is None


def test_lookup_unreadable_forge_is_unknown(bitbucket, monkeypatch) -> None:
    monkeypatch.setattr(forge, "run_twg", FakeTwg({}))
    monkeypatch.setattr(forge, "slug_from_checkout", lambda cwd: SLUG)
    assert forge.lookup("b", "/tmp")["known"] is False


def test_github_lookup_never_touches_twg(mm_home, monkeypatch) -> None:
    monkeypatch.setattr(forge, "run_twg", lambda *a, **k: pytest.fail("twg called on GitHub"))
    monkeypatch.setattr(watch, "_run_gh", lambda args, cwd=None: (0, "[]"))
    assert watch._lookup_pr("b", "/tmp")["known"] is True


def test_checks_combine_head_statuses_and_blocking_merge_checks(bitbucket, monkeypatch) -> None:
    fake = FakeTwg(
        {
            ("pull-requests", "get"): fixture("get_237_statuses.json"),
            ("pull-requests", "mergeability"): fixture("mergeability_237.json"),
        }
    )
    monkeypatch.setattr(forge, "run_twg", fake)
    rows = stack_merge.layer_checks(237, cwd="/tmp", slug=SLUG)
    assert [r["bucket"] for r in rows] == ["pass", "pass", "pass", "fail", "fail", "fail"]
    monkeypatch.setattr(forge, "run_twg", FakeTwg({}))
    with pytest.raises(stack_merge.StackError, match="build statuses on PR #237"):
        stack_merge.layer_checks(237, cwd="/tmp", slug=SLUG)


def test_open_pr_creates_with_a_description_file(bitbucket, monkeypatch) -> None:
    created = fixture("get_237_statuses.json")
    bodies = []

    def fake(args, *, slug, cwd=None):
        if args[:2] == ["pull-requests", "query"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(created | {"data": []}), "")
        bodies.append(Path(args[args.index("--description-file") + 1]).read_text())
        fake.args = list(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(created), "")

    monkeypatch.setattr(forge, "run_twg", fake)
    out = forge.open_pr(
        branch="feat/x", base="main", title="T", body="the body", slug=SLUG, cwd="/tmp"
    )
    assert out == {
        "exists": True,
        "created": True,
        "url": f"https://bitbucket.org/{SLUG}/pull-requests/237",
        "note": "pushed; PR opened",
    }
    assert bodies == ["the body"]
    path = fake.args[fake.args.index("--description-file") + 1]
    assert not Path(path).exists()
    assert fake.args[:6] == ["pull-requests", "create", "--title", "T", "--source", "feat/x"]
    assert fake.args[-2:] == ["--dest", "main"]


def test_open_pr_reuses_an_open_one(bitbucket, monkeypatch) -> None:
    fake = FakeTwg({("pull-requests", "query"): fixture("query_open.json")})
    monkeypatch.setattr(forge, "run_twg", fake)
    out = forge.open_pr(branch="b", base=None, title="T", body="", slug=SLUG, cwd="/tmp")
    assert out["exists"] and not out["created"]
    assert all(call[0][1] != "create" for call in fake.calls)


def test_open_pr_failure_relays_the_summary(bitbucket, monkeypatch) -> None:
    fake = FakeTwg({("pull-requests", "query"): (77, AUTH_ERROR)})
    monkeypatch.setattr(forge, "run_twg", fake)
    out = forge.open_pr(branch="b", base=None, title="T", body="", slug=SLUG, cwd="/tmp")
    assert not out["exists"]
    assert "twg setup bitbucket" in out["note"]


def test_retarget_and_merge_argv(bitbucket, monkeypatch) -> None:
    pr = data("get_244_statuses.json")
    fake = FakeTwg(
        {
            ("pull-requests", "update"): fixture("get_237_statuses.json"),
            ("pull-requests", "merge"): {
                "apiVersion": "v2",
                "command": "bitbucket pull-requests merge",
                "data": {"task_status": "SUCCESS", "merge_result": pr},
                "meta": {},
            },
        }
    )
    monkeypatch.setattr(forge, "run_twg", fake)
    assert forge.retarget(237, "main", cwd="/tmp", slug=SLUG) is None
    assert forge.merge(244, cwd="/tmp", slug=SLUG) is None
    assert fake.calls == [
        (["pull-requests", "update", "--pull-request", "237", "--dest", "main"], SLUG),
        (
            [
                "pull-requests",
                "merge",
                "--pull-request",
                "244",
                "--merge-strategy",
                "merge_commit",
                "--wait",
            ],
            SLUG,
        ),
    ]
    monkeypatch.setattr(forge, "run_twg", FakeTwg({}))
    assert "twg setup bitbucket" in forge.merge(244, cwd="/tmp", slug=SLUG)


def test_bitbucket_has_no_stacks(bitbucket, monkeypatch) -> None:
    monkeypatch.setattr(gh_stack, "_gh", lambda: pytest.fail("gh stack called on Bitbucket"))
    assert gh_stack.available() is False
    cfg = load_config()
    cfg.authority.merge = True
    save_config(cfg)
    seen = {}

    def layer_by_layer(identifier, result, **kwargs):
        seen["mode"] = result.mode
        return result

    monkeypatch.setattr(stack_merge, "_merge_layer_by_layer", layer_by_layer)
    result = stack_merge.merge_stack(5)
    assert result.notes == ["Bitbucket has no native stacks, so this stack merges layer by layer"]
    assert result.mode == "layer-by-layer" == seen["mode"]


# --------------------------------------------------------------------------- #
# The skill's script
# --------------------------------------------------------------------------- #


def _script():
    spec = importlib.util.spec_from_file_location("twg_pr_state", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_verdict_on_an_open_pr() -> None:
    verdict = _script().verdict(data("get_237_statuses.json"), data("mergeability_237.json"))
    assert verdict == {
        "pr": 237,
        "state": "OPEN",
        "head": "49f3b57d59e8",
        "checks": "pass",
        "failing": [],
        "pending": [],
        "blocking": [
            "current_user_permission_check: FAILED",
            "minimum_approvals: FAILED",
            "minimum_default_reviewer_approvals: FAILED",
        ],
        "mergeable": False,
    }


def test_script_verdict_on_a_merged_pr() -> None:
    verdict = _script().verdict(data("get_244_statuses.json"), data("mergeability_244.json"))
    assert (verdict["state"], verdict["checks"], verdict["mergeable"]) == ("MERGED", "pass", False)


def test_script_and_forge_agree_on_head_statuses() -> None:
    raw = copy.deepcopy(data("get_237_statuses.json"))
    old = copy.deepcopy(raw["_statuses"][0])
    old["state"], old["commit"]["hash"] = "FAILED", "1" * 40
    raw["_statuses"].append(old)
    verdict = _script().verdict(raw, [])
    buckets = {row["bucket"] for row in forge.head_status_rows(raw)}
    assert verdict["checks"] == "pass" and buckets == {"pass"}
    assert verdict["mergeable"] is True


def test_script_is_stdlib_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "dependencies = []" in text
    assert "middle_manager" not in text.split('"""', 2)[2]


def test_fixtures_are_labelled_real() -> None:
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    assert "atlassian/forge-bitbucket-related-prs" in readme and "2026-11-17" in readme


def test_config_module_names_both_forges() -> None:
    assert config.FORGES == forge.FORGES
    assert os.path.basename(SCRIPT) == "twg_pr_state.py"
