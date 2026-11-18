"""Which code host mm talks to, and the Bitbucket backend that drives Atlassian's ``twg``.

GitHub through ``gh`` is the default and stays the default: nothing here changes a
GitHub install, and every GitHub call site keeps its own code. Bitbucket Cloud is
used only when the install was explicitly configured for it (``[forge] kind =
"bitbucket"`` in ``.mm/config.toml``, set by ``mm setup --forge bitbucket`` or
``mm config forge --forge bitbucket``). A ``bitbucket.org`` remote alone never
switches the forge.

Bitbucket is reached through the TWG CLI (``twg bitbucket``/``twg bb``). Every call
asks for pure JSON (``--output json --output-summary none``) with
``TWG_AGENT_DEFAULTS`` removed from the environment, so the answer is the
``{apiVersion, command, data, meta}`` envelope on stdout rather than an agent
summary pointing at a file. mm never reads, stores or passes the TWG or Bitbucket
credentials: ``twg`` owns them (``twg login``, ``twg setup bitbucket``).

The pull-request payloads below are normalized into the field names the GitHub
code paths already consume (``number``, ``state``, ``baseRefName``,
``mergeCommit``...), so the stack, watch and merge logic stay one implementation.

The twg contract (1.4.5, read from its bundled source and checked against real
Bitbucket responses; fixtures in ``tests/fixtures/bitbucket``): success is exit 0
with the envelope on stdout; failure is a non-zero exit with ``{"ok": false,
"error": {code, summary, remediation: {command}}}`` on stdout (exit 77 is missing
or invalid Bitbucket auth). ``data`` is Bitbucket's REST v2 object, untouched.

How Bitbucket differs from what mm relies on in GitHub:

- **No native stacks.** There is no ``gh stack`` equivalent, so a Bitbucket stack
  always merges layer by layer.
- **Short hashes.** ``source.commit.hash`` and ``merge_commit.hash`` are 12
  characters; a merge commit is expanded with git before mm records it.
- **Statuses span commits.** A pull request's statuses include older pushes; only
  those on the head commit count.
- **Merge checks, not required checks.** ``pull-requests mergeability`` lists
  Bitbucket's merge checks; any with ``blocking: true`` stops a merge.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

GITHUB = "github"
BITBUCKET = "bitbucket"
FORGES = (GITHUB, BITBUCKET)

#: A slow forge must not hang a heartbeat or a merge loop.
TWG_TIMEOUT_SECONDS = 120.0

INSTALL_HINT = (
    "install the TWG CLI (`curl -fsSL https://teamwork-graph.atlassian.com/cli/install | bash`), "
    "then run `twg login` and `twg setup bitbucket`"
)
AUTH_HINT = "run `twg login`, then `twg setup bitbucket`"
NO_STACKS = "Bitbucket has no native stacks"

_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}\Z")

_BITBUCKET_URL_PATTERNS = (
    re.compile(
        r"^https://(?:[^@/]+@)?bitbucket\.org/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
    ),
    re.compile(r"^git@bitbucket\.org:(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@bitbucket\.org/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def kind() -> str:
    """The configured forge. GitHub unless the install was explicitly set to Bitbucket.

    An absent or unreadable config is GitHub, exactly as before forges existed.
    """
    from middle_manager.config import ConfigError, load_config

    try:
        return load_config().forge.kind
    except (ConfigError, OSError, ValueError, TypeError):
        return GITHUB


def is_bitbucket() -> bool:
    return kind() == BITBUCKET


def tool_name() -> str:
    """The CLI a person would run by hand for this forge."""
    return "twg" if is_bitbucket() else "gh"


def _configured_twg_path() -> str | None:
    from middle_manager.config import ConfigError, load_config

    try:
        return load_config().forge.twg_path or None
    except (ConfigError, OSError, ValueError, TypeError):
        return None


def _executable(path: str | os.PathLike | None) -> str | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def twg_bin() -> str | None:
    """The ``twg`` executable: the configured path, else ``PATH``, else the installer's spot.

    The official installer puts it in ``~/.local/bin``, which a detached supervisor
    or watch process with a thin ``PATH`` may not see.
    """
    return (
        _executable(_configured_twg_path())
        or shutil.which("twg")
        or _executable(Path.home() / ".local" / "bin" / "twg")
    )


# --------------------------------------------------------------------------- #
# Repository identity
# --------------------------------------------------------------------------- #


def bitbucket_slug(url: str | None) -> str | None:
    """``workspace/repo`` for a bitbucket.org URL (https, scp-style or ssh), else None."""
    if not url:
        return None
    for pattern in _BITBUCKET_URL_PATTERNS:
        match = pattern.match(url.strip())
        if match:
            return f"{match.group('owner')}/{match.group('name')}"
    return None


def slug_from_checkout(cwd: str | None) -> str | None:
    """The Bitbucket ``workspace/repo`` a checkout's ``forge`` or ``origin`` remote names."""
    if not cwd:
        return None
    from middle_manager.repos import FORGE_REMOTE

    for remote in (FORGE_REMOTE, "origin"):
        proc = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", remote],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            slug = bitbucket_slug(proc.stdout.strip())
            if slug:
                return slug
    return None


