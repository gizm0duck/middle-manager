# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""One normalized verdict for a Bitbucket pull request, from ``twg bb``.

Usage::

    uv run .agents/skills/bitbucket-twg/scripts/twg_pr_state.py --repo WORKSPACE/REPO 237

Combines ``pull-requests get <id> --statuses`` and ``pull-requests mergeability
<id>`` and prints one JSON object::

    {"pr": 237, "state": "OPEN", "head": "49f3b57d59e8", "checks": "pass",
     "failing": [], "pending": [], "blocking": ["minimum_approvals: FAILED"],
     "mergeable": false}

- ``checks`` covers only build statuses on the head commit (statuses on older
  pushes are ignored): ``pass``, ``fail``, ``pending`` or ``none``.
- ``blocking`` lists every merge check with ``blocking: true``; ``UNKNOWN`` means
  not evaluated yet, never passed.
- ``mergeable`` is true only for an open pull request with nothing blocking.

On a twg failure it prints ``{"error": ..., "remediation": ...}`` and exits with
twg's exit code (77 is missing or invalid Bitbucket auth: the user runs
``twg setup bitbucket``). It never reads or passes a token.

Stdlib only; the parsing mirrors ``middle_manager.forge`` so it works from any
repository a worker is in.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def twg_bin() -> str | None:
    found = shutil.which("twg")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "twg"
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def run(tool: str, args: list[str], repo: str) -> tuple[int, dict]:
    env = dict(os.environ)
    env.pop("TWG_AGENT_DEFAULTS", None)
    argv = [tool, "--output", "json", "--output-summary", "none", "bb", *args, "--repo", repo]
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        payload = {}
    return proc.returncode, payload if isinstance(payload, dict) else {}


def _hash(obj: object) -> str:
    commit = obj.get("commit") if isinstance(obj, dict) else None
    value = commit.get("hash") if isinstance(commit, dict) else None
    return str(value or "").lower()


def verdict(pr: dict, merge_checks: list) -> dict:
    """The verdict for one pull request (``get --statuses`` data) and its merge checks."""
    head = _hash(pr.get("source"))
    failing, pending, passing = [], [], []
    for status in pr.get("_statuses") or []:
        if not isinstance(status, dict) or (head and not _hash(status).startswith(head)):
            continue
        name = str(status.get("name") or status.get("key") or "a check")
        state = str(status.get("state") or "").upper()
        if state == "SUCCESSFUL":
            passing.append(name)
        elif state in ("FAILED", "STOPPED"):
            failing.append(name)
        else:
            pending.append(name)
    checks = "fail" if failing else "pending" if pending else "pass" if passing else "none"
    blocking = []
    for check in merge_checks or []:
        if isinstance(check, dict) and check.get("blocking") is True:
            spec = check.get("check") if isinstance(check.get("check"), dict) else {}
            name = spec.get("name") or spec.get("kind") or check.get("type") or "a merge check"
            blocking.append(f"{name}: {str(check.get('status') or 'UNKNOWN').upper()}")
    state = str(pr.get("state") or "").upper()
    return {
        "pr": pr.get("id"),
        "state": state,
        "head": head,
        "checks": checks,
        "failing": sorted(failing),
        "pending": sorted(pending),
        "blocking": blocking,
        "mergeable": state == "OPEN" and not blocking,
    }


def _error(code: int, payload: dict) -> int:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    remediation = error.get("remediation") if isinstance(error.get("remediation"), dict) else {}
    print(
        json.dumps(
            {
                "error": error.get("summary") or error.get("message") or f"twg exited {code}",
                "remediation": remediation.get("command"),
            }
        )
    )
    return code or 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="WORKSPACE/REPO")
    parser.add_argument("pr", type=int)
    args = parser.parse_args(argv)
    tool = twg_bin()
    if tool is None:
        print(json.dumps({"error": "twg is not installed", "remediation": "twg setup"}))
        return 127
    code, got = run(tool, ["pull-requests", "get", str(args.pr), "--statuses"], args.repo)
    if code != 0 or not isinstance(got.get("data"), dict):
        return _error(code, got)
    code, gates = run(tool, ["pull-requests", "mergeability", str(args.pr)], args.repo)
    if code != 0 or not isinstance(gates.get("data"), list):
        return _error(code, gates)
    print(json.dumps(verdict(got["data"], gates["data"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
