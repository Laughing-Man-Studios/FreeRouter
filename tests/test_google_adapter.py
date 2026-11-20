"""Google (Gemini) adapter: translation, error mapping, and the timeout envelope."""

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
    UnsupportedModelError,
)
from free_router.core.normalization import NormalizedRequest, NormalizedResponse
from free_router.keys.manager import KeyRef
from free_router.providers.base import ProviderAdapter, build_adapter, supported_providers
from free_router.providers.google.adapter import (
    GOOGLE_API_BASE,
    PROVIDER_TIMEOUT_SECONDS,
    GoogleAdapter,
    _chunk_from_gemini,
    from_gemini_response,
    to_gemini_request,
)

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"
STREAM_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse"

SUCCESS_BODY: dict[str, Any] = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello from gemini"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 4,
        "candidatesTokenCount": 3,
        "totalTokenCount": 7,
    },
}


@pytest.fixture
def key() -> KeyRef:
    """A dummy credential.

    The adapter receives key material from the pool rather than reading the
    environment, so there is nothing to set here. That is the point: no variable
    name reaches an adapter, and a test cannot make one by exporting a variable.
    """
    return KeyRef(
        alias="primary",
        provider="google",
        model_id="gemini-3.5-flash-lite",
        secret=SecretStr("test-google-key-primary"),
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
        model="google/gemini-3.5-flash-lite",
        messages=messages or [{"role": "user", "content": "hello"}],
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
    )


# --- Protocol and factory (T010) ---


def test_google_adapter_satisfies_protocol(http_client: httpx.AsyncClient) -> None:
    """The adapter structurally satisfies ProviderAdapter."""
    assert isinstance(build_adapter("google", http_client), ProviderAdapter)


def test_supported_providers_includes_google() -> None:
    assert "google" in supported_providers()


def test_build_adapter_rejects_unknown_provider(http_client: httpx.AsyncClient) -> None:
    """An unregistered provider is refused rather than silently dispatched.

    The name used to be ``mistral``, back when it was the obvious example of a
    provider that did not exist yet. It is registered now, so using it here
    would have quietly stopped testing anything.
    """
    with pytest.raises(UnsupportedModelError) as excinfo:
        build_adapter("anthropic", http_client)

    assert "anthropic" in excinfo.value.message


# --- Request translation (T011) ---


def test_system_message_becomes_system_instruction() -> None:
    """Gemini has no system role inside contents; it uses systemInstruction."""
    payload = to_gemini_request(
        _request(
            [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ]
        )
    )

    assert payload["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_multiple_system_messages_are_concatenated() -> None:
    """Consecutive system messages merge into one instruction block."""
    payload = to_gemini_request(
        _request(
            [
                {"role": "system", "content": "one"},
                {"role": "system", "content": "two"},
            ]
        )
    )

    assert payload["systemInstruction"]["parts"] == [{"text": "one"}, {"text": "two"}]


def test_conversation_order_is_preserved() -> None:
    """User and assistant turns keep their sequence."""
    payload = to_gemini_request(
        _request(
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
            ]
        )
    )

    assert [item["role"] for item in payload["contents"]] == [
        "user",
        "assistant",
        "user",
    ]


def test_no_generation_config_is_sent() -> None:
    """M0 honours only model and messages; no temperature/max_tokens leaks."""
    payload = to_gemini_request(_request())

    assert "generationConfig" not in payload


# --- Response translation (T011) ---


def test_response_parts_are_joined() -> None:
    """Multiple text parts concatenate into one message."""
    normalized = from_gemini_response(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "a"}, {"text": "b"}]},
                    "finishReason": "STOP",
                }
            ]
        },
        "gemini-3.5-flash-lite",
    )

    assert normalized.content == "ab"
    assert normalized.model_used == "gemini-3.5-flash-lite"
    assert normalized.finish_reason == "stop"


