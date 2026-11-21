"""Keep private information out of changes contributed back to Middle Manager.

Middle Manager is public. An installation, though, works on its user's private
repositories, so the agents improving mm learn private names along the way and
can write them into a comment, a test or a commit message: on 2026-11-20 one
wrote "a seven-line commit in <a private repository>" into a docstring. This
module finds those leaks before they are committed.

Two kinds of finding:

- **What this installation knows is private.** Every registered repository's name
  and its owner (organization or workspace) and non-public host, the git user's
  name and email, the home directory, and any term listed one per line in
  ``<MM_HOME>/hygiene/private-terms.txt`` (company names, product names, ticket
  prefixes). Nothing of this is stored in the repository.
- **What is private anywhere.** Email addresses outside a short allowlist, and
  absolute paths into a user's home directory.

Only added lines are read, so existing text never blocks a commit. Text that
leaves the installation another way, such as an issue's title and body or a
pull request description, is checked whole with ``--text-file``. A line that
must keep a match on purpose carries ``hygiene: allow`` and is skipped.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from middle_manager.paths import db_path, mm_home

#: Hosts anyone can see. A repository's owner on one of these is still private;
#: the host itself is not.
PUBLIC_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sr.ht")

#: Email domains that are placeholders or no-reply addresses, never a person.
ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "anthropic.com",
    "users.noreply.github.com",
    "localhost",
)

#: Top-level domains reserved for examples and tests (RFC 2606).
RESERVED_TLDS = ("test", "example", "invalid", "localhost")

#: The user part of an SSH remote (``git@github.com:owner/repo``), not a person.
SSH_USERS = {"git", "hg", "ssh"}

#: Home-directory names used as placeholders in docs and tests.
PLACEHOLDER_USERS = {"me", "you", "user", "username", "runner", "example", "name", "<user>"}

#: Repository names too generic to be private on their own.
GENERIC_NAMES = {
    "main",
    "test",
    "tests",
    "docs",
    "demo",
    "app",
    "api",
    "web",
    "core",
    "site",
    "service",
    "services",
    "server",
    "client",
    "frontend",
    "backend",
    "source",
    "repo",
    "deep",
    "shallow",
}

#: A forge's own name is public even when a workspace or organization is called it.
_FORGE_BRANDS = {host.split(".")[0] for host in PUBLIC_HOSTS}

ALLOW_MARKER = "hygiene: allow"
MIN_TERM_LENGTH = 4

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_HOME_PATH = re.compile(r"(?:/Users|/home)/([^/\s'\"`]+)/")
_URL = re.compile(
    r"^(?:[a-z+]+://)?(?:[^@/\s]+@)?(?P<host>[^:/\s]+)[:/](?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class Finding:
    where: str
    line: int
    match: str
    reason: str

    def render(self) -> str:
        return f"{self.where}:{self.line}: {self.match!r} ({self.reason})"


@dataclass(frozen=True)
class Term:
    text: str
    reason: str


def _is_public_host(host: str) -> bool:
    host = host.lower()
    return any(host == public or host.endswith("." + public) for public in PUBLIC_HOSTS)


def _url_parts(url: str | None) -> tuple[str, str, str] | None:
    if not url:
        return None
    match = _URL.match(url.strip())
    if not match:
        return None
    return match.group("host"), match.group("owner"), match.group("name")


def _git_config(key: str, cwd: str | None) -> str:
    proc = subprocess.run(
        ["git", "config", "--get", key], cwd=cwd, capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _own_upstream(cwd: str | None) -> set[str]:
    """mm's own owner and name: they belong in its docs, so they never count."""
    parts = _url_parts(_git_config("remote.origin.url", cwd))
    return {parts[1].lower(), parts[2].lower()} if parts else set()


def _registered_repos() -> list[dict]:
    """Registered repositories, read without creating any state."""
    path = db_path()
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM repos").fetchall()
    except sqlite3.Error:
        return []
    keys = rows[0].keys() if rows else []
    return [{k: row[k] for k in keys} for row in rows]


