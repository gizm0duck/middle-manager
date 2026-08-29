"""Worker routing and hard ceiling enforcement.

The runtime — not the manager prompt — enforces the configured worker ceiling.
Routing selects the smallest eligible worker profile and escalates only on
explicit request, never above the ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from middle_manager.config import MMConfig

# Ordered cheapest -> most capable. Unknown models compare as "unranked".
# Codex exposes cost primarily via reasoning effort rather than model tiers, so
# its ladder holds only the known-valid worker model; keep these names real —
# invented names (e.g. a nonexistent "-mini") make the provider error out.
MODEL_LADDERS: dict[str, list[str]] = {
    "claude": ["haiku", "sonnet", "opus"],
    "codex": ["gpt-5-codex"],
}
REASONING_LADDER = ["low", "medium", "high", "xhigh"]


class CeilingError(Exception):
    """Raised when a requested worker profile exceeds the configured ceiling."""


@dataclass
class WorkerProfile:
    provider: str
    model: str
    reasoning: str


def _reasoning_rank(level: str) -> int:
    try:
        return REASONING_LADDER.index(level)
    except ValueError as exc:
        raise CeilingError(f"unknown reasoning level {level!r}") from exc


def _model_rank(provider: str, model: str) -> int | None:
    ladder = MODEL_LADDERS.get(provider, [])
    return ladder.index(model) if model in ladder else None


def enforce_ceiling(config: MMConfig, profile: WorkerProfile) -> None:
    """Raise CeilingError if ``profile`` exceeds the configured worker ceiling."""
    ceiling = config.worker
    if _reasoning_rank(profile.reasoning) > _reasoning_rank(ceiling.max_reasoning):
        raise CeilingError(
            f"reasoning {profile.reasoning!r} exceeds ceiling {ceiling.max_reasoning!r}"
        )
    # Model comparison is only well-defined within one provider's ladder.
    if profile.provider == ceiling.provider:
        req = _model_rank(profile.provider, profile.model)
        cap = _model_rank(ceiling.provider, ceiling.max_model)
        if req is not None and cap is not None and req > cap:
            raise CeilingError(f"model {profile.model!r} exceeds ceiling {ceiling.max_model!r}")
        if req is None and profile.model != ceiling.max_model:
            raise CeilingError(
                f"model {profile.model!r} is not in the {profile.provider} ladder and "
                f"is not the ceiling model {ceiling.max_model!r}"
            )


def choose_worker(config: MMConfig, *, complexity: str = "normal") -> WorkerProfile:
    """Select the smallest eligible worker profile for a task.

    ``complexity`` in {"normal", "hard"}: normal picks the cheapest model and low
    reasoning; hard bumps one step, clamped to the ceiling.
    """
    provider = config.worker.provider
    ladder = MODEL_LADDERS.get(provider, [config.worker.max_model])
    cap_idx = _model_rank(provider, config.worker.max_model)
    if cap_idx is None:
        cap_idx = len(ladder) - 1

    idx = 0 if complexity == "normal" else min(1, cap_idx)
    idx = min(idx, cap_idx)
    model = ladder[idx] if ladder else config.worker.max_model

    reasoning = "low" if complexity == "normal" else "medium"
    if _reasoning_rank(reasoning) > _reasoning_rank(config.worker.max_reasoning):
        reasoning = config.worker.max_reasoning

    profile = WorkerProfile(provider=provider, model=model, reasoning=reasoning)
    enforce_ceiling(config, profile)  # defensive: selection must never exceed ceiling
    return profile