# --------------------------------------------------------------------------- #
# Calling twg
# --------------------------------------------------------------------------- #


def twg_argv(tool: str, args: list[str], *, slug: str | None) -> list[str]:
    """``twg --output json --output-summary none bb <args> [--repo workspace/repo]``."""
    argv = [tool, "--output", "json", "--output-summary", "none", "bb", *args]
    if slug:
        argv += ["--repo", slug]
    return argv


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Agent defaults turn stdout into a YAML summary pointing at a file; mm wants
    # the JSON envelope itself.
    env.pop("TWG_AGENT_DEFAULTS", None)
    return env


def _failure(argv: list[str], summary: str) -> subprocess.CompletedProcess:
    """A failed call in twg's own error envelope, so every failure reads the same way."""
    payload = {"ok": False, "error": {"summary": summary, "remediation": {}}}
    return subprocess.CompletedProcess(argv, 127, json.dumps(payload), "")


def run_twg(
    args: list[str], *, slug: str | None, cwd: str | None = None
) -> subprocess.CompletedProcess:
    """Run one ``twg bb`` command. A missing or hung ``twg`` is a failure, never a raise."""
    tool = twg_bin()
    if tool is None:
        return _failure(args, f"`twg` is not installed; {INSTALL_HINT}")
    argv = twg_argv(tool, args, slug=slug)
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=TWG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _failure(argv, f"`twg` could not run ({type(exc).__name__})")


def parse_envelope(text: str | None) -> Any:
    """The ``data`` of a successful envelope; ``None`` for an error or unreadable output."""
    try:
        payload = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is False or "data" not in payload:
        return None
    return payload["data"]


