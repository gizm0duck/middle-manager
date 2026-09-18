"""Contract-heavy briefs go to the top tier by default; the dispatch says why (issue #78)."""

from __future__ import annotations

import time

from middle_manager import repos, routing
from middle_manager.config import MMConfig, WorkerCeiling, save_config
from middle_manager.providers.fake import FakeProvider
from middle_manager.state import init_db
from middle_manager.supervisor import core
from middle_manager.supervisor.core import Supervisor


def _config() -> MMConfig:
    cfg = MMConfig(worker=WorkerCeiling("claude", "opus", "high", "sonnet", "medium", 2))
    save_config(cfg)
    return cfg


MIGRATION_BRIEF = "# Add events\n\n## In scope\n\nAdd an Alembic migration for the table.\n"
ROUTES_BRIEF = "# Feed\n\n## In scope\n\nExpose two new routes under /api/feed.\n"
STATE_BRIEF = "# Retry\n\n## Semantics\n\nThe back-off is a state machine; table below.\n"
FOLLOW_UP = "# Rename a helper\n\n## In scope\n\nRename `foo` to `bar` in one file.\n"


def test_contract_signals_name_each_reason_and_nothing_for_a_follow_up() -> None:
    assert routing.contract_signals(MIGRATION_BRIEF) == ["adds or touches a migration"]
    assert routing.contract_signals(ROUTES_BRIEF) == ["names new routes or endpoints"]
    assert routing.contract_signals(STATE_BRIEF) == ["specifies a state machine"]
    assert routing.contract_signals(FOLLOW_UP) == []
    # "route" alone is not a signal; adding one is.
    assert routing.contract_signals("Fix the typo on the settings route.") == []


def test_more_than_five_numbered_scope_items_is_a_contract() -> None:
    items = "\n".join(f"{i}. item {i}" for i in range(1, 7))
    brief = f"# Big\n\n## In scope\n\n{items}\n\n## Out of scope\n\n1. not this\n2. nor this\n"
    assert routing.contract_signals(brief) == ["lists 6 numbered scope items"]
    five = "\n".join(f"{i}. item {i}" for i in range(1, 6))
    assert routing.contract_signals(f"# Small\n\n## Scope\n\n{five}\n") == []
    # Numbered items outside a scope section do not count.
    steps = "\n".join(f"{i}. step {i}" for i in range(1, 9))
    assert routing.contract_signals(f"# Steps\n\n## Plan\n\n{steps}\n") == []


def test_route_picks_the_ceiling_for_a_contract_and_the_default_otherwise() -> None:
    cfg = _config()
    top = routing.route(
        cfg, provider="claude", instructions=MIGRATION_BRIEF, model=None, reasoning=None
    )
    assert (top.model, top.reasoning, top.tier) == ("opus", "high", "top-tier")
    assert top.rule.startswith("contract-heavy brief: adds or touches a migration")

    small = routing.route(
        cfg, provider="claude", instructions=FOLLOW_UP, model=None, reasoning=None
    )
    assert (small.model, small.reasoning, small.tier) == ("sonnet", "medium", "default")

    pinned = routing.route(
        cfg, provider="claude", instructions=MIGRATION_BRIEF, model="haiku", reasoning=None
    )
    assert (pinned.model, pinned.reasoning, pinned.tier) == ("haiku", "medium", "explicit")

    # A brief for another provider is never routed onto this ceiling's ladder.
    other = routing.route(
        cfg, provider="codex", instructions=MIGRATION_BRIEF, model=None, reasoning=None
    )
    assert other.tier == "default"


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_dispatch_routes_and_records_the_rule(mm_home, source_repo, monkeypatch) -> None:
    _config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    captured = []
    monkeypatch.setattr(core, "_adapter_for", lambda provider: FakeProvider())

    def capture(runner, spec, execution=None):
        captured.append(spec)
        supervisor._release(execution)

    monkeypatch.setattr(supervisor, "_run_task", capture)

    heavy = supervisor.dispatch_task(
        repo=added.name, title="events", instructions=MIGRATION_BRIEF, provider="claude"
    )
    assert heavy["routing"]["tier"] == "top-tier"
    assert heavy["routing"]["model"] == "opus"
    _wait_for(lambda: len(captured) == 1)
    assert (captured[0].model, captured[0].reasoning) == ("opus", "high")
    row = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'dispatched'",
            (heavy["task_id"],),
        )
        .fetchone()
    )
    assert '"tier": "top-tier"' in row["payload"]

    light = supervisor.dispatch_task(
        repo=added.name, title="rename", instructions=FOLLOW_UP, provider="claude"
    )
    assert light["routing"]["tier"] == "default"
    _wait_for(lambda: len(captured) == 2)
    assert captured[1].model == "sonnet"

    pinned = supervisor.dispatch_task(
        repo=added.name,
        title="events again",
        instructions=MIGRATION_BRIEF,
        provider="claude",
        model="haiku",
    )
    assert pinned["routing"]["tier"] == "explicit"
    _wait_for(lambda: len(captured) == 3)
    assert captured[2].model == "haiku"
