"""`mm stack merge`: land a stack of pull requests, atomically by default.

The default is GitHub's native stack merge (see :mod:`middle_manager.gh_stack`):
every unmerged layer must have an open pull request whose checks are green, the
chain is linked as a GitHub stack on the forge's default branch, and one ``gh stack
merge`` lands all of it in a single all-or-nothing operation and one CI pass. Each
layer's merge is then confirmed on the forge and recorded, exactly as ``mm deliver
--merged`` records it (which also tears down the task's compose stack).

Layer by layer — merge the lowest layer, retarget the next, repeat — is what
``mm stack merge`` did until 2026-09-23. On a repository that requires up-to-date
branches it costs an update-branch and a full CI run per layer, so it is now only
the explicit ``--layer-by-layer`` fallback, and the automatic one when ``gh stack``
is not installed.

"Green" means the pull request's required checks, or — on a repository with no
branch protection, where ``gh pr checks --required`` reports that there are none
rather than an empty list — every check in the pull request's status rollup:
all completed, none failing.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from middle_manager import gh_stack, stacks
from middle_manager.stacks import StackError
from middle_manager.state import init_db, store


@dataclass
class StackMergeResult:
    identifier: int
    merged: list[dict] = field(default_factory=list)
    retargeted: list[dict] = field(default_factory=list)
    stopped: str | None = None
    #: "atomic" (one `gh stack merge`) or "layer-by-layer".
    mode: str = "atomic"
    notes: list[str] = field(default_factory=list)
    #: Layers brought back into order with `gh pr update-branch` (nothing merged then).
    repaired: list[dict] = field(default_factory=list)


def _require_merge_authority() -> None:
    from middle_manager.config import ConfigError, load_config

    try:
        allowed = load_config().authority.merge
    except ConfigError as exc:
        raise StackError(f"cannot merge without a valid config: {exc}") from exc
    if not allowed:
        raise StackError(
            "merge authority is off; a human must enable it with "
            "`mm config authority --allow-merge` before running `mm stack merge`"
        )


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

#: What `gh pr checks` says instead of printing JSON when there is nothing to list:
#: "no required checks reported on the '<branch>' branch" (exit 1, empty stdout).
_NO_CHECKS = re.compile(r"no (?:required )?checks reported", re.IGNORECASE)
_ROLLUP_PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_STATUS_PENDING = {"PENDING", "EXPECTED"}


def _repo_flag(slug: str | None) -> list[str]:
    return ["--repo", slug] if slug else []


def required_checks(pr_number: int, *, cwd: str | None, slug: str | None = None) -> list[dict]:
    """Required check rows for a PR; ``[]`` when the repository requires none.

    An unreadable forge is a refusal, not green.
    """
    tool = stacks._pr_tool()
    if tool is None:
        raise StackError("cannot read required checks because plain `gh` is unavailable")
    argv = [tool, "pr", "checks", str(pr_number), "--required", "--json", "name,bucket"]
    proc = stacks._run_forge(argv + _repo_flag(slug), cwd=cwd)
    if proc.returncode != 0 and _NO_CHECKS.search(f"{proc.stderr or ''}\n{proc.stdout or ''}"):
        return []
    try:
        rows = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise StackError(f"could not read required checks for PR #{pr_number}") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StackError(f"could not read required checks for PR #{pr_number}")
    return rows


def rollup_checks(pr_number: int, *, cwd: str | None, slug: str | None = None) -> list[dict]:
    """Every check on a PR's head, as ``name``/``bucket`` rows like `gh pr checks` gives."""
    tool = stacks._pr_tool()
    if tool is None:
        raise StackError("cannot read checks because plain `gh` is unavailable")
    argv = [tool, "pr", "view", str(pr_number), "--json", "statusCheckRollup"]
    proc = stacks._run_forge(argv + _repo_flag(slug), cwd=cwd)
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise StackError(f"could not read the checks on PR #{pr_number}") from exc
    items = data.get("statusCheckRollup") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict) and proc.returncode == 0:
        items = []
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise StackError(f"could not read the checks on PR #{pr_number}")
    rows = []
    for item in items:
        name = item.get("name") or item.get("context") or "a check"
        if "status" in item or "conclusion" in item:  # a check run
            if str(item.get("status") or "").upper() != "COMPLETED":
                bucket = "pending"
            elif str(item.get("conclusion") or "").upper() in _ROLLUP_PASS:
                bucket = "pass"
            else:
                bucket = "fail"
        else:  # a commit status
            state = str(item.get("state") or "").upper()
            bucket = (
                "pass" if state == "SUCCESS" else "pending" if state in _STATUS_PENDING else "fail"
            )
        rows.append({"name": str(name), "bucket": bucket})
    return rows


