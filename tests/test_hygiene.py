"""`mm hygiene check`: private information never reaches a change to Middle Manager.

Middle Manager is public, and an installation works on private repositories. An
agent improving mm once wrote a private repository's name into a docstring and
four commit messages. These tests pin what the check derives from the installation
and what it refuses, in real git repositories in temp dirs.
"""

from __future__ import annotations

import subprocess

import pytest

from middle_manager import cli, hygiene
from middle_manager.state import init_db, store


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A stand-in mm checkout whose own upstream is public and allowed."""
    path = tmp_path / "mm-checkout"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Pat Example")
    _git(path, "config", "user.email", "pat.example@corp-internal.test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", "git@github.com:mm-org/middle-manager.git")
    (path / "README.md").write_text("Middle Manager\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "start")
    monkeypatch.chdir(path)
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "pat"))
    return path


@pytest.fixture
def registered(mm_home):
    conn = init_db()
    store.add_repo(
        conn,
        name="secret-billing-service",
        origin="git@ssh.bitbucket.org:acmecorp/secret-billing-service.git",
        local_path="/x",
        default_branch="main",
        base_sha=None,
        forge_url="https://bitbucket.org/acmecorp/secret-billing-service",
    )
    store.add_repo(
        conn,
        name="ledger-tools",
        origin="git@bitbucket.org:bitbucket/ledger-tools.git",
        local_path="/z",
        default_branch="main",
        base_sha=None,
    )
    store.add_repo(
        conn,
        name="api",
        origin="https://git.acme.internal/platform/api.git",
        local_path="/y",
        default_branch="main",
        base_sha=None,
    )


def _stage(path, name: str, text: str) -> None:
    (path / name).write_text(text)
    _git(path, "add", name)


def _reasons(findings) -> dict[str, str]:
    return {f.match.lower(): f.reason for f in findings}


def test_derives_this_installations_private_names(checkout, registered) -> None:
    terms = {t.text.lower(): t.reason for t in hygiene.private_terms(str(checkout))}

    assert terms["secret-billing-service"] == "a registered repository"
    assert terms["acmecorp"] == "a registered repository's owner"
    assert terms["git.acme.internal"] == "a registered repository's host"
    assert terms["platform"] == "a registered repository's owner"
    assert terms["pat.example@corp-internal.test"] == "the git user's email"
    assert terms["pat example"] == "the git user's name"
    assert "api" not in terms, "too short and generic to be private on its own"
    assert "bitbucket.org" not in terms and "ssh.bitbucket.org" not in terms, "public host"
    assert "bitbucket" not in terms, "a workspace named after the forge is still the forge's name"
    assert terms["ledger-tools"] == "a registered repository"
    assert "middle-manager" not in terms and "mm-org" not in terms, "mm's own upstream"


def test_refuses_a_staged_private_name_and_its_owner(checkout, registered) -> None:
    _stage(checkout, "notes.py", "# a slow fetch in secret-billing-service (acmecorp)\n")

    found = _reasons(hygiene.check(staged=True, cwd=str(checkout)))

    assert found == {
        "secret-billing-service": "a registered repository",
        "acmecorp": "a registered repository's owner",
    }


def test_generic_wording_and_existing_text_pass(checkout, registered) -> None:
    _stage(checkout, "notes.py", "# a slow fetch in a large monorepo\n")
    assert hygiene.check(staged=True, cwd=str(checkout)) == []


def test_only_added_lines_count(checkout, registered) -> None:
    _stage(checkout, "old.py", "acmecorp\n")
    _git(checkout, "commit", "-qm", "an existing leak", "--no-verify")
    _stage(checkout, "old.py", "acmecorp\nclean line\n")

    assert hygiene.check(staged=True, cwd=str(checkout)) == []


def test_emails_and_home_paths_are_private_anywhere(checkout) -> None:
    _stage(
        checkout,
        "notes.md",
        "ask jane.doe@othercorp.io\n"  # hygiene: allow
        "log at /Users/jdoe/work/tool.log\n"  # hygiene: allow
        "co-author noreply@anthropic.com, see /Users/me/project and dev@example.com\n"
        "remote git@github.com:owner/repo.git, fixture ci@build.test\n",
    )

    found = _reasons(hygiene.check(staged=True, cwd=str(checkout)))

    assert found == {
        "jane.doe@othercorp.io": "an email address",  # hygiene: allow
        "/users/jdoe/": "a path into a home directory",  # hygiene: allow
    }


def test_the_allow_marker_skips_one_line(checkout, registered) -> None:
    _stage(checkout, "notes.py", "acmecorp  # hygiene: allow\n")
    assert hygiene.check(staged=True, cwd=str(checkout)) == []


def test_terms_the_user_lists_are_private(checkout, mm_home) -> None:
    terms_file = mm_home / "hygiene" / "private-terms.txt"
    terms_file.parent.mkdir(parents=True)
    terms_file.write_text("# company and ticket prefixes\nGlobex\nPROJX\n")
    _stage(checkout, "notes.py", "# fixed for globex in PROJX-12\n")

    found = _reasons(hygiene.check(staged=True, cwd=str(checkout)))

    assert found == {
        "globex": "listed in hygiene/private-terms.txt",
        "projx": "listed in hygiene/private-terms.txt",
    }


def test_commit_messages_are_checked(checkout, registered, tmp_path) -> None:
    message = tmp_path / "MSG"
    message.write_text("Speed up pushes from secret-billing-service\n\n# a git comment: acmecorp\n")

    found = _reasons(hygiene.check(message_file=str(message), cwd=str(checkout)))

    assert found == {"secret-billing-service": "a registered repository"}, "comments skipped"


def test_a_range_checks_added_lines_and_every_message(checkout, registered) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _stage(checkout, "a.py", "clean\n")
    _git(checkout, "commit", "-qm", "Faster fetches for acmecorp", "--no-verify")

    found = hygiene.check(revision_range=f"{base}..HEAD", cwd=str(checkout))

    assert [f.match.lower() for f in found] == ["acmecorp"]
    assert found[0].where.endswith("message")


def test_no_installation_state_is_created_or_needed(checkout, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MM_HOME", str(tmp_path / "absent" / ".mm"))
    _stage(checkout, "notes.py", "clean\n")

    assert hygiene.check(staged=True, cwd=str(checkout)) == []
    assert not (tmp_path / "absent").exists()


def test_the_cli_exits_nonzero_with_findings(checkout, registered, capsys) -> None:
    _stage(checkout, "notes.py", "# secret-billing-service\n")

    assert cli.main(["hygiene", "check", "--staged"]) == 1
    err = capsys.readouterr().err
    assert "notes.py:1: 'secret-billing-service'" in err
    assert "Middle Manager is public" in err

    _git(checkout, "reset", "-q")
    _stage(checkout, "notes.py", "# a large monorepo\n")
    assert cli.main(["hygiene", "check", "--staged", "--quiet"]) == 0


def test_an_issue_body_is_checked_whole_headings_included(checkout, registered, tmp_path) -> None:
    """A performance review's action item filed as an issue is public at once; no hook sees it."""
    issue = tmp_path / "issue.md"
    issue.write_text(
        "Workers lose a turn to slow fetches\n\n"
        "## Seen on secret-billing-service\n"
        "Workers on a large monorepo waited minutes for `git fetch`.\n"
    )

    found = hygiene.check(text_files=[str(issue)], cwd=str(checkout))

    assert [(f.line, f.match) for f in found] == [(3, "secret-billing-service")]
    assert found[0].where == str(issue)


def test_the_cli_checks_text_files(checkout, registered, tmp_path, capsys) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("## Workers on a large monorepo waited minutes for a fetch\n")
    leaky = tmp_path / "leaky.md"
    leaky.write_text("Seen in acmecorp's workspace\n")

    assert cli.main(["hygiene", "check", "--text-file", str(clean), "--quiet"]) == 0
    assert cli.main(["hygiene", "check", "--text-file", str(clean), "--text-file", str(leaky)]) == 1
    assert "acmecorp" in capsys.readouterr().err
