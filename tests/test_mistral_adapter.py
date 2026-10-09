"""Mistral adapter: translation, error mapping, and the timeout envelope.

Fixture shapes here are taken from Mistral's published OpenAPI spec
(``docs.mistral.ai/openapi.yaml``) and the Chat endpoint reference, checked
2026-10-08, rather than written from memory. Three of them are load-bearing:

- ``SUCCESS_BODY`` mirrors the documented response, where ``finish_reason`` is
  already ``stop`` and the content lives at ``choices[0].message.content``.
- ``ERROR_BODY`` mirrors the documented error envelope,
  ``{object, message, type, param, code}``, which is the same shape this router
  emits. There is deliberately no assertion that the adapter parses it: Mistral
  status codes already distinguish the cases, so the body is never read.
- ``VALIDATION_BODY_422`` mirrors the spec's ``HTTPValidationError``, which is
  FastAPI-shaped (``{detail: [...]}``) and *not* the documented envelope. A
  422 therefore cannot be classified by parsing the body.
"""

import json
from typing import Any

import httpx
import pytest
import respx

from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
    UnsupportedModelError,
)
from free_router.core.normalization import NormalizedRequest
from free_router.providers.base import ProviderAdapter, build_adapter, supported_providers
from free_router.providers.mistral.adapter import (
    MISTRAL_API_BASE,
    MISTRAL_CHAT_PATH,
    PROVIDER_TIMEOUT_SECONDS,
    MistralAdapter,
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


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide a dummy key so the adapter can build its auth header."""
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    return "test-mistral-key"


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


@pytest.fixture
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the key the autouse fixture set, to exercise the unset path."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)


def _request(messages: list[dict[str, Any]] | None = None) -> NormalizedRequest:
    return NormalizedRequest(
        model=f"mistral/{MODEL_ID}",
        messages=messages if messages is not None else [{"role": "user", "content": "hello"}],
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


def test_null_content_becomes_empty_string() -> None:
    """Mistral returns null content for a tool-call-only message.

    M0 strips tools at ingress so this is not reachable today; raising on a
    complete response would be the wrong behaviour if it ever were.
    """
    body = json.loads(json.dumps(SUCCESS_BODY))
    body["choices"][0]["message"]["content"] = None

    assert from_mistral_response(body, MODEL_ID).content == ""


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
async def test_success_round_trip(http_client: httpx.AsyncClient) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    response = await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)

    assert response.content == "hello from mistral"
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == MODEL_ID


@respx.mock
async def test_key_travels_in_a_bearer_header_not_the_url(
    http_client: httpx.AsyncClient,
) -> None:
    """A key in the URL would reach proxy and access logs."""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)

    assert route.calls[0].request.headers["Authorization"] == "Bearer test-mistral-key"
    assert "test-mistral-key" not in str(route.calls[0].request.url)


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_maps_to_auth_error(http_client: httpx.AsyncClient, status: int) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(status, json={**ERROR_BODY, "type": "authentication_error"})
    )

    with pytest.raises(ProviderAuthError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
async def test_rate_limit_maps_to_rate_limit_error(http_client: httpx.AsyncClient) -> None:
    """429 must not be reported as a malformed request.

    Collapsing it into the generic 4xx branch makes a capacity problem look like
    a client payload error, which sends an operator to debug the wrong thing.
    """
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(429, json={**ERROR_BODY, "type": "rate_limit_error"})
    )

    with pytest.raises(ProviderRateLimitError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_rejected_payload_maps_to_validation_error(
    http_client: httpx.AsyncClient, status: int
) -> None:
    # 422 uses the FastAPI-shaped body from the OpenAPI spec, not the documented
    # envelope. It must still be classified, and without parsing the body.
    body = VALIDATION_BODY_422 if status == 422 else ERROR_BODY
    respx.post(CHAT_URL).mock(return_value=httpx.Response(status, json=body))

    with pytest.raises(ProviderValidationError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error_maps_to_server_error(
    http_client: httpx.AsyncClient, status: int
) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(status, json={"message": "oops"}))

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
async def test_transport_failure_maps_to_server_error(http_client: httpx.AsyncClient) -> None:
    """A connection failure is an exception, not a response, so it bypasses status mapping.

    Uncaught it reaches the generic handler and is reported as a router fault,
    misattributing a network problem to the router itself.
    """
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
async def test_timeout_maps_to_timeout_error(http_client: httpx.AsyncClient) -> None:
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderTimeoutError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
async def test_undecodable_body_maps_to_server_error(http_client: httpx.AsyncClient) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, content=b"not json", headers={"Content-Type": "text/html"})
    )

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@respx.mock
async def test_non_object_body_maps_to_server_error(http_client: httpx.AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=["a", "list"]))

    with pytest.raises(ProviderServerError):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


@pytest.mark.usefixtures("no_api_key")
async def test_missing_api_key_raises_auth_error(http_client: httpx.AsyncClient) -> None:
    """An unset key is an auth failure, and it must not reach the network."""
    with pytest.raises(ProviderAuthError, match="MISTRAL_API_KEY"):
        await MistralAdapter(http_client=http_client).chat_completion(_request(), MODEL_ID)


def test_provider_timeout_is_shared_with_the_google_adapter() -> None:
    """One environment variable must have one resolved value across adapters."""
    from free_router.providers.google import adapter as google_adapter

    assert PROVIDER_TIMEOUT_SECONDS == google_adapter.PROVIDER_TIMEOUT_SECONDS