def layer_checks(pr_number: int, *, cwd: str | None, slug: str | None = None) -> list[dict]:
    """The checks that decide whether a layer may merge: required, else the whole rollup."""
    rows = required_checks(pr_number, cwd=cwd, slug=slug)
    return rows or rollup_checks(pr_number, cwd=cwd, slug=slug)


def _not_green(checks: list[dict]) -> tuple[list[str], list[str]]:
    """Names of the red checks and of the ones still running."""
    red, running = [], []
    for check in checks:
        bucket = check.get("bucket")
        if not isinstance(bucket, str):
            continue
        name = check.get("name")
        name = name if isinstance(name, str) and name else "a check"
        if bucket.lower() in {"fail", "cancel"}:
            red.append(name)
        elif bucket.lower() == "pending":
            running.append(name)
    return sorted(red), sorted(running)


def _repo_facts(conn, task) -> tuple[bool, str]:
    """Whether the task's repo declared it has no CI, and the repo's name."""
    from middle_manager import repos

    if not task["repo_id"]:
        return False, "<repo>"
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    if row is None:
        return False, "<repo>"
    return repos.has_no_ci(row), row["name"]


def _blocked(layer, checks: list[dict], *, no_ci: bool, repo: str) -> str | None:
    """Why a layer may not merge yet, or None when its checks are green.

    No checks at all is not green: on a repository with CI it means CI has not
    registered its runs yet, and merging then lands untested work. Only a repository
    declared to have no CI (`mm repo set <repo> --no-ci`) merges with none.
    """
    if not checks and not no_ci:
        return (
            f"task {layer.task_id} PR #{layer.pr_number} has no checks at all yet (CI may not "
            f"have registered them); wait for them, or if {repo} has no CI run "
            f"`mm repo set {repo} --no-ci`"
        )
    red, running = _not_green(checks)
    parts = []
    if red:
        parts.append("red check(s): " + ", ".join(red))
    if running:
        parts.append("check(s) still running: " + ", ".join(running))
    if not parts:
        return None
    return f"task {layer.task_id} PR #{layer.pr_number} has " + "; ".join(parts)


def _merge_sha(pr: dict | None) -> str | None:
    """A strictly typed merge confirmation from the normalized forge payload."""
    if not isinstance(pr, dict) or pr.get("merged") is not True:
        return None
    value = pr.get("merge_commit")
    if not isinstance(value, str):
        commit = pr.get("mergeCommit")
        value = commit.get("oid") if isinstance(commit, dict) else None
    if not isinstance(value, str):
        return None
    from middle_manager.delivery import _SHA_RE

    return value if _SHA_RE.fullmatch(value) else None


def _record(result: StackMergeResult, layer, merged_sha: str, note: str) -> bool:
    from middle_manager.delivery import DeliveryError, record_merged

    try:
        recorded = record_merged(layer.task_id, merged_sha, note=note)
    except DeliveryError as exc:
        result.stopped = str(exc)
        return False
    result.merged.append(
        {
            "task_id": layer.task_id,
            "pr": layer.pr_number,
            "merge_commit": merged_sha,
            "compose": recorded.compose,
        }
    )
    return True


# --------------------------------------------------------------------------- #
# Layers out of order
# --------------------------------------------------------------------------- #

#: What `gh stack merge` says when an upper layer no longer contains the one below:
#: "Stack needs to be rebased: PR #159's branch is not a linear descendant of PR #157's".
_OUT_OF_ORDER = re.compile(r"needs to be rebased|not a linear descendant", re.IGNORECASE)
_NOT_DESCENDANT = re.compile(r"PR #(\d+)'s branch is not a linear descendant", re.IGNORECASE)


