"""Google (Gemini) adapter: translation, error mapping, and the timeout envelope."""

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
    UnsupportedModelError,
)
from free_router.core.normalization import NormalizedRequest, NormalizedResponse
from free_router.providers.base import ProviderAdapter, build_adapter, supported_providers
from free_router.providers.google.adapter import (
    GOOGLE_API_BASE,
    PROVIDER_TIMEOUT_SECONDS,
    GoogleAdapter,
    from_gemini_response,
    to_gemini_request,
)

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"

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


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide a dummy key so the adapter can build its auth header."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    return "test-gemini-key"


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


def _request(messages: list[dict[str, Any]] | None = None) -> NormalizedRequest:
    return NormalizedRequest(
        model="google/gemini-3.5-flash-lite",
        messages=messages or [{"role": "user", "content": "hello"}],
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
) -> None:
    """Scenario 1: a 200 becomes a NormalizedResponse."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    adapter = GoogleAdapter(http_client=http_client)
    result = await adapter.chat_completion(_request(), "gemini-3.5-flash-lite")

    assert isinstance(result, NormalizedResponse)
    assert result.content == "hello from gemini"
    assert route.called


@respx.mock
async def test_request_uses_api_key_header_not_query_param(
    http_client: httpx.AsyncClient,
) -> None:
    """The key travels in a header so it cannot leak into URLs."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    await GoogleAdapter(http_client=http_client).chat_completion(
        _request(), "gemini-3.5-flash-lite"
    )

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "test-gemini-key"
    assert "key=" not in str(request.url)
    assert "test-gemini-key" not in str(request.url)


@respx.mock
async def test_auth_error_raises_provider_auth_error(
    http_client: httpx.AsyncClient,
) -> None:
    """Scenario 8: provider 401 becomes ProviderAuthError."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(401, json={"error": {}}))

    with pytest.raises(ProviderAuthError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_invalid_api_key_400_maps_to_auth_error(
    http_client: httpx.AsyncClient,
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
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_permission_denied_400_maps_to_auth_error(
    http_client: httpx.AsyncClient,
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
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_genuine_bad_payload_400_is_not_an_auth_error(
    http_client: httpx.AsyncClient,
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
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_unparseable_400_body_is_a_validation_error(
    http_client: httpx.AsyncClient,
) -> None:
    """An unreadable body falls back to the default mapping, not auth."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(400, text="not json"))

    with pytest.raises(ProviderValidationError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_server_error_raises_provider_server_error(
    http_client: httpx.AsyncClient,
) -> None:
    """Scenario 9: provider 5xx becomes ProviderServerError."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(ProviderServerError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_bad_request_raises_provider_validation_error(
    http_client: httpx.AsyncClient,
) -> None:
    """Provider 400 becomes ProviderValidationError."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )

    with pytest.raises(ProviderValidationError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_error_message_never_contains_key_or_prompt(
    http_client: httpx.AsyncClient,
) -> None:
    """Provider errors must not echo the secret or the prompt (spec 9)."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "reflected: my-secret-prompt"}})
    )

    with pytest.raises(ProviderValidationError) as excinfo:
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request([{"role": "user", "content": "my-secret-prompt"}]),
            "gemini-3.5-flash-lite",
        )

    message = excinfo.value.message
    assert "my-secret-prompt" not in message
    assert "test-gemini-key" not in message


@respx.mock
async def test_non_json_body_raises_provider_server_error(
    http_client: httpx.AsyncClient,
) -> None:
    """An undecodable body is an upstream fault, not a crash."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))

    with pytest.raises(ProviderServerError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_missing_api_key_raises_provider_auth_error(
    http_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key disappearing after startup surfaces as an auth error, not a crash."""
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(ProviderAuthError, match="GEMINI_API_KEY"):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


# --- Timeout envelope (Scenario 10) ---


@respx.mock
async def test_slow_headers_raise_timeout(http_client: httpx.AsyncClient) -> None:
    """Scenario 10: exceeding the 0.4s header budget raises ProviderTimeoutError."""
    respx.post(GENERATE_URL).mock(side_effect=httpx.ReadTimeout("headers too slow"))

    with pytest.raises(ProviderTimeoutError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_asyncio_timeout_is_enforced_on_slow_headers(
    http_client: httpx.AsyncClient,
) -> None:
    """A connection slower than 0.4s is cut off by asyncio.timeout itself.

    This asserts the real budget rather than trusting the httpx exception path:
    the route sleeps past PROVIDER_TIMEOUT_SECONDS, so only the strict
    asyncio.timeout can produce the failure.
    """

    async def _slow_response(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(PROVIDER_TIMEOUT_SECONDS * 3)
        return httpx.Response(200, json=SUCCESS_BODY)

    respx.post(GENERATE_URL).mock(side_effect=_slow_response)

    with pytest.raises(ProviderTimeoutError):
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request(), "gemini-3.5-flash-lite"
        )


@respx.mock
async def test_slow_body_is_not_subject_to_the_header_timeout(
    http_client: httpx.AsyncClient,
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
        _request(), "gemini-3.5-flash-lite"
    )

    assert result.content == "hello from gemini"


class _SlowBody(httpx.AsyncByteStream):
    """A response body that sleeps past the header budget before yielding."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> Any:
        await asyncio.sleep(PROVIDER_TIMEOUT_SECONDS * 2)
        yield self._payload