@pytest.mark.parametrize(
    ("gemini_reason", "expected"),
    [("STOP", "stop"), ("MAX_TOKENS", "length"), ("SAFETY", "content_filter")],
)
def test_finish_reasons_map_to_openai_vocabulary(gemini_reason: str, expected: str) -> None:
    """Gemini's finish vocabulary is translated, not passed through."""
    normalized = from_gemini_response(
        {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": gemini_reason}]},
        "gemini-3.5-flash-lite",
    )

    assert normalized.finish_reason == expected


def test_unknown_finish_reason_defaults_to_stop() -> None:
    """An unmapped value degrades to stop rather than failing the request."""
    normalized = from_gemini_response(
        {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "OTHER"}]},
        "gemini-3.5-flash-lite",
    )

    assert normalized.finish_reason == "stop"


def test_empty_candidates_raises_rather_than_index_error() -> None:
    """Safety-filtered responses yield no candidates; that is a clean error."""
    with pytest.raises(ProviderServerError, match="no completion candidates"):
        from_gemini_response({"candidates": []}, "gemini-3.5-flash-lite")


# --- Dispatch and error mapping (T011, Scenarios 1, 8, 9) ---


@respx.mock
async def test_successful_dispatch_returns_normalized_response(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Scenario 1: a 200 becomes a NormalizedResponse."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    adapter = GoogleAdapter(http_client=http_client)
    result = await adapter.chat_completion(_request(), "gemini-3.5-flash-lite", key)

    assert isinstance(result, NormalizedResponse)
    assert result.content == "hello from gemini"
    assert route.called


@respx.mock
async def test_request_uses_api_key_header_not_query_param(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """The key travels in a header so it cannot leak into URLs."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    await GoogleAdapter(http_client=http_client).chat_completion(
        _request(), "gemini-3.5-flash-lite", key
    )

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "test-google-key-primary"
    assert "key=" not in str(request.url)
    assert "test-google-key-primary" not in str(request.url)


@respx.mock
async def test_auth_error_raises_provider_auth_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Scenario 8: provider 401 becomes ProviderAuthError."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(401, json={"error": {}}))

    with pytest.raises(ProviderAuthError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_invalid_api_key_400_maps_to_auth_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Google reports a bad API key as 400, not 401. Verified live.

    Mapping every 400 to a payload error would report an auth failure as a bad
    request, sending an operator to debug their payload instead of their
    credentials. This is the real body Google returns for an invalid key.
    """
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
    )

    with pytest.raises(ProviderAuthError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_permission_denied_400_maps_to_auth_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """The PERMISSION_DENIED status flag is also an auth failure."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"status": "PERMISSION_DENIED", "message": "nope"}},
        )
    )

    with pytest.raises(ProviderAuthError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_genuine_bad_payload_400_is_not_an_auth_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """A real payload rejection must stay a validation error.

    Guards the classifier from over-reaching: only credential signals promote a
    400 to an auth error.
    """
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "* contents.parts.text: field required",
                }
            },
        )
    )

    with pytest.raises(ProviderValidationError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_unparseable_400_body_is_a_validation_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """An unreadable body falls back to the default mapping, not auth."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(400, text="not json"))

    with pytest.raises(ProviderValidationError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_server_error_raises_provider_server_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Scenario 9: provider 5xx becomes ProviderServerError."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(ProviderServerError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_bad_request_raises_provider_validation_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Provider 400 becomes ProviderValidationError."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )

    with pytest.raises(ProviderValidationError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_error_message_never_contains_key_or_prompt(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Provider errors must not echo the secret or the prompt (spec 9)."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "reflected: my-secret-prompt"}})
    )

    with pytest.raises(ProviderValidationError) as excinfo:
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request([{"role": "user", "content": "my-secret-prompt"}]),
            "gemini-3.5-flash-lite",
            key,
        )

    message = excinfo.value.message
    assert "my-secret-prompt" not in message
    assert "test-google-key-primary" not in message


@respx.mock
async def test_non_json_body_raises_provider_server_error(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """An undecodable body is an upstream fault, not a crash."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))

    with pytest.raises(ProviderServerError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_credential_material_comes_from_the_pool_not_the_environment(
    http_client: httpx.AsyncClient, key: KeyRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No variable name reaches an adapter.

    The adapter previously read ``GEMINI_API_KEY`` from the process environment
    itself. It no longer does, so exporting that variable cannot change which
    credential is used -- the pool decides. This test exists because that is a
    security property, and a test that sets the old variable and asserts nothing
    about it is the only thing that will catch a regression.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "a-decoy-that-must-not-be-used")
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "another-decoy")
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    await GoogleAdapter(http_client=http_client).chat_completion(
        _request(), "gemini-3.5-flash-lite", key
    )

    assert route.calls[0].request.headers["x-goog-api-key"] == "test-google-key-primary"
    assert "decoy" not in str(route.calls[0].request.headers)


@respx.mock
async def test_slow_headers_raise_timeout(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Scenario 10: exceeding the 0.4s header budget raises ProviderTimeoutError."""
    respx.post(GENERATE_URL).mock(side_effect=httpx.ReadTimeout("headers too slow"))

    with pytest.raises(ProviderTimeoutError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite", key
        )


@respx.mock
async def test_asyncio_timeout_is_enforced_on_a_streaming_handshake(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """A stream whose handshake exceeds the window is cut off by asyncio.timeout.

    This asserts the real budget rather than trusting the httpx exception path:
    the route sleeps past PROVIDER_TIMEOUT_SECONDS, so only the strict
    asyncio.timeout can produce the failure.

    It moved from the unary URL to the streaming one when the window was scoped
    (2026-10-30). On the unary URL there *is* no handshake -- Gemini sends no
    headers until the answer exists -- so the window was bounding generation,
    and this test was pinning a defect rather than a policy.
    """

    async def _slow_response(_: httpx.Request) -> httpx.Response:
        # Only has to outlast the window: asyncio.timeout cancels the sleep.
        await asyncio.sleep(PROVIDER_TIMEOUT_SECONDS + 1.0)
        return httpx.Response(200, text="")

    respx.post(STREAM_URL).mock(side_effect=_slow_response)
    adapter = GoogleAdapter(http_client=http_client)

    with pytest.raises(ProviderTimeoutError):
        async for _ in adapter.stream_chat_completion(_request(), "gemini-3.5-flash-lite", key):
            pass


@respx.mock
async def test_a_unary_generation_slower_than_the_header_window_succeeds(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """The window bounds a handshake, and a unary request has none.

    ``generateContent`` sends nothing until the answer has been generated, so
    applying the 5 s window there made every generation longer than five seconds
    unservable unary while the same prompt streamed fine. Measured live on
    2026-10-30: one `504 provider_timeout` in twenty requests, from two
    consecutive attempts each just over the window.
    """

    async def _slow_generation(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(PROVIDER_TIMEOUT_SECONDS + 0.5)
        return httpx.Response(200, json=SUCCESS_BODY)

    respx.post(GENERATE_URL).mock(side_effect=_slow_generation)

    result = await GoogleAdapter(http_client=http_client).chat_completion(
        _request(), "gemini-3.5-flash-lite", key
    )

    assert result.content == "hello from gemini"


@respx.mock
async def test_slow_body_is_not_subject_to_the_header_timeout(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """The 0.4s budget covers headers only, so a slow body still succeeds.

    This is the regression guard for the obvious 'simplification': calling
    client.post() instead of send(stream=True) reads the body inside the
    timeout window, which would turn every real multi-second completion into a
    504.
    """

    async def _slow_body(_: httpx.Request) -> httpx.Response:
        # Headers arrive promptly; only the body is slow.
        return httpx.Response(200, stream=_SlowBody(json.dumps(SUCCESS_BODY).encode()))

    respx.post(GENERATE_URL).mock(side_effect=_slow_body)

    result = await GoogleAdapter(http_client=http_client).chat_completion(
        _request(), "gemini-3.5-flash-lite", key
    )

    assert result.content == "hello from gemini"


class _SlowBody(httpx.AsyncByteStream):
    """A response body that sleeps past the header budget before yielding."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> Any:
        await asyncio.sleep(PROVIDER_TIMEOUT_SECONDS + 0.5)
        yield self._payload


# --- Tools and function calling (M4 T078 / T080) ---


def test_to_gemini_request_tools_and_tool_choice() -> None:
    """tools maps to functionDeclarations, and tool_choice maps to functionCallingConfig."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }
    ]

    # auto mode
    req_auto = _request(tools=tools, tool_choice="auto")
    payload_auto = to_gemini_request(req_auto)
    assert payload_auto["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                }
            ]
        }
    ]
    assert payload_auto["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}

    # none mode
    payload_none = to_gemini_request(_request(tools=tools, tool_choice="none"))
    assert payload_none["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}

    # required mode -> ANY
    payload_required = to_gemini_request(_request(tools=tools, tool_choice="required"))
    assert payload_required["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}

    # specific function choice
    payload_named = to_gemini_request(
        _request(
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "get_weather"}},
        )
    )
    assert payload_named["toolConfig"] == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["get_weather"],
        }
    }


def test_tool_choice_unknown_value_is_refused_not_defaulted() -> None:
    """A mode the boundary would reject must not quietly become ``AUTO`` here."""
    with pytest.raises(ValueError, match="Unsupported tool_choice"):
        to_gemini_request(_request(tool_choice="banana"))


def test_to_gemini_request_assistant_tool_calls_in_history() -> None:
    """An assistant message with tool_calls converts to a 'model' turn with functionCall parts."""
    req = _request(
        messages=[
            {"role": "user", "content": "What is the weather in Tokyo?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Tokyo"}',
                        },
                    }
                ],
            },
        ]
    )
    payload = to_gemini_request(req)
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "What is the weather in Tokyo?"}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {
                        "name": "get_weather",
                        "args": {"location": "Tokyo"},
                    },
                    # The client echoed no signature, and Gemini 3 rejects the
                    # turn without one, so the documented placeholder is sent.
                    "thoughtSignature": "skip_thought_signature_validator",
                }
            ],
        },
    ]