def _behind(lower, upper, *, cwd: str | None, slug: str | None) -> bool:
    """Does ``upper`` lack commits ``lower`` has — is it no longer stacked on it?

    GitHub's ``mergeStateStatus`` says BEHIND when the pull request's base (the
    layer below) has moved on without it; the compare API answers the same
    question by commit and does not depend on branch protection. A compare that
    cannot be read claims nothing: ``gh stack merge`` then says so itself.
    """
    if str(upper.pr_merge_state or "").upper() == "BEHIND":
        return True
    tool = stacks._pr_tool()
    if tool is None or not slug or not lower.pr_head or not upper.pr_head:
        return False
    proc = stacks._run_forge(
        [tool, "api", f"repos/{slug}/compare/{lower.pr_head}...{upper.pr_head}"], cwd=cwd
    )
    if proc.returncode != 0:
        return False
    try:
        behind_by = json.loads(proc.stdout).get("behind_by")
    except (AttributeError, TypeError, ValueError):
        return False
    return isinstance(behind_by, int) and not isinstance(behind_by, bool) and behind_by > 0


def _first_out_of_order(pending: list, *, cwd: str | None, slug: str | None) -> int | None:
    for index in range(1, len(pending)):
        if _behind(pending[index - 1], pending[index], cwd=cwd, slug=slug):
            return index
    return None


def _repair_order(
    result: StackMergeResult, identifier: int, pending: list, start: int, why: str, ctx
) -> StackMergeResult:
    """Bring layers ``start``.. back onto the layer below each, bottom-up, and stop.

    ``gh pr update-branch`` merges each layer's base into it — a normal push, never a
    force-push. Every layer above a repaired one is behind it afterwards, so all of
    them are updated. Their CI then re-runs, which the running-checks guard waits
    out on the next ``mm stack merge``.
    """
    tool = stacks._pr_tool()
    updated: list = []
    failure = None
    for layer in pending[start:]:
        if tool is None:
            failure = "plain `gh` is unavailable"
            break
        argv = [tool, "pr", "update-branch", str(layer.pr_number)] + _repo_flag(ctx.slug)
        proc = stacks._run_forge(argv, cwd=ctx.cwd)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "forge refused").strip()[:240]
            failure = f"`gh pr update-branch {layer.pr_number}` failed: {detail}"
            break
        updated.append(layer)
    names = ", ".join(f"task {layer.task_id} (PR #{layer.pr_number})" for layer in updated)
    result.repaired = [{"task_id": layer.task_id, "pr": layer.pr_number} for layer in updated]
    if failure:
        result.stopped = (
            f"the stack's layers are out of order ({why}); updated {names or 'no layer'} "
            f"before {failure}. Nothing was merged"
        )
    else:
        result.stopped = (
            f"the stack's layers are out of order ({why}); updated layers {names} with "
            "`gh pr update-branch` (a merge push, never a force-push). CI is re-running on "
            f"them: run `mm stack merge {identifier}` again when they are green. "
            "Nothing was merged"
        )
    return result


# --------------------------------------------------------------------------- #
# The merge
# --------------------------------------------------------------------------- #


def merge_stack(
    identifier: int,
    *,
    all_layers: bool = False,
    layer_by_layer: bool = False,
    sleep=time.sleep,
    confirm_attempts: int = 120,
) -> StackMergeResult:
    """Merge a stack: atomically with ``gh stack``, or layer by layer when asked or forced.

    A merge is confirmed on the forge every half second, ``confirm_attempts`` times
    (a minute by default): a stack of several layers can take GitHub that long.
    """
    _require_merge_authority()
    result = StackMergeResult(identifier)
    if not layer_by_layer:
        if gh_stack.available():
            return _merge_atomically(
                identifier, result, sleep=sleep, confirm_attempts=confirm_attempts
            )
        result.notes.append(
            "`gh stack` is not installed, so this stack merges layer by layer (one CI run "
            f"per layer); install it with `{gh_stack.INSTALL_HINT}`"
        )
    result.mode = "layer-by-layer"
    return _merge_layer_by_layer(
        identifier, result, all_layers=all_layers, sleep=sleep, confirm_attempts=confirm_attempts
    )


