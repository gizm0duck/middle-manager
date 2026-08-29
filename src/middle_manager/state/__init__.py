"""Durable SQLite state for Middle Manager.

SQLite is the source of truth for repositories, runs, versioned task contracts,
dependencies, decisions, sessions, runner identities, events, usage, reviews,
and lifecycle state (see the technical plan). Append-only run artifacts on disk
carry the bulky evidence.
"""

from middle_manager.state.db import SCHEMA_VERSION, connect, init_db

__all__ = ["SCHEMA_VERSION", "connect", "init_db"]
