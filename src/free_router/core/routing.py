"""Scoring, ranking and selection for the router-controlled model (spec M4, ADR-0010).

Pure functions for scoring model candidates using static operator-declared priors,
enforcing hard eligibility constraints (capabilities, quota, and cooldown),
ranking surviving candidates deterministically with configuration order as a tie-breaker,
and producing an explainable RoutingDecision record before dispatch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from free_router.core.capabilities import (
    CapacityProbe,
    Mismatch,
    RequestRequirements,
    Target,
    mismatch,
    resolve_target,
)
from free_router.core.config import Config, ModelConfig, RoutingConfig, RoutingWeights
from free_router.core.exceptions import (
    CapabilityUnsatisfiableError,
    ContextWindowExceededError,
    PersistenceUnavailableError,
    ProviderRateLimitError,
    RouterBaseError,
    StreamingUnsupportedError,
    UnsupportedModelError,
)

__all__ = [
    "Exclusion",
    "FREE_MODEL_ID",
    "ModelScore",
    "RankedCandidate",
    "RoutingDecision",
    "rank_candidates",
    "resolve_request",
    "score_model",
    "select_model",
]

FREE_MODEL_ID: Literal["free"] = "free"
"""The router-controlled virtual model identifier. Case-sensitive."""


class ModelScore(BaseModel):
    """The computed score for an eligible model under a routing policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: float
    contributions: dict[str, float]
    """Weighted contribution of each dimension, divided by total weights."""
    components: dict[str, float]
    """Raw unweighted normalized components in [0.0, 1.0]."""


class RankedCandidate(BaseModel):
    """A scored model candidate in rank order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    score: float
    components: dict[str, float]


class Exclusion(BaseModel):
    """A model removed from selection with its reason code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    reason: str
    detail: str | None = None


