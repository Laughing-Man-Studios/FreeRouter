"""Mistral adapter: translation, error mapping, and the timeout envelope.

Fixture shapes here are taken from Mistral's published OpenAPI spec
(``docs.mistral.ai/openapi.yaml``) and the Chat endpoint reference, checked
2026-10-08, rather than written from memory. Three of them are load-bearing:

- ``SUCCESS_BODY`` mirrors the documented response, where ``finish_reason`` is
  already ``stop`` and the content lives at ``choices[0].message.content``.
- ``ERROR_BODY`` mirrors the documented error envelope,
  ``{object, message, type, param, code}``, which is the same shape this router
  emits. Its ``code`` is read for ``provider_code`` (T060), so a client can tell
  a credential problem from a payload problem; the ``message`` is never read,
  because it can echo prompt content.
- ``VALIDATION_BODY_422`` mirrors the spec's ``HTTPValidationError``, which is
  FastAPI-shaped (``{detail: [...]}``) and *not* the documented envelope. A
  422 therefore cannot be classified by parsing the body.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
    UnsupportedModelError,
)
from free_router.core.normalization import NormalizedRequest
from free_router.keys.manager import KeyRef
from free_router.providers.base import ProviderAdapter, build_adapter, supported_providers
from free_router.providers.mistral.adapter import (
    MISTRAL_API_BASE,
    MISTRAL_CHAT_PATH,
    PROVIDER_TIMEOUT_SECONDS,
    MistralAdapter,
    _chunk_from_mistral,
    from_mistral_response,
    to_mistral_request,
)

CHAT_URL = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"

MODEL_ID = "ministral-3b-2512"
"""The model proposed for config.yaml, per Issue #7: 1,300,000 TPM at 12.50 RPS."""

SUCCESS_BODY: dict[str, Any] = {
    "id": "cmpl-e5cc70bb28c444948073e77776eb30ef",
    "object": "chat.completion",
    "created": 1702256327,
    "model": MODEL_ID,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello from mistral"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
}

# The documented Mistral error envelope. Note it is already OpenAI-shaped.
ERROR_BODY: dict[str, Any] = {
    "object": "error",
    "message": "Invalid model",
    "type": "invalid_request_error",
    "param": "model",
    "code": "unknown_model",
}

# The spec's HTTPValidationError, which is FastAPI-shaped rather than the
# documented envelope. Recorded so the 422 case is tested against the shape
# Mistral actually returns for a rejected payload.
VALIDATION_BODY_422: dict[str, Any] = {
    "detail": [{"type": "missing", "loc": ["body", "messages"], "msg": "Field required"}]
}


@pytest.fixture
def key() -> KeyRef:
    """A dummy credential. See the Google adapter's equivalent fixture."""
    return KeyRef(
        alias="primary",
        provider="mistral",
        model_id=MODEL_ID,
        secret=SecretStr("test-mistral-key-primary"),
    )


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


def _request(
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
) -> NormalizedRequest:
    return NormalizedRequest(
        model=f"mistral/{MODEL_ID}",
        messages=messages if messages is not None else [{"role": "user", "content": "hello"}],
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
    )


# --- Registration ---


def test_mistral_is_a_supported_provider() -> None:
    assert "mistral" in supported_providers()


def test_build_adapter_constructs_mistral(http_client: httpx.AsyncClient) -> None:
    adapter = build_adapter("mistral", http_client)

    assert isinstance(adapter, MistralAdapter)
    assert isinstance(adapter, ProviderAdapter)


def test_build_adapter_still_rejects_unknown(http_client: httpx.AsyncClient) -> None:
    with pytest.raises(UnsupportedModelError):
        build_adapter("anthropic", http_client)


# --- Request translation ---


def test_system_message_stays_in_the_message_list() -> None:
    """Mistral has a native system role, so nothing is hoisted out of the list.

    Gemini requires this to become a top-level systemInstruction. Doing the same
    here would be copying a workaround for an API shape Mistral does not have.
    """
    payload = to_mistral_request(
        _request(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ]
        ),
        MODEL_ID,
    )

    assert payload["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
    ]
    assert "systemInstruction" not in payload


def test_message_order_is_preserved() -> None:
    payload = to_mistral_request(
        _request(
            [
                {"role": "system", "content": "one"},
                {"role": "user", "content": "two"},
                {"role": "assistant", "content": "three"},
                {"role": "user", "content": "four"},
            ]
        ),
        MODEL_ID,
    )

    assert [m["content"] for m in payload["messages"]] == ["one", "two", "three", "four"]