def _merge_atomically(
    identifier: int, result: StackMergeResult, *, sleep, confirm_attempts: int
) -> StackMergeResult:
    layers = stacks.stack_layers(identifier)
    pending = [layer for layer in layers if not layer.recorded_merged]
    if not pending:
        return result
    # Naming a layer whose upper layers have no pull request yet merges up to it.
    named = next((i for i, layer in enumerate(pending) if layer.task_id == identifier), None)
    if named is not None and any(layer.pr_number is None for layer in pending[named + 1 :]):
        pending = pending[: named + 1]
        result.notes.append(
            f"merging up to task {identifier}: the layers above it have no pull request yet"
        )
    for layer in pending:
        if layer.pr_number is None:
            result.stopped = f"task {layer.task_id} has no pull request; deliver that layer first"
            return result
        if str(layer.pr_state or "").upper() != "OPEN":
            result.stopped = (
                f"task {layer.task_id} PR #{layer.pr_number} is {layer.pr_state or 'unknown'}, "
                "not open"
            )
            return result
    if len(pending) == 1:
        # One pull request is no stack; it merges on its own.
        result.mode = "layer-by-layer"
        return _merge_layer_by_layer(
            identifier, result, all_layers=False, sleep=sleep, confirm_attempts=confirm_attempts
        )

    conn = init_db()
    top = store.get_task(conn, pending[-1].task_id)
    if top is None:
        result.stopped = f"task {pending[-1].task_id} disappeared before merge"
        return result
    chain = [
        int(row["id"])
        for row in stacks._chain_for_task(conn, int(top["id"]))
        if not row["merged_at"]
    ]
    if int(top["id"]) in chain:
        chain = chain[: chain.index(int(top["id"])) + 1]
    if chain != [layer.task_id for layer in pending]:
        result.stopped = (
            "the unmerged layers are not one chain ("
            + ", ".join(f"task {layer.task_id}" for layer in pending)
            + "); name a task in the stack, not a run. Nothing was merged"
        )
        return result
    ctx = gh_stack.repo_context(conn, top)

    # A bottom layer that took the default branch in (a repository that requires
    # up-to-date branches) leaves every layer above it off the chain, and GitHub
    # refuses the stack merge. Repair that first: the CI it re-runs is the wait.
    out_of_order = _first_out_of_order(pending, cwd=ctx.cwd, slug=ctx.slug)
    if out_of_order is not None:
        lower, upper = pending[out_of_order - 1], pending[out_of_order]
        why = (
            f"PR #{upper.pr_number}'s branch no longer contains PR #{lower.pr_number}'s, "
            "so it is not a linear descendant of it"
        )
        return _repair_order(result, identifier, pending, out_of_order, why, ctx)

    no_ci, repo_name = _repo_facts(conn, top)
    blocked = []
    for layer in pending:
        try:
            checks = layer_checks(layer.pr_number, cwd=ctx.cwd, slug=ctx.slug)
        except StackError as exc:
            result.stopped = f"task {layer.task_id}: {exc}; nothing was merged"
            return result
        reason = _blocked(layer, checks, no_ci=no_ci, repo=repo_name)
        if reason:
            blocked.append(reason)
    if blocked:
        result.stopped = "nothing was merged: " + "; ".join(blocked)
        return result

    linked = gh_stack.link_chain(conn, top, upto=int(top["id"]), ctx=ctx)
    if not linked["linked"]:
        result.stopped = (
            "could not link the layers as a GitHub stack, so nothing was merged: "
            f"{linked['detail']}"
        )
        return result
    result.notes.append(
        f"linked {len(pending)} pull requests as a GitHub stack on {linked['base']}"
    )
    call = gh_stack.merge(pending[-1].pr_number, cwd=ctx.cwd, slug=ctx.slug)
    if not call.ok:
        if _OUT_OF_ORDER.search(call.detail):
            named_pr = _NOT_DESCENDANT.search(call.detail)
            start = next(
                (
                    i
                    for i, layer in enumerate(pending)
                    if i and named_pr and layer.pr_number == int(named_pr.group(1))
                ),
                1,
            )
            why = call.detail.splitlines()[0].strip()
            return _repair_order(result, identifier, pending, start, why, ctx)
        result.stopped = f"the forge refused the stack merge, so nothing was merged: {call.detail}"
        return result

    confirmed: dict[int, str] = {}
    for attempt in range(confirm_attempts):
        for layer in pending:
            if layer.task_id not in confirmed:
                pr = stacks.pull_request_for(layer.branch or "", cwd=ctx.cwd, slug=ctx.slug)
                merged_sha = _merge_sha(pr)
                if merged_sha:
                    confirmed[layer.task_id] = merged_sha
        if len(confirmed) == len(pending):
            break
        if attempt + 1 < confirm_attempts:
            sleep(0.5)
    tops = pending[-1].pr_number
    for layer in pending:
        if layer.task_id in confirmed and not _record(
            result,
            layer,
            confirmed[layer.task_id],
            f"mm stack merge merged pull request #{layer.pr_number} in one stack merge "
            f"up to #{tops}",
        ):
            return result
    missing = [layer for layer in pending if layer.task_id not in confirmed]
    if missing:
        result.stopped = (
            "`gh stack merge` succeeded but the forge has not returned a merge commit for "
            + ", ".join(f"task {layer.task_id} PR #{layer.pr_number}" for layer in missing)
            + "; check them, then record each with `mm deliver <task> --merged <sha>`"
        )
    return result