class RoutingDecision(BaseModel):
    """Explainability record emitted for every auto-selection (spec §6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: str = FREE_MODEL_ID
    selected: str
    score: float
    contributions: dict[str, float]
    weights: RoutingWeights
    candidates: tuple[RankedCandidate, ...]
    excluded: tuple[Exclusion, ...]


def score_model(model: ModelConfig, policy: RoutingConfig) -> ModelScore | None:
    """Score a candidate model under a routing policy.

    Returns ``None`` when the model does not declare routing priors (spec R10).

    Dimensions (spec §4):
    - quality_component = quality / 100
    - tool_component = 1 if supports_tools is usable and true, else 0 (assumed counts as 0)
    - latency_component = max(0, 1 - latency_ms / latency_ceiling_ms)
    """
    if model.routing is None:
        return None

    quality_comp = model.routing.quality / 100.0

    tool_comp = (
        1.0
        if (
            model.capabilities.supports_tools.is_usable
            and model.capabilities.supports_tools.value is True
        )
        else 0.0
    )

    latency_comp = max(0.0, 1.0 - (model.routing.latency_ms / policy.latency_ceiling_ms))

    weights = policy.weights
    w_total = weights.quality + weights.tool_support + weights.latency

    c_quality = (weights.quality * quality_comp) / w_total
    c_tool = (weights.tool_support * tool_comp) / w_total
    c_latency = (weights.latency * latency_comp) / w_total
    total = c_quality + c_tool + c_latency

    return ModelScore(
        total=total,
        contributions={
            "quality": c_quality,
            "tool_support": c_tool,
            "latency": c_latency,
        },
        components={
            "quality": quality_comp,
            "tool_support": tool_comp,
            "latency": latency_comp,
        },
    )


def rank_candidates(
    candidates_with_scores: list[tuple[int, ModelConfig, ModelScore]],
) -> list[tuple[ModelConfig, ModelScore]]:
    """Sort surviving scored candidates deterministically.

    Sorts by ``(-total_score, config_index)`` so ties resolve to declaration order
    and the same configuration always produces the same ranking (spec §4, ADR-0010).
    """
    sorted_items = sorted(
        candidates_with_scores,
        key=lambda item: (-item[2].total, item[0]),
    )
    return [(model, score) for _, model, score in sorted_items]


def _capability_reason(
    m: Mismatch,
    model: ModelConfig,
    requirements: RequestRequirements,
) -> str:
    """Name the reason code for a capability exclusion (spec §6)."""
    if m.code == "streaming_unsupported":
        return "streaming"
    if m.code == "context_window_exceeded":
        return "context_window"
    if m.param == "tools":
        return "tools"
    if m.param == "response_format":
        return "response_format"
    if m.param == "messages":
        caps = model.capabilities
        vision_usable = caps.supports_vision.is_usable and caps.supports_vision.value is True
        if requirements.requires_vision and not vision_usable:
            return "vision"
        # An unverified window reports as "context_window" too: the request
        # cannot be shown to fit, and that is the constraint that failed.
        return "context_window"
    return "capability_unsatisfiable"


def _capability_error(m: Mismatch) -> RouterBaseError:
    """The M3 error a capability mismatch would raise on its own."""
    if m.code == "streaming_unsupported":
        return StreamingUnsupportedError(m.detail, param="stream")
    if m.code == "context_window_exceeded":
        return ContextWindowExceededError(m.detail, param="messages")
    return CapabilityUnsatisfiableError(m.detail, param=m.param)


def _capacity_reason(error: RouterBaseError) -> str:
    """Name the reason code for a capacity exclusion (spec §3 step 2).

    Derived from the exception type, not from message text: dispatch already
    distinguishes cooling (429), an unrecordable limit, and exhausted/breached
    capacity, and those are the distinctions an operator needs in the log.
    """
    if isinstance(error, ProviderRateLimitError):
        return "cooling"
    if isinstance(error, PersistenceUnavailableError):
        return "persistence_degraded"
    return "no_quota"


def select_model(
    config: Config,
    requirements: RequestRequirements,
    *,
    capacity_probe: CapacityProbe | None = None,
) -> tuple[Target, RoutingDecision]:
    """Select the highest-scoring eligible model for a 'free' request.

    Sweeps every model through:
    1. Capability filter (mismatch check).
    2. Capacity probe (Dispatcher._check_capacity).
    3. Priors check (model.routing is not None).
    4. Scoring and ranking survivors.

    Raises:
        RouterBaseError: Appropriate refusal if no candidates survive.
    """
    if config.routing is None:
        available = ", ".join(m.id for m in config.models)
        raise UnsupportedModelError(
            f"Model '{FREE_MODEL_ID}' is not available. Configured models: {available}.",
            param="model",
        )

    excluded: list[Exclusion] = []
    candidates_with_scores: list[tuple[int, ModelConfig, ModelScore]] = []

    first_capability_error: RouterBaseError | None = None
    # Only a capacity failure on a *scoreable* model explains why ``free``
    # could not be served: a model without priors is not a candidate at any
    # score, so its capacity is irrelevant to the decision (R10).
    first_scoreable_capacity_error: RouterBaseError | None = None

    for idx, model in enumerate(config.models):
        # 1. Capability filtering: a required capability removes the model
        #    before scoring, and the exclusion is recorded (spec §3).
        m = mismatch(model, requirements)
        if m is not None:
            excluded.append(
                Exclusion(
                    model=model.id,
                    reason=_capability_reason(m, model, requirements),
                    detail=m.detail,
                )
            )
            if first_capability_error is None:
                first_capability_error = _capability_error(m)
            continue

        # 2. Quota and cooldown: binary, never proportional (spec §3).
        if capacity_probe is not None:
            unavailable = capacity_probe(model, requirements.input_tokens)
            if unavailable is not None:
                excluded.append(
                    Exclusion(
                        model=model.id,
                        reason=_capacity_reason(unavailable),
                        detail=str(unavailable),
                    )
                )
                if model.routing is not None and first_scoreable_capacity_error is None:
                    first_scoreable_capacity_error = unavailable
                continue

        # 3. Selection: a model without priors is not a candidate for ``free``
        #    at any score, but stays explicitly selectable (R10).
        if model.routing is None:
            excluded.append(
                Exclusion(
                    model=model.id,
                    reason="not_configured_for_routing",
                    detail="no routing priors",
                )
            )
            continue

        # 4. Ranking: score every survivor.
        score = score_model(model, config.routing)
        assert score is not None
        candidates_with_scores.append((idx, model, score))

    # Handle case where no model survived.
    if not candidates_with_scores:
        considered = ", ".join(f"{e.model} ({e.reason})" for e in excluded)
        base_message = (
            f"No configured model can serve '{FREE_MODEL_ID}' for this request. "
            f"Considered: {considered}."
        )

        if first_scoreable_capacity_error is not None:
            if isinstance(first_scoreable_capacity_error, ProviderRateLimitError):
                raise ProviderRateLimitError(
                    base_message,
                    retry_after=first_scoreable_capacity_error.retry_after,
                    provider_code=first_scoreable_capacity_error.provider_code,
                )
            raise type(first_scoreable_capacity_error)(base_message)

        if first_capability_error is not None:
            if isinstance(first_capability_error, StreamingUnsupportedError):
                raise StreamingUnsupportedError(base_message, param="stream")
            if isinstance(first_capability_error, ContextWindowExceededError):
                raise ContextWindowExceededError(base_message, param="messages")
            raise CapabilityUnsatisfiableError(
                base_message, param=first_capability_error.param or "messages"
            )

        raise CapabilityUnsatisfiableError(base_message, param="model")

    # 5. Rank survivors
    ranked = rank_candidates(candidates_with_scores)
    winner_model, winner_score = ranked[0]

    ranked_candidates = tuple(
        RankedCandidate(
            model=m.id,
            score=round(s.total, 3),
            components={k: round(v, 3) for k, v in s.components.items()},
        )
        for m, s in ranked
    )

    decision = RoutingDecision(
        requested=FREE_MODEL_ID,
        selected=winner_model.id,
        score=round(winner_score.total, 3),
        contributions={k: round(v, 3) for k, v in winner_score.contributions.items()},
        weights=config.routing.weights,
        candidates=ranked_candidates,
        excluded=tuple(excluded),
    )

    target = Target(
        model=winner_model,
        selection="auto",
        substituted_from=None,
        forcing_requirement=None,
        score=winner_score,
    )
    return target, decision


def resolve_request(
    config: Config,
    requirements: RequestRequirements,
    requested_model: str,
    *,
    capacity_probe: CapacityProbe | None = None,
) -> tuple[Target, RoutingDecision | None]:
    """Single resolution entry point for both unary and streaming routes (spec §1, plan Part 3).

    - If requested_model == "free": evaluates routing policy and selects the best candidate.
    - If requested_model != "free": delegates to resolve_target unchanged.
    """
    if requested_model != FREE_MODEL_ID:
        target = resolve_target(
            config,
            requirements,
            requested_model,
            capacity_probe=capacity_probe,
        )
        return target, None

    if config.routing is None:
        available = ", ".join(m.id for m in config.models)
        raise UnsupportedModelError(
            f"Model '{FREE_MODEL_ID}' is not available. Configured models: {available}.",
            param="model",
        )

    return select_model(config, requirements, capacity_probe=capacity_probe)
