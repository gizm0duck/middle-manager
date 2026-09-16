"""Where a repo keeps its migrations, and what an added revision says it builds on."""

from __future__ import annotations

import pytest

from middle_manager import cli, migrations, repos
from middle_manager.state import init_db
from middle_manager.state.db import _column_names


def test_schema_gains_the_migrations_glob_on_fresh_and_existing_databases(mm_home) -> None:
    conn = init_db()
    assert "migrations_glob" in _column_names(conn, "repos")
    conn.execute("ALTER TABLE repos DROP COLUMN migrations_glob")
    conn.commit()
    assert "migrations_glob" not in _column_names(conn, "repos")
    conn = init_db()
    assert "migrations_glob" in _column_names(conn, "repos")
    init_db()  # a second run must not fail on the now-present column


def test_a_repo_with_nothing_configured_uses_the_ordinary_alembic_layout(
    mm_home, source_repo, capsys
) -> None:
    added = repos.add_repo(source_repo)
    assert cli.main(["repo", "set", added.name]) == 0
    out = capsys.readouterr().out
    assert "**/alembic/versions/*.py" in out
    assert "the default" in out


def test_setting_a_migrations_glob_changes_which_added_files_count(
    mm_home, source_repo, capsys
) -> None:
    added = repos.add_repo(source_repo)
    assert cli.main(["repo", "set", added.name, "--migrations-glob", "db/changes/*.sql"]) == 0
    assert "db/changes/*.sql" in capsys.readouterr().out

    conn = init_db()
    glob = migrations.glob_for_repo(conn.execute("SELECT * FROM repos").fetchone())
    assert glob == "db/changes/*.sql"
    assert migrations.matches("db/changes/0007-events.sql", glob)
    assert not migrations.matches("backend/alembic/versions/aaa.py", glob)

    # An empty string puts the repo back on the default rather than matching nothing.
    assert cli.main(["repo", "set", added.name, "--migrations-glob", ""]) == 0
    assert "the default" in capsys.readouterr().out


def test_the_default_glob_finds_alembic_revisions_at_any_depth() -> None:
    glob = migrations.DEFAULT_MIGRATIONS_GLOB
    assert migrations.matches("alembic/versions/aaa.py", glob)
    assert migrations.matches("backend/alembic/versions/aaa.py", glob)
    assert migrations.matches("services/api/alembic/versions/aaa.py", glob)
    assert not migrations.matches("backend/models/events.py", glob)
    assert not migrations.matches("backend/alembic/env.py", glob)


@pytest.mark.parametrize(
    ("path", "glob", "expected"),
    [
        ("alembic/versions/aaa.py", migrations.DEFAULT_MIGRATIONS_GLOB, True),
        ("services/api/alembic/versions/aaa.py", "**/alembic/versions/*.py", True),
        ("db/changes/0007.sql", "db/changes/*.sql", True),
        ("db/changes/nested/0007.sql", "db/changes/*.sql", False),
        ("db/changes/a1.sql", "db/changes/??.sql", True),
        ("db/changes/a/1.sql", "db/changes/??.sql", False),
        ("db/changes/7.sql", "db/changes/[0-9].sql", True),
        ("db/changes/x.sql", "db/changes/[0-9].sql", False),
        ("backend/models/events.py", "**/alembic/versions/*.py", False),
    ],
)
def test_migration_globs_have_the_same_segment_semantics_on_every_python(
    path: str, glob: str, expected: bool
) -> None:
    assert migrations.matches(path, glob) is expected


def test_down_revision_is_read_from_every_shape_alembic_writes() -> None:
    assert migrations.parse_down_revision('down_revision = "abc123"\n') == "abc123"
    assert migrations.parse_down_revision("down_revision: str | None = 'abc123'\n") == "abc123"
    assert migrations.parse_down_revision("down_revision = None\n") == "None"
    # A merge revision names both heads it joins.
    assert migrations.parse_down_revision('down_revision = ("aaa", "bbb")\n') == "aaa, bbb"
    # A file mid-edit still answers, from the line itself.
    assert migrations.parse_down_revision('def upgrade(\ndown_revision = "abc"\n') == "abc"
    assert migrations.parse_down_revision("revision = 'aaa'\n") is None
    assert migrations.parse_down_revision("down_revision = some_name\n") is None
