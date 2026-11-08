"""Capability-aware request detection and requirements derivation (M3).

Pure functions for inspecting incoming OpenAI payloads locally before normalization.
Derives hard requirements (tools, vision, JSON-schema output, streaming) and context
size estimates (input tokens and reserved output) without I/O, external calls, or LLM
classifiers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from free_router.quota.estimation import estimate_input_tokens

__all__ = [
    "RequestRequirements",
    "detect_requirements",
]


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
            estimator. Documented bias: under-counts per-message framing overhead and
            returns 1 for list-shaped content.
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
