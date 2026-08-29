"""Repository registration (`mm repo add|list|sync`).

Registers repositories as read-only base clones under ``.mm/repos/``. A remote
URL is cloned; an existing local path is cloned locally so the base clone stays
isolated from the user's working checkout. Task worktrees (M2) branch from these
bases via the worktree lease manager.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from middle_manager.paths import ensure_layout, repos_dir
from middle_manager.state import init_db, store


class RepoError(Exception):
    pass


@dataclass
class AddedRepo:
    name: str
    origin: str
    local_path: str
    default_branch: str | None
    base_sha: str | None


def _git(args: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


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
    return AddedRepo(repo_name, source, str(dest), default_branch, base_sha)


def list_repos() -> list[dict]:
    conn = init_db()
    return [dict(r) for r in store.list_repos(conn)]


def sync_repo(name: str) -> str:
    conn = init_db()
    row = store.get_repo(conn, name)
    if row is None:
        raise RepoError(f"repo {name!r} is not registered")
    _git(["fetch", "--quiet", "--all"], cwd=row["local_path"])
    base_sha = _git(["rev-parse", "HEAD"], cwd=row["local_path"])
    store.update_repo_base_sha(conn, name, base_sha)
    return base_sha
