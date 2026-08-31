"""Filesystem layout for Middle Manager runtime state.

Canonical state lives under a gitignored ``.mm/`` directory (see the technical
plan). The location is the current working directory by default, overridable
with the ``MM_HOME`` environment variable. The ``bin/mm`` launcher always sets
``MM_HOME`` to its own checkout's ``.mm/`` (unless already set), so the control
plane addresses the same instance no matter which directory it is invoked from;
the cwd fallback exists for tests and for running the module directly.
"""

from __future__ import annotations

import os
from pathlib import Path


def mm_home() -> Path:
    """Return the ``.mm`` root, honoring the ``MM_HOME`` override."""
    override = os.environ.get("MM_HOME")
    if override:
        return Path(override)
    return Path.cwd() / ".mm"


def config_path() -> Path:
    return mm_home() / "config.toml"


def db_path() -> Path:
    return mm_home() / "state.db"


def repos_dir() -> Path:
    return mm_home() / "repos"


def runs_dir() -> Path:
    return mm_home() / "runs"


def run_dir() -> Path:
    return mm_home() / "run"


def tools_dir() -> Path:
    return mm_home() / "tools"


def tools_bin_dir() -> Path:
    """Aggregated symlink dir for provisioned companion executables."""
    return tools_dir() / "bin"


def worktree_pools_dir() -> Path:
    return mm_home() / "worktree-pools"


def memory_dir() -> Path:
    return mm_home() / "memory"


def ensure_layout() -> Path:
    """Create the ``.mm`` directory tree if missing and return the root."""
    root = mm_home()
    for path in (
        root,
        repos_dir(),
        runs_dir(),
        run_dir(),
        tools_dir(),
        tools_bin_dir(),
        worktree_pools_dir(),
        memory_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
    return root
