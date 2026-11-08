"""Local request capability and size detection tests (T069-T070).

Verifies Issue #18, spec §3, §2, and plan.md § Detection:
- Pure-function derivation of RequestRequirements from raw OpenAI payload.
- Tools: non-empty tools array, tool_choice semantics (auto/none are not
  requirements; required and function objects are), empty tools array (not
  required), and tool_choice: "none" with non-empty tools.
- Vision: content parts with image_url or input_image (URLs and data-URIs),
  vs string-only or text-part content.
- JSON schema: json_object and json_schema response_format.
- Streaming: stream=True vs stream=False or omitted.
- Reserved output: max_tokens or max_completion_tokens, fallback to assumed_output.
- Input tokens: shared reservation estimator, under-counting bias documented.
- Detection precedes normalization: payload carrying tools is detected, but normalizer
  discards tools.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from free_router.core.capabilities import RequestRequirements, detect_requirements
from free_router.core.normalization import normalize_chat_completion_request
from free_router.quota.estimation import (
    DEFAULT_OUTPUT_TOKEN_ASSUMPTION,
    estimate_input_tokens,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # --- Tools detection ---
        (
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            {"requires_tools": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
            },
            {"requires_tools": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f"}}],
            },
            {"requires_tools": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "none",
            },
            {"requires_tools": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
                "tool_choice": "none",
            },
            {"requires_tools": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f"}}],
                "tool_choice": "none",
            },
            {"requires_tools": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            },
            {"requires_tools": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "required",
            },
            {"requires_tools": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": {"type": "function", "function": {"name": "f"}},
            },
            {"requires_tools": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": {},
            },
            {"requires_tools": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": {"type": "none"},
            },
            {"requires_tools": False},
        ),
        # --- Vision detection ---
        (
            {"model": "m", "messages": [{"role": "user", "content": "plain text"}]},
            {"requires_vision": False},
        ),
        (
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "plain text part"},
                        ],
                    }
                ],
            },
            {"requires_vision": False},
        ),
        (
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look at this"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/cat.png"},
                            },
                        ],
                    }
                ],
            },
            {"requires_vision": True},
        ),
        (
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="},
                            }
                        ],
                    }
                ],
            },
            {"requires_vision": True},
        ),
        (
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "input_image": "image-bytes"}],
                    }
                ],
            },
            {"requires_vision": True},
        ),
        # --- JSON schema / structured output detection ---
        (
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            {"requires_json_schema": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "text"},
            },
            {"requires_json_schema": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
            },
            {"requires_json_schema": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "schema", "schema": {}},
                },
            },
            {"requires_json_schema": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": "json_object",
            },
            {"requires_json_schema": True},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": "json_schema",
            },
            {"requires_json_schema": True},
        ),
        # --- Streaming detection ---
        (
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            {"requires_streaming": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            {"requires_streaming": False},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            {"requires_streaming": True},
        ),
        # --- Reserved output sizing ---
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4096,
            },
            {"reserved_output": 4096},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 2048,
            },
            {"reserved_output": 2048},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1000,
                "max_completion_tokens": 2000,
            },
            {"reserved_output": 1000},
        ),
        (
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            {"reserved_output": DEFAULT_OUTPUT_TOKEN_ASSUMPTION},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 0,
            },
            {"reserved_output": DEFAULT_OUTPUT_TOKEN_ASSUMPTION},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": -100,
            },
            {"reserved_output": DEFAULT_OUTPUT_TOKEN_ASSUMPTION},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": "lots",
            },
            {"reserved_output": DEFAULT_OUTPUT_TOKEN_ASSUMPTION},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": True,
            },
            {"reserved_output": DEFAULT_OUTPUT_TOKEN_ASSUMPTION},
        ),
        (
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 0,
                "max_completion_tokens": 2000,
            },
            {"reserved_output": 2000},
        ),
    ],
)
def test_detect_requirements_table_driven(
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """Verifies table in plan.md § Detection and acceptance criteria."""
    requirements = detect_requirements(payload, DEFAULT_OUTPUT_TOKEN_ASSUMPTION)
    for field, expected_value in expected.items():
        assert getattr(requirements, field) == expected_value


def test_reserved_output_custom_assumed_output() -> None:
    """Custom assumed_output is respected when max_tokens is absent."""
    payload: dict[str, Any] = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [{"role": "user", "content": "hi"}],
    }
    req = detect_requirements(payload, assumed_output=512)
    assert req.reserved_output == 512


def test_assumed_output_is_required() -> None:
    """The caller must supply the configured output assumption explicitly."""
    payload: dict[str, Any] = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(TypeError):
        detect_requirements(payload)  # type: ignore[call-arg]


def test_input_tokens_uses_shared_reservation_estimator() -> None:
    """The token estimate uses the shared reservation estimator without drift."""
    payload: dict[str, Any] = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Tell me a story about a little robot."},
        ],
    }
    req = detect_requirements(payload, DEFAULT_OUTPUT_TOKEN_ASSUMPTION)
    expected_tokens = estimate_input_tokens(payload)
    assert req.input_tokens == expected_tokens
    assert req.input_tokens > 1


def test_input_tokens_documented_bias_for_list_shaped_content() -> None:
    """The estimator returns 1 for list-shaped content parts (documented in plan.md § Risks)."""
    payload: dict[str, Any] = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/cat.png"},
                    }
                ],
            }
        ],
    }
    req = detect_requirements(payload, DEFAULT_OUTPUT_TOKEN_ASSUMPTION)
    assert req.requires_vision is True
    # The shared estimator returns 1 for list-shaped content:
    assert req.input_tokens == 1


def test_detection_precedes_normalization_ordering() -> None:
    """A payload carrying tools is detected, and still normalizes to a request with no tools.

    This proves detection runs on the raw payload before normalization strips tools.
    """
    raw_payload: dict[str, Any] = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [{"role": "user", "content": "What is the weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
    }

    requirements = detect_requirements(raw_payload, DEFAULT_OUTPUT_TOKEN_ASSUMPTION)
    assert requirements.requires_tools is True
    assert requirements.requires_json_schema is True

    # Normalization strips tools and response_format
    normalized = normalize_chat_completion_request(raw_payload)
    assert normalized.model == "google/gemini-3.5-flash-lite"
    assert len(normalized.messages) == 1
    assert not hasattr(normalized, "tools")
    assert not hasattr(normalized, "tool_choice")
    assert not hasattr(normalized, "response_format")


def test_request_requirements_immutability() -> None:
    """RequestRequirements is frozen and forbids extra attributes."""
    req = RequestRequirements(
        requires_tools=False,
        requires_vision=False,
        requires_json_schema=False,
        requires_streaming=False,
        input_tokens=10,
        reserved_output=1025,
    )
    with pytest.raises(ValidationError):
        req.requires_tools = True  # type: ignore[misc]
