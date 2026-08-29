"""Resolving provisioned companion executables.

Companions (treehouse, lavish-axi, gh-axi) are provisioned by ``mm setup`` /
``mm tools install`` into ``.mm/tools/`` with a stable symlink in
``.mm/tools/bin/``. Runtime code resolves a companion by preferring the
provisioned copy and falling back to whatever is on ``PATH`` — so Middle Manager
works whether the user let us manage the tool or installed it themselves.
"""

from __future__ import annotations

import os
import shutil

from middle_manager.paths import tools_bin_dir

COMPANION_NAMES = ("treehouse", "lavish-axi", "gh-axi")


def companion_bin(name: str) -> str | None:
    """Absolute path to a companion executable, or ``None`` if unavailable.

    Resolution order: the provisioned symlink under ``.mm/tools/bin`` first, then
    the ambient ``PATH``.
    """
    managed = tools_bin_dir() / name
    if managed.exists() and os.access(managed, os.X_OK):
        return str(managed)
    return shutil.which(name)


def has_companion(name: str) -> bool:
    return companion_bin(name) is not None
