"""Ingress normalization and egress serialization."""

import pytest

from free_router.core.exceptions import ValidationError
from free_router.core.normalization import (
    NormalizedRequest,
    normalize_chat_completion_request,
    to_openai_completion,
)

VALID_PAYLOAD = {
    "model": "google/gemini-3.5-flash-lite",
    "messages": [{"role": "user", "content": "hello"}],
}


def test_normalizer_keeps_only_model_and_messages() -> None:
    """Scenario 5: unsupported parameters are discarded, not forwarded."""
    payload = {**VALID_PAYLOAD, "temperature": 0.7, "max_tokens": 100, "top_p": 0.9}

    normalized = normalize_chat_completion_request(payload)

    assert isinstance(normalized, NormalizedRequest)
    assert normalized.model == "google/gemini-3.5-flash-lite"
    assert normalized.messages == [{"role": "user", "content": "hello"}]
    dumped = normalized.model_dump()
    assert set(dumped) == {"model", "messages"}


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
    from free_router.core.normalization import NormalizedResponse

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