def test_to_gemini_request_second_turn_tool_response() -> None:
    """A role: "tool" message maps to functionResponse in a role: "user" turn."""
    req = _request(
        messages=[
            {"role": "user", "content": "What is the weather in Tokyo?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_tokyo_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Tokyo"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_tokyo_1",
                "content": '{"temperature": 15, "unit": "celsius"}',
            },
        ]
    )
    payload = to_gemini_request(req)
    assert len(payload["contents"]) == 3
    tool_turn = payload["contents"][2]
    assert tool_turn["role"] == "user"
    assert tool_turn["parts"] == [
        {
            "functionResponse": {
                "name": "get_weather",
                "response": {"temperature": 15, "unit": "celsius"},
            }
        }
    ]


def test_to_gemini_request_replays_the_thought_signature() -> None:
    """A signature echoed by the client goes back on the same part it arrived on."""
    req = _request(
        messages=[
            {"role": "user", "content": "What is the weather in Tokyo?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_tokyo_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Tokyo"}',
                        },
                        "extra_content": {"google": {"thought_signature": "a-real-signature"}},
                    }
                ],
            },
        ]
    )
    payload = to_gemini_request(req)
    model_turn = payload["contents"][1]
    assert model_turn["parts"][0]["thoughtSignature"] == "a-real-signature"
    # The passthrough field itself is not a Gemini field and must not leak out.
    assert "extra_content" not in model_turn["parts"][0]["functionCall"]


