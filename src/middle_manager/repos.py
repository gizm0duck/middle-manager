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


def add_repo(url_or_path: str, name: str | None = None, forge_url: str | None = None) -> AddedRepo:
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
    _git(["clone", "--quiet", resolved_forge, str(dest)])
    remote = ensure_forge_remote(str(dest), resolved_forge)

    # The forge names the default branch; the clone's HEAD is only whatever the
    # source had checked out.
    default_branch = forge_default_branch(str(dest), remote)
    notes: list[str] = []
    if default_branch:
        if remote != "origin":
            rc, _out, err = _git_ok(["fetch", "--quiet", remote, default_branch], cwd=str(dest))
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
    )
    # Seed per-repo memory so learnings about this repo persist across sessions.
    seed_repo_memory(repo_name, origin=resolved_forge, default_branch=default_branch)
    return AddedRepo(
        repo_name, resolved_forge, str(dest), default_branch, base_sha, resolved_forge, notes=notes
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

    def describe(self) -> str:
        origin = "the default" if self.migrations_glob_is_default else "set for this repo"
        lines = [f"{self.repo}: migrations live at {self.migrations_glob} ({origin})"]
        lines.append(
            "  CI: none (set with --no-ci; a stack layer with no checks may merge)"
            if self.no_ci
            else "  CI: expected (a stack layer with no checks at all does not merge)"
        )
        if self.environment is not None:
            lines.extend(f"  {line}" for line in self.environment.describe())
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

    remote = upstream_remote(row)
    _git(["fetch", "--quiet", "--prune", remote], cwd=local_path)
    forge_branch = forge_default_branch(local_path, remote)
    branch = forge_branch or _default_branch(row, remote)
    base_sha, moved = _fast_forward(local_path, branch, remote)
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
