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

from free_router.core.exceptions import CapabilityUnsatisfiableError, ValidationError

__all__ = [
    "REQUIRED_INGRESS_FIELDS",
    "FinishReason",
    "NormalizedChunk",
    "NormalizedRequest",
    "NormalizedResponse",
    "TokenUsage",
    "ToolCall",
    "ToolCallDelta",
    "normalize_chat_completion_request",
    "to_openai_completion",
]

REQUIRED_INGRESS_FIELDS = ("model", "messages")
"""The request fields that must be present; everything else is optional.

This is a *required-field* list, not a list of honoured fields. Optional fields
this router reads are named in :func:`normalize_chat_completion_request`; every
other parameter is discarded here and never reaches an adapter (spec 3).
"""

FinishReason = Literal["stop", "length", "content_filter", "tool_calls"]
"""Completion termination reasons, matching the OpenAI vocabulary."""


class ToolCall(BaseModel):
    """An assistant-generated function invocation (OpenAI shape).

    Attributes:
        extra_content: Provider-specific data the client must echo back, keyed
            by provider. Gemini's own OpenAI-compatible surface spells its
            required thought signature as ``{"google": {"thought_signature":
            ...}}``, so the router uses the same spelling rather than inventing
            one. A client that drops the field still gets a usable second turn;
            see the Google adapter for how that is handled.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["function"] = "function"
    function: dict[str, str]  # {"name": ..., "arguments": <JSON string>}
    extra_content: dict[str, Any] | None = None


class ToolCallDelta(BaseModel):
    """One fragment of a streamed tool call (OpenAI delta shape).

    ``extra_content`` matters on this side too: an agentic client streams the
    call, reassembles it, and replays the whole message on the next turn. If the
    signature were only attached to the unary response, the streamed path -- the
    one an agent actually uses -- could never complete its loop.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    id: str | None = None
    function: dict[str, str] | None = None  # partial name / partial arguments
    extra_content: dict[str, Any] | None = None


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
        tools: Client-supplied tool definitions.
        tool_choice: Client-supplied tool selection policy.
        response_format: Client-supplied structured-output constraint.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    include_usage: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None


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
        content: The assistant's message text, or ``None`` when generation
            produced a tool call.
        model_used: The provider-native model id that actually served the
            request, for operational logging.
        finish_reason: Why generation stopped.
        usage: Token counts the provider reported, or ``None`` when it reported
            none. A provider that does not report usage leaves the
            reservation's estimate standing rather than releasing capacity that
            may well have been used.
        tool_calls: Tool calls the model emitted, or ``None`` when it produced text.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    model_used: str
    finish_reason: FinishReason = "stop"
    usage: TokenUsage | None = None
    tool_calls: list[ToolCall] | None = None


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
        tool_call_deltas: Tool-call fragments emitted by this chunk.
    """

    model_config = ConfigDict(extra="forbid")

    delta: str = ""
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None
    tool_call_deltas: list[ToolCallDelta] | None = None


def normalize_chat_completion_request(payload: dict[str, Any]) -> NormalizedRequest:
    """Translate an OpenAI-style request body into a :class:`NormalizedRequest`.

    Honours ``model``, ``messages``, ``stream``, ``stream_options.include_usage``,
    ``tools``, ``tool_choice``, and ``response_format``. Any other parameter --
    including ``temperature``, ``max_tokens``, and ``top_p`` -- is discarded
    here and can therefore never reach a provider adapter (spec 3, Scenario 5).

    Args:
        payload: The decoded JSON request body.

    Returns:
        The internal request representation.

    Raises:
        ValidationError: If ``model`` or ``messages`` is missing, ``messages``
            is empty, a message is missing ``role`` or ``content`` (an assistant
            message carrying ``tool_calls`` may omit ``content``), or its content
            is a part array carrying text only — parts are accepted solely when
            they include an image, which the capability filter then judges.
            Also raised when ``tools``, ``tool_choice`` or a message's
            ``tool_calls`` / ``tool_call_id`` has a shape no adapter can
            translate, so a malformed tool is a request error rather than an
            adapter crash. Raised before any outbound call (spec Scenario 6).
    """
    missing = [name for name in REQUIRED_INGRESS_FIELDS if name not in payload]
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
        if "role" not in message:
            raise ValidationError(
                f"messages[{index}] is missing required field 'role'.",
                code="invalid_message",
                param=f"messages[{index}].role",
            )
        if "content" not in message and not (
            message.get("role") == "assistant" and "tool_calls" in message
        ):
            raise ValidationError(
                f"messages[{index}] is missing required field 'content'.",
                code="invalid_message",
                param=f"messages[{index}].content",
            )
        content = message.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise ValidationError(
                f"messages[{index}].content must be a string, an array of parts, or null.",
                code="invalid_message",
                param=f"messages[{index}].content",
            )
        if isinstance(content, list):
            has_image_part = False
            for part_idx, part in enumerate(content):
                if not isinstance(part, dict):
                    raise ValidationError(
                        f"messages[{index}].content[{part_idx}] must be an object.",
                        code="invalid_message",
                        param=f"messages[{index}].content[{part_idx}]",
                    )
                has_image_part = _validate_content_part(part, index, part_idx) or has_image_part
            if not has_image_part:
                raise ValidationError(
                    "Content part arrays carrying only text are not supported; "
                    "send the message content as a string.",
                    code="invalid_message",
                    param=f"messages[{index}].content",
                )
        _validate_message_tool_fields(message, index)

    return NormalizedRequest(
        model=payload["model"],
        messages=messages,
        stream=_optional_bool(payload, "stream") or False,
        include_usage=_include_usage(payload),
        tools=_optional_tools(payload),
        tool_choice=_optional_tool_choice(payload),
        response_format=_optional_response_format(payload),
    )


def _is_image_part(part: dict[str, Any]) -> bool:
    """Whether a content part carries image input, in either OpenAI spelling.

    Deliberately the same test :func:`~free_router.core.capabilities.detect_requirements`
    applies: a part array this boundary accepts must be one the vision requirement
    will then judge. If the two disagreed, a request could pass normalization and
    reach an adapter that never expected it.
    """
    return (
        part.get("type") in ("image_url", "input_image")
        or "image_url" in part
        or "input_image" in part
    )


_IMAGE_SCHEMES = frozenset({"http", "https"})
"""Schemes R11 refuses: fetching one would make the router an image proxy."""


def _validate_image_url(url: Any, param: str) -> None:
    """Require a base64 ``data:`` image URL, and refuse a remote one by policy.

    R11: the router is not an image proxy. A plain ``http(s)`` URL would have to
    be fetched by somebody, and fetching a client-supplied URL server-side is an
    SSRF surface and an egress proxy this project has no reason to open. The rule
    is deliberately provider-independent — Mistral can fetch URLs, and the
    contract still refuses them, because one consistent rule is worth more than
    provider-specific leniency.

    The check lives here, at the boundary, rather than in each adapter: an
    adapter-level refusal happens after a key is selected and quota is reserved,
    so a request the router never sends still costs capacity and is logged as
    dispatched.

    Raises:
        CapabilityUnsatisfiableError: For a remote ``http(s)`` URL. The client
            sees 400 with the limitation named and nothing was fetched
            (spec Scenario 15).
        ValidationError: For anything that is not a base64 ``data:`` URL.
    """
    if not isinstance(url, str) or not url:
        raise ValidationError(
            f"{param} must be a string carrying an image data URL.",
            code="invalid_field_type",
            param=param,
        )
    scheme, _, _ = url.partition(":")
    if scheme.lower() in _IMAGE_SCHEMES:
        raise CapabilityUnsatisfiableError(
            "Remote http(s) image URLs cannot be fetched by this router. "
            "Encode the image as a base64 data URL and send it inline "
            "(spec R11, ADR-0011).",
            param="messages",
        )
    header, separator, payload = url.partition(",")
    mime_type, _, encoding = header.partition(";")
    if (
        scheme.lower() != "data"
        or not separator
        or encoding.lower() != "base64"
        or not mime_type.partition(":")[2]
        or not payload
    ):
        raise ValidationError(
            f"{param} must be a base64 data URL of the form 'data:<mime-type>;base64,<data>'.",
            code="invalid_field_type",
            param=param,
        )


def _validate_content_part(part: dict[str, Any], index: int, part_idx: int) -> bool:
    """Validate one content part and report whether it carries an image.

    The boundary admits exactly two kinds of part: ``text``, and an image in
    either OpenAI spelling. Anything else used to travel onward, where Gemini
    rendered it with ``str(part)`` — a Python ``repr`` pasted into the prompt as
    if the client had written it — and Mistral forwarded it to a 422. Neither is
    a translation of what was asked for, so the part is refused here instead.

    Returns:
        True when the part is an image.

    Raises:
        ValidationError: If the part is malformed or is not a supported kind.
    """
    param = f"messages[{index}].content[{part_idx}]"
    if _is_image_part(part):
        field = "image_url" if "image_url" in part else "input_image"
        image = part[field]
        _validate_image_url(
            image.get("url") if isinstance(image, dict) else image,
            f"{param}.{field}",
        )
        return True

    if part.get("type") != "text":
        raise ValidationError(
            f"{param}.type must be 'text' or 'image_url'.",
            code="invalid_message",
            param=f"{param}.type",
        )
    if not isinstance(part.get("text"), str):
        raise ValidationError(
            f"{param}.text must be a string.",
            code="invalid_message",
            param=f"{param}.text",
        )
    return False


_TOOL_CHOICE_MODES = frozenset({"auto", "none", "required"})
"""The string forms of ``tool_choice`` this router can express to a provider."""


def _validate_function(function: Any, param: str, *, code: str = "invalid_field_type") -> None:
    """Validate an OpenAI ``function`` object by name.

    Both adapters dereference this object, and it arrives as free-form JSON. A
    string here used to pass the boundary and raise ``AttributeError`` inside
    the adapter, after the request had been accepted — a 500 for a client error.
    Checking the shape once, here, keeps adapter failure modes to provider ones.

    Raises:
        ValidationError: With ``code``, so a top-level ``tools`` entry reports
            ``invalid_field_type`` while a message's ``tool_calls`` entry reports
            ``invalid_message`` like the rest of that message's checks.
    """
    if not isinstance(function, dict):
        raise ValidationError(
            f"{param}.function must be an object.",
            code=code,
            param=f"{param}.function",
        )
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise ValidationError(
            f"{param}.function.name must be a non-empty string.",
            code=code,
            param=f"{param}.function.name",
        )
    description = function.get("description")
    if description is not None and not isinstance(description, str):
        raise ValidationError(
            f"{param}.function.description must be a string.",
            code=code,
            param=f"{param}.function.description",
        )
    parameters = function.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise ValidationError(
            f"{param}.function.parameters must be an object.",
            code=code,
            param=f"{param}.function.parameters",
        )


def _validate_tool_call(tool_call: Any, param: str) -> None:
    """Validate one assistant ``tool_calls`` entry, which adapters read back."""
    if not isinstance(tool_call, dict):
        raise ValidationError(
            f"{param} must be an object.",
            code="invalid_message",
            param=param,
        )
    call_id = tool_call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise ValidationError(
            f"{param}.id must be a non-empty string.",
            code="invalid_message",
            param=f"{param}.id",
        )
    if tool_call.get("type", "function") != "function":
        raise ValidationError(
            f"{param}.type must be 'function'.",
            code="invalid_message",
            param=f"{param}.type",
        )
    _validate_function(tool_call.get("function"), param, code="invalid_message")
    arguments = tool_call["function"].get("arguments", "{}")
    if not isinstance(arguments, str):
        raise ValidationError(
            f"{param}.function.arguments must be a JSON string.",
            code="invalid_message",
            param=f"{param}.function.arguments",
        )
    extra_content = tool_call.get("extra_content")
    if extra_content is not None and not isinstance(extra_content, dict):
        raise ValidationError(
            f"{param}.extra_content must be an object.",
            code="invalid_message",
            param=f"{param}.extra_content",
        )


def _validate_message_tool_fields(message: dict[str, Any], index: int) -> None:
    """Validate the tool-call fields of one message, when present.

    An assistant turn replays calls this router issued, and a ``tool`` turn
    answers one. Mistral forwards both verbatim, so their shape is checked once
    here rather than discovered by a provider rejecting the payload.
    """
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise ValidationError(
                f"messages[{index}].tool_calls must be an array.",
                code="invalid_message",
                param=f"messages[{index}].tool_calls",
            )
        for call_index, tool_call in enumerate(tool_calls):
            _validate_tool_call(tool_call, f"messages[{index}].tool_calls[{call_index}]")

    if message.get("role") == "tool":
        call_id = message.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValidationError(
                f"messages[{index}].tool_call_id must be a non-empty string on a tool message.",
                code="invalid_message",
                param=f"messages[{index}].tool_call_id",
            )
        name = message.get("name")
        if name is not None and not isinstance(name, str):
            raise ValidationError(
                f"messages[{index}].name must be a string.",
                code="invalid_message",
                param=f"messages[{index}].name",
            )


def _optional_tools(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read and validate the optional ``tools`` array."""
    if "tools" not in payload:
        return None
    tools = payload["tools"]
    if not isinstance(tools, list):
        raise ValidationError(
            "'tools' must be an array.",
            code="invalid_field_type",
            param="tools",
        )
    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ValidationError(
                f"tools[{idx}] must be an object.",
                code="invalid_field_type",
                param=f"tools[{idx}]",
            )
        if tool.get("type", "function") != "function":
            raise ValidationError(
                f"tools[{idx}].type must be 'function'.",
                code="invalid_field_type",
                param=f"tools[{idx}].type",
            )
        _validate_function(tool.get("function"), f"tools[{idx}]")
    return tools


