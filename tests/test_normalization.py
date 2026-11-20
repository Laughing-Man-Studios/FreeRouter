"""Ingress normalization and egress serialization."""

from typing import get_args

import pytest

from free_router.core.exceptions import ValidationError
from free_router.core.normalization import (
    FinishReason,
    NormalizedChunk,
    NormalizedRequest,
    NormalizedResponse,
    ToolCall,
    ToolCallDelta,
    normalize_chat_completion_request,
    to_openai_completion,
)

VALID_PAYLOAD = {
    "model": "google/gemini-3.5-flash-lite",
    "messages": [{"role": "user", "content": "hello"}],
}


def test_normalizer_keeps_only_honoured_fields() -> None:
    """Scenario 5: unsupported parameters are discarded, not forwarded.

    ``stream`` and ``include_usage`` are honoured since M2 (T059, B7).
    ``tools``, ``tool_choice``, and ``response_format`` are honoured in M4 (T076).
    Everything else -- including generation controls the providers would
    happily accept -- must not survive, which is what the exclusion
    assertions pin.
    """
    payload = {**VALID_PAYLOAD, "temperature": 0.7, "max_tokens": 100, "top_p": 0.9}

    normalized = normalize_chat_completion_request(payload)

    assert isinstance(normalized, NormalizedRequest)
    assert normalized.model == "google/gemini-3.5-flash-lite"
    assert normalized.messages == [{"role": "user", "content": "hello"}]
    dumped = normalized.model_dump()
    assert set(dumped) == {
        "model",
        "messages",
        "stream",
        "include_usage",
        "tools",
        "tool_choice",
        "response_format",
    }
    assert "temperature" not in dumped
    assert "max_tokens" not in dumped
    assert "top_p" not in dumped
    assert normalized.stream is False
    assert normalized.include_usage is False
    assert normalized.tools is None
    assert normalized.tool_choice is None
    assert normalized.response_format is None


def test_normalized_request_forbids_extra_fields() -> None:
    """The stripped parameters must be structurally unable to survive."""
    with pytest.raises(ValueError):
        NormalizedRequest(**VALID_PAYLOAD, temperature=0.7)  # type: ignore[call-arg]


def test_normalizer_rejects_empty_messages() -> None:
    """Scenario 6: an empty messages array fails before dispatch."""
    with pytest.raises(ValidationError) as excinfo:
        normalize_chat_completion_request({"model": "google/x", "messages": []})

    assert excinfo.value.code == "empty_messages"
    assert excinfo.value.param == "messages"
    assert excinfo.value.error_type == "invalid_request_error"


def test_normalizer_rejects_missing_fields() -> None:
    """A payload without messages names the missing field in the error."""
    with pytest.raises(ValidationError) as excinfo:
        normalize_chat_completion_request({"model": "google/x"})

    assert excinfo.value.code == "missing_required_field"
    assert excinfo.value.param == "messages"


def test_normalizer_rejects_message_missing_role() -> None:
    """A message without a role is reported against its exact location."""
    with pytest.raises(ValidationError) as excinfo:
        normalize_chat_completion_request({"model": "google/x", "messages": [{"content": "hi"}]})

    assert excinfo.value.param == "messages[0].role"


def test_error_payload_is_openai_shaped() -> None:
    """The error envelope matches the documented OpenAI structure."""
    error = ValidationError("bad request", code="invalid_request", param="messages")

    assert error.to_payload() == {
        "error": {
            "message": "bad request",
            "type": "invalid_request_error",
            "param": "messages",
            "code": "invalid_request",
        }
    }


def test_egress_renders_openai_completion() -> None:
    """The serialized response is a standard chat.completion object."""
    completion = to_openai_completion(
        NormalizedResponse(
            content="hi there", model_used="gemini-3.5-flash-lite", finish_reason="stop"
        ),
        request_model="google/gemini-3.5-flash-lite",
        response_id="chatcmpl-abc",
        created=1700000000,
    )

    assert completion["object"] == "chat.completion"
    assert completion["model"] == "google/gemini-3.5-flash-lite"
    assert completion["choices"][0]["message"] == {"role": "assistant", "content": "hi there"}
    assert completion["choices"][0]["finish_reason"] == "stop"


def test_normalizer_accepts_tools_field() -> None:
    """Payload with tools is honoured by NormalizedRequest."""
    tools = [
        {
            "type": "function",
            "function": {"name": "get_weather", "description": "weather info"},
        }
    ]
    payload = {**VALID_PAYLOAD, "tools": tools}
    normalized = normalize_chat_completion_request(payload)

    assert normalized.tools == tools


