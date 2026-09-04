"""Two tasks in one repository adding a migration off the same head.

On 2026-09-04 two independent backend tasks each added an Alembic revision whose
``down_revision`` was the same commit's head. Nothing in `mm` noticed: the second
to merge would have left the repository with two Alembic heads and a red
migration-graph test. The manager caught it by reading both diffs by hand and
steering one worker onto the other's branch.

The harness can see it earlier, at two moments and from two angles:

- **At dispatch**, before the second worker has written a line: another task in
  this repository is in flight and its worktree already adds a migration. That is
  a *suggestion* — start from its branch with ``--stack-on`` — not a refusal,
  because two migrations off one head are fine as long as somebody sequences them.
- **At `mm review show`**, when both diffs exist and the question is sharper: do
  these two added migrations name the *same* ``down_revision``? That is no longer
  a guess about what a worker might do, it is the two-heads condition itself.

Neither one refuses anything. Alembic is never run, nothing is rebased: the
manager decides what to do with what it is told.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath

from middle_manager.lifecycle import TERMINAL_STATUSES

# Where Alembic revisions live in a repository laid out the ordinary way. A repo
# that keeps them somewhere else sets its own with
# `mm repo set <name> --migrations-glob <glob>`; the pattern is matched against
# the repo-relative path with `PurePosixPath.full_match`, so `**` spans any
# number of directories.
DEFAULT_MIGRATIONS_GLOB = "**/alembic/versions/*.py"

# `down_revision = "abc123"` on one line, for a file too broken to parse.
_DOWN_REVISION_LINE = re.compile(
    r"^\s*down_revision\s*(?::[^=]+)?=\s*(?P<value>.+?)\s*(?:#.*)?$", re.MULTILINE
)


@dataclass(frozen=True)
class AddedMigration:
    """A migration file one task adds, and the revision it builds on."""

    task_id: int
    title: str
    status: str
    branch: str | None
    path: str
    down_revision: str | None

    @property
    def task_label(self) -> str:
        """The task and where its work lives, named the way `mm stack` names a layer."""
        branch = f"branch {self.branch}" if self.branch else "no branch yet"
        return f'task {self.task_id} "{self.title}" ({self.status}, {branch})'

    def describe(self) -> str:
        """The task, its branch, and its migration, in one clause."""
        return f"{self.task_label} adds {self.path}"


def _git(worktree: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", worktree, *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


def matches(path: str, glob: str) -> bool:
    """Does a repo-relative path match this repo's migrations glob?"""
    if not glob:
        return False
    try:
        return PurePosixPath(path).full_match(glob)
    except ValueError:
        return False


def glob_for_repo(repo_row) -> str:
    """A registered repo's migrations glob, falling back to the default."""
    if repo_row is None:
        return DEFAULT_MIGRATIONS_GLOB
    keys = repo_row.keys()
    configured = repo_row["migrations_glob"] if "migrations_glob" in keys else None  # noqa: SIM118
    cleaned = str(configured).strip() if configured else ""
    return cleaned or DEFAULT_MIGRATIONS_GLOB


def glob_for_repo_id(conn, repo_id) -> str:
    if not repo_id:
        return DEFAULT_MIGRATIONS_GLOB
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return glob_for_repo(row)