def test_model_is_the_provider_native_id_not_the_external_one() -> None:
    """The router's provider/model id must be replaced, not forwarded."""
    payload = to_mistral_request(_request(), MODEL_ID)

    assert payload["model"] == MODEL_ID
    assert "/" not in payload["model"]


def test_non_streaming_is_explicit() -> None:
    """The body is read outside the header timeout, which needs a complete body.

    Relying on Mistral's default would make that coupling implicit.
    """
    payload = to_mistral_request(_request(), MODEL_ID)

    assert payload["stream"] is False


def test_unsupported_sampling_parameters_are_not_forwarded() -> None:
    """Only model and messages are sent; temperature and max_tokens are M0-stripped."""
    payload = to_mistral_request(_request(), MODEL_ID)

    assert "temperature" not in payload
    assert "max_tokens" not in payload
    assert "top_p" not in payload


def test_non_string_content_is_coerced() -> None:
    """Guards the invariant the ingress normalizer already enforced."""
    payload = to_mistral_request(_request([{"role": "user", "content": 42}]), MODEL_ID)

    assert payload["messages"][0]["content"] == "42"


# --- Response translation ---


def test_success_body_is_translated() -> None:
    response = from_mistral_response(SUCCESS_BODY, MODEL_ID)

    assert response.content == "hello from mistral"
    assert response.model_used == MODEL_ID
    assert response.finish_reason == "stop"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("model_length", "length"),
        ("content_filter", "content_filter"),
    ],
)
def test_finish_reasons_map_to_the_openai_vocabulary(raw: str, expected: str) -> None:
    body = json.loads(json.dumps(SUCCESS_BODY))
    body["choices"][0]["finish_reason"] = raw

    assert from_mistral_response(body, MODEL_ID).finish_reason == expected


def test_unknown_finish_reason_does_not_fail_a_complete_completion() -> None:
    """An unrecognised termination reason is not a reason to discard output.

    Mistral documents more reasons than M1 can reach (tool_calls, for one).
    Failing on an unknown one would turn a successful completion into a 502.
    """
    body = json.loads(json.dumps(SUCCESS_BODY))
    body["choices"][0]["finish_reason"] = "some_future_reason"

    assert from_mistral_response(body, MODEL_ID).finish_reason == "stop"


def test_null_content_is_preserved() -> None:
    """Mistral returns null content for a tool-call message.

    Preserved as None rather than coerced to empty string, matching the
    OpenAI contract (M4 T076 / T079).
    """
    body = json.loads(json.dumps(SUCCESS_BODY))
    body["choices"][0]["message"]["content"] = None

    assert from_mistral_response(body, MODEL_ID).content is None


def test_empty_string_content_normalised_to_none_when_tool_calls_present() -> None:
    """Mistral sends content='' (not null) when returning tool calls.

    The router normalises empty-string content to None in that case so that
    Scenario 10 ('content is null') is satisfied.
    """
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"location": "Tokyo"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": MODEL_ID,
    }
    resp = from_mistral_response(body, MODEL_ID)
    assert resp.content is None
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls is not None and len(resp.tool_calls) == 1


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"choices": []}, "no choices"),
        ({}, "no choices key"),
        ({"choices": ["not-an-object"]}, "choice is not an object"),
        ({"choices": [{"finish_reason": "stop"}]}, "choice has no message"),
        ({"choices": [{"message": "not-an-object"}]}, "message is not an object"),
    ],
)
def test_malformed_response_raises_provider_error_not_index_error(
    body: dict[str, Any], why: str
) -> None:
    """An opaque IndexError or TypeError must not become a 500."""
    with pytest.raises(ProviderServerError):
        from_mistral_response(body, MODEL_ID)


# --- Error mapping ---