def test_normalizer_accepts_tool_choice_string_and_dict() -> None:
    """tool_choice accepts string ('auto', 'none', 'required') or function dict."""
    payload_str = {**VALID_PAYLOAD, "tool_choice": "auto"}
    assert normalize_chat_completion_request(payload_str).tool_choice == "auto"

    choice_dict = {"type": "function", "function": {"name": "get_weather"}}
    payload_dict = {**VALID_PAYLOAD, "tool_choice": choice_dict}
    assert normalize_chat_completion_request(payload_dict).tool_choice == choice_dict


def test_normalizer_accepts_response_format() -> None:
    """response_format object is preserved."""
    rf = {"type": "json_object"}
    payload = {**VALID_PAYLOAD, "response_format": rf}
    assert normalize_chat_completion_request(payload).response_format == rf


def test_normalizer_still_strips_temperature_when_tools_present() -> None:
    """The boundary grew without becoming permissive: temperature is still stripped."""
    payload = {
        **VALID_PAYLOAD,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "temperature": 0.5,
    }
    normalized = normalize_chat_completion_request(payload)
    dumped = normalized.model_dump()
    assert "temperature" not in dumped
    assert normalized.tools is not None


def test_normalizer_rejects_invalid_tool_types() -> None:
    """Invalid types for tools, tool_choice, and response_format are rejected."""
    with pytest.raises(ValidationError) as exc1:
        normalize_chat_completion_request({**VALID_PAYLOAD, "tools": "not_a_list"})
    assert exc1.value.param == "tools"

    with pytest.raises(ValidationError) as exc2:
        normalize_chat_completion_request({**VALID_PAYLOAD, "tools": ["not_a_dict"]})
    assert exc2.value.param == "tools[0]"

    with pytest.raises(ValidationError) as exc3:
        normalize_chat_completion_request({**VALID_PAYLOAD, "tool_choice": 123})
    assert exc3.value.param == "tool_choice"

    with pytest.raises(ValidationError) as exc4:
        normalize_chat_completion_request({**VALID_PAYLOAD, "response_format": "json_object"})
    assert exc4.value.param == "response_format"


def test_normalizer_accepts_image_parts_and_assistant_tool_calls() -> None:
    """Image part arrays and assistant tool calls with null content are accepted."""
    payload_parts = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/cat.png"},
                    },
                ],
            }
        ],
    }
    norm_parts = normalize_chat_completion_request(payload_parts)
    assert norm_parts.messages[0]["content"] == payload_parts["messages"][0]["content"]

    payload_tool_call = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            }
        ],
    }
    norm_tc = normalize_chat_completion_request(payload_tool_call)
    assert "tool_calls" in norm_tc.messages[0]


def test_normalizer_refuses_text_only_part_arrays() -> None:
    """A part array carrying only text is refused until vision forwarding exists.

    Text parts do not set ``requires_vision``, so admitting them here would let the
    capability filter pass the request to an adapter that stringifies the list
    rather than sending the text. Refusing locally keeps the boundary honest;
    Batch 4 lifts this restriction.
    """
    payload = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
    }
    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request(payload)
    assert exc.value.code == "invalid_message"
    assert exc.value.param == "messages[0].content"


def test_normalizer_refuses_malformed_tool_definitions() -> None:
    """A tool the adapters cannot translate is a request error, not a crash."""
    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request(
            {**VALID_PAYLOAD, "tools": [{"type": "function", "function": "oops"}]}
        )
    assert exc.value.code == "invalid_field_type"
    assert exc.value.param == "tools[0].function"

    with pytest.raises(ValidationError) as exc2:
        normalize_chat_completion_request(
            {**VALID_PAYLOAD, "tools": [{"type": "function", "function": {"name": ""}}]}
        )
    assert exc2.value.param == "tools[0].function.name"

    with pytest.raises(ValidationError) as exc3:
        normalize_chat_completion_request(
            {**VALID_PAYLOAD, "tools": [{"type": "retrieval", "function": {"name": "f"}}]}
        )
    assert exc3.value.param == "tools[0].type"


def test_normalizer_refuses_malformed_assistant_tool_calls() -> None:
    """An echoed tool call with a broken shape would 500 inside an adapter."""

    def payload(tool_calls: list[object]) -> dict[str, object]:
        return {
            "model": "google/gemini-3.5-flash-lite",
            "messages": [{"role": "assistant", "tool_calls": tool_calls}],
        }

    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request(
            payload([{"id": "c1", "type": "function", "function": "nope"}])
        )
    assert exc.value.code == "invalid_message"
    assert exc.value.param == "messages[0].tool_calls[0].function"

    with pytest.raises(ValidationError) as exc2:
        normalize_chat_completion_request(
            payload([{"type": "function", "function": {"name": "f"}}])
        )
    assert exc2.value.param == "messages[0].tool_calls[0].id"

    with pytest.raises(ValidationError) as exc3:
        normalize_chat_completion_request(
            payload(
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": {"a": 1}},
                    }
                ]
            )
        )
    assert exc3.value.param == "messages[0].tool_calls[0].function.arguments"


