"""Alignment with the user goes on a Lavish review surface, the same way every time.

A plan for the user to review, or more than two things to align on, goes on a
surface. Before 2026-11-21 that was a judgment call ("when a plan is easier to see
than to read"), development sessions never read the runtime contract at all, and
plans for alignment kept landing in the chat as prose. Every place an agent learns
how to work now names the rule and the skill that owns it, so a later edit can't
quietly turn it back into a judgment call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from middle_manager.manager import launch

ROOT = Path(__file__).resolve().parent.parent
SKILL = ".agents/skills/align-with-the-user/SKILL.md"


def _text(rel: str) -> str:
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    "doc",
    ["AGENTS.md", "docs/manager-runtime.md", ".agents/skills/review-surfaces/SKILL.md"],
)
def test_every_contract_routes_alignment_through_the_skill(doc: str) -> None:
    text = _text(doc)
    assert "align-with-the-user" in text
    assert "more than two things to align on" in text


def test_the_skill_states_the_rule() -> None:
    text = _text(SKILL)
    assert "name: align-with-the-user" in text
    assert (
        "| A plan or proposal for the user to review, of any size | A Lavish review surface |"
        in text
    )
    assert "| More than two things to align on | A Lavish review surface |" in text
    # It hands the mechanics to the surface skill rather than driving lavish-axi itself.
    assert "review-surfaces" in text
    assert "lavish-axi poll" not in text


def test_the_runtime_manager_is_launched_knowing_the_rule() -> None:
    assert "align-with-the-user" in launch._ROLE
    assert "more than two things to align on" in launch._ROLE