@respx.mock
async def test_success_round_trip(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    response = await MistralAdapter(http_client=http_client).chat_completion(
        _request(), MODEL_ID, key
    )

    assert response.content == "hello from mistral"
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == MODEL_ID


@respx.mock
async def test_key_travels_in_a_bearer_header_not_the_url(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """A key in the URL would reach proxy and access logs."""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)

    assert route.calls[0].request.headers["Authorization"] == "Bearer test-mistral-key-primary"
    assert "test-mistral-key-primary" not in str(route.calls[0].request.url)


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_maps_to_auth_error(
    http_client: httpx.AsyncClient, status: int, key: KeyRef
) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(status, json={**ERROR_BODY, "type": "authentication_error"})
    )

    with pytest.raises(ProviderAuthError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
async def test_rate_limit_maps_to_rate_limit_error(
    http_client: httpx.AsyncClient, key: KeyRef
) -> None:
    """429 must not be reported as a malformed request.

    Collapsing it into the generic 4xx branch makes a capacity problem look like
    a client payload error, which sends an operator to debug the wrong thing.
    """
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(429, json={**ERROR_BODY, "type": "rate_limit_error"})
    )

    with pytest.raises(ProviderRateLimitError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_rejected_payload_maps_to_validation_error(
    http_client: httpx.AsyncClient,
    status: int,
    key: KeyRef,
) -> None:
    # 422 uses the FastAPI-shaped body from the OpenAPI spec, not the documented
    # envelope. It must still be classified, and without parsing the body.
    body = VALIDATION_BODY_422 if status == 422 else ERROR_BODY
    respx.post(CHAT_URL).mock(return_value=httpx.Response(status, json=body))

    with pytest.raises(ProviderValidationError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error_maps_to_server_error(
    http_client: httpx.AsyncClient,
    status: int,
    key: KeyRef,
) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(status, json={"message": "oops"}))

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
async def test_transport_failure_maps_to_server_error(
    http_client: httpx.AsyncClient, key: KeyRef
) -> None:
    """A connection failure is an exception, not a response, so it bypasses status mapping.

    Uncaught it reaches the generic handler and is reported as a router fault,
    misattributing a network problem to the router itself.
    """
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
async def test_timeout_maps_to_timeout_error(http_client: httpx.AsyncClient, key: KeyRef) -> None:
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderTimeoutError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
async def test_the_unary_path_has_no_header_window(
    http_client: httpx.AsyncClient, key: KeyRef
) -> None:
    """Same rule as Google: a unary answer's headers arrive with the answer.

    Bounding them bounds the generation, so the unary path passes
    ``header_window=None`` and the read timeout governs instead. Asserted here
    as well as in the Google tests because the two adapters are separate copies
    of the rule, and only one of them drifting would be silent.
    """

    async def _slow_generation(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(PROVIDER_TIMEOUT_SECONDS + 0.5)
        return httpx.Response(200, json=SUCCESS_BODY)

    route = respx.post(CHAT_URL).mock(side_effect=_slow_generation)

    result = await MistralAdapter(http_client=http_client).chat_completion(
        _request(), MODEL_ID, key
    )

    assert route.call_count == 1
    assert result.content == "hello from mistral"


@respx.mock
async def test_undecodable_body_maps_to_server_error(
    http_client: httpx.AsyncClient, key: KeyRef
) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, content=b"not json", headers={"Content-Type": "text/html"})
    )

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
async def test_non_object_body_maps_to_server_error(
    http_client: httpx.AsyncClient, key: KeyRef
) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=["a", "list"]))

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)


@respx.mock
async def test_credential_material_comes_from_the_pool_not_the_environment(
    http_client: httpx.AsyncClient, key: KeyRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No variable name reaches an adapter. See the Google adapter's equivalent."""
    monkeypatch.setenv("MISTRAL_API_KEY", "a-decoy-that-must-not-be-used")
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "another-decoy")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID, key)

    assert route.calls[0].request.headers["Authorization"] == "Bearer test-mistral-key-primary"
    assert "decoy" not in str(route.calls[0].request.headers)


def test_provider_timeout_is_shared_with_the_google_adapter() -> None:
    """One environment variable must have one resolved value across adapters."""
    from free_router.providers.google import adapter as google_adapter

    assert PROVIDER_TIMEOUT_SECONDS == google_adapter.PROVIDER_TIMEOUT_SECONDS


# --- Tools and function calling (M4 T079 / T080) ---


