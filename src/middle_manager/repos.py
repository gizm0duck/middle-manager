"""Repository registration (`mm repo add|list|sync`).

Registers repositories as read-only base clones under ``.mm/repos/``. A remote
URL is cloned; an existing local path is cloned locally so the base clone stays
isolated from the user's working checkout. Task worktrees (M2) branch from these
bases via the worktree lease manager.

``sync`` is the command that keeps a base clone honest. It used to fetch and then
record the base clone's *local* ``HEAD``, which never moved — so a base clone that
had been registered weeks earlier kept reporting a stale commit and every worker
dispatched without an explicit starting branch began from it (2026-08-31: a
backend base clone sat on a local `main` far behind the remote). Sync now
fast-forwards the base clone's default branch to the remote tip and records
*that* commit, and it refuses to touch a base clone somebody has been editing by
hand.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from middle_manager.memory import seed_repo_memory
from middle_manager.paths import ensure_layout, repos_dir
from middle_manager.state import init_db, store

# A base clone is not a workspace, but `mm`'s own state directory can end up
# inside one: ``mm_home()`` falls back to ``<cwd>/.mm`` when ``MM_HOME`` is unset,
# and ``ensure_layout()`` then creates the whole tree wherever the command ran.
# The ``bin/mm`` launcher exports ``MM_HOME``, so this only happens when ``mm`` is
# invoked another way (``python -m middle_manager``, or a ``mm`` shim that is not
# the launcher) from inside a base clone — which is exactly what a `cd` into
# ``.mm/repos/<name>` followed by a bare ``mm ...`` does. The directory is empty
# state nobody reads; sync reports it and ``--clean-stray-mm`` removes it.
STRAY_MM_CAUSE = (
    "an `mm` command was run with this directory as its working directory without "
    "MM_HOME set (the bin/mm launcher always sets it, so this is a bare `mm` or "
    "`python -m middle_manager` after a `cd` into the base clone); mm then created "
    "its state tree here instead of in the instance home"
)


class RepoError(Exception):
    pass


@dataclass
class AddedRepo:
    name: str
    origin: str
    local_path: str
    default_branch: str | None
    base_sha: str | None


@dataclass
class SyncResult:
    """What one ``mm repo sync`` did, in enough detail to report it plainly."""

    name: str
    default_branch: str | None
    base_sha: str
    previous_sha: str | None
    fast_forwarded: bool
    remote: str = "origin"
    already_current: bool = False
    stray_mm: bool = False
    stray_mm_removed: bool = False
    notes: list[str] = field(default_factory=list)


def _git(args: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _git_ok(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def derive_name(url_or_path: str) -> str:
    base = url_or_path.rstrip("/")
    base = re.sub(r"\.git$", "", base)
    name = base.split("/")[-1]
    if ":" in name and "/" not in name:  # owner:repo edge
        name = name.split(":")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    if not name:
        raise RepoError(f"could not derive a repo name from {url_or_path!r}")
    return name


def _resolve_source(url_or_path: str) -> str:
    """Expand a local path to an absolute path; leave URLs untouched."""
    if os.path.exists(os.path.expanduser(url_or_path)):
        return os.path.abspath(os.path.expanduser(url_or_path))
    return url_or_path


def add_repo(url_or_path: str, name: str | None = None) -> AddedRepo:
    ensure_layout()
    conn = init_db()
    repo_name = name or derive_name(url_or_path)
    if store.get_repo(conn, repo_name) is not None:
        raise RepoError(f"repo {repo_name!r} is already registered")

    source = _resolve_source(url_or_path)
    dest = repos_dir() / repo_name
    if dest.exists():
        raise RepoError(f"destination {dest} already exists")

    _git(["clone", "--quiet", source, str(dest)])

    default_branch: str | None
    try:
        default_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(dest))
    except RepoError:
        default_branch = None
    try:
        base_sha = _git(["rev-parse", "HEAD"], cwd=str(dest))
    except RepoError:
        base_sha = None

    store.add_repo(
        conn,
        name=repo_name,
        origin=source,
        local_path=str(dest),
        default_branch=default_branch,
        base_sha=base_sha,
    )
    # Seed per-repo memory so learnings about this repo persist across sessions.
    seed_repo_memory(repo_name, origin=source, default_branch=default_branch)
    return AddedRepo(repo_name, source, str(dest), default_branch, base_sha)


def list_repos() -> list[dict]:
    conn = init_db()
    return [dict(r) for r in store.list_repos(conn)]


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def dirty_paths(local_path: str) -> list[str]:
    """Uncommitted or untracked paths in a base clone, ignoring a stray ``.mm/``.

    A base clone is meant to be read-only: worktrees branch from it, nobody edits
    it. Anything here is either a hand edit that a fast-forward would clobber, or
    build output nobody meant to keep — both are reasons to stop and say so. The
    one exception is ``.mm/``, which is machine state accidentally created inside
    the clone (see ``STRAY_MM_CAUSE``) and is reported separately.
    """
    proc = subprocess.run(
        ["git", "-C", local_path, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RepoError(f"{local_path} is not a readable git clone: {proc.stderr.strip()}")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        # Porcelain v1 lines are `XY <path>`; a rename is `XY <old> -> <new>`.
        path = line[2:].strip().split(" -> ")[-1].strip('"')
        if not path or path == ".mm" or path.startswith(".mm/"):
            continue
        paths.append(path)
    return sorted(set(paths))


def stray_mm_dir(local_path: str) -> Path | None:
    """The stray ``.mm`` directory inside a base clone, if one is there."""
    candidate = Path(local_path) / ".mm"
    return candidate if candidate.is_dir() else None


def _default_branch(conn, row) -> str:
    recorded = row["default_branch"]
    if recorded:
        return str(recorded)
    rc, out, _err = _git_ok(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=row["local_path"]
    )
    if rc == 0 and out:
        return out.split("/", 1)[-1]
    rc, out, _err = _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=row["local_path"])
    if rc == 0 and out and out != "HEAD":
        return out
    raise RepoError(
        f"repo {row['name']!r} has no default branch on record and none could be read "
        "from the clone; re-register it or set one"
    )


def _fast_forward(local_path: str, branch: str, remote: str) -> tuple[str, bool]:
    """Move the base clone's ``branch`` to ``<remote>/<branch>``. Never rewrites.

    Returns the resulting commit and whether the branch actually moved. A branch
    that has drifted away from the remote (someone committed in the base clone)
    cannot fast-forward: that is refused rather than reset, because a reset here
    would silently destroy the only copy of those commits.
    """
    target_rc, target, _err = _git_ok(["rev-parse", f"{remote}/{branch}"], cwd=local_path)
    if target_rc != 0 or not target:
        raise RepoError(
            f"{remote}/{branch} does not exist in the base clone after fetching; "
            f"is {branch!r} still the default branch?"
        )
    current_rc, current_branch, _err = _git_ok(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=local_path
    )
    before_rc, before, _err = _git_ok(["rev-parse", branch], cwd=local_path)
    before = before if before_rc == 0 else ""

    if before == target:
        return target, False

    if current_rc == 0 and current_branch == branch:
        rc, _out, err = _git_ok(["merge", "--ff-only", "--quiet", f"{remote}/{branch}"], local_path)
    else:
        # Not checked out here: update the ref directly. `git fetch <remote>
        # <branch>:<branch>` is fast-forward-only for a branch that is not HEAD,
        # so a diverged base clone fails instead of being rewritten.
        rc, _out, err = _git_ok(["fetch", "--quiet", remote, f"{branch}:{branch}"], local_path)
    if rc != 0:
        raise RepoError(
            f"the base clone's {branch!r} cannot fast-forward to {remote}/{branch} "
            f"({(before or '?')[:8]} vs {target[:8]}) — it holds commits the remote does "
            f"not: {err or 'non-fast-forward'}"
        )
    after = _git(["rev-parse", branch], cwd=local_path)
    return after, after != before


def sync_repo(name: str, *, clean_stray_mm: bool = False) -> SyncResult:
    """Fetch, fast-forward the base clone's default branch, and record that commit.

    Refuses (changing nothing) when the base clone has uncommitted changes or
    untracked files outside ``.mm/`` — a fast-forward over a hand edit is how work
    disappears — and reports a stray ``.mm/`` directory so it can be cleaned.
    """
    conn = init_db()
    row = store.get_repo(conn, name)
    if row is None:
        raise RepoError(f"repo {name!r} is not registered")
    local_path = row["local_path"]
    previous = row["base_sha"]

    dirty = dirty_paths(local_path)
    if dirty:
        shown = ", ".join(dirty[:10])
        more = f" (+{len(dirty) - 10} more)" if len(dirty) > 10 else ""
        raise RepoError(
            f"refusing to sync {name!r}: the base clone has uncommitted changes or "
            f"untracked files — {shown}{more}. A base clone is read-only; move or "
            "remove these, then sync again."
        )

    _git(["fetch", "--quiet", "--prune", "origin"], cwd=local_path)
    branch = _default_branch(conn, row)
    base_sha, moved = _fast_forward(local_path, branch, "origin")
    store.update_repo_base_sha(conn, name, base_sha)
    if row["default_branch"] != branch:
        store.update_repo_fields(conn, name, default_branch=branch)

    result = SyncResult(
        name=name,
        default_branch=branch,
        base_sha=base_sha,
        previous_sha=previous,
        fast_forwarded=moved,
        already_current=not moved,
    )

    stray = stray_mm_dir(local_path)
    if stray is not None:
        result.stray_mm = True
        if clean_stray_mm:
            shutil.rmtree(stray, ignore_errors=True)
            result.stray_mm_removed = not stray.exists()
            result.notes.append(f"removed the stray {stray} directory")
        else:
            result.notes.append(
                f"a stray {stray} directory is sitting inside the base clone: "
                f"{STRAY_MM_CAUSE}. Remove it with `mm repo sync {name} --clean-stray-mm`."
            )
    return result
