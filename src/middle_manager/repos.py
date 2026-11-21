"""Repository registration (`mm repo add|list|sync`).

Registers repositories as read-only base clones under ``.mm/repos/``. The base
clone is always cloned from the repository's forge: a local path is only a way to
name the repository, read for its forge URL and never used as a remote, because a
clone whose ``origin`` is the user's checkout pushes task branches into it
(2026-09-24: a registration from 2026-09-02 did exactly that). Task worktrees (M2) branch from these
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

import json
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


# Where a pull request can actually be opened. A repo registered from a local
# path has a local `origin`, which is no forge at all: on 2026-09-04 a worker on
# a path-registered repo pushed fine and `mm deliver` had nowhere to open the
# pull request, so the manager pushed to GitHub and opened it by hand.
_FORGE_URL_PATTERNS = (
    re.compile(r"^https://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
)

# The second remote a base clone gets when its own `origin` is not the forge.
# Rewriting `origin` would break the clone's relationship with the local checkout
# it was cloned from (and every worktree already leased off it), so the forge is
# added alongside it under a name nothing else uses. Worktrees share the base
# clone's remote configuration, so a worker's worktree can fetch and push there
# without any extra setup.
FORGE_REMOTE = "forge"


class RepoError(Exception):
    pass


def forge_slug(url: str | None) -> str | None:
    """``owner/name`` for a GitHub URL, else None. This is what ``gh --repo`` wants.

    An install explicitly configured for Bitbucket also reads bitbucket.org URLs as
    ``workspace/repo`` (what ``twg bb --workspace --repo`` wants); otherwise they are
    no forge at all, exactly as before.
    """
    if not url:
        return None
    for pattern in _FORGE_URL_PATTERNS:
        match = pattern.match(url.strip())
        if match:
            return f"{match.group('owner')}/{match.group('name')}"
    from middle_manager import forge

    if forge.bitbucket_slug(url) and forge.is_bitbucket():
        return forge.bitbucket_slug(url)
    return None


def is_forge_url(url: str | None) -> bool:
    """True where a pull request can be opened: GitHub, or Bitbucket when configured for it."""
    return forge_slug(url) is not None


# A remote that is not a path on this filesystem: anything carrying a URL scheme
# (`https://`, `ssh://`, `file://`, ...) or written scp-style (`git@host:owner/x`).
_REMOTE_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_SCP_LIKE = re.compile(r"^[^/@]+@[^/:]+:")


def is_local_remote(url: str | None) -> bool:
    """True when a git remote URL is a plain path on this machine.

    The fake provider is only safe against a local remote: it writes a stub and
    pushes it, which on a real forge is a junk branch strangers can see (issue
    #49). Anything with a scheme or an scp-style ``user@host:`` prefix is off the
    machine; a remote that cannot be read at all counts as non-local, because
    "unknown" is not a reason to push.
    """
    if not url or not url.strip():
        return False
    candidate = url.strip()
    return not (_REMOTE_SCHEME.match(candidate) or _SCP_LIKE.match(candidate))


@dataclass
class AddedRepo:
    name: str
    origin: str
    local_path: str
    default_branch: str | None
    base_sha: str | None
    forge_url: str | None = None
    notes: list[str] = field(default_factory=list)
    #: Cone directories of a sparse checkout; None is a full checkout.
    sparse_paths: list[str] | None = None


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


def remote_url(path: str, remote: str = "origin") -> str | None:
    """A git repository's URL for ``remote``, or None when it has none."""
    if not os.path.isdir(path):
        return None
    rc, out, _err = _git_ok(["remote", "get-url", remote], cwd=path)
    return out or None if rc == 0 else None


def ensure_forge_remote(local_path: str, forge_url: str | None) -> str:
    """Make the forge reachable from the base clone, and name the remote to use.

    When the clone's own ``origin`` is already the forge, that is the remote —
    nothing to add. Otherwise the forge is added as a *second* remote
    (``forge``), because rewriting ``origin`` would cut the clone off from the
    local checkout it was cloned from. Returns the remote name to fetch and push
    with.
    """
    if not forge_url:
        return "origin"
    origin = remote_url(local_path)
    if origin and origin.rstrip("/") == forge_url.rstrip("/"):
        return "origin"
    existing = remote_url(local_path, FORGE_REMOTE)
    if existing is None:
        _git(["remote", "add", FORGE_REMOTE, forge_url], cwd=local_path)
    elif existing.rstrip("/") != forge_url.rstrip("/"):
        _git(["remote", "set-url", FORGE_REMOTE, forge_url], cwd=local_path)
    return FORGE_REMOTE


def forge_default_branch(local_path: str, remote: str) -> str | None:
    """The default branch the forge itself names, read from ``remote``'s own HEAD.

    ``git ls-remote --symref`` asks the remote, not the clone: a base clone's
    ``refs/remotes/origin/HEAD`` is whatever it was at clone time — for a clone of a
    local checkout, the branch that checkout happened to be on — and on 2026-09-23
    it still named ``feat/repo-picker`` when GitHub's default was ``main``. None
    when the remote cannot be reached or names no HEAD.
    """
    proc = subprocess.run(
        ["git", "ls-remote", "--symref", remote, "HEAD"],
        cwd=local_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        ref, _sep, name = line.partition("\t")
        if ref.startswith("ref: refs/heads/") and name.strip() == "HEAD":
            return ref.removeprefix("ref: refs/heads/").strip() or None
    return None


def point_remote_heads(local_path: str, branch: str, remotes: list[str]) -> list[str]:
    """Make each remote's ``HEAD`` in the base clone name ``branch``; notes for any that can't.

    Tools that are not told a base read ``refs/remotes/origin/HEAD`` (``gh stack link``
    did, and retargeted a stack's bottom pull request onto a stale feature branch).
    """
    notes = []
    for remote in dict.fromkeys(remotes):
        rc, _out, err = _git_ok(["remote", "set-head", remote, branch], cwd=local_path)
        if rc != 0:
            notes.append(f"could not point {remote}/HEAD at {branch}: {err or 'git refused'}")
    return notes


def remote_head(local_path: str, remote: str) -> str | None:
    """The branch the base clone's ``refs/remotes/<remote>/HEAD`` names, if it has one."""
    rc, out, _err = _git_ok(
        ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd=local_path
    )
    return out.split("/", 1)[-1] if rc == 0 and out else None


def upstream_remote(row) -> str:
    """The remote a registered repo's work actually travels through, or a refusal.

    Every fetch-for-delivery and every push resolves its remote here, so this is
    where mm refuses to write into a checkout outside ``.mm/``: a remote that is a
    path on this machine is allowed only when it is the repo's registered forge
    (which the CLI accepts only as a forge URL).
    """
    forge = row["forge_url"] if "forge_url" in row.keys() else None  # noqa: SIM118 - sqlite3.Row
    remote = "origin" if not forge else ensure_forge_remote(row["local_path"], forge)
    url = remote_url(row["local_path"], remote)
    if is_local_remote(url) and not _same_remote(url, forge):
        raise RepoError(
            f"repo {row['name']!r} would push to {url}, a checkout on this machine, not a "
            "forge. mm never writes into a checkout outside .mm/; register the repository "
            "from its forge URL instead (`mm repo add https://github.com/<owner>/<repo>`)."
        )
    return remote


def _same_remote(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return _normalized(a) == _normalized(b)


def _normalized(url: str) -> str:
    if is_local_remote(url):
        return os.path.abspath(os.path.expanduser(url))
    return url.rstrip("/").removesuffix(".git")


def _registered_under(conn, forge_url: str) -> str | None:
    """The name another registration already uses for this forge repository, if any."""
    slug = forge_slug(forge_url)
    for row in store.list_repos(conn):
        other = row["forge_url"] or row["origin"]
        if (slug and forge_slug(other) == slug) or _same_remote(other, forge_url):
            return row["name"]
    return None


def _resolve_forge_url(source: str, forge_url: str | None) -> str:
    """Decide where this repo's pull requests will be opened, or refuse.

    An explicit ``--forge-url`` always wins. Otherwise a forge URL registered
    directly is its own forge, and a local path inherits the forge from its own
    ``origin``. A local path whose origin is another local path — or which has no
    origin at all — has no forge, and registering it silently is what left a
    delivered task with nowhere to open a pull request.
    """
    if forge_url:
        return forge_url.strip()
    if is_forge_url(source):
        return source
    origin = remote_url(source) if os.path.isdir(source) else None
    if is_forge_url(origin):
        assert origin is not None
        return origin
    detail = f"its origin is {origin!r}" if origin else "it has no origin remote"
    raise RepoError(
        f"{source} has no forge to open pull requests against ({detail}). Register it "
        "with `mm repo add <path> --forge-url https://github.com/<owner>/<repo>` so "
        "delivery knows where the pull request goes."
    )


# --------------------------------------------------------------------------- #
# Sparse checkout
# --------------------------------------------------------------------------- #

#: How a sparse repository's base clone is cloned: blobs arrive on demand, so a
#: large repository costs only the directories it materializes. Best-effort: a
#: source that cannot filter falls back to a plain ``--sparse`` clone.
SPARSE_CLONE_FLAGS = ("--filter=blob:none", "--sparse")


def normalize_sparse_paths(paths: list[str] | None) -> list[str] | None:
    """Validated cone directories, or None for a full checkout.

    An empty list is valid: cone mode with only the repository's root files.
    """
    if paths is None:
        return None
    cleaned: list[str] = []
    for raw in paths:
        text = (raw or "").strip()
        path = text.strip("/")
        if not path or text.startswith("/") or os.path.isabs(text):
            raise RepoError(
                f"--sparse wants directories inside the repo, like src/app; got {raw!r}"
            )
        if any(part in ("..", ".") for part in path.split("/")):
            raise RepoError(f"--sparse directories cannot contain '..' or '.'; got {raw!r}")
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def sparse_paths_of(row) -> list[str] | None:
    """A registered repo's sparse cone directories, or None for a full checkout."""
    if row is None or "sparse_paths" not in row.keys():  # noqa: SIM118
        return None
    raw = row["sparse_paths"]
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return [str(item) for item in value] if isinstance(value, list) else None


def describe_sparse(paths: list[str] | None) -> str:
    if paths is None:
        return "full checkout"
    return "sparse: " + (", ".join(paths) if paths else "root files only")


def apply_sparse(path: str, paths: list[str]) -> str | None:
    """Put a checkout in cone mode on exactly ``paths``. Returns why it failed, or None.

    Idempotent, so a reused pool worktree gets the same answer every time. The
    result is read back: a checkout that is not sparse on these paths afterwards is
    a failure, never a silent full checkout.
    """
    proc = subprocess.run(
        ["git", "-C", path, "sparse-checkout", "set", "--cone", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return f"git sparse-checkout set failed: {(proc.stderr or proc.stdout).strip()[:240]}"
    return verify_sparse(path, paths)


def verify_sparse(path: str, paths: list[str]) -> str | None:
    """Why ``path`` is not sparse on exactly ``paths``, or None when it is."""
    enabled = subprocess.run(
        ["git", "-C", path, "config", "--get", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if enabled != "true":
        return "sparse checkout is not enabled; the whole repository is checked out"
    listed = subprocess.run(
        ["git", "-C", path, "sparse-checkout", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    have = sorted(line.strip() for line in listed.stdout.splitlines() if line.strip())
    if listed.returncode != 0 or have != sorted(paths):
        return f"sparse directories are {have or 'root files only'}, expected {sorted(paths)}"
    return None


def disable_sparse(path: str) -> str | None:
    """Turn a checkout back into a full one. Returns why it failed, or None."""
    proc = subprocess.run(
        ["git", "-C", path, "sparse-checkout", "disable"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return f"git sparse-checkout disable failed: {(proc.stderr or proc.stdout).strip()[:240]}"
    return None


def _clone(
    source: str,
    dest: Path,
    sparse: list[str] | None,
    notes: list[str],
    depth: int | None = None,
) -> None:
    """Clone the base clone; a sparse one filtered and in cone mode on ``sparse``.

    ``depth`` makes it a shallow clone of the default branch's last ``depth``
    commits. That is what makes a huge repository usable at all: on 2026-11-18 a
    partial clone of a large monorepo ran 18 minutes and failed fetching
    the full commit history, while the latest commit plus a sparse checkout took
    under a minute and a half. A shallow clone that falls back keeps its depth: the
    fallback never quietly downloads the full history a depth was asked to avoid.
    """
    shallow = ["--depth", str(depth)] if depth is not None else []
    if sparse is None:
        _git(["clone", "--quiet", *shallow, source, str(dest)])
        if depth is not None:
            notes.append(_describe_depth(depth))
        return
    rc, _out, err = _git_ok(["clone", "--quiet", *SPARSE_CLONE_FLAGS, *shallow, source, str(dest)])
    if rc != 0:
        shutil.rmtree(dest, ignore_errors=True)
        _git(["clone", "--quiet", "--sparse", *shallow, source, str(dest)])
        history = f"the last {depth} commit(s)" if depth is not None else "full history"
        notes.append(
            f"partial clone refused by the source; cloned {history} with a sparse "
            f"checkout instead ({err or 'git refused --filter'})"
        )
    elif "filter" in err.lower() and "ignor" in err.lower():
        notes.append("the source cannot filter blobs; sparse checkout only, no partial clone")
    failed = apply_sparse(str(dest), sparse)
    if failed:
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoError(f"could not make the base clone sparse: {failed}")
    notes.append(f"{describe_sparse(sparse)} (workers widen with `git sparse-checkout add <dir>`)")
    if depth is not None:
        notes.append(_describe_depth(depth))


def _describe_depth(depth: int) -> str:
    return (
        f"shallow: the last {depth} commit(s) of the default branch "
        "(deepen with `git fetch --deepen=<n>` in the base clone if a task needs history)"
    )


def is_shallow(path: str) -> bool:
    """Whether ``path`` is a shallow clone, or a worktree of one (they share its shallow file)."""
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def shallow_fetch_args(path: str) -> list[str]:
    """``["--depth", "1"]`` for a fetch of the default branch's tip in a shallow clone.

    The base clone only needs the default branch's latest commit, and a plain
    ``git fetch`` there can walk far back into the history the clone exists to avoid:
    on 2026-11-18 ``git fetch --depth 1`` of a large monorepo's default branch took 5
    seconds. Only the default branch is fetched this way. Any other branch goes
    through :func:`fetch_branch`, because a depth on a branch whose history is
    already here cuts that history off. Always follow the fetch with
    :func:`reconnect_shallow`. ``[]`` for a full clone.
    """
    return ["--depth", "1"] if is_shallow(path) else []


def _shallow_file(path: str) -> Path | None:
    rc, rel, _err = _git_ok(["rev-parse", "--git-path", "shallow"], cwd=path)
    if rc != 0 or not rel:
        return None
    return Path(rel) if Path(rel).is_absolute() else Path(path) / rel


def reconnect_shallow(path: str) -> list[str]:
    """Clear the shallow mark from every commit whose parents are all here. Returns them.

    git marks the deepest commit of every depth-limited fetch as shallow, even when
    its parents are already in the clone, and even when that commit was here before
    the fetch. From then on git hides those parents, for every worktree of the
    clone: a branch's own history ends at that commit, ``merge-base`` finds nothing,
    and the commit's patch is the whole tree. A mark is only right where the parents
    are actually missing, so every other mark is removed. Best effort: when another
    git process holds the shallow file's lock, nothing is changed.
    """
    shallow_file = _shallow_file(path)
    if shallow_file is None or not shallow_file.exists():
        return []
    marked = [line.strip() for line in shallow_file.read_text().split() if line.strip()]
    connected = [sha for sha in marked if _parents_present(path, sha)]
    if not connected:
        return []
    lock = shallow_file.with_name(shallow_file.name + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return []
    try:
        kept = [sha for sha in marked if sha not in connected]
        os.write(fd, "".join(f"{sha}\n" for sha in kept).encode())
        os.close(fd)
        if kept:
            os.replace(lock, shallow_file)
        else:
            lock.unlink()
            shallow_file.unlink()
    except OSError:
        lock.unlink(missing_ok=True)
        return []
    return connected


def _parents_present(path: str, sha: str) -> bool:
    """Whether every parent of ``sha`` is in the local object store (its raw object, no grafts)."""
    rc, raw, _err = _git_ok(["cat-file", "commit", sha], cwd=path)
    if rc != 0:
        return False
    parents = []
    for line in raw.splitlines():
        if not line:
            break
        if line.startswith("parent "):
            parents.append(line.split()[1])
    return bool(parents) and all(_has_commit_locally(path, parent) for parent in parents)


#: The depths :func:`fetch_branch` tries, in order, to reach history the clone holds.
CONNECT_DEPTHS = (8, 64, 256)


def fetch_branch(path: str, remote: str, branch: str) -> tuple[str | None, str]:
    """Fetch ``branch`` from ``remote`` and return ``(its tip, "")``, or ``(None, why)``.

    A full clone gets a plain fetch, exactly as before. A shallow clone needs more
    care, because neither plain fetch is right for it:

    - a ``--depth`` cuts off history that is already here. A task's own branch, a
      stack's parent and a lease branch are all built on commits the clone holds, and
      once their history is cut a stack can't find its merge base and delivery's
      carry check sees no commits at all;
    - a plain fetch of a branch that forks below the clone's shallow boundary pulls
      in that branch's entire history.

    So a tip that is already here isn't fetched at all. Otherwise the branch is
    fetched at growing depths (:data:`CONNECT_DEPTHS`) until its new commits reach
    commits the clone holds, and :func:`reconnect_shallow` joins them up. A branch
    that is still not connected at the deepest try forks too far below the boundary.
    It stays cut there, as its own fresh starting point.
    """
    if not Path(path).is_dir():
        return None, f"{path} is not a directory"
    if not is_shallow(path):
        rc, _out, err = _git_ok(["fetch", "--quiet", remote, branch], cwd=path)
        if rc != 0:
            return None, err or f"git fetch {remote} {branch} failed"
        rc, sha, err = _git_ok(["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], path)
        return (sha, "") if rc == 0 and sha else (None, err or "FETCH_HEAD is not a commit")
    rc, out, err = _git_ok(["ls-remote", remote, f"refs/heads/{branch}"], cwd=path)
    if rc != 0:
        return None, err or f"git ls-remote {remote} failed"
    words = out.split()
    if not words:
        return None, f"{remote} has no branch {branch!r}"
    sha = words[0]
    if _has_commit_locally(path, sha):
        return sha, ""
    before = shallow_roots(path)
    for depth in CONNECT_DEPTHS:
        rc, _out, err = _git_ok(
            ["fetch", "--quiet", "--no-tags", f"--depth={depth}", remote, f"refs/heads/{branch}"],
            cwd=path,
        )
        if rc != 0:
            return None, err or f"git fetch {remote} {branch} failed"
        reconnect_shallow(path)
        if not shallow_roots(path) - before:
            break
    return sha, ""


def shallow_roots(path: str) -> set[str]:
    """The commits a shallow clone (or a worktree of one) holds without their parents.

    git diffs such a commit against nothing, so its patch is the whole tree. In a
    partial clone with no file contents, asking for that patch fetches every blob
    in the repository: on 2026-11-20 ``mm deliver`` stalled for minutes on a large
    monorepo's base commit for exactly that reason. Empty for a
    full clone.
    """
    if not is_shallow(path):
        return set()
    shallow_file = _shallow_file(path)
    if shallow_file is None:
        return set()
    try:
        lines = shallow_file.read_text().split()
    except OSError:
        return set()
    return {line.strip() for line in lines if line.strip()}


def _has_commit_locally(path: str, sha: str) -> bool:
    """Whether ``sha`` is in the local object store, without asking the promisor remote."""
    proc = subprocess.run(
        ["git", "-C", str(path), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    return proc.returncode == 0


def prepare_shallow_push(
    path: str, remote: str, branches: list[str], *, attempts: int = 3
) -> list[str]:
    """Make sure the remote's current tips of ``branches`` exist locally before a push.

    A push only leaves out objects the server already has when a commit the server
    advertises also exists locally. A shallow clone holds none of the server's
    refs, so on 2026-11-20 a one-commit push from a shallow clone of a large
    monorepo uploaded the whole tree (92,000 objects, 123 MB). A busy default
    branch moves every few minutes, so a tip fetched earlier is usually stale by push time. With the
    current tip fetched right before the push, the same push is 20 objects. Fetches
    stay shallow and filtered. Returns the branches it fetched. No-op for a full clone.
    """
    if not is_shallow(path):
        return []
    wanted = [b for b in dict.fromkeys(branches) if b]
    fetched: list[str] = []
    for _ in range(attempts):
        rc, out, _err = _git_ok(
            ["ls-remote", remote, *(f"refs/heads/{b}" for b in wanted)], cwd=path
        )
        if rc != 0:
            return fetched
        missing = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and not _has_commit_locally(path, parts[0]):
                missing.append(parts[1].removeprefix("refs/heads/"))
        if not missing:
            return fetched
        rc, _out, _err = _git_ok(
            [
                "fetch",
                "--depth",
                "1",
                "--no-tags",
                "--no-write-fetch-head",
                remote,
                *(f"refs/heads/{b}" for b in missing),
            ],
            cwd=path,
        )
        if rc != 0:
            return fetched
        reconnect_shallow(path)
        fetched.extend(b for b in missing if b not in fetched)
    return fetched


def align_shallow_remote(local_path: str, remote: str, branch: str) -> None:
    """Make ``remote`` fetch only ``branch``, filtered like ``origin``, in a shallow clone.

    A clone made with ``--depth`` is single-branch, but a second remote mm adds
    (``forge``, when the clone's origin URL differs from the registered forge URL
    only by host alias) gets git's default ``refs/heads/*`` refspec and no
    partial-clone filter. One fetch from it then pulls a commit for every branch,
    with file contents: on 2026-11-18 that ran 25 minutes on
    a large monorepo before it was stopped. No-op for a full clone.
    """
    if not is_shallow(local_path):
        return
    _git(
        [
            "config",
            f"remote.{remote}.fetch",
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ],
        cwd=local_path,
    )
    rc, flt, _err = _git_ok(["config", "--get", "remote.origin.partialclonefilter"], cwd=local_path)
    if rc == 0 and flt and remote != "origin":
        _git(["config", f"remote.{remote}.promisor", "true"], cwd=local_path)
        _git(["config", f"remote.{remote}.partialclonefilter", flt], cwd=local_path)


def recorded_default_branch(conn, repo_id: int | None) -> str:
    """The default branch mm recorded for a registered repository; ``main`` when unknown."""
    if repo_id is None:
        return "main"
    row = conn.execute("SELECT default_branch FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "main"
    return (row["default_branch"] or "main").strip() or "main"


def normalize_depth(depth: int | None) -> int | None:
    if depth is None:
        return None
    if depth < 1:
        raise RepoError(f"--depth wants a number of commits, 1 or more; got {depth}")
    return depth


def add_repo(
    url_or_path: str,
    name: str | None = None,
    forge_url: str | None = None,
    sparse: list[str] | None = None,
    depth: int | None = None,
) -> AddedRepo:
    """Register a repository. ``sparse`` (cone directories) makes it a sparse checkout.

    ``sparse=None`` is a full clone, exactly as before; ``[]`` is root files only.
    ``depth`` makes the clone shallow (the last ``depth`` commits); None is full history.
    """
    sparse = normalize_sparse_paths(sparse)
    depth = normalize_depth(depth)
    ensure_layout()
    conn = init_db()
    repo_name = name or derive_name(url_or_path)
    if store.get_repo(conn, repo_name) is not None:
        raise RepoError(f"repo {repo_name!r} is already registered")

    source = _resolve_source(url_or_path)
    resolved_forge = _resolve_forge_url(source, forge_url)
    dest = repos_dir() / repo_name
    if dest.exists():
        raise RepoError(f"destination {dest} already exists")

    taken = _registered_under(conn, resolved_forge)
    if taken is not None:
        raise RepoError(f"{resolved_forge} is already registered as {taken!r}")

    # Clone from the forge, never from the local path: the clone's origin is where
    # task branches are pushed.
    notes: list[str] = []
    _clone(resolved_forge, dest, sparse, notes, depth=depth)
    remote = ensure_forge_remote(str(dest), resolved_forge)

    # The forge names the default branch; the clone's HEAD is only whatever the
    # source had checked out.
    default_branch = forge_default_branch(str(dest), remote)
    if default_branch:
        if remote != "origin":
            align_shallow_remote(str(dest), remote, default_branch)
            rc, _out, err = _git_ok(
                ["fetch", "--quiet", *shallow_fetch_args(str(dest)), remote, default_branch],
                cwd=str(dest),
            )
            reconnect_shallow(str(dest))
            if rc != 0:
                notes.append(f"could not fetch {remote}/{default_branch}: {err or 'git refused'}")
        notes.extend(point_remote_heads(str(dest), default_branch, [remote, "origin"]))
    else:
        notes.append(
            "could not read the forge's default branch; recorded the clone's branch instead "
            "(`mm repo sync` asks again)"
        )
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
        origin=resolved_forge,
        local_path=str(dest),
        default_branch=default_branch,
        base_sha=base_sha,
        forge_url=resolved_forge,
        sparse_paths=json.dumps(sparse) if sparse is not None else None,
    )
    # Seed per-repo memory so learnings about this repo persist across sessions.
    seed_repo_memory(repo_name, origin=resolved_forge, default_branch=default_branch)
    return AddedRepo(
        repo_name,
        resolved_forge,
        str(dest),
        default_branch,
        base_sha,
        resolved_forge,
        notes=notes,
        sparse_paths=sparse,
    )


def list_repos() -> list[dict]:
    conn = init_db()
    return [dict(r) for r in store.list_repos(conn)]


@dataclass
class ProvisionSettings:
    """What a repo wants done to a fresh worktree before its worker starts."""

    repo: str
    command: str | None
    reuse_venv: str | None

    @property
    def configured(self) -> bool:
        return bool(self.command or self.reuse_venv)

    def describe(self) -> str:
        if not self.configured:
            return f"{self.repo}: no worktree provisioning configured"
        parts = []
        if self.reuse_venv:
            parts.append(f"reuses {self.reuse_venv} from the base clone")
        if self.command:
            parts.append(f"runs `{self.command}` in each new worktree")
        return f"{self.repo}: " + ", and ".join(parts)


def _require_repo(name: str):
    conn = init_db()
    row = store.get_repo(conn, name)
    if row is None:
        raise RepoError(f"repo {name!r} is not registered")
    return conn, row


def get_provision(name: str) -> ProvisionSettings:
    _conn, row = _require_repo(name)
    keys = row.keys()
    return ProvisionSettings(
        repo=name,
        command=row["provision_command"] if "provision_command" in keys else None,
        reuse_venv=row["provision_venv"] if "provision_venv" in keys else None,
    )


@dataclass
class RepoSettings:
    """The per-repo knobs `mm repo set` owns, and where each value came from."""

    repo: str
    migrations_glob: str
    migrations_glob_is_default: bool
    # The environment block a worker is handed at dispatch (issue #60).
    environment: object = None
    # No CI at all: `mm stack merge` may land a layer with no checks.
    no_ci: bool = False
    # Cone directories of a sparse checkout; None is a full checkout.
    sparse_paths: list[str] | None = None
    # What changing the sparse setting did to the base clone, when it was changed.
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        origin = "the default" if self.migrations_glob_is_default else "set for this repo"
        lines = [f"{self.repo}: migrations live at {self.migrations_glob} ({origin})"]
        lines.append(f"  checkout: {describe_sparse(self.sparse_paths)}")
        lines.append(
            "  CI: none (set with --no-ci; a stack layer with no checks may merge)"
            if self.no_ci
            else "  CI: expected (a stack layer with no checks at all does not merge)"
        )
        if self.environment is not None:
            lines.extend(f"  {line}" for line in self.environment.describe())
        lines.extend(f"  {note}" for note in self.notes)
        return "\n".join(lines)


def get_settings(name: str) -> RepoSettings:
    from middle_manager import environment
    from middle_manager.migrations import DEFAULT_MIGRATIONS_GLOB, glob_for_repo

    _conn, row = _require_repo(name)
    glob = glob_for_repo(row)
    return RepoSettings(
        repo=name,
        migrations_glob=glob,
        migrations_glob_is_default=glob == DEFAULT_MIGRATIONS_GLOB,
        environment=environment.for_repo(row),
        no_ci=has_no_ci(row),
        sparse_paths=sparse_paths_of(row),
    )


def has_no_ci(row) -> bool:
    """Whether a registered repo was declared to have no CI (``mm repo set --no-ci``)."""
    return bool(row["no_ci"]) if row is not None and "no_ci" in row.keys() else False  # noqa: SIM118


def set_settings(
    name: str,
    *,
    migrations_glob: str | None = None,
    no_ci: str | bool | None = None,
    **environment_fields: object,
) -> RepoSettings:
    """Configure a repo's settings. An empty string puts a field back on the default.

    ``environment_fields`` are the environment-block columns (``compose_stack``,
    ``db_port_base``, ``db_port_variable``, ``push_hook_runs_full_suite``, ``local_gate``,
    ``full_suite_owner``, ``evidence_dir``, URL templates, source ceiling and localhost
    sandbox fact); a refused value raises ``RepoError``.
    """
    from middle_manager import environment

    conn, _row = _require_repo(name)
    if migrations_glob is not None:
        store.update_repo_fields(conn, name, migrations_glob=migrations_glob.strip() or None)
    try:
        if no_ci is not None:
            store.update_repo_fields(conn, name, no_ci=1 if environment.parse_bool(no_ci) else None)
        environment.set_fields(conn, name, **environment_fields)
    except environment.RepoEnvironmentError as exc:
        raise RepoError(str(exc)) from exc
    return get_settings(name)


def set_sparse(name: str, paths: list[str] | None) -> RepoSettings:
    """Make a registered repo sparse on ``paths`` (``[]``: root files only) or full (None).

    The base clone changes now; every worktree leased afterwards follows it (a
    pooled worktree is re-applied on each lease). Worktrees already leased keep the
    checkout they have.
    """
    paths = normalize_sparse_paths(paths)
    conn, row = _require_repo(name)
    local = row["local_path"]
    failed = apply_sparse(local, paths) if paths is not None else disable_sparse(local)
    if failed:
        raise RepoError(f"{name}: base clone unchanged in the record: {failed}")
    store.update_repo_fields(
        conn, name, sparse_paths=json.dumps(paths) if paths is not None else None
    )
    settings = get_settings(name)
    settings.notes.append(
        f"base clone is now a {describe_sparse(paths)}; worktrees leased from here on follow it"
    )
    return settings


def set_provision(
    name: str,
    *,
    command: str | None = None,
    reuse_venv: str | None = None,
    clear: bool = False,
) -> ProvisionSettings:
    """Configure (or clear) a repo's worktree provisioning.

    ``None`` leaves a field as it was; an empty string clears that one field, and
    ``clear`` clears both — so turning provisioning off never needs a DB edit.
    """
    conn, _row = _require_repo(name)
    fields: dict[str, object] = {}
    if clear:
        fields = {"provision_command": None, "provision_venv": None}
    else:
        if command is not None:
            fields["provision_command"] = command.strip() or None
        if reuse_venv is not None:
            cleaned = reuse_venv.strip().strip("/")
            if cleaned.startswith("..") or os.path.isabs(reuse_venv.strip()):
                raise RepoError(
                    f"--reuse-venv wants a path inside the repo, like backend/.venv; "
                    f"got {reuse_venv!r}"
                )
            fields["provision_venv"] = cleaned or None
    store.update_repo_fields(conn, name, **fields)
    return get_provision(name)


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


def _default_branch(row, remote: str) -> str:
    recorded = row["default_branch"]
    if recorded:
        return str(recorded)
    rc, out, _err = _git_ok(
        ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd=row["local_path"]
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


def _fast_forward(
    local_path: str, branch: str, remote: str, recorded: str | None = None
) -> tuple[str, bool]:
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

    if is_shallow(local_path):
        return _move_shallow(local_path, branch, remote, target, before, recorded, current_branch)

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


def _move_shallow(
    local_path: str,
    branch: str,
    remote: str,
    target: str,
    before: str,
    recorded: str | None,
    current_branch: str,
) -> tuple[str, bool]:
    """Move a shallow base clone's ``branch`` to ``target``. Never rewrites local work.

    A shallow clone holds only the remote's latest commit, so git can't show that
    the move is a fast-forward (the new tip's parents aren't here, and a merge
    reports "unrelated histories"). What makes the move safe is the same thing a
    fast-forward proves: the branch holds nothing the remote doesn't. Here that
    means the branch still sits at the commit mm last recorded from the remote.
    Anything else is refused, exactly as a diverged full clone is.
    """
    if not before or before != recorded:
        raise RepoError(
            f"the shallow base clone's {branch!r} is at {(before or '?')[:8]}, not the commit mm "
            f"last recorded from {remote} ({(recorded or '?')[:8]}); it may hold local commits, "
            "so it is not moved"
        )
    if current_branch == branch:
        _git(["reset", "--quiet", "--hard", target, "--"], cwd=local_path)
    else:
        _git(["update-ref", f"refs/heads/{branch}", target, before], cwd=local_path)
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

    remote = upstream_remote(row)
    if row["default_branch"]:
        align_shallow_remote(local_path, remote, row["default_branch"])
    _git(["fetch", "--quiet", "--prune", *shallow_fetch_args(local_path), remote], cwd=local_path)
    reconnect_shallow(local_path)
    forge_branch = forge_default_branch(local_path, remote)
    branch = forge_branch or _default_branch(row, remote)
    base_sha, moved = _fast_forward(local_path, branch, remote, recorded=previous)
    store.update_repo_base_sha(conn, name, base_sha)
    notes: list[str] = []
    if row["default_branch"] != branch:
        store.update_repo_fields(conn, name, default_branch=branch)
        if row["default_branch"]:
            notes.append(
                f"the forge's default branch is {branch}; the record said "
                f"{row['default_branch']} and now follows the forge"
            )
    if forge_branch:
        stale = {r: remote_head(local_path, r) for r in dict.fromkeys([remote, "origin"])}
        notes.extend(point_remote_heads(local_path, branch, list(stale)))
        for r, was in stale.items():
            if was and was != branch and remote_head(local_path, r) == branch:
                notes.append(f"{r}/HEAD named {was}; it now names {branch}, as the forge does")

    result = SyncResult(
        name=name,
        default_branch=branch,
        base_sha=base_sha,
        previous_sha=previous,
        fast_forwarded=moved,
        remote=remote,
        already_current=not moved,
        notes=notes,
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