def _optional_tool_choice(payload: dict[str, Any]) -> str | dict[str, Any] | None:
    """Read and validate the optional ``tool_choice`` parameter.

    An unrecognised string is refused rather than defaulted. The Google adapter
    used to map anything unknown to ``AUTO``, which silently replaced the
    client's instruction with a different one; Mistral would have forwarded the
    string and let the provider reject it. Refusing here makes both providers
    behave the same way and keeps the adapter's mapping total.
    """
    if "tool_choice" not in payload:
        return None
    tool_choice = payload["tool_choice"]
    if isinstance(tool_choice, str):
        if tool_choice not in _TOOL_CHOICE_MODES:
            raise ValidationError(
                '\'tool_choice\' must be one of "auto", "none", "required", '
                "or a function selector object.",
                code="invalid_field_type",
                param="tool_choice",
            )
        return tool_choice
    if not isinstance(tool_choice, dict):
        raise ValidationError(
            "'tool_choice' must be a string or object.",
            code="invalid_field_type",
            param="tool_choice",
        )
    if tool_choice.get("type") != "function":
        raise ValidationError(
            "'tool_choice.type' must be 'function'.",
            code="invalid_field_type",
            param="tool_choice.type",
        )
    _validate_function(tool_choice.get("function"), "tool_choice")
    return tool_choice


