"""Stacked pull requests as a first-class thing `mm` understands.

Stacked pull requests are the documented working model for related changes in one
repository, but the mechanics were entirely manual: the manager had to look up
the previous task's lease branch and pass it as ``--base``, nothing recorded that
the two tasks were one stack, and nothing noticed that GitHub's cascading rebase
had rewritten a branch out from under a worktree. On 2026-09-03, tasks 88-100
were each dispatched from ``main`` and merged serially, costing a rebase and a CI
rerun per merge.

Three things live here:

- **The chain.** A task records the task it is stacked on, so `mm stack` can walk
  a stack bottom-up without the manager remembering branch names.
- **The view.** Each layer's branch, pull request, base, review state, and — the
  question that decides whether the next merge is safe — whether the pull
  request's base still matches the layer below it, i.e. whether GitHub's cascade
  has run.
- **Cascade awareness.** A cascade *rewrites* the upper branches. Before a resume
  or a delivery, a worktree whose remote branch has moved on without it is
  fast-forwarded onto the remote; a worktree that has commits of its own *and* is
  behind is refused, naming both commits. A force-push over a rebased branch is
  never the answer: it destroys the cascade and whatever else landed with it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from middle_manager.state import init_db, store


class StackError(Exception):
    """A stack operation was refused; nothing was changed."""


# --------------------------------------------------------------------------- #
# Branch state against the remote
# --------------------------------------------------------------------------- #


@dataclass
class BranchState:
    """How a worktree stands against the branch it pushes to.

    ``ahead`` counts commits whose *patch* is not upstream, not commits whose SHA
    is not upstream. A rebase — which is what GitHub's cascade does — gives every
    replayed commit a new SHA, so counting SHAs would report a freshly cascaded
    branch as full of unpushed work and refuse every resume and delivery in the
    stack. ``git cherry`` answers the question that actually matters: is there
    anything here that upstream does not already contain?
    """

    branch: str
    remote: str
    local_sha: str
    remote_sha: str | None
    ahead: int = 0  # commits here whose patch is nowhere on the remote branch
    behind: int = 0  # commits on the remote that this worktree does not have
    replayed: int = 0  # local commits the remote already holds under a new SHA

    @property
    def on_remote(self) -> bool:
        return self.remote_sha is not None

    @property
    def diverged(self) -> bool:
        return self.ahead > 0 and self.behind > 0

    @property
    def rewritten_upstream(self) -> bool:
        """The remote moved and this worktree has nothing of its own: a cascade."""
        return self.behind > 0 and self.ahead == 0


def _git(worktree: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", worktree, *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout.strip()


def branch_state(worktree: str, branch: str, remote: str = "origin") -> BranchState:
    """Compare the worktree's head with ``<remote>/<branch>``, fetching first.

    A branch the remote has never seen comes back with ``remote_sha`` unset and no
    counts — there is nothing to be behind or ahead of yet.
    """
    rc, local = _git(worktree, "rev-parse", "HEAD")
    if rc != 0:
        raise StackError(f"{worktree} is not a readable git worktree")
    fetched, _out = _git(worktree, "fetch", "--quiet", remote, branch)
    if fetched != 0:
        return BranchState(branch=branch, remote=remote, local_sha=local, remote_sha=None)
    rc, remote_sha = _git(worktree, "rev-parse", "FETCH_HEAD")
    if rc != 0 or not remote_sha:
        return BranchState(branch=branch, remote=remote, local_sha=local, remote_sha=None)
    rc, behind_out = _git(worktree, "rev-list", "--count", f"{local}..{remote_sha}")
    behind = int(behind_out) if rc == 0 and behind_out.isdigit() else 0
    rc, cherry = _git(worktree, "cherry", remote_sha, local)
    ahead = replayed = 0
    if rc == 0:
        for line in cherry.splitlines():
            if line.startswith("+"):
                ahead += 1
            elif line.startswith("-"):
                replayed += 1
    return BranchState(
        branch=branch,
        remote=remote,
        local_sha=local,
        remote_sha=remote_sha,
        ahead=ahead,
        behind=behind,
        replayed=replayed,
    )


def push_remote(conn, task) -> str:
    """The remote a task's branch belongs on: its repo's forge, else ``origin``.

    A worktree inherits the base clone's ``origin``, which for a repo registered
    from a local path reaches nobody. Every question about where this branch
    stands — and every push of it — goes through the same answer.
    """
    from middle_manager import repos

    repo_id = task["repo_id"] if "repo_id" in task.keys() else None  # noqa: SIM118 - sqlite3.Row
    if not repo_id:
        return "origin"
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "origin"
    try:
        return repos.upstream_remote(row)
    except repos.RepoError:
        return "origin"


def task_branch_state(conn, task) -> BranchState | None:
    """``branch_state`` for a task, resolved through its repo's forge remote."""
    worktree, branch = task["worktree_path"], task["branch"]
    if not worktree or not branch:
        return None
    return branch_state(worktree, branch, push_remote(conn, task))


