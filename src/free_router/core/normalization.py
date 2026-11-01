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
    "FinishReason",
    "NormalizedChunk",
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
        stream: Whether the client asked for a streamed response.
        include_usage: Whether the client asked for token usage in the stream.
            Ignored unless ``stream`` is set. Providers differ here -- Gemini
            always reports usage on the final chunk, Mistral only when asked --
            so the adapter needs the client's preference to translate it, not to
            invent one.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    include_usage: bool = False


class TokenUsage(BaseModel):
    """Token counts a provider reported for one request.

    **Counts, not content.** A token count says how much was spent, not what was
    said, so it is operational metadata and is safe to reconcile against and to
    persist (CONSTITUTION §9.1). The reverse is not true: a token count cannot
    be used to reconstruct text, and nothing attempts to.

    Attributes:
        input_tokens: Tokens consumed by the request itself.
        output_tokens: Tokens produced by the response.
        total_tokens: The sum, when the provider reported it. Derived from the
            other two when it did not.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    def resolved_total(self) -> int:
        """The total token count, derived when the provider did not report one."""
        if self.total_tokens is not None:
            return self.total_tokens
        return self.input_tokens + self.output_tokens


class NormalizedResponse(BaseModel):
    """A provider-agnostic chat completion result.

    Attributes:
        content: The assistant's message text.
        model_used: The provider-native model id that actually served the
            request, for operational logging.
        finish_reason: Why generation stopped.
        usage: Token counts the provider reported, or ``None`` when it reported
            none. A provider that does not report usage leaves the
            reservation's estimate standing rather than releasing capacity that
            may well have been used.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    model_used: str
    finish_reason: FinishReason = "stop"
    usage: TokenUsage | None = None


class NormalizedChunk(BaseModel):
    """**One** increment of a streamed completion.

    The streaming sibling of :class:`NormalizedResponse`, and deliberately
    *smaller*: a chunk carries only what a provider actually told us, and the
    OpenAI chunk envelope (ids, ``object``, the leading role delta) is the
    writer's business rather than the adapter's.

    This split is what keeps provider knowledge in the adapters and OpenAI
    envelope knowledge in :mod:`free_router.api.streaming`. An adapter that had
    to emit ``chat.completion.chunk`` objects would encode the client protocol in
    two more places.

    Attributes:
        delta: Text appended by this chunk. Often empty -- a provider may send a
            usage-only or finish-only chunk, and those carry no content.
        finish_reason: Set on the chunk that terminates generation, ``None``
            before that. Absent is not the same as ``stop``: a stream cut short
            never sets it, which is how the route distinguishes a truncated
            answer from a finished one.
        usage: Token counts, when the provider reports them. Gemini reports usage
            on the final chunk; Mistral only when asked. ``None`` means the
            reservation's estimate stands, which is the conservative direction.
    """

    model_config = ConfigDict(extra="forbid")

    delta: str = ""
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None


def normalize_chat_completion_request(payload: dict[str, Any]) -> NormalizedRequest:
    """Translate an OpenAI-style request body into a :class:`NormalizedRequest`.

    Only ``model``, ``messages``, ``stream`` and ``stream_options.include_usage``
    are read. Any other parameter -- including ``temperature``, ``max_tokens``,
    and ``top_p`` -- is discarded here and can therefore never reach a provider
    adapter (spec 3, Scenario 5).

    The two streaming fields are read because the route must branch on them and
    the adapter must honour them (B7). They are still read *here* rather than in
    the route, because normalization is the single place that decides which
    fields are meaningful -- a second reader would be a second list of honoured
    fields to keep in step.

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

    return NormalizedRequest(
        model=payload["model"],
        messages=messages,
        stream=_optional_bool(payload, "stream") or False,
        include_usage=_include_usage(payload),
    )


def _optional_bool(payload: dict[str, Any], name: str) -> bool | None:
    """Read an optional boolean field, rejecting a non-boolean rather than coercing.

    ``"stream": "true"`` is a client bug. Coercing it would hide the bug; passing
    the string through would make ``if request.stream`` true for ``"false"``,
    which is worse. So a wrong type is a request error with the field named.

    Raises:
        ValidationError: If the field is present and is not a boolean.
    """
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, bool):
        raise ValidationError(
            f"'{name}' must be a boolean.",
            code="invalid_field_type",
            param=name,
        )
    return value


def _include_usage(payload: dict[str, Any]) -> bool:
    """Read ``stream_options.include_usage``, defaulting to absent.

    Only the one key is read. ``stream_options`` is an extensible OpenAI object
    and a client may legitimately send fields this router does not implement;
    rejecting the whole object because of one unknown key would be stricter than
    the protocol it is imitating.

    Raises:
        ValidationError: If ``stream_options`` is not an object, or
            ``include_usage`` is not a boolean.
    """
    options = payload.get("stream_options")
    if options is None:
        return False
    if not isinstance(options, dict):
        raise ValidationError(
            "'stream_options' must be an object.",
            code="invalid_field_type",
            param="stream_options",
        )
    return bool(_optional_bool(options, "include_usage"))


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