def parse_down_revision(source: str) -> str | None:
    """The ``down_revision`` an Alembic revision file declares, as one string.

    Alembic writes either a single revision id or — for a migration that merges
    two heads — a tuple of them; ``None`` marks the first revision in a branch and
    is returned as the string ``"None"``, because two tasks each adding a *first*
    migration collide exactly as two off a shared parent do. ``None`` from here
    means "no ``down_revision`` could be read at all", which is not a collision.
    """
    unset = object()
    value: object = unset
    try:
        tree = ast.parse(source)
    except SyntaxError:
        match = _DOWN_REVISION_LINE.search(source)
        if match is None:
            return None
        try:
            value = ast.literal_eval(match.group("value"))
        except (ValueError, SyntaxError):
            return None
    else:
        for node in tree.body:
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            )
            named = any(isinstance(t, ast.Name) and t.id == "down_revision" for t in targets)
            if not named or getattr(node, "value", None) is None:
                continue
            try:
                value = ast.literal_eval(node.value)  # type: ignore[arg-type]
            except (ValueError, SyntaxError):
                return None
            break
    if value is unset:
        return None
    if isinstance(value, tuple | list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _down_revision_at_head(worktree: str, path: str) -> str | None:
    """Read the revision file as the worktree's head has it, not as the disk has it."""
    rc, out = _git(worktree, "show", f"HEAD:{path}")
    return parse_down_revision(out) if rc == 0 else None


def added_migrations(conn, task, *, glob: str | None = None) -> list[AddedMigration]:
    """The migration files this task's worktree adds, relative to its own base.

    Added files only (``--diff-filter=A``): editing an existing revision is not
    what puts a second head on the graph. A task with no worktree, no base, or a
    worktree that has since been reclaimed contributes nothing rather than raising
    — this is an advisory, and it never gets to be the reason a dispatch failed.
    """
    worktree, base = task["worktree_path"], task["base_sha"]
    if not worktree or not base:
        return []
    if glob is None:
        glob = glob_for_repo_id(conn, task["repo_id"])
    rc, out = _git(worktree, "diff", "--name-only", "--diff-filter=A", f"{base}..HEAD")
    if rc != 0:
        return []
    found: list[AddedMigration] = []
    for line in out.splitlines():
        path = line.strip()
        if not path or not matches(path, glob):
            continue
        found.append(
            AddedMigration(
                task_id=int(task["id"]),
                title=task["title"],
                status=task["status"],
                branch=task["branch"],
                path=path,
                down_revision=_down_revision_at_head(worktree, path),
            )
        )
    return found


def _sibling_tasks(conn, repo_id, *, exclude: set[int], include_delivered: bool) -> list:
    """Other tasks in this repository whose migration could still collide.

    In flight means any non-terminal status. ``include_delivered`` adds a task
    that is delivered but not merged: its pull request is open, its migration has
    not landed, and it is exactly as able to produce a second head as one still
    being written.
    """
    if not repo_id:
        return []
    rows = conn.execute("SELECT * FROM tasks WHERE repo_id = ? ORDER BY id", (repo_id,)).fetchall()
    kept = []
    for row in rows:
        if int(row["id"]) in exclude:
            continue
        status = row["status"]
        if status in TERMINAL_STATUSES and not (
            include_delivered and status == "delivered" and not row["merged_at"]
        ):
            continue
        kept.append(row)
    return kept


def in_flight_migrations(
    conn, repo_id, *, exclude: set[int] | None = None, include_delivered: bool = False
) -> list[AddedMigration]:
    """Every migration the other live tasks in this repository already add."""
    glob = glob_for_repo_id(conn, repo_id)
    found: list[AddedMigration] = []
    for row in _sibling_tasks(
        conn, repo_id, exclude=exclude or set(), include_delivered=include_delivered
    ):
        found.extend(added_migrations(conn, row, glob=glob))
    return found


def dispatch_advisory(conn, repo_row, *, stack_on: int | None = None) -> str | None:
    """What to tell the manager, at dispatch, about migrations already in flight.

    A task this dispatch is stacking on is not reported: its migration is about to
    be part of the new worker's starting commit, which is the whole point of
    ``--stack-on`` and the opposite of a second head.
    """
    exclude = {int(stack_on)} if stack_on else set()
    others = in_flight_migrations(conn, repo_row["id"], exclude=exclude)
    if not others:
        return None
    lines = [
        "migration advisory: work already in flight in this repository adds a database "
        "migration, and a second one started from the same head leaves two Alembic heads "
        "when both merge."
    ]
    for other in others:
        parent = f" (down_revision {other.down_revision!r})" if other.down_revision else ""
        lines.append(f"  {other.describe()}{parent}")
    suggestions = ", ".join(f"--stack-on {other.task_id}" for other in _unique_tasks(others))
    lines.append(
        "  Nothing was refused. To build on that work instead of beside it, dispatch with "
        f"{suggestions}."
    )
    return "\n".join(lines)


def _unique_tasks(found: list[AddedMigration]) -> list[AddedMigration]:
    seen: set[int] = set()
    out = []
    for item in found:
        if item.task_id in seen:
            continue
        seen.add(item.task_id)
        out.append(item)
    return out


def review_conflicts(conn, task) -> list[tuple[AddedMigration, AddedMigration]]:
    """Pairs of (this task's migration, another task's) that share a parent revision.

    This is the two-heads condition itself, not a guess about one: both diffs
    exist, both name a ``down_revision``, and the two are the same string.
    """
    glob = glob_for_repo_id(conn, task["repo_id"])
    mine = [m for m in added_migrations(conn, task, glob=glob) if m.down_revision]
    if not mine:
        return []
    others = in_flight_migrations(
        conn, task["repo_id"], exclude={int(task["id"])}, include_delivered=True
    )
    return [(m, other) for m in mine for other in others if other.down_revision == m.down_revision]


def review_flag(conn, task) -> str | None:
    """The lines `mm review show` prints above the diffstat, or None when clear."""
    conflicts = review_conflicts(conn, task)
    if not conflicts:
        return None
    lines = [
        "MIGRATION COLLISION: this change adds a migration off a revision another "
        "unmerged task in this repository also builds on. Both merging leaves two "
        "Alembic heads."
    ]
    for mine, other in conflicts:
        lines.append(
            f"  {mine.path} here and {other.path} on {other.task_label} both declare "
            f"down_revision {mine.down_revision!r}."
        )
    lines.append(
        "  Sequence them before delivering — rebase one onto the other, or give the "
        "later one the earlier revision as its parent."
    )
    return "\n".join(lines)