def unpushed_commits(conn, task) -> int:
    """How many commits exist only in this task's worktree.

    The same question ``mm worktree prune`` asks before removing a slot, and the
    one that says whether a worker actually pushed its work. It is answered by
    patch, not by SHA (see :class:`BranchState`), so a branch that GitHub's
    cascade rebased upstream reads as pushed — which it is — instead of as a pile
    of unpushed work. A branch the remote has never seen falls back to "commits on
    no remote at all", because there is no branch to compare against yet.
    """
    worktree = task["worktree_path"]
    if not worktree:
        return 0
    try:
        state = task_branch_state(conn, task)
    except StackError:
        return 0
    if state is not None and state.on_remote:
        return state.ahead
    rc, out = _git(worktree, "rev-list", "--count", "HEAD", "--not", "--remotes")
    return int(out) if rc == 0 and out.isdigit() else 0


def sync_worktree_with_remote(task_id: int) -> dict:
    """Bring a task's worktree onto its rebased remote branch, or refuse.

    Called before ``mm resume`` and ``mm deliver``. GitHub's cascade rewrites the
    branches above a merged layer, which leaves the worktree pointing at commits
    that no longer exist upstream; resuming or delivering from there either
    force-pushes the cascade away or opens a pull request full of duplicates.

    - Remote moved, worktree has nothing of its own -> reset onto the remote and
      record it as an event.
    - Both sides have commits -> refuse, naming both commits. A force-push here
      would destroy work; which side is right is the manager's call, not ours.
    - Anything else -> no-op.
    """
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise StackError(f"task {task_id} not found")
    try:
        state = task_branch_state(conn, task)
    except StackError:
        # An unreadable worktree is not this check's problem to report: whatever
        # runs next (a push, a diff, a resume) fails on it with a better message.
        return {"task_id": task_id, "action": "none", "note": "no readable worktree to compare"}
    if state is None or not state.on_remote:
        return {"task_id": task_id, "action": "none", "note": "no remote branch to compare with"}
    if state.diverged:
        raise StackError(
            f"task {task_id}: the worktree and {state.remote}/{state.branch} have both moved "
            f"— worktree at {state.local_sha[:8]} with {state.ahead} commit(s) the remote "
            f"lacks (nothing upstream matches those patches), remote at "
            f"{state.remote_sha[:8]} with {state.behind} the worktree lacks. "
            "This is what a cascade plus local work looks like; sort out which commits "
            "survive before resuming or delivering. Nothing was force-pushed."
        )
    if not state.rewritten_upstream:
        return {"task_id": task_id, "action": "none", "note": "worktree matches the remote branch"}

    rc, _out = _git(task["worktree_path"], "reset", "--hard", "--quiet", state.remote_sha or "")
    if rc != 0:
        raise StackError(
            f"task {task_id}: could not move the worktree onto {state.remote}/{state.branch}"
        )
    payload = {
        "task_id": task_id,
        "branch": state.branch,
        "remote": state.remote,
        "from": state.local_sha,
        "to": state.remote_sha,
        "behind": state.behind,
        "replayed": state.replayed,
        "summary": (
            f"{state.remote}/{state.branch} was rewritten upstream (a stack cascade); "
            f"moved the worktree from {state.local_sha[:8]} to {(state.remote_sha or '')[:8]} "
            f"— its {state.replayed} commit(s) are already there under new SHAs and it held "
            "nothing else"
        ),
    }
    store.append_event(
        conn,
        kind="worktree_cascaded",
        payload=payload,
        run_id=task["run_id"],
        task_id=task_id,
    )
    return {"task_id": task_id, "action": "reset", **payload}