def test_to_mistral_request_tools_and_tool_choice_forwarded() -> None:
    """tools and tool_choice are forwarded directly in the payload."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up an item",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        }
    ]
    payload = to_mistral_request(_request(tools=tools, tool_choice="auto"), MODEL_ID)
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"

    payload_named = to_mistral_request(
        _request(tools=tools, tool_choice={"type": "function", "function": {"name": "lookup"}}),
        MODEL_ID,
    )
    assert payload_named["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}


def test_to_mistral_request_assistant_tool_calls_in_history() -> None:
    """Assistant tool_calls and null content are preserved."""
    req = _request(
        messages=[
            {"role": "user", "content": "Fetch data"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"id": "42"}'},
                    }
                ],
            },
        ]
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert len(payload["messages"]) == 2
    assistant_msg = payload["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] is None
    assert assistant_msg["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"id": "42"}'},
        }
    ]


def test_to_mistral_request_drops_the_gemini_signature_passthrough() -> None:
    """A Gemini thought signature must not reach Mistral's schema.

    ``extra_content`` only has meaning to the Google adapter. Forwarding it
    verbatim would send an unknown key to a provider that validates its
    tool_calls shape, so the Mistral edge removes it.
    """
    req = _request(
        messages=[
            {"role": "user", "content": "Fetch data"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"id": "42"}'},
                        "extra_content": {"google": {"thought_signature": "sig"}},
                    }
                ],
            },
        ]
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert payload["messages"][1]["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"id": "42"}'},
        }
    ]


def test_to_mistral_request_tool_result_message() -> None:
    """role: 'tool' message passes through with tool_call_id and content."""
    req = _request(
        messages=[
            {"role": "user", "content": "Fetch data"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"id": "42"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc",
                "content": '{"result": "found"}',
            },
        ]
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert len(payload["messages"]) == 3
    tool_msg = payload["messages"][2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_abc"
    assert tool_msg["content"] == '{"result": "found"}'


def test_from_mistral_response_tool_calls() -> None:
    """Mistral response with tool_calls produces tool_calls.

    Produces content: None and finish_reason: tool_calls.
    """
    raw = {
        "id": "cmpl-tool-123",
        "object": "chat.completion",
        "created": 1702256327,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mistral_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id": "42"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20},
    }

    resp = from_mistral_response(raw, MODEL_ID)
    assert resp.content is None
    assert resp.finish_reason == "tool_calls"
    assert resp.model_used == MODEL_ID
    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_mistral_1"
    assert tc.type == "function"
    assert tc.function == {"name": "lookup", "arguments": '{"id": "42"}'}


def test_tool_call_turn_reported_as_stop_is_promoted() -> None:
    """A tool-call turn that says "stop" still reports ``tool_calls``.

    Mistral can end a call with "stop"; an OpenAI client branches on the finish
    reason to decide whether to run the call, so the two must stay distinct.
    """
    raw = {
        "id": "cmpl-promoted",
        "object": "chat.completion",
        "created": 1702256328,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_stop_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
    }

    resp = from_mistral_response(raw, MODEL_ID)
    assert resp.finish_reason == "tool_calls"
    assert resp.content is None


def test_chunk_from_mistral_tool_call_delta() -> None:
    """Streaming chunk with delta.tool_calls produces tool_call_deltas."""
    event = {
        "id": "cmpl-stream-1",
        "object": "chat.completion.chunk",
        "created": 1702256327,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_m1",
                            "function": {"name": "lookup", "arguments": '{"id": "42"}'},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }

    chunk = _chunk_from_mistral(event)
    assert chunk is not None
    assert chunk.delta == ""
    assert chunk.tool_call_deltas is not None
    assert len(chunk.tool_call_deltas) == 1
    assert chunk.tool_call_deltas[0].index == 0
    assert chunk.tool_call_deltas[0].id == "call_m1"
    assert chunk.tool_call_deltas[0].function == {"name": "lookup", "arguments": '{"id": "42"}'}


@respx.mock
async def test_mistral_unary_second_turn_round_trip(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Second-turn tool result produces normal assistant answer in Mistral."""
    turn1_body = {
        "id": "cmpl-t1",
        "object": "chat.completion",
        "created": 1702256327,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mis_1",
                            "type": "function",
                            "function": {"name": "get_stock", "arguments": '{"ticker": "AAPL"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    turn2_body = {
        "id": "cmpl-t2",
        "object": "chat.completion",
        "created": 1702256328,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "AAPL is at $150.00."},
                "finish_reason": "stop",
            }
        ],
    }

    route = respx.post(CHAT_URL)
    route.side_effect = [
        httpx.Response(200, json=turn1_body),
        httpx.Response(200, json=turn2_body),
    ]

    adapter = MistralAdapter(http_client=http_client)

    req1 = _request(
        messages=[{"role": "user", "content": "What is AAPL stock price?"}],
        tools=[{"type": "function", "function": {"name": "get_stock"}}],
    )
    resp1 = await adapter.chat_completion(req1, MODEL_ID, key)
    assert resp1.finish_reason == "tool_calls"
    assert resp1.tool_calls is not None
    call_id = resp1.tool_calls[0].id

    req2 = _request(
        messages=[
            {"role": "user", "content": "What is AAPL stock price?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tc.model_dump() for tc in resp1.tool_calls],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": '{"price": 150.00}',
            },
        ],
        tools=[{"type": "function", "function": {"name": "get_stock"}}],
    )
    resp2 = await adapter.chat_completion(req2, MODEL_ID, key)
    assert resp2.finish_reason == "stop"
    assert resp2.content == "AAPL is at $150.00."
    assert resp2.tool_calls is None


