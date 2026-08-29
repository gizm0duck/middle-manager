"""The interactive manager entry point (`mm start`).

Launches the configured manager harness (Claude or Codex) as an interactive
session with the Middle Manager runtime contract in force, so the user talks to
one manager in natural language and the manager drives the whole `mm` control
plane on their behalf.
"""

from __future__ import annotations

from middle_manager.manager.launch import Launch, ManagerLaunchError, build_launch, start

__all__ = ["Launch", "ManagerLaunchError", "build_launch", "start"]