_RESPONSE_FORMAT_TYPES = frozenset({"text", "json_object", "json_schema"})
"""The ``response_format`` forms an OpenAI-compatible client may send."""


def _optional_response_format(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Read and validate the optional ``response_format`` object.

    Only the shape is checked here; the adapters translate it. That split is
    deliberate, and the shape is checked *completely* because a ``json_schema``
    that reaches an adapter without a schema is dangerous rather than merely
    useless: Gemini maps ``type: json_schema`` to a mime type and attaches a
    schema only if it finds one, so a missing schema container degrades to
    "valid JSON, any shape" — a weaker constraint than the client asked for,
    delivered with a 200. Spec §7.9 forbids exactly that, and the only place to
    notice it is before the request is accepted, where both providers can agree.

    ``text`` is legitimate — it is OpenAI's "no constraint" default — so it is
    accepted and left to the adapters, which drop or forward it as they see fit.

    Raises:
        ValidationError: If the field is present and is not an object, names a
            ``type`` no provider can honour, or carries a ``json_schema``
            without a name and an object schema.
    """
    if "response_format" not in payload:
        return None
    response_format = payload["response_format"]
    if not isinstance(response_format, dict):
        raise ValidationError(
            "'response_format' must be an object.",
            code="invalid_field_type",
            param="response_format",
        )
    fmt_type = response_format.get("type")
    if fmt_type not in _RESPONSE_FORMAT_TYPES:
        raise ValidationError(
            "'response_format.type' must be 'text', 'json_object' or 'json_schema'.",
            code="invalid_field_type",
            param="response_format.type",
        )
    if fmt_type != "json_schema":
        return response_format

    container = response_format.get("json_schema")
    if not isinstance(container, dict):
        raise ValidationError(
            "'response_format.json_schema' must be an object.",
            code="invalid_field_type",
            param="response_format.json_schema",
        )
    name = container.get("name")
    if not isinstance(name, str) or not name:
        raise ValidationError(
            "'response_format.json_schema.name' must be a non-empty string.",
            code="invalid_field_type",
            param="response_format.json_schema.name",
        )
    if not isinstance(container.get("schema"), dict):
        raise ValidationError(
            "'response_format.json_schema.schema' must be an object.",
            code="invalid_field_type",
            param="response_format.json_schema.schema",
        )
    return response_format


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
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls is not None:
        # exclude_none keeps the OpenAI shape byte-identical when a provider
        # supplied no provider-specific passthrough data. Only Gemini fills
        # ``extra_content``, and only when it returned a thought signature.
        message["tool_calls"] = [
            tool_call.model_dump(exclude_none=True) for tool_call in response.tool_calls
        ]
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": request_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": response.finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