def private_terms(cwd: str | None = None) -> list[Term]:
    """Every name this installation knows to be private, longest first."""
    own = _own_upstream(cwd)
    found: dict[str, Term] = {}

    def add(text: str | None, reason: str) -> None:
        if not text:
            return
        text = text.strip()
        key = text.lower()
        if (
            len(text) < MIN_TERM_LENGTH
            or key in GENERIC_NAMES
            or key in own
            or key in _FORGE_BRANDS
        ):
            return
        found.setdefault(key, Term(text, reason))

    for repo in _registered_repos():
        add(repo.get("name"), "a registered repository")
        for url in (repo.get("origin"), repo.get("forge_url")):
            parts = _url_parts(url)
            if not parts:
                continue
            host, owner, name = parts
            add(name, "a registered repository")
            add(owner, "a registered repository's owner")
            if not _is_public_host(host):
                add(host, "a registered repository's host")
    add(_git_config("user.email", cwd), "the git user's email")
    add(_git_config("user.name", cwd), "the git user's name")
    home = os.path.expanduser("~")
    if home not in ("", "~", "/"):
        add(home, "the home directory")
    terms_file = mm_home() / "hygiene" / "private-terms.txt"
    if terms_file.exists():
        for line in terms_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                add(line, "listed in hygiene/private-terms.txt")
    return sorted(found.values(), key=lambda t: -len(t.text))


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def scan_lines(lines: list[tuple[str, int, str]], terms: list[Term]) -> list[Finding]:
    """Findings in ``(where, line number, text)`` triples."""
    patterns = [(term, _term_pattern(term.text)) for term in terms]
    findings: list[Finding] = []
    for where, number, text in lines:
        if ALLOW_MARKER in text:
            continue
        for term, pattern in patterns:
            match = pattern.search(text)
            if match:
                findings.append(Finding(where, number, match.group(0), term.reason))
        for match in _EMAIL.finditer(text):
            domain = match.group(1).lower()
            user = match.group(0).split("@", 1)[0].lower()
            if user in SSH_USERS or domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
                continue
            if not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
                findings.append(Finding(where, number, match.group(0), "an email address"))
        for match in _HOME_PATH.finditer(text):
            if match.group(1).lower() not in PLACEHOLDER_USERS:
                findings.append(
                    Finding(where, number, match.group(0), "a path into a home directory")
                )
    unique: dict[tuple[str, int, str], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.where, finding.line, finding.match.lower()), finding)
    return list(unique.values())


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """``(file, new line number, text)`` for every added line of a unified diff."""
    out: list[tuple[str, int, str]] = []
    current = "?"
    number = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = path[2:] if path.startswith("b/") else path
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            number = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            out.append((current, number, raw[1:]))
            number += 1
        elif not raw.startswith("-"):
            number += 1
    return out


def diff_for(*, staged: bool, revision_range: str | None, cwd: str | None) -> str:
    args = ["git", "diff", "--no-color", "--unified=0", "--no-ext-diff"]
    if revision_range:
        args.append(revision_range)
    elif staged:
        args.append("--cached")
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.stdout


def messages_for(revision_range: str, cwd: str | None) -> list[tuple[str, int, str]]:
    proc = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x01", revision_range],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    lines: list[tuple[str, int, str]] = []
    for entry in proc.stdout.split("\x01"):
        if "\x00" not in entry:
            continue
        sha, body = entry.strip("\n").split("\x00", 1)
        for i, text in enumerate(body.splitlines(), start=1):
            lines.append((f"commit {sha[:9]} message", i, text))
    return lines


def message_lines(text: str, where: str = "commit message") -> list[tuple[str, int, str]]:
    return [
        (where, i, line)
        for i, line in enumerate(text.splitlines(), start=1)
        if not line.startswith("#")
    ]


def text_lines(text: str, where: str) -> list[tuple[str, int, str]]:
    """Every line of ``text``. An issue or pull request body's ``#`` lines are headings."""
    return [(where, i, line) for i, line in enumerate(text.splitlines(), start=1)]


def check(
    *,
    staged: bool = False,
    revision_range: str | None = None,
    message_file: str | None = None,
    text_files: list[str] | None = None,
    cwd: str | None = None,
) -> list[Finding]:
    terms = private_terms(cwd)
    lines: list[tuple[str, int, str]] = []
    if message_file:
        lines += message_lines(Path(message_file).read_text(encoding="utf-8"))
    for path in text_files or []:
        lines += text_lines(Path(path).read_text(encoding="utf-8"), path)
    if staged or revision_range:
        lines += added_lines(diff_for(staged=staged, revision_range=revision_range, cwd=cwd))
    if revision_range:
        lines += messages_for(revision_range, cwd)
    return scan_lines(lines, terms)
