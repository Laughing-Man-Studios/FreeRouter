"""Capability-aware request detection and requirements derivation (M3).

Pure functions for inspecting incoming OpenAI payloads locally before normalization.
Derives hard requirements (tools, vision, JSON-schema output, streaming) and context
size estimates (input tokens and reserved output) without I/O, external calls, or LLM
classifiers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from free_router.core.config import Config, ModelConfig
from free_router.core.exceptions import (
    CapabilityUnsatisfiableError,
    ContextWindowExceededError,
    RouterBaseError,
    StreamingUnsupportedError,
    UnsupportedModelError,
)
from free_router.quota.estimation import estimate_input_tokens

__all__ = [
    "CapacityProbe",
    "Mismatch",
    "RequestRequirements",
    "Target",
    "detect_requirements",
    "eligible_models",
    "mismatch",
    "resolve_target",
]

CapacityProbe = Callable[[ModelConfig, int], RouterBaseError | None]
"""Answers "may this model be dispatched right now, for this many input tokens?".

Returns ``None`` when capacity is available, or the error the dispatcher would
raise (cooling, breaker-degraded, out of quota, persistence-degraded). Injected
by the route so fallback selection consults the dispatcher's own capacity gate
rather than keeping a second copy of the quota, cooldown and breaker rules.
"""


class RequestRequirements(BaseModel):
    """Derived hard requirements from an incoming OpenAI payload.

    Purely derived from the raw request payload; performs no I/O and makes no outbound
    calls.

    Attributes:
        requires_tools: True if ``tools`` is present and non-empty, or ``tool_choice``
            requests one. An empty ``tools: []`` does not require tools; ``tool_choice: "none"``
            alongside non-empty ``tools`` does require tools (forwarding is what matters).
        requires_vision: True if any message content part is of type ``image_url`` or
            ``input_image`` (URL and base64/data-URI forms).
        requires_json_schema: True if ``response_format`` is ``json_object`` or
            ``json_schema``. Both map to structured output requirements.
        requires_streaming: True if ``payload.stream is True``.
        input_tokens: Estimated input tokens computed using the shared reservation
            estimator, which counts per-message framing overhead, serialised tool
            and response-format payloads, and image parts by a configured
            tokens-per-image assumption rather than by base64 length.
        reserved_output: Expected response tokens: ``max_tokens`` or ``max_completion_tokens``
            when either is a positive integer, otherwise the caller-supplied
            ``assumed_output`` (normally ``quota.output_token_assumption``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requires_tools: bool = Field(
        ...,
        description="Whether the request requires tool-calling capabilities.",
    )
    requires_vision: bool = Field(
        ...,
        description="Whether the request requires multimodal/vision input.",
    )
    requires_json_schema: bool = Field(
        ...,
        description="Whether the request requires JSON-schema / structured output.",
    )
    requires_streaming: bool = Field(
        ...,
        description="Whether the request requires streaming.",
    )
    input_tokens: int = Field(
        ...,
        description="Estimated input tokens from shared reservation estimator.",
    )
    reserved_output: int = Field(
        ...,
        description="Reserved output tokens from max_tokens or output token assumption.",
    )


def _detect_tools(payload: dict[str, Any]) -> bool:
    """Detect whether tools are required by tools array or tool_choice."""
    tools = payload.get("tools")
    if isinstance(tools, list) and len(tools) > 0:
        return True
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        # Only a function object requests a specific tool. ``{}`` and
        # ``{"type": "none"}`` do not.
        return tool_choice.get("type") == "function"
    if isinstance(tool_choice, str):
        # ``auto`` lets the model decide and ``none`` forbids calling a tool;
        # neither is a hard requirement. Only ``required`` asks for one.
        return tool_choice == "required"
    return False


def _detect_vision(messages: Any) -> bool:
    """Detect whether any message content contains vision parts."""
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    part_type = part.get("type")
                    if (
                        part_type in ("image_url", "input_image")
                        or "image_url" in part
                        or "input_image" in part
                    ):
                        return True
    return False


def _detect_json_schema(payload: dict[str, Any]) -> bool:
    """Detect whether response_format requires structured output."""
    fmt = payload.get("response_format")
    if isinstance(fmt, dict):
        return fmt.get("type") in ("json_object", "json_schema")
    if isinstance(fmt, str):
        return fmt in ("json_object", "json_schema")
    return False


def _detect_streaming(payload: dict[str, Any]) -> bool:
    """Detect whether streaming was explicitly requested."""
    return payload.get("stream") is True


def _positive_int(value: Any) -> int | None:
    """Return ``value`` only when it is a positive integer, not a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _reserved_output(payload: dict[str, Any], assumed_output: int) -> int:
    """Resolve the output reservation, ignoring malformed token limits.

    Detection runs before normalization, which strips these fields without
    validating them. A string, boolean, zero or negative limit is not a usable
    reservation, so it falls through to the caller's assumption rather than
    raising or under-reserving.
    """
    for key in ("max_tokens", "max_completion_tokens"):
        limit = _positive_int(payload.get(key))
        if limit is not None:
            return limit
    return assumed_output


def detect_requirements(
    payload: dict[str, Any],
    assumed_output: int,
) -> RequestRequirements:
    """Derive hard capability requirements and token sizing from a raw request payload.

    Must be evaluated on the raw payload *before* normalization, because
    `normalize_chat_completion_request` discards fields like `tools`, `tool_choice`,
    and `response_format`.

    Args:
        payload: Decoded JSON request payload received from client.
        assumed_output: Tokens assumed for output when neither `max_tokens` nor
            `max_completion_tokens` carries a positive integer. Callers pass
            ``quota.output_token_assumption`` so the configured policy is not
            bypassed; there is deliberately no default.

    Returns:
        Immutable ``RequestRequirements`` describing the hard requirements of the payload.
    """
    messages = payload.get("messages")
    return RequestRequirements(
        requires_tools=_detect_tools(payload),
        requires_vision=_detect_vision(messages),
        requires_json_schema=_detect_json_schema(payload),
        requires_streaming=_detect_streaming(payload),
        input_tokens=estimate_input_tokens(payload),
        reserved_output=_reserved_output(payload, assumed_output),
    )


class Mismatch(BaseModel):
    """Why a model cannot serve a request. Maps 1:1 to an error in spec §6."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["capability_unsatisfiable", "context_window_exceeded", "streaming_unsupported"]
    param: str
    detail: str


@dataclass(frozen=True, slots=True)
class Target:
    """Resolved execution target for a request."""

    model: ModelConfig
    substituted_from: str | None = None
    forcing_requirement: str | None = None


def _capability_mismatch(model: ModelConfig, requirements: RequestRequirements) -> Mismatch | None:
    """Check the boolean capabilities, ignoring request size."""
    caps = model.capabilities

    if requirements.requires_tools:
        if not (caps.supports_tools.is_usable and caps.supports_tools.value):
            return Mismatch(
                code="capability_unsatisfiable",
                param="tools",
                detail="tools are required but model does not support tool calling.",
            )

    if requirements.requires_vision:
        if not (caps.supports_vision.is_usable and caps.supports_vision.value):
            return Mismatch(
                code="capability_unsatisfiable",
                param="messages",
                detail="vision is required but model does not support multimodal/vision input.",
            )

    if requirements.requires_json_schema:
        if not (caps.supports_json_schema.is_usable and caps.supports_json_schema.value):
            return Mismatch(
                code="capability_unsatisfiable",
                param="response_format",
                detail="JSON schema / structured output is required but model does not support it.",
            )

    if requirements.requires_streaming:
        if not (caps.supports_streaming.is_usable and caps.supports_streaming.value):
            return Mismatch(
                code="streaming_unsupported",
                param="stream",
                detail="streaming was requested but model does not support streaming.",
            )

    return None


def _context_mismatch(model: ModelConfig, requirements: RequestRequirements) -> Mismatch | None:
    """Check whether the request fits the model's context window.

    An unusable (``assumed``) window is *unknown*, not too small: it is reported
    as an unsatisfiable capability, because claiming the request is oversized
    would be a statement the router cannot support.
    """
    window = model.capabilities.context_window
    if not window.is_usable:
        return Mismatch(
            code="capability_unsatisfiable",
            param="messages",
            detail=(
                "the model's context window is not verified, so the request size cannot be checked."
            ),
        )

    total_tokens = requirements.input_tokens + requirements.reserved_output
    if total_tokens > window.value:
        return Mismatch(
            code="context_window_exceeded",
            param="messages",
            detail=(
                f"request requires about {requirements.input_tokens:,} input tokens "
                f"plus {requirements.reserved_output:,} reserved output tokens."
            ),
        )

    return None


def mismatch(model: ModelConfig, requirements: RequestRequirements) -> Mismatch | None:
    """Evaluate whether ``model`` satisfies all hard ``requirements``.

    Returns ``None`` if all hard requirements are satisfied, or a :class:`Mismatch`
    identifying the unsatisfied requirement under fail-closed semantics (provenance
    must be usable, context comparison is inclusive: total <= context_window).
    """
    capability = _capability_mismatch(model, requirements)
    if capability is not None:
        return capability
    return _context_mismatch(model, requirements)


def eligible_models(config: Config, requirements: RequestRequirements) -> list[ModelConfig]:
    """Return all models in ``config.models`` that satisfy ``requirements``."""
    return [model for model in config.models if mismatch(model, requirements) is None]


def resolve_target(
    config: Config,
    requirements: RequestRequirements,
    requested_model: str,
    *,
    capacity_probe: CapacityProbe | None = None,
) -> Target:
    """Resolve the execution target for ``requested_model`` under ``requirements``.

    The requested model is honoured whenever it is eligible. If it is not, its
    ``fallback_models`` are tried in declared order and the first entry that is
    both capability-eligible and currently dispatchable wins.

    ``capacity_probe`` is step 2 of the pipeline (quota and cooldown) applied to
    fallback candidates. A fallback that is cooling, breaker-degraded or out of
    quota is skipped rather than selected and then refused by the dispatcher. The
    requested model is never probed: a capacity failure on it keeps its M2
    outcome and must not trigger substitution.

    Returns a :class:`Target` if the requested model or an eligible opt-in fallback
    can serve the request. Raises an appropriate error if nothing can: the first
    capacity error if capable fallbacks existed but none were available, otherwise
    the capability failure the requested model reported.
    """
    model = config.model_by_id(requested_model)
    if model is None:
        available = ", ".join(entry.id for entry in config.models)
        raise UnsupportedModelError(
            f"Model '{requested_model}' is not available. Configured models: {available}.",
            param="model",
        )

    m = mismatch(model, requirements)
    if m is None:
        return Target(model=model, substituted_from=None, forcing_requirement=None)

    considered: list[ModelConfig] = [model]
    capacity_error: RouterBaseError | None = None
    for fallback_id in model.fallback_models:
        fallback = config.model_by_id(fallback_id)
        if fallback is None:
            continue
        considered.append(fallback)
        if mismatch(fallback, requirements) is not None:
            continue

        if capacity_probe is not None:
            unavailable = capacity_probe(fallback, requirements.input_tokens)
            if unavailable is not None:
                if capacity_error is None:
                    capacity_error = unavailable
                continue

        return Target(
            model=fallback,
            substituted_from=requested_model,
            forcing_requirement=m.code,
        )

    # A capability-eligible fallback existed but was not dispatchable. That is a
    # capacity outcome, not a capability one: keep the M2 status code instead of
    # reporting a misleading 400.
    if capacity_error is not None:
        raise capacity_error

    considered_ids = ", ".join(c.id for c in considered)
    if m.code == "streaming_unsupported":
        raise StreamingUnsupportedError(
            f"Model '{requested_model}' does not support streaming.",
            param="stream",
        )

    if m.code == "context_window_exceeded":
        # Only models that satisfy the non-context requirements can contribute a
        # usable window; an ineligible fallback's window is not an option.
        largest_window = max(
            (
                c.capabilities.context_window.value
                for c in considered
                if _capability_mismatch(c, requirements) is None
                and c.capabilities.context_window.is_usable
            ),
            default=0,
        )
        message = (
            f"No configured model can accept this request: it requires about "
            f"{requirements.input_tokens:,} input tokens plus "
            f"{requirements.reserved_output:,} reserved output tokens, and the largest "
            f"eligible context window is {largest_window:,}. Considered: {considered_ids}."
        )
        raise ContextWindowExceededError(message, param="messages")

    message = (
        f"No configured model can accept this request: {m.detail} Considered: {considered_ids}."
    )
    raise CapabilityUnsatisfiableError(message, param=m.param)