# --- Structured output (T083) ---


def test_json_object_passthrough() -> None:
    """Mistral is OpenAI-shaped, so ``response_format`` passes through as sent."""
    req = _request(
        messages=[{"role": "user", "content": "Return json"}],
        response_format={"type": "json_object"},
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert payload.get("response_format") == {"type": "json_object"}


def test_json_schema_passthrough() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    fmt = {
        "type": "json_schema",
        "json_schema": {
            "name": "Person",
            "schema": schema,
        },
    }
    req = _request(
        messages=[{"role": "user", "content": "Return json"}],
        response_format=fmt,
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert payload.get("response_format") == fmt


def test_text_response_format_is_forwarded() -> None:
    """``text`` means "no constraint" and Mistral accepts it, so it is not stripped.

    Measured, not assumed: ``ministral-3b-2512`` returns 200 for it. Stripping
    it here would be a second translation rule with nothing to justify it.
    """
    req = _request(
        messages=[{"role": "user", "content": "Return text"}],
        response_format={"type": "text"},
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert payload.get("response_format") == {"type": "text"}


def test_no_response_format() -> None:
    req = _request(messages=[{"role": "user", "content": "Return text"}])
    payload = to_mistral_request(req, MODEL_ID)
    assert "response_format" not in payload


def test_response_format_coexists_with_tools() -> None:
    tools = [{"type": "function", "function": {"name": "get_stock"}}]
    req = _request(
        messages=[{"role": "user", "content": "Hi"}],
        tools=tools,
        response_format={"type": "json_object"},
    )
    payload = to_mistral_request(req, MODEL_ID)
    assert payload.get("response_format") == {"type": "json_object"}
    assert payload.get("tools") == tools


@respx.mock
async def test_round_trip_with_response_format(http_client: httpx.AsyncClient, key: KeyRef) -> None:
    route = respx.post(CHAT_URL).respond(
        200,
        json={
            "id": "cmpl-fmt-1",
            "object": "chat.completion",
            "created": 1702256327,
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"success": true}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )
    adapter = MistralAdapter(http_client=http_client)
    req = _request(
        messages=[{"role": "user", "content": "Return json"}],
        response_format={"type": "json_object"},
    )
    resp = await adapter.chat_completion(req, MODEL_ID, key)
    assert resp.content == '{"success": true}'
    assert resp.finish_reason == "stop"
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body.get("response_format") == {"type": "json_object"}


# --- Vision pass-through (T085) ---


def test_to_mistral_request_base64_image_part() -> None:
    """A base64 image part is forwarded in the shape Mistral documents."""
    b64 = "iVBORw0KGgo="
    request = NormalizedRequest(
        model="mistral/ministral-3b-2512",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
    )
    payload = to_mistral_request(request, MODEL_ID)
    content = payload["messages"][0]["content"]
    assert isinstance(content, list)
    assert content == [
        {"type": "text", "text": "Describe:"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]


def test_to_mistral_request_input_image_spelling_is_normalised() -> None:
    """``input_image`` becomes ``image_url``: Mistral rejects the former.

    Measured (T085): forwarding the Responses spelling verbatim returns 422.
    """
    request = NormalizedRequest(
        model="mistral/ministral-3b-2512",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "input_image": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ],
    )
    payload = to_mistral_request(request, MODEL_ID)
    assert payload["messages"][0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    ]


def test_content_part_without_url_is_refused_not_forwarded() -> None:
    """A part the boundary would reject raises rather than reaching the provider."""
    request = NormalizedRequest(
        model="mistral/ministral-3b-2512",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    {"type": "file", "file": {"file_id": "f-1"}},
                ],
            }
        ],
    )
    with pytest.raises(ValueError, match="without a data URL"):
        to_mistral_request(request, MODEL_ID)


@respx.mock
async def test_mistral_image_round_trip(http_client: httpx.AsyncClient, key: KeyRef) -> None:
    """A base64 image part round-trips and the completion is returned (T085)."""
    route = respx.post(CHAT_URL).respond(
        200,
        json={
            "id": "cmpl-img-1",
            "object": "chat.completion",
            "created": 1702256327,
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "A red pixel."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
    )
    adapter = MistralAdapter(http_client=http_client)
    req = NormalizedRequest(
        model="mistral/ministral-3b-2512",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What colour is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ],
    )
    resp = await adapter.chat_completion(req, MODEL_ID, key)

    assert resp.content == "A red pixel."
    assert resp.finish_reason == "stop"
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["messages"][0]["content"] == [
        {"type": "text", "text": "What colour is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
