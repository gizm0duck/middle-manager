"""SQLite schema and connection management.

The schema is created idempotently and versioned via ``PRAGMA user_version`` so
future milestones can migrate forward. WAL mode and foreign keys are enabled for
concurrent supervisor/runner access.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from middle_manager.paths import db_path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL,
    local_path TEXT NOT NULL,
    default_branch TEXT,
    base_sha TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    repo_id INTEGER REFERENCES repos(id),
    role TEXT NOT NULL DEFAULT 'implementer',
    title TEXT NOT NULL,
    contract_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'requested',
    provider TEXT,
    model TEXT,
    reasoning TEXT,
    base_sha TEXT,
    branch TEXT,
    worktree_path TEXT,
    lease_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_session_id TEXT,
    start_token TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    rationale TEXT,
    scope TEXT NOT NULL DEFAULT 'task',
    author TEXT NOT NULL DEFAULT 'user',
    standing INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT,
    superseded_by INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT,
    reasoning TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    verdict TEXT NOT NULL,
    findings TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    session_state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    repo_id INTEGER REFERENCES repos(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    branch TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    base_sha TEXT,
    backend TEXT NOT NULL DEFAULT 'git',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS runners (
    id TEXT PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    pid INTEGER,
    provider TEXT,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'starting',
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    exit_code INTEGER,
    result_recorded INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path | None = None) -> sqlite3.Connection:
    """Create the schema idempotently and stamp the version."""
    conn = connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