def test_to_gemini_request_signs_only_the_first_parallel_call() -> None:
    """Parallel calls carry the signature on the first part only, as Gemini sends it."""
    req = _request(
        messages=[
            {"role": "user", "content": "Weather in Tokyo and Paris?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'},
                        "extra_content": {"google": {"thought_signature": "sig-one"}},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    },
                ],
            },
        ]
    )
    parts = to_gemini_request(req)["contents"][1]["parts"]
    assert parts[0]["thoughtSignature"] == "sig-one"
    assert "thoughtSignature" not in parts[1]


def test_from_gemini_response_ignores_an_empty_thought_signature() -> None:
    """An empty signature is absence, not data; the placeholder covers it later."""
    raw = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "get_weather", "args": {}},
                            "thoughtSignature": "",
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    resp = from_gemini_response(raw, "gemini-3.5-flash-lite")
    assert resp.tool_calls is not None
    assert resp.tool_calls[0].extra_content is None


def test_to_gemini_request_consecutive_tool_responses_bundled() -> None:
    """Consecutive tool result messages bundle into a single 'user' turn with multiple parts."""
    req = _request(
        messages=[
            {"role": "user", "content": "Weather in Tokyo and Paris?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"location": "Tokyo"}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"location": "Paris"}'},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "Sunny 15C",
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "content": '{"temp": 12}',
            },
        ]
    )
    payload = to_gemini_request(req)
    assert len(payload["contents"]) == 3
    tool_turn = payload["contents"][2]
    assert tool_turn["role"] == "user"
    assert len(tool_turn["parts"]) == 2
    assert tool_turn["parts"][0] == {
        "functionResponse": {
            "name": "get_weather",
            "response": {"output": "Sunny 15C"},
        }
    }
    assert tool_turn["parts"][1] == {
        "functionResponse": {
            "name": "get_weather",
            "response": {"temp": 12},
        }
    }


