"""The internal normalization boundary (spec 2, CONSTITUTION 3).

This module owns the only data contract the router core is allowed to see.
Clients speak OpenAI-shaped JSON and providers speak their own native schemas;
neither vocabulary leaks past this boundary. The ingress normalizer builds a
:class:`NormalizedRequest`, a provider adapter builds a
:class:`NormalizedResponse`, and nothing in between knows which provider was
used.

Both models are ``extra="forbid"``. That is deliberate: M0 supports only
``model`` and ``messages``, and a model that silently accepts unknown fields
would make it impossible to prove that ``temperature`` and ``max_tokens`` were
stripped rather than forwarded (spec 3, Scenario 5).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from free_router.core.exceptions import ValidationError

__all__ = [
    "SUPPORTED_INGRESS_FIELDS",
    "NormalizedRequest",
    "NormalizedResponse",
    "normalize_chat_completion_request",
    "to_openai_completion",
]

SUPPORTED_INGRESS_FIELDS = ("model", "messages")
"""The only request fields M0 honours. Everything else is discarded (spec 3)."""

FinishReason = Literal["stop", "length", "content_filter"]
"""Completion termination reasons, matching the OpenAI vocabulary."""


class NormalizedRequest(BaseModel):
    """A provider-agnostic chat completion request.

    Attributes:
        model: The externally-addressable model id, e.g.
            ``google/gemini-3.5-flash-lite``.
        messages: The conversation so far, in OpenAI ``{role, content}`` shape.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)


class NormalizedResponse(BaseModel):
    """A provider-agnostic chat completion result.

    Attributes:
        content: The assistant's message text.
        model_used: The provider-native model id that actually served the
            request, for operational logging.
        finish_reason: Why generation stopped.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    model_used: str
    finish_reason: FinishReason = "stop"


def normalize_chat_completion_request(payload: dict[str, Any]) -> NormalizedRequest:
    """Translate an OpenAI-style request body into a :class:`NormalizedRequest`.

    Only ``model`` and ``messages`` are read. Any other parameter -- including
    ``temperature``, ``max_tokens``, and ``top_p`` -- is discarded here and can
    therefore never reach a provider adapter (spec 3, Scenario 5).

    Args:
        payload: The decoded JSON request body.

    Returns:
        The internal request representation.

    Raises:
        ValidationError: If ``model`` or ``messages`` is missing, ``messages``
            is empty, or a message is missing ``role``/``content``. Raised
            before any outbound call (spec Scenario 6).
    """
    missing = [name for name in SUPPORTED_INGRESS_FIELDS if name not in payload]
    if missing:
        raise ValidationError(
            f"Missing required field(s): {', '.join(missing)}.",
            code="missing_required_field",
            param=missing[0],
        )

    messages = payload["messages"]
    if not isinstance(messages, list):
        raise ValidationError(
            "'messages' must be an array of message objects.",
            code="invalid_messages",
            param="messages",
        )
    if not messages:
        raise ValidationError(
            "'messages' must contain at least one message.",
            code="empty_messages",
            param="messages",
        )

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValidationError(
                f"messages[{index}] must be an object.",
                code="invalid_message",
                param=f"messages[{index}]",
            )
        for field in ("role", "content"):
            if field not in message:
                raise ValidationError(
                    f"messages[{index}] is missing required field '{field}'.",
                    code="invalid_message",
                    param=f"messages[{index}].{field}",
                )

    return NormalizedRequest(model=payload["model"], messages=messages)


def to_openai_completion(
    response: NormalizedResponse,
    *,
    request_model: str,
    response_id: str,
    created: int,
) -> dict[str, Any]:
    """Render a :class:`NormalizedResponse` as an OpenAI completion object.

    Args:
        response: The internal result to serialise.
        request_model: The model the client asked for, echoed back as ``model``.
        response_id: The completion id for this response.
        created: Unix timestamp for the ``created`` field.

    Returns:
        A JSON-ready OpenAI-compatible chat completion object (spec 5).
    """
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": request_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.content},
                "finish_reason": response.finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