def test_normalizer_refuses_a_tool_message_without_a_call_id() -> None:
    """A tool result that cannot be matched to a call is refused, not guessed."""
    payload = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [{"role": "tool", "content": "42"}],
    }
    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request(payload)
    assert exc.value.code == "invalid_message"
    assert exc.value.param == "messages[0].tool_call_id"


@pytest.mark.parametrize("tool_choice", ["banana", "", "AUTO"])
def test_normalizer_refuses_an_unrecognised_tool_choice(tool_choice: str) -> None:
    """An unknown mode is refused rather than silently becoming ``auto``."""
    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request({**VALID_PAYLOAD, "tool_choice": tool_choice})
    assert exc.value.code == "invalid_field_type"
    assert exc.value.param == "tool_choice"


def test_normalizer_refuses_a_non_object_extra_content() -> None:
    """Provider passthrough data is carried, so its shape is checked like the rest."""
    payload = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                        "extra_content": "not-an-object",
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request(payload)
    assert exc.value.code == "invalid_message"
    assert exc.value.param == "messages[0].tool_calls[0].extra_content"


def test_normalizer_refuses_a_malformed_tool_choice_object() -> None:
    """The object form must name a function, so the adapter has something to send."""
    with pytest.raises(ValidationError) as exc:
        normalize_chat_completion_request({**VALID_PAYLOAD, "tool_choice": {"type": "function"}})
    assert exc.value.param == "tool_choice.function"

    with pytest.raises(ValidationError) as exc2:
        normalize_chat_completion_request({**VALID_PAYLOAD, "tool_choice": {"name": "f"}})
    assert exc2.value.param == "tool_choice.type"


def test_normalizer_accepts_a_second_turn_tool_payload() -> None:
    """Call, result and follow-up survive the boundary intact."""
    payload = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {"role": "user", "content": "What is the weather in Tokyo?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 15}'},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    }
    normalized = normalize_chat_completion_request(payload)
    assert normalized.messages[1]["tool_calls"][0]["id"] == "call_1"
    assert normalized.messages[2]["tool_call_id"] == "call_1"


def test_normalized_response_content_is_optional() -> None:
    """content can be None on NormalizedResponse when returning tool_calls."""
    resp = NormalizedResponse(
        content=None,
        model_used="gemini-3.5-flash-lite",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="call_123",
                type="function",
                function={"name": "test_func", "arguments": "{}"},
            )
        ],
    )
    assert resp.content is None
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls is not None
    assert resp.tool_calls[0].id == "call_123"


def test_to_openai_completion_emits_null_content_and_tool_calls() -> None:
    """OpenAI serialization returns null content and tool_calls when content is None."""
    resp = NormalizedResponse(
        content=None,
        model_used="gemini-3.5-flash-lite",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="call_abc",
                type="function",
                function={"name": "get_weather", "arguments": '{"location": "Tokyo"}'},
            )
        ],
    )
    completion = to_openai_completion(
        resp,
        request_model="google/gemini-3.5-flash-lite",
        response_id="chatcmpl-tool",
        created=1700000000,
    )
    msg = completion["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"location": "Tokyo"}'},
        }
    ]
    assert completion["choices"][0]["finish_reason"] == "tool_calls"


def test_normalized_chunk_accepts_tool_call_deltas() -> None:
    """NormalizedChunk can carry tool_call_deltas and tool_calls finish reason."""
    chunk = NormalizedChunk(
        delta="",
        finish_reason="tool_calls",
        tool_call_deltas=[
            ToolCallDelta(
                index=0,
                id="call_part",
                function={"name": "get_weather", "arguments": '{"loc'},
            )
        ],
    )
    assert chunk.finish_reason == "tool_calls"
    assert chunk.tool_call_deltas is not None
    assert chunk.tool_call_deltas[0].index == 0
    assert chunk.tool_call_deltas[0].function == {"name": "get_weather", "arguments": '{"loc'}


def test_finish_reason_vocabulary_includes_tool_calls() -> None:
    """FinishReason Literal vocabulary includes 'tool_calls' for tool termination."""
    assert "tool_calls" in get_args(FinishReason)
