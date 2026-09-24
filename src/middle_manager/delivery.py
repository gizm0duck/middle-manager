"""GitHub delivery, gated by the exact-HEAD review.

Delivery is the only path that leaves the machine, so it is the most conservative
step: it refuses unless an approved review is bound to the worktree's current
head. Push uses plain git (works against any origin); PR creation uses ``gh``
(preferred: ``gh-axi`` when present) and is skippable for local fixtures.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from middle_manager import compose
from middle_manager.companions import pr_tool
from middle_manager.review import ReviewError, head_sha, is_approved_at_head
from middle_manager.state import init_db, store


class DeliveryError(Exception):
    pass


_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}\Z")
#: A pull request's web URL, wherever it sits in a tool's output. Plain ``gh``
#: prints exactly the URL; ``gh-axi`` prints a structured record followed by
#: help lines, so the last line of stdout was "Run `gh-axi pr checks 72 ...` to
#: monitor CI" and that string was persisted as the PR URL of #72 (issue #73).
_PR_URL_RE = re.compile(r"https?://[^\s`'\"<>()]+/(?:pull|pulls|pr|merge_requests)/\d+")


@dataclass
class DeliveryResult:
    task_id: int
    branch: str
    head_sha: str
    pushed: bool
    pr_url: str | None
    note: str
    #: What tearing down the task's compose stack did, when it owned one.
    compose: dict | None = None
    #: Whether a pull request exists for the branch after this delivery — created
    #: now, or found already open. ``True`` with ``pr_url`` None means the PR is
    #: real but its address could not be read back; retrying creation would only
    #: be refused as a duplicate.
    pr_exists: bool = False


def extract_pr_url(text: str | None) -> str | None:
    """The first pull-request URL anywhere in ``text``, or None."""
    if not text:
        return None
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else None


def _lookup_pr_url(tool: str, branch: str, forge_slug: str | None, cwd: str) -> str | None:
    """A bounded follow-up: ask the forge which open PR has this head branch.

    Used only when creation reported success without a readable URL. Plain
    ``gh`` answers in JSON; ``gh-axi`` answers in its own text, which is searched
    for a URL the same way the creation output was.
    """
    is_plain_gh = Path(tool).name == "gh"
    argv = [tool, "pr", "list", "--head", branch, "--state", "open"]
    if is_plain_gh:
        argv += ["--json", "url", "--jq", ".[0].url"]
    if forge_slug:
        argv += ["--repo", forge_slug]
    proc = _run(argv, cwd=cwd)
    if proc.returncode != 0:
        return None
    return extract_pr_url(proc.stdout)


def _run(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _git_lines(worktree: str, *args: str) -> list[str]:
    proc = _run(["git", "-C", worktree, *args])
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _resolved_ref(worktree: str, *candidates: str | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        lines = _git_lines(worktree, "rev-parse", "--verify", candidate)
        if lines:
            return lines[0]
    return None


def _patch_ids(worktree: str, commits: set[str]) -> dict[str, str]:
    """Stable patch-id -> commit for ``commits``; merges and empty commits have none."""
    if not commits:
        return {}
    shown = _run(["git", "-C", worktree, "log", "-p", "--no-color", "--no-walk", *sorted(commits)])
    if shown.returncode != 0 or not shown.stdout:
        return {}
    ids = subprocess.run(
        ["git", "-C", worktree, "patch-id", "--stable"],
        input=shown.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if ids.returncode != 0:
        return {}
    pairs = (line.split() for line in ids.stdout.splitlines())
    return {pair[0]: pair[1] for pair in pairs if len(pair) == 2}


def _stack_parent_head(worktree: str, sibling, *, remote: str, delivering: int) -> str | None:
    """The head of the branch ``sibling`` is stacked on, as far as it may be trusted.

    A child that was dispatched onto an empty parent branch records the default
    branch's SHA as its ``base_sha``; once it rebases onto the parent's pushed
    head, ``base_sha..HEAD`` holds the parent's commits too. On 2026-09-17 that
    refused delivery of a parent because its own child "carried" its commits.
    Whatever the parent's branch reaches is the parent's, not the child's.

    When the parent is the task being delivered, only its *published* head
    counts: its local head is exactly what is under inspection, and excluding it
    would wave through a child commit that found its way onto the parent's branch.
    """
    parent_branch = (sibling["stacked_on"] or "").strip()
    if not parent_branch:
        return None
    published = f"{remote}/{parent_branch}"
    if sibling["stacked_on_task"] is not None and int(sibling["stacked_on_task"]) == delivering:
        return _resolved_ref(worktree, published)
    return _resolved_ref(worktree, published, parent_branch)


def _own_commits(
    worktree: str, sibling, *, head: str, remote: str, default_branch: str, delivering: int
) -> set[str]:
    """The commits ``sibling`` wrote itself: none that main or its stack parent already holds."""
    excluded = [sibling["base_sha"]]
    for ref in (
        _resolved_ref(worktree, f"{remote}/{default_branch}"),
        _stack_parent_head(worktree, sibling, remote=remote, delivering=delivering),
    ):
        if ref:
            excluded.append(ref)
    return set(_git_lines(worktree, "rev-list", head, *(f"^{ref}" for ref in excluded)))


def composition_tasks(conn, task, *, remote: str, base: str | None, head: str) -> list[dict]:
    """Open sibling tasks whose own commits appear in this task's proposed PR diff.

    A commit counts when its SHA is in the diff, or when a copy of it is — the same
    patch cherry-picked under a new SHA carries the sibling's work just the same.
    """
    worktree = task["worktree_path"]
    if not worktree:
        return []
    default_branch = "main"
    if task["repo_id"]:
        repo = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo is not None:
            default_branch = repo["default_branch"] or "main"
    base_name = base or default_branch
    base_sha = _resolved_ref(worktree, f"{remote}/{base_name}", base_name, task["base_sha"])
    if not base_sha:
        return []
    proposed = set(_git_lines(worktree, "rev-list", f"{base_sha}..{head}"))
    if not proposed:
        return []
    rows = conn.execute(
        "SELECT * FROM tasks WHERE repo_id = ? AND id != ? "
        "AND (merged_sha IS NULL OR merged_sha = '') "
        "AND status NOT IN ('closed', 'cancelled') ORDER BY id",
        (task["repo_id"], task["id"]),
    ).fetchall()
    conflicts: list[dict] = []
    proposed_patches: dict[str, str] | None = None
    for sibling in rows:
        sibling_head = _resolved_ref(
            worktree,
            f"{remote}/{sibling['branch']}" if sibling["branch"] else None,
            sibling["branch"],
        )
        if sibling_head is None and sibling["lease_id"] and sibling["worktree_path"]:
            lease = conn.execute(
                "SELECT * FROM leases WHERE id = ? AND status = 'active'",
                (sibling["lease_id"],),
            ).fetchone()
            if (
                lease is not None
                and lease["task_id"] == sibling["id"]
                and lease["worktree_path"] == sibling["worktree_path"]
            ):
                sibling_head = _resolved_ref(sibling["worktree_path"], "HEAD")
        sibling_base = sibling["base_sha"]
        if not sibling_head or not sibling_base:
            continue
        own_commits = _own_commits(
            worktree,
            sibling,
            head=sibling_head,
            remote=remote,
            default_branch=default_branch,
            delivering=int(task["id"]),
        )
        shared_shas = proposed & own_commits
        copied = own_commits - shared_shas
        if copied:
            if proposed_patches is None:
                proposed_patches = _patch_ids(worktree, proposed)
            shared_shas |= {
                proposed_patches[pid]
                for pid in _patch_ids(worktree, copied)
                if pid in proposed_patches
            }
        shared = sorted(shared_shas)
        if shared:
            conflicts.append(
                {
                    "task_id": int(sibling["id"]),
                    "title": sibling["title"],
                    "commits": shared,
                }
            )
    return conflicts


def _pr_tool() -> str | None:
    # One resolver for both directions: see ``companions.pr_tool``.
    return pr_tool()


def _pr_body(task_id: int, head: str, body_file: str | None) -> str:
    """The pull request body: the caller's file if given, else a composed one.

    ``--body-file`` overrides everything — a manager who has written the body by
    hand should never have it second-guessed. Otherwise the body is composed from
    the brief, the worker's reports, and the review; see
    :mod:`middle_manager.pr_body`.
    """
    if body_file:
        return Path(body_file).expanduser().read_text(encoding="utf-8")
    from middle_manager import pr_body

    return pr_body.compose(task_id, head_sha=head)


def _forge_for_task(conn, task) -> tuple[str, str | None]:
    """The remote to push through and the ``owner/name`` to open the PR against.

    A repo registered from a local path has a local ``origin``: pushing there
    reaches nobody and there is no forge to open a pull request on. The
    registration now carries the forge URL, and the base clone gets a ``forge``
    remote when its ``origin`` is not it — so delivery pushes and opens the pull
    request where the work is actually reviewed.
    """
    from middle_manager import repos

    repo_id = task["repo_id"] if "repo_id" in task.keys() else None  # noqa: SIM118 - sqlite3.Row
    if not repo_id:
        return "origin", None
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "origin", None
    try:
        return repos.upstream_remote(row), repos.forge_slug(row["forge_url"])
    except repos.RepoError:
        return "origin", repos.forge_slug(row["forge_url"])


def deliver(
    task_id: int,
    *,
    push: bool = True,
    open_pr: bool = True,
    remote: str | None = None,
    base: str | None = None,
    title: str | None = None,
    body_file: str | None = None,
) -> DeliveryResult:
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise DeliveryError(f"task {task_id} not found")
    forge_remote, forge_slug = _forge_for_task(conn, task)
    remote = remote or forge_remote
    worktree = task["worktree_path"]
    branch = task["branch"]
    if not worktree or not branch:
        raise DeliveryError("task has no worktree/branch to deliver")
    explicit_base = base is not None
    if base is None and "stacked_on" in task.keys():  # noqa: SIM118 - sqlite3.Row: `in` scans values
        # A task dispatched with --base opens its PR against that branch unless
        # the caller says otherwise; stacked PRs no longer depend on remembering.
        base = task["stacked_on"] or None

    # A cascade may have rewritten this branch since the worker last touched it.
    # Delivering from a stale worktree force-pushes the cascade away, so move onto
    # the remote first — or refuse, when both sides hold commits.
    from middle_manager import stacks

    cascaded = None
    try:
        result = stacks.sync_worktree_with_remote(task_id)
        cascaded = result if result.get("action") == "reset" else None
    except stacks.StackError as exc:
        raise DeliveryError(str(exc)) from exc

    # A layer whose parent has already merged cannot target the parent's branch;
    # one whose parent has not merged is refused against the default branch, and
    # against a parent branch the forge has not seen, unless --base says otherwise.
    base_note = None
    if base:
        try:
            base, base_note = stacks.delivery_base(
                conn, task, base, explicit=explicit_base, remote=remote
            )
        except stacks.StackError as exc:
            raise DeliveryError(str(exc)) from exc

    head = head_sha(worktree)
    compositions = composition_tasks(conn, task, remote=remote, base=base, head=head)
    if compositions:
        carried = ", ".join(f'task {item["task_id"]} "{item["title"]}"' for item in compositions)
        raise DeliveryError(
            f'refusing composition: task {task_id} "{task["title"]}" would carry commits '
            f"from another open task ({carried}). Keep the layer pull requests and merge them "
            f"as one stack with `mm stack merge {task_id}`."
        )

    # The gate: never ship work that was not reviewed at its current head.
    approved, reason = is_approved_at_head(task_id)
    if not approved:
        raise DeliveryError(f"refusing to deliver: {reason}")

    pushed = False
    if push:
        proc = _run(["git", "push", remote, f"HEAD:{branch}"], cwd=worktree)
        if proc.returncode != 0:
            raise DeliveryError(f"git push failed: {proc.stderr.strip()}")
        pushed = True

    pr_url: str | None = None
    pr_exists = False
    pr_created = False
    note = "pushed" if pushed else "no-op"
    if open_pr:
        tool = _pr_tool()
        if tool is None:
            note = "pushed; no gh/gh-axi found, PR not opened"
        else:
            argv = [
                tool,
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                title or task["title"],
                "--body",
                _pr_body(task_id, head, body_file),
            ]
            if base:
                argv += ["--base", base]
            if forge_slug:
                # The worktree's `origin` may be a local clone; name the forge
                # explicitly so `gh` opens the pull request in the right place.
                argv += ["--repo", forge_slug]
            proc = _run(argv, cwd=worktree)
            if proc.returncode == 0:
                pr_exists = pr_created = True
                pr_url = extract_pr_url(proc.stdout) or extract_pr_url(proc.stderr)
                if pr_url is None:
                    pr_url = _lookup_pr_url(tool, branch, forge_slug, worktree)
                note = (
                    "pushed; PR opened"
                    if pr_url
                    else "pushed; PR opened, but its URL could not be read from the tool's "
                    f"output or looked up by branch — find it with `{Path(tool).name} pr list "
                    f"--head {branch}`; do not create another"
                )
            else:
                # `gh` refuses a second PR for the same head and names the one that
                # exists. That is the retry path: the PR is real, so record it.
                pr_url = extract_pr_url(proc.stderr) or extract_pr_url(proc.stdout)
                if pr_url and "already exists" in (proc.stderr + proc.stdout):
                    pr_exists = True
                    note = "pushed; a PR for this branch was already open"
                else:
                    pr_url = None
                    note = f"pushed; PR creation failed: {proc.stderr.strip()[:200]}"

    # A stacked layer's pull request joins its GitHub stack; an explicit --base for
    # one that already existed retargets it (unstacking first when GitHub insists).
    # Neither can fail the delivery: the push and the pull request already happened.
    stack = None
    if pr_exists:
        from middle_manager import gh_stack

        stack = gh_stack.after_delivery(
            conn,
            task,
            base=base,
            retarget_to_base=explicit_base and not pr_created,
            cwd=worktree,
            slug=forge_slug,
        )

    store.set_task_status(conn, task_id, "delivered")
    store.append_event(
        conn,
        kind="delivered",
        payload={
            "task_id": task_id,
            "branch": branch,
            "head_sha": head,
            "pr_url": pr_url,
            "pr_exists": pr_exists,
            "remote": remote,
            "forge": forge_slug,
            "base": base,
            "base_note": base_note,
            "cascaded": cascaded,
            "stack": stack,
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    for extra in (
        cascaded["summary"] if cascaded else None,
        base_note,
        *(stack["notes"] if stack else []),
    ):
        if extra:
            note = f"{note}; {extra}"
    return DeliveryResult(task_id, branch, head, pushed, pr_url, note, pr_exists=pr_exists)


def record_merged(task_id: int, merged_sha: str, *, note: str | None = None) -> DeliveryResult:
    """Record delivery against a commit that is already merged upstream. Pushes nothing.

    When the manager merges a task's PR on GitHub outside ``mm deliver``, the task
    stays ``worker_done``: the heartbeat keeps listing it as work awaiting the
    manager, and the worktree is never eligible for reclamation. There is nothing
    left to push and no review gate to satisfy — the commit shipped — so this is
    pure bookkeeping: it names the merge commit and closes the task out.

    A squash or rebase merge produces a *new* commit that the task's worktree has
    never seen, so the SHA is validated by shape only and taken at its word.
    """
    if not _SHA_RE.match((merged_sha or "").strip()):
        raise DeliveryError(
            f"--merged wants the commit SHA the work landed as; {merged_sha!r} is not one "
            "(7-40 hex characters)"
        )
    merged_sha = merged_sha.strip().lower()
    conn = init_db()
    # Serialize the read-and-set so a watch tick racing a hand-run
    # ``mm deliver --merged`` produces exactly one delivery/teardown event.
    conn.execute("BEGIN IMMEDIATE")
    task = store.get_task(conn, task_id)
    if task is None:
        conn.rollback()
        raise DeliveryError(f"task {task_id} not found")
    branch = task["branch"] or ""
    recorded = (task["merged_sha"] or "").lower()
    if recorded:
        if recorded != merged_sha:
            conn.rollback()
            raise DeliveryError(
                f"task {task_id} is already recorded merged at {recorded}; refusing to "
                f"replace it with {merged_sha}"
            )
        conn.rollback()
        return DeliveryResult(
            task_id=task_id,
            branch=branch,
            head_sha=recorded,
            pushed=False,
            pr_url=None,
            note=f"merge {recorded[:8]} was already recorded; nothing changed",
        )
    # Record the landing on the task, not only in the event stream: a delivered
    # task never leaves that status, so without a merge on the row the heartbeat
    # would re-ask the forge about this branch on every tick, forever.
    store.update_task_fields(
        conn, task_id, merged_sha=merged_sha, merged_at=datetime.now(UTC).isoformat()
    )
    store.set_task_status(conn, task_id, "delivered")
    store.append_event(
        conn,
        kind="delivered",
        payload={
            "task_id": task_id,
            "branch": branch,
            "head_sha": merged_sha,
            "pr_url": None,
            "merged_outside_mm": True,
            "note": note or "recorded against an already-merged commit; nothing was pushed",
        },
        run_id=task["run_id"],
        task_id=task_id,
    )
    # The task is over, so the database it brought up for itself is over too. This
    # is the last moment anything knows the stack belongs to this task.
    torn_down = compose.teardown_for_task(task_id, trigger="deliver --merged", conn=conn)
    return DeliveryResult(
        task_id=task_id,
        branch=branch,
        head_sha=merged_sha,
        pushed=False,
        pr_url=None,
        note=f"recorded as delivered at {merged_sha[:8]} (already merged; nothing pushed)",
        compose=torn_down,
    )


__all__ = [
    "DeliveryError",
    "DeliveryResult",
    "ReviewError",
    "deliver",
    "extract_pr_url",
    "record_merged",
]
