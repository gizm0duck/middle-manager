"""Worker routing and hard ceiling enforcement.

The runtime — not the manager prompt — enforces the configured worker ceiling.
Routing selects the smallest eligible worker profile and escalates only on
explicit request, never above the ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from middle_manager.config import MMConfig

# Ordered cheapest -> most capable. Unknown models compare as "unranked".
# Codex exposes cost via reasoning effort, not model tiers, and a ChatGPT
# account rejects any explicit model other than its default, so codex has no
# ladder: the only eligible model is the configured ceiling model itself. Never
# put an invented name here — the provider errors out on it.
MODEL_LADDERS: dict[str, list[str]] = {
    "claude": ["haiku", "sonnet", "opus"],
    "codex": [],
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
        if req is not None and cap is not None:
            if req > cap:
                raise CeilingError(f"model {profile.model!r} exceeds ceiling {ceiling.max_model!r}")
        elif profile.model != ceiling.max_model:
            # Either side unranked: only the ceiling model itself is eligible.
            raise CeilingError(
                f"model {profile.model!r} is not comparable within the {profile.provider} "
                f"ladder and is not the ceiling model {ceiling.max_model!r}"
            )


def choose_worker(config: MMConfig, *, complexity: str = "normal") -> WorkerProfile:
    """Select the smallest eligible worker profile for a task.

    ``complexity`` in {"normal", "hard"}: normal picks the cheapest model and low
    reasoning; hard bumps one step, clamped to the ceiling.
    """
    provider = config.worker.provider
    ladder = MODEL_LADDERS.get(provider, [])
    cap_idx = _model_rank(provider, config.worker.max_model)
    if cap_idx is None:
        # No ladder (codex) or a ceiling model we can't rank: the ceiling model is
        # the only eligible choice. Never substitute a ladder entry for it.
        model = config.worker.max_model
    else:
        idx = 0 if complexity == "normal" else 1
        model = ladder[min(idx, cap_idx)]

    reasoning = "low" if complexity == "normal" else "medium"
    if _reasoning_rank(reasoning) > _reasoning_rank(config.worker.max_reasoning):
        reasoning = config.worker.max_reasoning

    profile = WorkerProfile(provider=provider, model=model, reasoning=reasoning)
    enforce_ceiling(config, profile)  # defensive: selection must never exceed ceiling
    return profile