def test_from_gemini_response_tool_call() -> None:
    """Gemini response with functionCall part produces tool_calls.

    Produces content: None and finish_reason: tool_calls.
    """
    raw = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_current_weather",
                                "args": {"location": "Boston, MA"},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": "10",
            "candidatesTokenCount": "5",
            "totalTokenCount": "15",
        },
    }

    resp = from_gemini_response(raw, "gemini-3.5-flash-lite")
    assert resp.content is None
    assert resp.finish_reason == "tool_calls"
    assert resp.model_used == "gemini-3.5-flash-lite"
    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1

    tc = resp.tool_calls[0]
    assert tc.type == "function"
    assert tc.function["name"] == "get_current_weather"
    assert json.loads(tc.function["arguments"]) == {"location": "Boston, MA"}
    assert tc.id.startswith("call_")

    # Call ID synthesis is deterministic for the same function and index
    resp2 = from_gemini_response(raw, "gemini-3.5-flash-lite")
    assert resp2.tool_calls is not None
    assert resp2.tool_calls[0].id == tc.id


def test_chunk_from_gemini_tool_call() -> None:
    """Stream event with functionCall produces ToolCallDelta and finish_reason: tool_calls."""
    event = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "calculator",
                                "args": {"expr": "2+2"},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ],
    }

    chunk = _chunk_from_gemini(event)
    assert chunk is not None
    assert chunk.delta == ""
    assert chunk.finish_reason == "tool_calls"
    assert chunk.tool_call_deltas is not None
    assert len(chunk.tool_call_deltas) == 1
    assert chunk.tool_call_deltas[0].index == 0
    assert chunk.tool_call_deltas[0].function == {
        "name": "calculator",
        "arguments": '{"expr": "2+2"}',
    }


