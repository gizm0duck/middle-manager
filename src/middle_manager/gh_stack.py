"""GitHub's native stacked pull requests, through the ``gh stack`` extension.

On 2026-09-23 three stacks (seven, four and three layers) were merged one layer at
a time: ``mm stack merge`` merged the lowest layer and retargeted the next, and on
a repository that requires up-to-date branches every layer then needed an
update-branch and a full CI run of its own. GitHub has stacks natively: ``gh stack
link`` registers a chain of pull requests as one stack, and ``gh stack merge``
lands every layer in the base branch in a single all-or-nothing operation.

This module is the only place that shells out to ``gh stack``:

- **Linking**, always by pull-request URL — a branch name makes the extension push
  that branch from wherever it runs — and always with ``--base <forge default>``.
  Without ``--base`` it reads the clone's ``refs/remotes/origin/HEAD``, which on
  2026-09-23 still named a feature branch, and it retargeted the bottom pull
  request there.
- **Merging** the whole stack at once (see :mod:`middle_manager.stack_merge`).
- **Retargeting** a pull request that sits in a GitHub stack, which GitHub refuses
  outright ("Cannot change the base branch because the pull request is part of a
  stack"): unstack, retarget, link again.

The extension has no ``--repo`` flag; it reads ``GH_REPO``, which is set to the
forge's ``owner/name`` so a base clone whose ``origin`` is a local path still talks
to the right repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from middle_manager import stacks
from middle_manager.state import store

INSTALL_HINT = "gh extension install github/gh-stack"

_STACK_NUMBER = re.compile(r"\bstack #(\d+)", re.IGNORECASE)
_STACKED_REFUSAL = re.compile(r"part of a stack", re.IGNORECASE)


@dataclass
class StackCall:
    """One ``gh stack`` invocation: whether it worked, what it said, and the stack it named."""

    ok: bool
    detail: str
    stack: int | None = None


def _gh() -> str | None:
    from middle_manager.companions import companion_bin

    # `gh stack` is an extension of plain gh; gh-axi does not carry extensions.
    return companion_bin("gh")


def _run(argv: list[str], *, cwd: str | None, slug: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if slug:
        env["GH_REPO"] = slug
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def available() -> bool:
    """Whether ``gh stack`` answers at all. gh exits non-zero for an unknown command."""
    tool = _gh()
    if tool is None:
        return False
    return _run([tool, "stack", "--help"], cwd=None, slug=None).returncode == 0


def _call(args: list[str], *, cwd: str | None, slug: str | None) -> StackCall:
    tool = _gh()
    if tool is None:
        return StackCall(False, "plain `gh` is unavailable")
    proc = _run([tool, "stack", *args], cwd=cwd, slug=slug)
    said = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    match = _STACK_NUMBER.search(said)
    detail = said[:400] or ("done" if proc.returncode == 0 else "gh stack failed")
    return StackCall(proc.returncode == 0, detail, int(match.group(1)) if match else None)


def link(pr_urls: list[str], *, base: str, cwd: str | None, slug: str | None) -> StackCall:
    return _call(["link", "--base", base, *pr_urls], cwd=cwd, slug=slug)


def unstack(ref: int, *, cwd: str | None, slug: str | None) -> StackCall:
    return _call(["unstack", str(ref)], cwd=cwd, slug=slug)


def merge(top_pr: int, *, cwd: str | None, slug: str | None) -> StackCall:
    """Merge every layer up to and including ``top_pr`` in one atomic operation."""
    return _call(["merge", str(top_pr), "--yes", "--merge"], cwd=cwd, slug=slug)


# --------------------------------------------------------------------------- #
# A task's chain, as the forge sees it
# --------------------------------------------------------------------------- #


@dataclass
class RepoContext:
    """Where ``gh stack`` runs for one repository, and the base a stack sits on."""

    slug: str | None
    base: str
    cwd: str | None


def repo_context(conn, task) -> RepoContext:
    """The base clone, the forge slug, and the forge's default branch for a task's repo.

    The default branch is asked of the forge itself; the recorded one is the
    fallback when the forge cannot be reached.
    """
    from middle_manager import repos

    cwd = task["worktree_path"]
    if not task["repo_id"]:
        return RepoContext(None, "main", cwd)
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
    if row is None:
        return RepoContext(None, "main", cwd)
    try:
        remote = repos.upstream_remote(row)
    except repos.RepoError:
        remote = "origin"
    base = repos.forge_default_branch(row["local_path"], remote)
    return RepoContext(
        repos.forge_slug(row["forge_url"]),
        base or row["default_branch"] or "main",
        row["local_path"] or cwd,
    )


def _chain_layers(
    conn, task, ctx: RepoContext, *, upto: int | None, stop_at_gap: bool = False
) -> tuple[list[dict], str, str]:
    """The unmerged layers of a task's chain with their open pull requests, bottom-up.

    Returns the layers, why the chain cannot be linked (if it cannot), and — with
    ``stop_at_gap`` — the layer the chain was cut at instead: a stack must be
    contiguous, so everything below the first layer with no open pull request is
    linked and the rest waits for its delivery.
    """
    chain = stacks._chain_for_task(conn, int(task["id"]))
    if upto is not None:
        ids = [int(row["id"]) for row in chain]
        if upto in ids:
            chain = chain[: ids.index(upto) + 1]
    layers: list[dict] = []
    for row in chain:
        if row["merged_at"] or row["merged_sha"]:
            continue
        task_id = int(row["id"])
        if not row["branch"]:
            if stop_at_gap:
                return layers, "", f"task {task_id} has no branch yet"
            return layers, f"task {task_id} has no branch yet", ""
        pr = stacks.pull_request_for(
            row["branch"], cwd=row["worktree_path"] or ctx.cwd, slug=ctx.slug
        )
        number = pr.get("number") if pr else None
        url = pr.get("url") if pr else None
        if (
            pr is None
            or str(pr.get("state") or "").upper() != "OPEN"
            or isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(url, str)
        ):
            why = f"task {task_id} has no open pull request to put in the stack"
            return (layers, "", why) if stop_at_gap else (layers, why, "")
        layers.append({"task_id": task_id, "pr": number, "url": url})
    return layers, "", ""


def on_chain(conn, task, base: str | None, default_branch: str) -> bool:
    """Is ``base`` where this layer sits in its stack: the layer below, or the default
    branch once everything below has merged? Only then does it belong in a GitHub stack."""
    parent_id = task["stacked_on_task"] if "stacked_on_task" in task.keys() else None  # noqa: SIM118
    parent = store.get_task(conn, int(parent_id)) if parent_id else None
    if parent is None or parent["merged_at"]:
        return base == default_branch
    return base == parent["branch"]


def link_chain(
    conn,
    task,
    *,
    upto: int | None = None,
    ctx: RepoContext | None = None,
    stop_at_gap: bool = False,
) -> dict:
    """Register a task's unmerged chain as one GitHub stack. Never raises.

    ``upto`` stops the chain at that task (delivery links up to the layer it just
    opened); ``stop_at_gap`` links up to the last layer with an open pull request
    instead of refusing at the first one without (re-linking after an unstack must
    not leave the stack dissolved). Fewer than two layers is no stack at all and
    records nothing; anything else records a ``stack_linked`` or
    ``stack_link_failed`` event on ``task``.
    """
    ctx = ctx or repo_context(conn, task)
    layers, problem, gap = _chain_layers(conn, task, ctx, upto=upto, stop_at_gap=stop_at_gap)
    outcome = {
        "task_id": int(task["id"]),
        "base": ctx.base,
        "task_ids": [layer["task_id"] for layer in layers],
        "prs": [layer["pr"] for layer in layers],
        "linked": False,
        "skipped": False,
        "stack": None,
        "detail": problem,
        "stopped_at": gap or None,
    }
    if not problem and len(layers) < 2:
        outcome.update(skipped=True, detail="fewer than two unmerged layers; nothing to stack")
        return outcome
    if not problem:
        if not available():
            outcome["detail"] = f"`gh stack` is not installed ({INSTALL_HINT})"
        else:
            call = link(
                [layer["url"] for layer in layers], base=ctx.base, cwd=ctx.cwd, slug=ctx.slug
            )
            outcome.update(linked=call.ok, detail=call.detail, stack=call.stack)
    store.append_event(
        conn,
        kind="stack_linked" if outcome["linked"] else "stack_link_failed",
        payload=outcome,
        run_id=task["run_id"],
        task_id=int(task["id"]),
    )
    return outcome


def describe_link(outcome: dict | None) -> str | None:
    """One line for a delivery note, or None when there was nothing to link."""
    if not outcome or outcome.get("skipped"):
        return None
    prs = ", ".join(f"#{number}" for number in outcome.get("prs") or [])
    if outcome.get("linked"):
        cut = f" (stopped below {outcome['stopped_at']})" if outcome.get("stopped_at") else ""
        return f"linked {prs} as a GitHub stack on {outcome['base']}{cut}"
    return f"not linked as a GitHub stack: {outcome.get('detail')}"


def recorded_stack_number(conn, task) -> int | None:
    """The GitHub stack number the last successful link in this chain reported."""
    ids = [int(row["id"]) for row in stacks._chain_for_task(conn, int(task["id"]))]
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT payload FROM events WHERE kind = 'stack_linked' AND task_id IN ({marks}) "  # noqa: S608
        "ORDER BY id DESC",
        ids,
    ).fetchall()
    for row in rows:
        try:
            number = json.loads(row["payload"]).get("stack")
        except (TypeError, ValueError):
            continue
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def after_delivery(
    conn, task, *, base: str | None, retarget_to_base: bool, cwd: str | None, slug: str | None
) -> dict:
    """What delivery does about the stack once a layer's pull request exists. Never raises.

    ``retarget_to_base`` is an explicit ``--base`` for a pull request that already
    existed: it is pointed there (through :func:`retarget`). A layer with a stack
    parent whose pull request targets the layer below is then linked, with every
    unmerged layer beneath it, as one GitHub stack.
    """
    out: dict = {"retargeted": None, "link": None, "notes": []}
    if retarget_to_base and base and task["branch"]:
        pr = stacks.pull_request_for(task["branch"], cwd=cwd, slug=slug)
        number = pr.get("number") if pr else None
        was = pr.get("baseRefName") if pr else None
        if (
            pr is not None
            and str(pr.get("state") or "").upper() == "OPEN"
            and was != base
            and isinstance(number, int)
            and not isinstance(number, bool)
        ):
            try:
                changed = retarget(conn, task, pr_number=number, new_base=base, cwd=cwd, slug=slug)
            except stacks.StackError as exc:
                out["notes"].append(f"PR #{number} still targets {was}: {exc}")
            else:
                out["retargeted"] = changed
                unstacked = (
                    " (unstacked from its GitHub stack first)" if changed["unstacked"] else ""
                )
                out["notes"].append(f"PR #{number} retargeted from {was} to {base}{unstacked}")
                relinked = describe_link(changed.get("relinked"))
                out["notes"].extend(line for line in (changed.get("note"), relinked) if line)
                if changed["unstacked"]:
                    return out
    parent_id = task["stacked_on_task"] if "stacked_on_task" in task.keys() else None  # noqa: SIM118
    if not parent_id:
        return out
    ctx = repo_context(conn, task)
    if not on_chain(conn, task, base, ctx.base):
        out["notes"].append(
            f"not linked as a GitHub stack: it targets {base}, not the layer below it"
        )
        return out
    out["link"] = link_chain(conn, task, upto=int(task["id"]), ctx=ctx)
    line = describe_link(out["link"])
    if line:
        out["notes"].append(line)
    return out


# --------------------------------------------------------------------------- #
# Retargeting
# --------------------------------------------------------------------------- #


def retarget(
    conn, task, *, pr_number: int, new_base: str, cwd: str | None, slug: str | None
) -> dict:
    """Point ``task``'s pull request at ``new_base``, unstacking it first when GitHub says so.

    A plain ``gh pr edit --base`` is tried first; a pull request in a GitHub stack
    is refused with "part of a stack", and only then is the stack taken apart
    (``gh stack unstack``, by the stack number the last link reported, else by the
    pull request), the base changed, and the chain linked again — when the new base
    is still where this layer sits in its stack.
    """
    tool = stacks._pr_tool()
    if tool is None:
        raise stacks.StackError("cannot retarget a pull request because plain `gh` is unavailable")
    argv = [tool, "pr", "edit", str(pr_number), "--base", new_base]
    if slug:
        argv += ["--repo", slug]
    proc = stacks._run_forge(argv, cwd=cwd)
    changed = {"task_id": int(task["id"]), "pr": pr_number, "base": new_base, "unstacked": False}
    if proc.returncode == 0:
        return changed
    detail = (proc.stderr or proc.stdout or "forge refused").strip()[:240]
    if not _STACKED_REFUSAL.search(detail):
        raise stacks.StackError(f"retargeting PR #{pr_number} to {new_base} failed: {detail}")
    ctx = repo_context(conn, task)
    ref = recorded_stack_number(conn, task) or pr_number
    undone = unstack(ref, cwd=ctx.cwd, slug=ctx.slug)
    if not undone.ok:
        raise stacks.StackError(
            f"PR #{pr_number} is part of a GitHub stack and `gh stack unstack {ref}` failed: "
            f"{undone.detail}"
        )
    proc = stacks._run_forge(argv, cwd=cwd)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "forge refused").strip()[:240]
        raise stacks.StackError(
            f"unstacked PR #{pr_number} but retargeting it to {new_base} still failed: {detail}"
        )
    changed["unstacked"] = True
    fresh = store.get_task(conn, int(task["id"]))
    if on_chain(conn, fresh, new_base, ctx.base):
        changed["relinked"] = link_chain(conn, fresh, stop_at_gap=True)
    else:
        changed["relinked"] = None
        changed["note"] = (
            f"PR #{pr_number} now targets {new_base}, which is not where it sits in its "
            "stack, so it was left out of the GitHub stack"
        )
    return changed
