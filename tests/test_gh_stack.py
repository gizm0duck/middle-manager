"""A stack of pull requests is a GitHub stack from delivery on, and merges in one go.

On 2026-09-23 the manager merged three stacks (seven, four and three layers) one
layer at a time — an update-branch and a full CI run per layer — while GitHub can
merge a linked stack atomically with ``gh stack merge``. The same day ``gh stack
link`` without ``--base`` retargeted a bottom pull request onto the stale branch in
a base clone's ``origin/HEAD``, a stacked pull request refused to be retargeted,
and ``mm stack merge`` refused two repositories with no branch protection because
``gh pr checks --required`` reports "no required checks" instead of an empty list.

Every forge call here goes to a fake ``gh``: ``FakeGh.forge`` stands in for plain
``gh pr ...`` (``stacks._run_forge``) and ``FakeGh.stack`` for ``gh stack ...``
(``gh_stack._run``).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from middle_manager import delivery, gh_stack, repos, stack_merge, stacks
from middle_manager.config import MMConfig, save_config
from middle_manager.state import init_db, store

SLUG = "acme/widgets"
NO_REQUIRED = "no required checks reported on the 'stack/l1' branch\n"


class FakeGh:
    """Pull requests by branch, checks by number, and every call in order."""

    def __init__(self, branches: list[str]) -> None:
        self.prs: dict[str, dict] = {}
        for index, branch in enumerate(branches):
            number = 101 + index
            self.prs[branch] = {
                "number": number,
                "baseRefName": branches[index - 1] if index else "main",
                "state": "OPEN",
                "mergedAt": None,
                "mergeCommit": None,
                "url": f"https://github.com/{SLUG}/pull/{number}",
                "title": branch,
                "isDraft": False,
                "headRefOid": f"{number:040x}",
                "mergeStateStatus": "CLEAN",
            }
        self.required: dict[int, subprocess.CompletedProcess | list] = {}
        self.rollup: dict[int, list] = {}
        self.calls: list[list[str]] = []
        self.stack_env: list[str | None] = []
        self.edit_refusals = 0
        self.link_output = "Created stack #7 with 4 PRs"
        #: PR numbers whose branch no longer contains the layer below (compare: behind).
        self.behind: set[int] = set()
        self.merge_refusal: str | None = None

    def _by_number(self, number: int) -> dict:
        return next(pr for pr in self.prs.values() if pr["number"] == number)

    def forge(self, argv, *, cwd=None):
        self.calls.append(argv[1:])
        verb = argv[1:3]
        if argv[1] == "api":
            upper_head = argv[2].rsplit("...", 1)[1]
            upper = next(pr for pr in self.prs.values() if pr["headRefOid"] == upper_head)
            behind_by = 3 if upper["number"] in self.behind else 0
            return subprocess.CompletedProcess(argv, 0, json.dumps({"behind_by": behind_by}), "")
        if verb == ["pr", "update-branch"]:
            self.behind.discard(int(argv[3]))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == ["pr", "view"] and "statusCheckRollup" in argv:
            rows = self.rollup.get(int(argv[3]), [])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"statusCheckRollup": rows}), "")
        if verb == ["pr", "view"]:
            pr = self.prs.get(argv[3])
            if pr is None:
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
            return subprocess.CompletedProcess(argv, 0, json.dumps(pr), "")
        if verb == ["pr", "checks"]:
            answer = self.required.get(int(argv[3]), [{"name": "Unit", "bucket": "pass"}])
            if isinstance(answer, subprocess.CompletedProcess):
                return answer
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        if verb == ["pr", "edit"]:
            if self.edit_refusals:
                self.edit_refusals -= 1
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    "GraphQL: Cannot change the base branch because the pull request is part "
                    "of a stack (updatePullRequest)",
                )
            self._by_number(int(argv[3]))["baseRefName"] = argv[5]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == ["pr", "merge"]:
            self._merge(self._by_number(int(argv[3])))
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    def _merge(self, pr: dict) -> None:
        pr.update(
            state="MERGED",
            mergedAt="2026-09-23T18:00:00Z",
            mergeCommit={"oid": f"{pr['number']:x}".rjust(40, "a")},
        )

    def stack(self, argv, *, cwd=None, slug=None):
        verb = argv[2]
        if verb == "--help":  # the availability probe, not a stack operation
            return subprocess.CompletedProcess(argv, 0, "Usage: gh stack", "")
        self.calls.append(argv[1:])
        self.stack_env.append(slug)
        if verb == "link":
            return subprocess.CompletedProcess(argv, 0, self.link_output, "")
        if verb == "unstack":
            return subprocess.CompletedProcess(argv, 0, "Stack removed on GitHub", "")
        if verb == "merge":
            if self.merge_refusal:
                return subprocess.CompletedProcess(argv, 1, "", self.merge_refusal)
            top = int(argv[3])
            for pr in self.prs.values():
                if pr["state"] == "OPEN" and pr["number"] <= top:
                    self._merge(pr)
            return subprocess.CompletedProcess(argv, 0, f"Merged stack up to #{top}", "")
        raise AssertionError(argv)

    def stack_calls(self, verb: str) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["stack", verb]]


def _install(monkeypatch, fake: FakeGh, *, with_stack: bool = True) -> None:
    monkeypatch.setattr(stacks, "_pr_tool", lambda: "gh")
    monkeypatch.setattr(stacks, "_run_forge", fake.forge)
    if with_stack:
        monkeypatch.setattr(gh_stack, "_gh", lambda: "gh")
        monkeypatch.setattr(gh_stack, "_run", fake.stack)


def _stack(source_repo, layers: int) -> tuple[list[int], list[str]]:
    """A registered repo on a GitHub forge and ``layers`` tasks stacked on each other."""
    cfg = MMConfig()
    cfg.authority.merge = True
    save_config(cfg)
    added = repos.add_repo(source_repo)
    conn = init_db()
    store.update_repo_fields(conn, added.name, forge_url=f"https://github.com/{SLUG}")
    repo = store.get_repo(conn, added.name)
    run_id = store.create_run(conn, "stack")
    ids: list[int] = []
    branches: list[str] = []
    for index in range(layers):
        branch = f"stack/l{index + 1}"
        task_id = store.add_task(
            conn, run_id=run_id, title=f"layer {index + 1}", repo_id=repo["id"]
        )
        fields = {"branch": branch, "worktree_path": source_repo, "base_sha": "1" * 40}
        if ids:
            fields.update(stacked_on=branches[-1], stacked_on_task=ids[-1])
        store.update_task_fields(conn, task_id, **fields)
        ids.append(task_id)
        branches.append(branch)
    return ids, branches


def _events(kind: str, task_id: int) -> list[dict]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE kind = ? AND task_id = ? ORDER BY id", (kind, task_id)
        )
        .fetchall()
    )
    return [json.loads(row["payload"]) for row in rows]


def _no_sleep(_seconds: float) -> None:
    return None


def test_gh_stack_runs_as_the_forge_repo_because_it_has_no_repo_flag(monkeypatch) -> None:
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gh_stack.subprocess, "run", run)
    gh_stack._run(["gh", "stack", "merge", "7", "--yes", "--merge"], cwd="/base/clone", slug=SLUG)
    assert seen["env"]["GH_REPO"] == SLUG
    assert seen["cwd"] == "/base/clone"


# --------------------------------------------------------------------------- #
# The atomic merge
# --------------------------------------------------------------------------- #


def test_a_green_four_layer_stack_merges_with_one_gh_stack_merge_and_records_every_layer(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 4)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[1], sleep=_no_sleep)

    assert result.stopped is None, result.stopped
    assert result.mode == "atomic"
    assert len(fake.stack_calls("merge")) == 1
    assert fake.stack_calls("merge")[0] == ["stack", "merge", "104", "--yes", "--merge"]
    assert not [call for call in fake.calls if call[:2] == ["pr", "merge"]], "no per-layer merge"
    assert not [call for call in fake.calls if call[:2] == ["pr", "edit"]], "no retargeting"
    # The chain was linked bottom-up by URL, on the forge's default branch, as the forge's repo.
    link = fake.stack_calls("link")[0]
    assert link[2:4] == ["--base", "main"]
    assert link[4:] == [fake.prs[branch]["url"] for branch in branches]
    assert fake.stack_env == [SLUG, SLUG], "link and merge run as the forge's repo (GH_REPO)"
    conn = init_db()
    for task_id, branch in zip(ids, branches, strict=True):
        merged_sha = fake.prs[branch]["mergeCommit"]["oid"]
        assert store.get_task(conn, task_id)["merged_sha"] == merged_sha
    assert [item["task_id"] for item in result.merged] == ids


def test_one_red_layer_stops_the_merge_before_anything_merges_and_names_it(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    fake.required[102] = [
        {"name": "Unit", "bucket": "pass"},
        {"name": "Integration", "bucket": "fail"},
    ]
    fake.required[103] = [{"name": "Lint", "bucket": "pending"}]
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)

    assert result.merged == []
    assert fake.stack_calls("merge") == []
    assert not [call for call in fake.calls if call[:2] == ["pr", "merge"]]
    assert f"task {ids[1]} PR #102 has red check(s): Integration" in result.stopped
    assert f"task {ids[2]} PR #103 has check(s) still running: Lint" in result.stopped
    assert "nothing was merged" in result.stopped
    assert all(not store.get_task(init_db(), task_id)["merged_sha"] for task_id in ids)


def test_a_repo_with_no_required_checks_merges_on_a_green_rollup(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    for number in (101, 102):
        fake.required[number] = subprocess.CompletedProcess([], 1, "", NO_REQUIRED)
        fake.rollup[number] = [
            {
                "__typename": "CheckRun",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "CheckRun",
                "name": "docs",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
            },
            {"__typename": "StatusContext", "context": "vercel", "state": "SUCCESS"},
        ]
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)

    assert result.stopped is None, result.stopped
    assert len(fake.stack_calls("merge")) == 1
    assert [call for call in fake.calls if "statusCheckRollup" in call], "the rollup was read"
    assert [item["task_id"] for item in result.merged] == ids


def test_a_repo_with_no_required_checks_still_refuses_a_failing_or_running_rollup(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    for number in (101, 102):
        fake.required[number] = subprocess.CompletedProcess([], 1, "", NO_REQUIRED)
    fake.rollup[101] = [{"name": "build", "status": "IN_PROGRESS", "conclusion": None}]
    fake.rollup[102] = [{"context": "ci/legacy", "state": "FAILURE"}]
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)

    assert fake.stack_calls("merge") == []
    assert "check(s) still running: build" in result.stopped
    assert "red check(s): ci/legacy" in result.stopped


def test_a_run_of_unrelated_layers_is_refused_before_anything_is_linked(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    conn = init_db()
    # Two bottom layers in one run are two stacks, not one. `mm stack` reads an id as
    # a task first, so the run needs an id no task has.
    store.update_task_fields(conn, ids[1], stacked_on=None, stacked_on_task=None)
    run_id = store.create_run(conn, "unrelated")
    while store.get_task(conn, run_id) is not None:
        run_id = store.create_run(conn, "unrelated")
    conn.execute("UPDATE tasks SET run_id = ? WHERE id IN (?, ?)", (run_id, *ids))
    conn.commit()
    fake = FakeGh(branches)
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(run_id, sleep=_no_sleep)

    assert "not one chain" in result.stopped
    assert fake.stack_calls("link") == [] and fake.stack_calls("merge") == []


def test_without_gh_stack_the_merge_falls_back_to_layer_by_layer_and_says_so_once(
    mm_home, source_repo, monkeypatch, capsys
) -> None:
    from middle_manager.cli import main

    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    _install(monkeypatch, fake, with_stack=False)  # conftest leaves gh_stack without gh

    assert main(["stack", "merge", str(ids[0])]) == 0
    out = capsys.readouterr().out
    fallback = [line for line in out.splitlines() if "gh stack` is not installed" in line]
    assert len(fallback) == 1
    assert gh_stack.INSTALL_HINT in fallback[0]
    assert [call[:3] for call in fake.calls if call[:2] == ["pr", "merge"]] == [
        ["pr", "merge", "101"]
    ]
    assert f"task {ids[0]}: PR #101 merged" in out
    assert f"task {ids[1]}: PR #102 retargeted to main" in out


def test_layer_by_layer_on_request_never_touches_gh_stack_merge(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], layer_by_layer=True, all_layers=True, sleep=_no_sleep)

    assert result.stopped is None, result.stopped
    assert result.mode == "layer-by-layer"
    assert fake.stack_calls("merge") == []
    assert [item["task_id"] for item in result.merged] == ids


def test_zero_checks_is_not_green_unless_the_repo_says_it_has_no_ci(
    mm_home, source_repo, monkeypatch
) -> None:
    """CI that has not registered its runs yet looks exactly like no CI at all."""
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    for number in (101, 102):
        fake.required[number] = subprocess.CompletedProcess([], 1, "", NO_REQUIRED)
    fake.rollup[101] = [{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    _install(monkeypatch, fake)  # PR #102 has no checks at all

    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)

    assert fake.stack_calls("merge") == []
    assert f"task {ids[1]} PR #102 has no checks at all yet" in result.stopped
    assert "mm repo set source --no-ci" in result.stopped
    assert f"task {ids[0]} PR #101" not in result.stopped, "the layer with a green check is fine"

    repos.set_settings("source", no_ci="yes")
    assert "CI: none" in repos.get_settings("source").describe()
    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)
    assert result.stopped is None, result.stopped
    assert [item["task_id"] for item in result.merged] == ids


def test_layers_out_of_order_are_updated_bottom_up_and_nothing_merges(
    mm_home, source_repo, monkeypatch, capsys
) -> None:
    """2026-09-23: the bottom layer took main in, so no layer above descended from it."""
    from middle_manager.cli import main

    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    fake.behind = {102}  # layer 2 lacks the merge of main that landed on layer 1
    _install(monkeypatch, fake)

    assert main(["stack", "merge", str(ids[0])]) == 1
    captured = capsys.readouterr()

    updates = [call[:3] for call in fake.calls if call[:2] == ["pr", "update-branch"]]
    assert updates == [["pr", "update-branch", "102"], ["pr", "update-branch", "103"]]
    assert fake.stack_calls("link") == [] and fake.stack_calls("merge") == []
    assert not [call for call in fake.calls if "push" in call], "never a force-push"
    assert f"task {ids[1]}: PR #102 updated onto the layer below it; CI re-running" in (
        captured.out
    )
    assert "not a linear descendant" in captured.err
    assert f"run `mm stack merge {ids[0]}` again when they are green" in captured.err
    assert all(not store.get_task(init_db(), task_id)["merged_sha"] for task_id in ids)


def test_a_behind_merge_state_is_read_as_out_of_order_too(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    fake.prs[branches[1]]["mergeStateStatus"] = "BEHIND"
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)

    assert [item["pr"] for item in result.repaired] == [102]
    assert fake.stack_calls("merge") == []


def test_gh_s_not_a_linear_descendant_refusal_is_repaired_from_the_layer_it_names(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    fake.merge_refusal = (
        "Stack needs to be rebased: PR #103's branch is not a linear descendant of PR #102's branch"
    )
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], sleep=_no_sleep)

    updates = [call[:3] for call in fake.calls if call[:2] == ["pr", "update-branch"]]
    assert updates == [["pr", "update-branch", "103"]]
    assert "Stack needs to be rebased" in result.stopped
    assert f"run `mm stack merge {ids[0]}` again when they are green" in result.stopped
    assert result.merged == []


def test_merging_names_a_layer_merges_up_to_it_when_the_layers_above_have_no_pr(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    del fake.prs[branches[2]]  # the top layer has not been delivered yet
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[1], sleep=_no_sleep)

    assert result.stopped is None, result.stopped
    assert fake.stack_calls("link")[0][4:] == [fake.prs[b]["url"] for b in branches[:2]]
    assert fake.stack_calls("merge") == [["stack", "merge", "102", "--yes", "--merge"]]
    assert [item["task_id"] for item in result.merged] == ids[:2]
    assert any(f"merging up to task {ids[1]}" in note for note in result.notes)
    assert not store.get_task(init_db(), ids[2])["merged_sha"]


def test_merging_without_merge_authority_raises_and_touches_nothing(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    cfg = MMConfig()
    cfg.authority.merge = False
    save_config(cfg)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)

    for layer_by_layer in (False, True):
        with pytest.raises(stacks.StackError) as refused:
            stack_merge.merge_stack(ids[0], layer_by_layer=layer_by_layer, sleep=_no_sleep)
        assert "merge authority is off" in str(refused.value)
    assert fake.stack_calls("merge") == [] and fake.stack_calls("link") == []
    assert not [call for call in fake.calls if call[:2] == ["pr", "merge"]]


def test_the_merge_is_confirmed_on_the_forge_for_up_to_a_minute(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)
    real_stack = fake.stack

    def merge_that_never_shows(argv, *, cwd=None, slug=None):
        if argv[2] == "merge":
            fake.calls.append(argv[1:])
            return subprocess.CompletedProcess(argv, 0, "merge queued", "")
        return real_stack(argv, cwd=cwd, slug=slug)

    monkeypatch.setattr(gh_stack, "_run", merge_that_never_shows)
    slept: list[float] = []

    result = stack_merge.merge_stack(ids[0], sleep=slept.append)

    assert 59 <= sum(slept) <= 60
    assert "has not returned a merge commit" in result.stopped


# --------------------------------------------------------------------------- #
# Retargeting a pull request that sits in a GitHub stack
# --------------------------------------------------------------------------- #


def test_after_an_unstack_the_chain_is_relinked_up_to_the_last_layer_with_an_open_pr(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 4)
    fake = FakeGh(branches)
    del fake.prs[branches[3]]  # the top layer has no pull request yet
    fake.edit_refusals = 1
    _install(monkeypatch, fake)

    result = stack_merge.merge_stack(ids[0], layer_by_layer=True, sleep=_no_sleep)

    assert result.stopped is None, result.stopped
    relink = fake.stack_calls("link")
    assert len(relink) == 1
    assert relink[0][4:] == [fake.prs[b]["url"] for b in branches[1:3]], "not dissolved"
    relinked = result.retargeted[0]["relinked"]
    assert relinked["linked"] is True
    assert f"task {ids[3]} has no open pull request" in relinked["stopped_at"]


def test_retargeting_a_stacked_pr_unstacks_retargets_and_links_again(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)
    conn = init_db()
    # Delivery linked the three layers earlier and GitHub called it stack #7.
    linked = gh_stack.link_chain(conn, store.get_task(conn, ids[2]))
    assert linked["linked"] and linked["stack"] == 7
    fake.calls.clear()
    fake.edit_refusals = 1  # GitHub refuses the first retarget: the PR is in a stack

    result = stack_merge.merge_stack(ids[0], layer_by_layer=True, sleep=_no_sleep)

    assert result.stopped is None, result.stopped
    sequence = [call[:3] for call in fake.calls if call[0] == "stack" or call[:2] == ["pr", "edit"]]
    assert sequence == [
        ["pr", "edit", "102"],
        ["stack", "unstack", "7"],
        ["pr", "edit", "102"],
        ["stack", "link", "--base"],
    ]
    relink = fake.stack_calls("link")[0]
    assert relink[4:] == [fake.prs[branch]["url"] for branch in branches[1:]], "the rest, re-linked"
    assert fake.prs[branches[1]]["baseRefName"] == "main"
    assert result.retargeted[0]["unstacked"] is True


# --------------------------------------------------------------------------- #
# Delivery links the stack
# --------------------------------------------------------------------------- #


def _deliverable(monkeypatch, fake: FakeGh, *, create_fails_as_existing: bool = False) -> list:
    calls: list[list[str]] = []

    def run(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "create"] and create_fails_as_existing:
            branch = argv[argv.index("--head") + 1]
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                f"a pull request for branch already exists:\n{fake.prs[branch]['url']}\n",
            )
        return subprocess.CompletedProcess(argv, 0, "https://github.com/acme/widgets/pull/1\n", "")

    monkeypatch.setattr(delivery, "_run", run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")
    monkeypatch.setattr(stacks, "sync_worktree_with_remote", lambda task_id: {"action": "none"})
    monkeypatch.setattr(delivery, "composition_tasks", lambda *a, **kw: [])
    return calls


def test_delivering_a_stacked_layer_links_the_chain_below_it_with_the_forge_default_base(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)
    _deliverable(monkeypatch, fake)

    result = delivery.deliver(ids[1])

    link = fake.stack_calls("link")
    assert len(link) == 1
    assert link[0][2:] == ["--base", "main"] + [fake.prs[b]["url"] for b in branches[:2]]
    assert "linked #101, #102 as a GitHub stack on main" in result.note
    assert _events("stack_linked", ids[1])[0]["stack"] == 7


def test_a_failed_link_is_printed_and_recorded_and_the_delivery_still_succeeds(
    mm_home, source_repo, monkeypatch, capsys
) -> None:
    from middle_manager.cli import main

    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)
    _deliverable(monkeypatch, fake)

    def refuse_link(argv, *, cwd=None, slug=None):
        if argv[2] == "link":
            fake.calls.append(argv[1:])
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 422: stacks are not enabled")
        return fake.stack(argv, cwd=cwd, slug=slug)

    monkeypatch.setattr(gh_stack, "_run", refuse_link)

    assert main(["deliver", str(ids[1])]) == 0
    out = capsys.readouterr().out
    assert "not linked as a GitHub stack: HTTP 422: stacks are not enabled" in out
    assert store.get_task(init_db(), ids[1])["status"] == "delivered"
    failed = _events("stack_link_failed", ids[1])
    assert len(failed) == 1 and "stacks are not enabled" in failed[0]["detail"]


def test_an_explicit_base_retargets_an_existing_stacked_pr_through_unstack_and_relink(
    mm_home, source_repo, monkeypatch
) -> None:
    """The layer below merged outside mm; the manager points layer 2 at main."""
    ids, branches = _stack(source_repo, 3)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)
    _deliverable(monkeypatch, fake, create_fails_as_existing=True)
    conn = init_db()
    store.update_task_fields(
        conn, ids[0], merged_sha="b" * 40, merged_at="2026-09-23T00:00:00+00:00"
    )
    fake.prs[branches[0]].update(state="MERGED", mergedAt="2026-09-23T00:00:00Z")
    fake.edit_refusals = 1

    result = delivery.deliver(ids[1], base="main")

    sequence = [call[:3] for call in fake.calls if call[0] == "stack" or call[:2] == ["pr", "edit"]]
    assert sequence == [
        ["pr", "edit", "102"],
        ["stack", "unstack", "102"],  # no link recorded a stack number: unstack by the PR
        ["pr", "edit", "102"],
        ["stack", "link", "--base"],
    ]
    assert fake.stack_calls("link")[0][4:] == [fake.prs[b]["url"] for b in branches[1:]]
    assert "PR #102 retargeted from stack/l1 to main (unstacked from its GitHub stack first)" in (
        result.note
    )


# --------------------------------------------------------------------------- #
# The default branch comes from the forge
# --------------------------------------------------------------------------- #


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_sync_corrects_a_stale_origin_head_and_doctor_flags_it_first(mm_home, source_repo) -> None:
    from middle_manager.setup.doctor import _repo_forges, render_text

    _git(source_repo, "branch", "feat/repo-picker")
    added = repos.add_repo(source_repo)
    clone = added.local_path
    assert added.default_branch == "main"
    assert repos.remote_head(clone, "origin") == "main"
    # The 2026-09-23 shape: the base clone's origin/HEAD names a feature branch.
    _git(clone, "remote", "set-head", "origin", "feat/repo-picker")

    found = _repo_forges()
    assert found[0]["stale_heads"] == {"origin": "feat/repo-picker"}
    rendered = render_text(
        {
            "mm_home": "/tmp/.mm",
            "config": {"path": "/tmp/.mm/config.toml", "present": False},
            "state_db": {"present": False},
            "environment": {"harnesses": [], "requirements": [], "companions": []},
            "usable_harnesses": [],
            "capability_drift": [],
            "repos": found,
        }
    )
    assert "origin/HEAD names feat/repo-picker" in rendered
    assert f"mm repo sync {added.name}" in rendered

    result = repos.sync_repo(added.name)
    assert repos.remote_head(clone, "origin") == "main"
    assert any("origin/HEAD named feat/repo-picker" in note for note in result.notes)
    assert _repo_forges()[0]["stale_heads"] == {}


def test_sync_records_the_forge_default_branch_over_a_stale_record(mm_home, source_repo) -> None:
    _git(source_repo, "checkout", "-q", "-b", "trunk")
    added = repos.add_repo(source_repo)
    assert added.default_branch == "trunk", "the forge's HEAD names trunk"
    # The forge's default moves back to main; the record and origin/HEAD follow on sync.
    _git(source_repo, "checkout", "-q", "main")

    result = repos.sync_repo(added.name)

    assert result.default_branch == "main"
    assert repos.list_repos()[0]["default_branch"] == "main"
    assert repos.remote_head(added.local_path, "origin") == "main"
    assert any("the record said trunk" in note for note in result.notes)


def test_link_passes_the_forge_default_even_when_origin_head_is_stale(
    mm_home, source_repo, monkeypatch
) -> None:
    ids, branches = _stack(source_repo, 2)
    fake = FakeGh(branches)
    _install(monkeypatch, fake)
    conn = init_db()
    clone = store.get_repo(conn, "source")["local_path"]
    _git(source_repo, "branch", "feat/repo-picker")
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "remote", "set-head", "origin", "feat/repo-picker")
    monkeypatch.setattr(repos, "forge_default_branch", lambda path, remote: "main")

    gh_stack.link_chain(conn, store.get_task(conn, ids[1]))

    assert fake.stack_calls("link")[0][2:4] == ["--base", "main"]


@pytest.mark.strict_forge
def test_repo_add_prints_what_it_could_not_do_about_the_default_branch(
    tmp_path, mm_home, capsys
) -> None:
    """The fixture keeps GitHub offline, so the forge's default cannot be read here."""
    from conftest import make_git_repo
    from middle_manager.cli import main

    source = make_git_repo(tmp_path / "widgets")
    assert main(["repo", "add", source, "--forge-url", f"https://github.com/{SLUG}"]) == 0
    assert "could not read the forge's default branch" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("ref: refs/heads/main\tHEAD\n0123abcd\tHEAD\n", "main"),
        ("ref: refs/heads/release/2026\tHEAD\n", "release/2026"),
        ("0123abcd\tHEAD\n", None),
    ],
)
def test_the_forge_default_branch_is_read_from_ls_remote_symref(
    monkeypatch, stdout, expected
) -> None:
    asked: list[list[str]] = []

    def run(argv, **kwargs):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(repos, "remote_url", lambda path, remote="origin": "/a/local/forge")
    monkeypatch.setattr(repos.subprocess, "run", run)
    assert repos.forge_default_branch("/nowhere", "origin") == expected
    assert asked == [["git", "ls-remote", "--symref", "origin", "HEAD"]]