@respx.mock
async def test_gemini_unary_second_turn_round_trip(
    http_client: httpx.AsyncClient,
    key: KeyRef,
) -> None:
    """Second-turn tool result message produces a normal assistant answer.

    The signature Gemini attached to the call must ride back out on the replayed
    model turn, exactly where it arrived. Without it Gemini 3 answers the second
    turn with a 400, which is what a mocked body could never have revealed.
    """
    signature = "signature-from-gemini-3"
    # First turn: Gemini returns a function call
    turn1_body = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"location": "Tokyo"},
                            },
                            "thoughtSignature": signature,
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    # Second turn: Gemini returns assistant text after tool result
    turn2_body = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": "The weather in Tokyo is 15C and sunny."}],
                },
                "finishReason": "STOP",
            }
        ]
    }

    route = respx.post(GENERATE_URL)
    route.side_effect = [
        httpx.Response(200, json=turn1_body),
        httpx.Response(200, json=turn2_body),
    ]

    adapter = GoogleAdapter(http_client=http_client)

    # 1. First turn
    req1 = _request(
        messages=[{"role": "user", "content": "What is the weather in Tokyo?"}],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
    )
    resp1 = await adapter.chat_completion(req1, "gemini-3.5-flash-lite", key)
    assert resp1.finish_reason == "tool_calls"
    assert resp1.tool_calls is not None
    call_id = resp1.tool_calls[0].id
    assert resp1.tool_calls[0].extra_content == {"google": {"thought_signature": signature}}

    # 2. Second turn: client echoes tool result
    req2 = _request(
        messages=[
            {"role": "user", "content": "What is the weather in Tokyo?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tc.model_dump(exclude_none=True) for tc in resp1.tool_calls],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": '{"temp": 15, "sky": "sunny"}',
            },
        ],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
    )
    resp2 = await adapter.chat_completion(req2, "gemini-3.5-flash-lite", key)
    assert resp2.finish_reason == "stop"
    assert resp2.content == "The weather in Tokyo is 15C and sunny."
    assert resp2.tool_calls is None

    replayed = json.loads(route.calls[1].request.content)
    model_turn = next(c for c in replayed["contents"] if c["role"] == "model")
    assert model_turn["parts"][0]["thoughtSignature"] == signature


class TestStructuredOutput:
    """Translation and handling of response_format in GoogleAdapter."""

    def test_json_object_sets_mime_type(self) -> None:
        req = _request(
            messages=[{"role": "user", "content": "Return json"}],
            response_format={"type": "json_object"},
        )
        payload = to_gemini_request(req)
        assert payload.get("generationConfig") == {"responseMimeType": "application/json"}

    def test_json_schema_sets_mime_type_and_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        req = _request(
            messages=[{"role": "user", "content": "Return json"}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "Person",
                    "schema": schema,
                },
            },
        )
        payload = to_gemini_request(req)
        assert payload.get("generationConfig") == {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }

    def test_json_schema_without_schema_property(self) -> None:
        req = _request(
            messages=[{"role": "user", "content": "Return json"}],
            response_format={"type": "json_schema", "json_schema": {"name": "NoSchema"}},
        )
        payload = to_gemini_request(req)
        assert payload.get("generationConfig") == {"responseMimeType": "application/json"}

    def test_no_response_format_no_generation_config(self) -> None:
        req = _request(messages=[{"role": "user", "content": "Return text"}])
        payload = to_gemini_request(req)
        assert "generationConfig" not in payload

    def test_unsupported_response_format_type_ignored(self) -> None:
        req = _request(
            messages=[{"role": "user", "content": "Return text"}],
            response_format={"type": "text"},
        )
        payload = to_gemini_request(req)
        assert "generationConfig" not in payload

    def test_response_format_coexists_with_tools(self) -> None:
        tools = [{"type": "function", "function": {"name": "get_weather"}}]
        req = _request(
            messages=[{"role": "user", "content": "Hi"}],
            tools=tools,
            response_format={"type": "json_object"},
        )
        payload = to_gemini_request(req)
        assert payload.get("generationConfig") == {"responseMimeType": "application/json"}
        assert "tools" in payload
        assert len(payload["tools"][0]["functionDeclarations"]) == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_round_trip_with_response_format(
        self, http_client: httpx.AsyncClient, key: KeyRef
    ) -> None:
        route = respx.post(GENERATE_URL).respond(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": '{"result": "ok"}'}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 9,
                },
            },
        )
        adapter = GoogleAdapter(http_client=http_client)
        req = _request(
            messages=[{"role": "user", "content": "Return json"}],
            response_format={"type": "json_object"},
        )
        resp = await adapter.chat_completion(req, "gemini-3.5-flash-lite", key)
        assert resp.content == '{"result": "ok"}'
        assert resp.finish_reason == "stop"
        sent_body = json.loads(route.calls[0].request.content)
        assert sent_body.get("generationConfig") == {"responseMimeType": "application/json"}
