"""Load the committed provider capability matrix and gate steering behavior.

Adapters consult this to decide whether they may offer mid-flight interrupt
steering (versus checkpoint-at-completion steering plus a fresh-session recovery
packet). Fail-closed: a missing file, provider, or flag reads as ``False``, and a
CLI whose version no longer matches the recorded probe is treated as unproven.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import middle_manager

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(middle_manager.__file__)))
_DEFAULT_PATH = os.path.join(_REPO_ROOT, "provider-capabilities.json")

_ALL_FLAGS = (
    "session_id_in_stream",
    "usage_in_stream",
    "resume_after_clean_exit",
    "session_survives_process_exit",
    "resume_after_sigint_model_turn",
    "resume_after_sigint_tool",
    "resume_after_sigkill",
    "worktree_resume",
    "duplicate_resume_ok",
    "mid_process_steer",
    "requires_resume_prompt",
    "orphan_tool_process",
)


@lru_cache(maxsize=8)
def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("providers", {})
    except (ValueError, OSError):
        return {}


def capabilities_for(
    provider: str, cli_version: str | None = None, path: str | None = None
) -> dict[str, bool]:
    """Return the capability flags for a provider, all False if unproven.

    If ``cli_version`` is given and differs from the recorded probe version, the
    record is treated as not applicable (all flags False), since interrupt/resume
    behavior drifts across CLI versions.
    """
    providers = _load(path or _DEFAULT_PATH)
    record = providers.get(provider)
    base = dict.fromkeys(_ALL_FLAGS, False)
    if not record:
        return base
    if cli_version is not None and record.get("cli_version") not in (None, cli_version):
        # Version drift: re-probe required before trusting the old record.
        return base
    caps = record.get("capabilities", {})
    for flag in _ALL_FLAGS:
        base[flag] = bool(caps.get(flag, False))
    return base


def allows_interrupt_steer(
    provider: str, cli_version: str | None = None, path: str | None = None
) -> bool:
    """True only when the provider/version proved resumable mid-work interrupts.

    Requires the strong proofs (a real in-progress tool interrupt and an
    uncatchable crash), not the racy model-turn timing alone.
    """
    caps = capabilities_for(provider, cli_version, path)
    return bool(
        caps["resume_after_sigint_tool"]
        and caps["resume_after_sigkill"]
        and caps["session_survives_process_exit"]
    )


def recorded_version(provider: str, path: str | None = None) -> str | None:
    providers = _load(path or _DEFAULT_PATH)
    record = providers.get(provider)
    return record.get("cli_version") if record else None