# --------------------------------------------------------------------------- #
# The forge view of a layer
# --------------------------------------------------------------------------- #


def _pr_tool() -> str | None:
    from middle_manager.companions import companion_bin

    for tool in ("gh-axi", "gh"):
        resolved = companion_bin(tool)
        if resolved:
            return resolved
    return None


def pull_request_for(
    branch: str, *, cwd: str | None = None, slug: str | None = None
) -> dict | None:
    """The pull request opened from ``branch``, or None. Tolerates a missing ``gh``.

    Everything a layer view needs from the forge comes from here, so a machine
    without ``gh`` renders the stack from local state instead of failing.
    """
    tool = _pr_tool()
    if tool is None:
        return None
    argv = [
        tool,
        "pr",
        "view",
        branch,
        "--json",
        "number,baseRefName,state,isDraft,title,url,mergedAt",
    ]
    if slug:
        argv += ["--repo", slug]
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# The stack itself
# --------------------------------------------------------------------------- #


@dataclass
class Layer:
    """One task in a stack, with everything needed to decide what to merge next."""

    task_id: int
    title: str
    status: str
    branch: str | None
    parent_task_id: int | None
    parent_branch: str | None
    review: str
    pr_number: int | None = None
    pr_base: str | None = None
    pr_state: str | None = None
    pr_url: str | None = None
    cascade_pending: bool | None = None
    notes: list[str] = field(default_factory=list)


def _review_state(task_id: int) -> str:
    from middle_manager.review import ReviewError, is_approved_at_head, latest_review

    review = latest_review(task_id)
    if review is None:
        return "not reviewed"
    try:
        approved, reason = is_approved_at_head(task_id)
    except ReviewError:
        return f"{review['verdict']} (worktree unreadable)"
    return "approved at head" if approved else reason


def _chain_for_task(conn, task_id: int) -> list:
    """The whole stack a task belongs to, bottom-up.

    Walks to the bottom of the chain, then back up through the children — naming
    any layer renders the same stack, because "what has to merge before this" is
    the question, whichever layer the manager happens to be looking at.
    """
    bottom = store.get_task(conn, task_id)
    if bottom is None:
        raise StackError(f"task {task_id} not found")
    climbed = {int(bottom["id"])}
    while True:
        parent_id = bottom["stacked_on_task"] if "stacked_on_task" in bottom.keys() else None  # noqa: SIM118
        if not parent_id or int(parent_id) in climbed:
            break
        parent = store.get_task(conn, int(parent_id))
        if parent is None:
            break
        climbed.add(int(parent["id"]))
        bottom = parent
    chain = [bottom]
    walked = {int(bottom["id"])}
    while True:
        child = conn.execute(
            "SELECT * FROM tasks WHERE stacked_on_task = ? ORDER BY id LIMIT 1",
            (int(chain[-1]["id"]),),
        ).fetchone()
        if child is None or int(child["id"]) in walked:
            break
        walked.add(int(child["id"]))
        chain.append(child)
    return chain