def _retarget_children(conn, task, *, new_base: str) -> list[dict]:
    if stacks._pr_tool() is None:
        raise StackError("cannot retarget child pull requests because plain `gh` is unavailable")
    slug, _default, parent_cwd = stacks._repo_context(conn, task)
    changed = []
    children = conn.execute(
        "SELECT * FROM tasks WHERE stacked_on_task = ? "
        "AND (merged_sha IS NULL OR merged_sha = '') ORDER BY id",
        (task["id"],),
    ).fetchall()
    for child in children:
        if not child["branch"]:
            continue
        cwd = child["worktree_path"] or parent_cwd
        pr = stacks.pull_request_for(child["branch"], cwd=cwd, slug=slug)
        if pr is None or pr.get("state") != "OPEN" or pr.get("baseRefName") == new_base:
            continue
        number = pr.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise StackError(f"task {child['id']} has a pull request with no numeric id")
        try:
            changed.append(
                gh_stack.retarget(
                    conn, child, pr_number=number, new_base=new_base, cwd=cwd, slug=slug
                )
            )
        except StackError as exc:
            raise StackError(f"PR #{number} merged below it, but {exc}") from exc
    return changed


def _merge_layer_by_layer(
    identifier: int,
    result: StackMergeResult,
    *,
    all_layers: bool,
    sleep,
    confirm_attempts: int,
) -> StackMergeResult:
    """Merge the lowest unmerged layer natively; optionally repeat upward."""
    while True:
        layers = stacks.stack_layers(identifier)
        layer = next((item for item in layers if not item.recorded_merged), None)
        if layer is None:
            return result
        if layer.pr_number is None:
            result.stopped = f"task {layer.task_id} has no pull request; deliver that layer first"
            return result
        if str(layer.pr_state or "").upper() != "OPEN":
            result.stopped = (
                f"task {layer.task_id} PR #{layer.pr_number} is {layer.pr_state or 'unknown'}, "
                "not open"
            )
            return result
        conn = init_db()
        task = store.get_task(conn, layer.task_id)
        if task is None:
            result.stopped = f"task {layer.task_id} disappeared before merge"
            return result
        slug, default_branch, cwd = stacks._repo_context(conn, task)
        no_ci, repo_name = _repo_facts(conn, task)
        try:
            checks = layer_checks(layer.pr_number, cwd=cwd, slug=slug)
            reason = _blocked(layer, checks, no_ci=no_ci, repo=repo_name)
        except StackError as exc:
            result.stopped = str(exc)
            return result
        if reason:
            result.stopped = reason
            return result
        tool = stacks._pr_tool()
        if tool is None:
            result.stopped = "cannot merge because plain `gh` is unavailable"
            return result
        argv = [tool, "pr", "merge", str(layer.pr_number), "--merge"] + _repo_flag(slug)
        proc = stacks._run_forge(argv, cwd=cwd)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "forge refused").strip()[:240]
            result.stopped = f"forge refused PR #{layer.pr_number}: {detail}"
            return result
        merged_sha = None
        for attempt in range(confirm_attempts):
            merged_sha = _merge_sha(stacks.pull_request_for(layer.branch or "", cwd=cwd, slug=slug))
            if merged_sha:
                break
            if attempt + 1 < confirm_attempts:
                sleep(0.5)
        if not merged_sha:
            result.stopped = (
                f"PR #{layer.pr_number} accepted the merge command but the forge did not "
                "return a typed merge commit"
            )
            return result
        if not _record(
            result, layer, merged_sha, f"mm stack merge merged pull request #{layer.pr_number}"
        ):
            return result
        try:
            result.retargeted.extend(_retarget_children(conn, task, new_base=default_branch))
        except StackError as exc:
            result.stopped = str(exc)
            return result
        if not all_layers:
            return result
