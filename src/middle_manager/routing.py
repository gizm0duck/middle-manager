"""Which worker tier a brief goes to when the caller names none.

The runtime used to prefer the smallest eligible worker for every dispatch. In
the first connected-agent window that was wrong for briefs whose correctness
depends on holding a multi-section contract in view: of three default-tier
dispatches, two were closed for under-delivery (one filed four numbered scope
items as "out of scope", twice; one shipped no new tests and a broken legacy
path) and one stopped mid-implementation, while the single top-tier dispatch
delivered a 62-test listener approved on first review (issue #78, assessment
cycle 1, action 1). The cheaper tier satisfies the letter of a scope list and
stops.

So a brief that adds a migration, names new routes, specifies a state machine,
or lists more than five numbered scope items is routed to the top of the
configured ladder — the ceiling's model and reasoning — unless ``--model`` (or
``--reasoning``) says otherwise. Single-file follow-ups keep the configured
default. The dispatch response says which rule fired, so the choice is visible
and can be measured against the closures it was meant to prevent. Nothing here
can exceed the ceiling: the top tier *is* the ceiling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from middle_manager.config import MMConfig

#: More numbered scope items than this, and the brief is a contract, not a chore.
NUMBERED_SCOPE_THRESHOLD = 5

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*\s*$")
_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+\S")
_MIGRATION = re.compile(r"\b(?:migrations?|alembic|down_revision)\b", re.IGNORECASE)
_NEW_ROUTES = re.compile(
    r"\b(?:new|add|adds|added|adding|create|creates|expose|exposes|introduce|introduces)\b"
    r"[^.\n]{0,60}?\b(?:routes?|endpoints?)\b",
    re.IGNORECASE,
)
_STATE_MACHINE = re.compile(
    r"\bstate[ -]machines?\b|\bstate[ -]transitions?\b|\btransition (?:table|truth table)\b",
    re.IGNORECASE,
)
_SCOPE_HEADING = re.compile(r"^(?:in[ -]scope|scope)\b")


@dataclass(frozen=True)
class Route:
    model: str
    reasoning: str
    #: ``"explicit"`` (the caller chose), ``"top-tier"`` or ``"default"``.
    tier: str
    #: Why, in one clause the dispatch output prints.
    rule: str


def contract_signals(text: str) -> list[str]:
    """The reasons a brief counts as contract-heavy, in a stable order; empty when none."""
    reasons: list[str] = []
    if _MIGRATION.search(text):
        reasons.append("adds or touches a migration")
    if _NEW_ROUTES.search(text):
        reasons.append("names new routes or endpoints")
    if _STATE_MACHINE.search(text):
        reasons.append("specifies a state machine")
    numbered = _numbered_scope_items(text)
    if numbered > NUMBERED_SCOPE_THRESHOLD:
        reasons.append(f"lists {numbered} numbered scope items")
    return reasons


def _numbered_scope_items(text: str) -> int:
    """Numbered list items under a Scope / In scope heading (Out of scope excluded)."""
    count = 0
    in_scope = False
    for raw in text.splitlines():
        heading = _HEADING.match(raw)
        if heading is not None:
            in_scope = bool(_SCOPE_HEADING.match(heading.group(2).strip().lower()))
            continue
        if in_scope and _NUMBERED.match(raw):
            count += 1
    return count


def route(
    config: MMConfig,
    *,
    provider: str,
    instructions: str,
    model: str | None,
    reasoning: str | None,
) -> Route:
    """The worker profile a dispatch runs with, and the rule that chose it.

    An explicit model or reasoning is kept as given (the ceiling check happens
    after, as before). Otherwise a contract-heavy brief for the ceiling's own
    provider takes the ceiling's model and reasoning; anything else takes the
    configured default.
    """
    ceiling = config.worker
    default_model = ceiling.default_model or ceiling.max_model
    default_reasoning = ceiling.default_reasoning or ceiling.max_reasoning
    if model or reasoning:
        return Route(
            model=model or default_model,
            reasoning=reasoning or default_reasoning,
            tier="explicit",
            rule="chosen by --model/--reasoning",
        )
    reasons = contract_signals(instructions or "") if provider == ceiling.provider else []
    if reasons:
        return Route(
            model=ceiling.max_model,
            reasoning=ceiling.max_reasoning,
            tier="top-tier",
            rule="contract-heavy brief: " + "; ".join(reasons),
        )
    return Route(
        model=default_model,
        reasoning=default_reasoning,
        tier="default",
        rule="no contract signals in the brief; the configured default tier",
    )


def describe(route: Route) -> str:
    return f"routing: {route.tier} ({route.model}/{route.reasoning}) — {route.rule}"