def stack_layers(identifier: int) -> list[Layer]:
    """The stack containing task ``identifier``, bottom-up.

    When no task has that id, ``identifier`` is read as a run id and every stacked
    task in the run is rendered instead — the two ways a manager refers to work in
    flight.
    """
    conn = init_db()
    if store.get_task(conn, identifier) is not None:
        chain = _chain_for_task(conn, identifier)
    elif store.get_run(conn, identifier) is not None:
        chain = list(
            conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (identifier,)
            ).fetchall()
        )
        if not chain:
            raise StackError(f"run {identifier} has no tasks")
    else:
        raise StackError(f"no task or run {identifier}")

    from middle_manager import repos

    layers: list[Layer] = []
    for index, task in enumerate(chain):
        slug = None
        repo_row = None
        if task["repo_id"]:
            repo_row = conn.execute(
                "SELECT * FROM repos WHERE id = ?", (task["repo_id"],)
            ).fetchone()
            if repo_row is not None:
                slug = repos.forge_slug(repo_row["forge_url"])
        below = chain[index - 1] if index else None
        expected_base = below["branch"] if below is not None else (task["stacked_on"] or None)
        layer = Layer(
            task_id=int(task["id"]),
            title=task["title"],
            status=task["status"],
            branch=task["branch"],
            parent_task_id=(
                int(task["stacked_on_task"]) if task["stacked_on_task"] else None  # type: ignore[index]
            ),
            parent_branch=task["stacked_on"],
            review=_review_state(int(task["id"])),
        )
        pr = (
            pull_request_for(task["branch"], cwd=task["worktree_path"], slug=slug)
            if task["branch"]
            else None
        )
        if pr is None:
            layer.notes.append("no pull request found (or no gh on this machine)")
        else:
            layer.pr_number = pr.get("number")
            layer.pr_base = pr.get("baseRefName")
            layer.pr_state = "merged" if pr.get("mergedAt") else pr.get("state")
            layer.pr_url = pr.get("url")
            if expected_base:
                layer.cascade_pending = layer.pr_base != expected_base
                if layer.cascade_pending:
                    layer.notes.append(
                        f"pull request targets {layer.pr_base!r}, not the layer below "
                        f"({expected_base!r}) — the cascade has already retargeted it, or "
                        "it was opened against the wrong branch"
                    )
        layers.append(layer)
    return layers


def render_stack(layers: list[Layer]) -> str:
    """The stack bottom-up, one block per layer, in the order it must be merged."""
    if not layers:
        return "no layers"
    out: list[str] = []
    for index, layer in enumerate(layers, start=1):
        base = layer.parent_branch or "main"
        head = f'{index}. task {layer.task_id} "{layer.title}" [{layer.status}]'
        out.append(head)
        out.append(f"     branch {layer.branch or '(none)'} onto {base}")
        if layer.pr_number:
            out.append(
                f"     PR #{layer.pr_number} ({layer.pr_state or '?'}) -> base "
                f"{layer.pr_base or '?'}"
            )
        for note in layer.notes:
            out.append(f"     note: {note}")
        out.append(f"     review: {layer.review}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Delivery base
# --------------------------------------------------------------------------- #


def delivery_base(conn, task, base: str | None) -> tuple[str | None, str | None]:
    """The base a stacked layer's pull request should target, and why if it changed.

    When the layer below has already merged, its branch is gone (or about to be):
    opening a pull request against it either fails or shows a diff nobody can
    review. GitHub's cascade retargets an *existing* pull request in that
    situation; a pull request being opened now has to be aimed at the default
    branch itself.

    Only a task that records a stack parent is checked: a bare ``--base`` says
    where to open the pull request and nothing about a chain, and asking the forge
    about it on every delivery would be a network call for no information.
    """
    parent_id = task["stacked_on_task"] if "stacked_on_task" in task.keys() else None  # noqa: SIM118
    if not base or not parent_id:
        return base, None
    slug = None
    default_branch = "main"
    if task["repo_id"]:
        from middle_manager import repos

        repo_row = conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if repo_row is not None:
            slug = repos.forge_slug(repo_row["forge_url"])
            default_branch = repo_row["default_branch"] or "main"
    if base == default_branch:
        return base, None
    pr = pull_request_for(base, cwd=task["worktree_path"], slug=slug)
    if pr is None or not pr.get("mergedAt"):
        return base, None
    parent_label = f"task {int(parent_id)}" if parent_id else f"branch {base}"
    return default_branch, (
        f"the layer below ({parent_label}, pull request #{pr.get('number')} on {base}) has "
        f"merged, so this pull request targets {default_branch} instead of a merged branch; "
        "GitHub's cascade has not retargeted it because it did not exist yet"
    )