def error_detail(proc: subprocess.CompletedProcess) -> str:
    """Why a twg call failed: the envelope's summary and its remediation command.

    Raw stderr is never relayed: it is twg's human rendering of the same error.
    """
    try:
        payload = json.loads(proc.stdout or "")
    except (TypeError, ValueError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return f"twg exited {proc.returncode} without a readable error"
    said = next(
        (
            value.strip()
            for value in (error.get("summary"), error.get("message"), error.get("code"))
            if isinstance(value, str) and value.strip()
        ),
        f"twg exited {proc.returncode}",
    )
    remediation = error.get("remediation")
    command = remediation.get("command") if isinstance(remediation, dict) else None
    if isinstance(command, str) and command.strip():
        said = f"{said} (run `{command.strip()}`)"
    return said[:300]


# --------------------------------------------------------------------------- #
# Reading Bitbucket objects
# --------------------------------------------------------------------------- #


def _dig(obj: Any, path: str) -> Any:
    node = obj
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def pr_rows(data: Any) -> list[dict]:
    """Pull requests from ``query`` (a list) or ``get`` (one object)."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and "id" in data:
        return [data]
    return []


_STATE = {"OPEN": "OPEN", "MERGED": "MERGED", "DECLINED": "CLOSED", "SUPERSEDED": "CLOSED"}


def normalize_pr(raw: dict) -> dict:
    """A Bitbucket pull request in the GitHub field names the stack code consumes.

    ``merge_commit`` is Bitbucket's short hash as returned; callers that record a
    merge expand it first (:func:`expand_sha`).
    """
    number = raw.get("id")
    if isinstance(number, bool) or not isinstance(number, int):
        number = None
    state = _STATE.get(str(raw.get("state") or "").upper(), "UNKNOWN")
    merge_oid = _dig(raw, "merge_commit.hash")
    merge_oid = merge_oid if isinstance(merge_oid, str) and _SHA_RE.fullmatch(merge_oid) else None
    # Bitbucket records no merge time; a merged pull request's last update is it.
    updated = raw.get("updated_on")
    merged_at = updated if state == "MERGED" and isinstance(updated, str) else None
    return {
        "number": number,
        "title": raw.get("title"),
        "state": state,
        "isDraft": raw.get("draft") is True,
        "url": _dig(raw, "links.html.href"),
        "baseRefName": _dig(raw, "destination.branch.name"),
        "headRefName": _dig(raw, "source.branch.name"),
        "headRefOid": _dig(raw, "source.commit.hash"),
        "mergeStateStatus": "UNKNOWN",
        "mergeable": "UNKNOWN",
        "mergeCommit": {"oid": merge_oid} if merge_oid else None,
        "mergedAt": merged_at,
        "merged": bool(state == "MERGED" and merged_at and merge_oid),
        "merge_commit": merge_oid,
    }


def status_bucket(state: str | None) -> str:
    """A Bitbucket build state as a ``gh pr checks`` bucket. Anything unknown is pending."""
    value = str(state or "").upper()
    if value == "SUCCESSFUL":
        return "pass"
    if value in ("FAILED", "STOPPED"):
        return "fail"
    return "pending"


def head_status_rows(raw: dict) -> list[dict]:
    """The ``_statuses`` on the pull request's head commit, as ``name``/``bucket`` rows.

    A pull request's statuses include every commit ever pushed to it, so a red
    build on an old push must not hold back a green head. The head is a short
    hash; statuses carry the full one.
    """
    head = str(_dig(raw, "source.commit.hash") or "").lower()
    rows = []
    for item in raw.get("_statuses") or []:
        if not isinstance(item, dict):
            continue
        commit = str(_dig(item, "commit.hash") or "").lower()
        if head and not commit.startswith(head):
            continue
        name = item.get("name") or item.get("key") or "a check"
        rows.append({"name": str(name), "bucket": status_bucket(item.get("state"))})
    return rows


def _check_name(check: dict) -> str:
    name = _dig(check, "check.name") or _dig(check, "check.kind") or check.get("type")
    return str(name or "a merge check")


def blocking_rows(checks: Any) -> list[dict]:
    """Merge checks that stop a merge now, as rows: FAILED is red, anything else waits.

    ``UNKNOWN`` means not yet evaluated, which is never a pass.
    """
    if not isinstance(checks, list):
        return []
    rows = []
    for check in checks:
        if not isinstance(check, dict) or check.get("blocking") is not True:
            continue
        status = str(check.get("status") or "").upper()
        rows.append(
            {
                "name": f"merge check {_check_name(check)}",
                "bucket": "fail" if status == "FAILED" else "pending",
            }
        )
    return rows


def git_mergeable(checks: Any) -> str:
    """GitHub's ``mergeable`` from Bitbucket's ``git_mergeability_check``."""
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict) and check.get("type") == "git_mergeability_check":
            status = str(check.get("status") or "").upper()
            return {"PASSED": "MERGEABLE", "FAILED": "CONFLICTING"}.get(status, "UNKNOWN")
    return "UNKNOWN"


def merge_result_pr(data: Any) -> dict | None:
    """The merged pull request from ``merge --wait``, in either of its two shapes."""
    if not isinstance(data, dict):
        return None
    for key in ("merge_result", "pullRequest", "data"):
        value = data.get(key)
        if isinstance(value, dict) and "id" in value:
            return value
    return None


def expand_sha(short: str | None, cwd: str | None) -> str | None:
    """The full 40-character SHA for Bitbucket's short hash, fetching once if needed."""
    if not short or not cwd or not _SHA_RE.fullmatch(short):
        return None

    def resolve() -> str | None:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--verify", "--quiet", f"{short}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        full = proc.stdout.strip()
        return full if proc.returncode == 0 and len(full) == 40 else None

    found = resolve()
    if found:
        return found
    from middle_manager.repos import FORGE_REMOTE

    for remote in (FORGE_REMOTE, "origin"):
        subprocess.run(
            ["git", "-C", cwd, "fetch", "--quiet", remote],
            capture_output=True,
            text=True,
            check=False,
        )
        found = resolve()
        if found:
            return found
    return None


def _with_full_merge_sha(pr: dict, cwd: str | None) -> dict:
    """Expand a merged pull request's short merge hash; an unexpandable one confirms nothing."""
    if pr.get("merged"):
        full = expand_sha(pr.get("merge_commit"), cwd)
        pr["merge_commit"] = full
        pr["mergeCommit"] = {"oid": full} if full else None
        pr["merged"] = full is not None
    return pr


# --------------------------------------------------------------------------- #
# Pull-request operations
# --------------------------------------------------------------------------- #

ANY_STATE = ("OPEN", "MERGED", "DECLINED")


def _query(branch: str, state: str, *, slug: str | None, cwd: str | None) -> list[dict] | None:
    """Pull requests from ``branch`` in ``state``; None when the forge could not be read."""
    proc = run_twg(
        ["pull-requests", "query", "--source", branch, "--state", state, "--limit", "1"],
        slug=slug,
        cwd=cwd,
    )
    if proc.returncode != 0:
        return None
    return pr_rows(parse_envelope(proc.stdout))


def find_pr(
    branch: str, *, slug: str | None, cwd: str | None, states: tuple[str, ...] = ("OPEN",)
) -> dict | None:
    """The newest raw pull request from ``branch`` in the first of ``states`` that has one."""
    for state in states:
        rows = _query(branch, state, slug=slug, cwd=cwd)
        if rows is None:
            return None
        if rows:
            return rows[0]
    return None


def get_raw(
    number: int, *, slug: str | None, cwd: str | None, statuses: bool = False
) -> dict | None:
    """One raw pull request (with ``_statuses`` when asked), or None."""
    args = ["pull-requests", "get", str(number)] + (["--statuses"] if statuses else [])
    proc = run_twg(args, slug=slug, cwd=cwd)
    if proc.returncode != 0:
        return None
    rows = pr_rows(parse_envelope(proc.stdout))
    return rows[0] if rows else None


def mergeability(number: int, *, slug: str | None, cwd: str | None) -> list | None:
    """Bitbucket's merge checks for a pull request, or None when unreadable."""
    proc = run_twg(["pull-requests", "mergeability", str(number)], slug=slug, cwd=cwd)
    data = parse_envelope(proc.stdout) if proc.returncode == 0 else None
    return data if isinstance(data, list) else None


def pull_request_for(branch: str, *, cwd: str | None, slug: str | None) -> dict | None:
    """``stacks.pull_request_for`` for Bitbucket: found by source branch, read in full."""
    slug = slug or slug_from_checkout(cwd)
    found = find_pr(branch, slug=slug, cwd=cwd, states=ANY_STATE)
    if found is None:
        return None
    number = found.get("id")
    full = get_raw(number, slug=slug, cwd=cwd) if isinstance(number, int) else None
    return _with_full_merge_sha(normalize_pr(full or found), cwd)


def checks(number: int, *, cwd: str | None, slug: str | None) -> list[dict] | None:
    """What decides whether a layer may merge: the head's build statuses plus every
    blocking merge check. None when either could not be read."""
    slug = slug or slug_from_checkout(cwd)
    raw = get_raw(number, slug=slug, cwd=cwd, statuses=True)
    gates = mergeability(number, slug=slug, cwd=cwd)
    if raw is None or gates is None:
        return None
    return head_status_rows(raw) + blocking_rows(gates)


def lookup(branch: str, cwd: str | None) -> dict:
    """The heartbeat's view of one branch, in the shape ``watch._lookup_pr`` returns."""
    unknown = {"known": False, "pr": None, "ci": "unknown", "failing": []}
    if cwd is None:
        return unknown
    slug = slug_from_checkout(cwd)
    found = None
    for state in ANY_STATE:
        rows = _query(branch, state, slug=slug, cwd=cwd)
        if rows is None:
            return unknown
        if rows:
            found = rows[0]
            break
    if found is None:
        return {"known": True, "pr": None, "ci": "none", "failing": []}
    number = found.get("id")
    if isinstance(number, bool) or not isinstance(number, int):
        return unknown
    is_open = str(found.get("state") or "").upper() == "OPEN"
    raw = get_raw(number, slug=slug, cwd=cwd, statuses=is_open) or found
    pr = _with_full_merge_sha(normalize_pr(raw), cwd)
    entry = {
        "known": True,
        "pr": pr["number"],
        "base": pr.get("baseRefName"),
        "state": pr["state"],
        "mergeable": "UNKNOWN",
        "merge_state": "UNKNOWN",
        "ci": "unknown",
        "failing": [],
        "merged": pr["merged"] is True,
        "merge_commit": pr.get("merge_commit"),
    }
    if pr["state"] == "OPEN":
        from middle_manager.watch import _ci_verdict

        if "_statuses" in raw:
            entry["ci"], entry["failing"] = _ci_verdict(head_status_rows(raw))
        gates = mergeability(number, slug=slug, cwd=cwd)
        if gates is not None:
            entry["mergeable"] = git_mergeable(gates)
            entry["merge_state"] = "BLOCKED" if blocking_rows(gates) else "CLEAN"
    return entry


def open_pr(
    *, branch: str, base: str | None, title: str, body: str, slug: str | None, cwd: str
) -> dict:
    """Open a pull request, or find the one already open for ``branch``.

    Returns ``{"exists", "created", "url", "note"}``. An already-open pull request is
    looked up first rather than inferred from a refusal's wording.
    """
    slug = slug or slug_from_checkout(cwd)
    existing = find_pr(branch, slug=slug, cwd=cwd)
    if existing is not None:
        return {
            "exists": True,
            "created": False,
            "url": _dig(existing, "links.html.href"),
            "note": "pushed; a PR for this branch was already open",
        }
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        body_path = fh.name
    try:
        args = ["pull-requests", "create", "--title", title, "--source", branch]
        args += ["--description-file", body_path]
        if base:
            args += ["--dest", base]
        proc = run_twg(args, slug=slug, cwd=cwd)
    finally:
        Path(body_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        return {
            "exists": False,
            "created": False,
            "url": None,
            "note": f"pushed; PR creation failed: {error_detail(proc)}",
        }
    rows = pr_rows(parse_envelope(proc.stdout))
    url = _dig(rows[0], "links.html.href") if rows else None
    if not isinstance(url, str):
        found = find_pr(branch, slug=slug, cwd=cwd)
        url = _dig(found, "links.html.href") if found else None
    note = (
        "pushed; PR opened"
        if url
        else "pushed; PR opened, but its URL could not be read back — find it with "
        f"`twg bb pull-requests query --source {branch}`; do not create another"
    )
    return {"exists": True, "created": True, "url": url, "note": note}


def retarget(number: int, new_base: str, *, cwd: str | None, slug: str | None) -> str | None:
    """Point a pull request at ``new_base``. Returns why it failed, or None."""
    proc = run_twg(
        ["pull-requests", "update", "--pull-request", str(number), "--dest", new_base],
        slug=slug or slug_from_checkout(cwd),
        cwd=cwd,
    )
    return None if proc.returncode == 0 else error_detail(proc)


def merge(number: int, *, cwd: str | None, slug: str | None) -> str | None:
    """Merge with a merge commit and wait until Bitbucket says MERGED.

    Returns why it was not merged, or None. The merge commit is read back
    afterwards (:func:`pull_request_for`), which expands its short hash.
    """
    proc = run_twg(
        [
            "pull-requests",
            "merge",
            "--pull-request",
            str(number),
            "--merge-strategy",
            "merge_commit",
            "--wait",
        ],
        slug=slug or slug_from_checkout(cwd),
        cwd=cwd,
    )
    if proc.returncode != 0:
        return error_detail(proc)
    merged = merge_result_pr(parse_envelope(proc.stdout))
    state = str(merged.get("state") or "").upper() if merged else ""
    if state and state != "MERGED":
        return f"Bitbucket reports PR #{number} as {state} after the merge"
    return None


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def doctor_status() -> dict:
    """What ``twg doctor`` says about Bitbucket access (``data.bitbucket``).

    Only the ok/status flags are read; mm never reads or relays a credential.
    """
    tool = twg_bin()
    if tool is None:
        return {"available": False, "path": None, "bitbucket_ok": False, "hint": INSTALL_HINT}
    try:
        proc = subprocess.run(
            [tool, "--output", "json", "--output-summary", "none", "doctor"],
            env=_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=TWG_TIMEOUT_SECONDS,
        )
        data = parse_envelope(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        data = None
    bitbucket = data.get("bitbucket") if isinstance(data, dict) else None
    ok = isinstance(bitbucket, dict) and bitbucket.get("ok") is True
    status = bitbucket.get("status") if isinstance(bitbucket, dict) else None
    return {
        "available": True,
        "path": tool,
        "bitbucket_ok": ok,
        "bitbucket_status": status if isinstance(status, str) else "unknown",
        "hint": None if ok else AUTH_HINT,
    }
