"""Adapter streaming: provider SSE in, :class:`NormalizedChunk` out.

Both providers stream as SSE and agree on almost nothing else, so each adapter is
tested against its own recorded event shapes. Gemini sends string token counts and
puts ``finishReason`` on the candidate; Mistral sends OpenAI-shaped choices with
numeric counts and reports usage only in a trailing chunk.

The shared framing (``data:`` lines, ``[DONE]``) is covered in
``test_streaming.py``; what is asserted here is the *semantics* each adapter adds.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from free_router.core.normalization import NormalizedChunk, NormalizedRequest
from free_router.keys.manager import KeyRef
from free_router.providers.base import ProviderAdapter, build_adapter
from free_router.providers.google.adapter import API_VERSION, GOOGLE_API_BASE, GoogleAdapter
from free_router.providers.mistral.adapter import (
    MISTRAL_API_BASE,
    MISTRAL_CHAT_PATH,
    MistralAdapter,
)

GEMINI_MODEL = "gemini-3.5-flash-lite"
MISTRAL_MODEL = "ministral-3b-2512"

GEMINI_STREAM_URL = (
    f"{GOOGLE_API_BASE}/{API_VERSION}/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse"
)
MISTRAL_STREAM_URL = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"

SSE_HEADERS = {"content-type": "text/event-stream"}

GEMINI_STREAM = "\n\n".join(
    [
        'data: {"candidates": [{"content": {"parts": [{"text": "Hel"}], "role": "model"}, '
        '"index": 0}]}',
        'data: {"candidates": [{"content": {"parts": [{"text": "lo"}], "role": "model"}, '
        '"finishReason": "STOP", "index": 0}], '
        '"usageMetadata": {"promptTokenCount": "8", "candidatesTokenCount": "2", '
        '"totalTokenCount": "10"}}',
    ]
)

MISTRAL_STREAM = "\n\n".join(
    [
        'data: {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1, '
        f'"model": "{MISTRAL_MODEL}", '
        '"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, '
        '"finish_reason": null}]}',
        'data: {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1, '
        f'"model": "{MISTRAL_MODEL}", '
        '"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": null}]}',
        'data: {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1, '
        f'"model": "{MISTRAL_MODEL}", '
        '"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}',
        'data: {"id": "cmpl-1", "object": "chat.completion.chunk", "created": 1, '
        f'"model": "{MISTRAL_MODEL}", "choices": [], '
        '"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}',
        "data: [DONE]",
    ]
)


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


def _google_key() -> KeyRef:
    return KeyRef(
        alias="primary",
        provider="google",
        model_id=GEMINI_MODEL,
        secret=SecretStr("test-google-key"),
    )


def _mistral_key() -> KeyRef:
    return KeyRef(
        alias="primary",
        provider="mistral",
        model_id=MISTRAL_MODEL,
        secret=SecretStr("test-mistral-key"),
    )


def _request(model: str, *, include_usage: bool = False) -> NormalizedRequest:
    return NormalizedRequest(
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        include_usage=include_usage,
    )


async def _collect(
    adapter: Any, request: NormalizedRequest, model_id: str, key: KeyRef
) -> list[NormalizedChunk]:
    return [chunk async for chunk in adapter.stream_chat_completion(request, model_id, key)]


# --- Capability ---


def test_both_adapters_advertise_streaming(http_client: httpx.AsyncClient) -> None:
    """The route reads this before dispatch to decide on ``streaming_unsupported``."""
    assert build_adapter("google", http_client).supports_streaming is True
    assert build_adapter("mistral", http_client).supports_streaming is True


def test_adapters_satisfy_the_protocol(http_client: httpx.AsyncClient) -> None:
    """A sibling method that does not match the protocol would only fail at runtime."""
    assert isinstance(GoogleAdapter(http_client=http_client), ProviderAdapter)
    assert isinstance(MistralAdapter(http_client=http_client), ProviderAdapter)


# --- Google ---


@respx.mock
async def test_google_streams_text_finish_and_usage(http_client: httpx.AsyncClient) -> None:
    respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GEMINI_STREAM, headers=SSE_HEADERS)
    )

    chunks = await _collect(
        GoogleAdapter(http_client=http_client),
        _request(f"google/{GEMINI_MODEL}"),
        GEMINI_MODEL,
        _google_key(),
    )

    assert "".join(chunk.delta for chunk in chunks) == "Hello"
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_tokens == 8
    assert chunks[-1].usage.output_tokens == 2
    assert chunks[-1].usage.total_tokens == 10


@respx.mock
async def test_google_uses_the_sse_variant_of_the_endpoint(http_client: httpx.AsyncClient) -> None:
    """Without ``alt=sse`` Gemini answers with one array after generation completes.

    That would look like a working stream and deliver nothing until the end.
    """
    route = respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GEMINI_STREAM, headers=SSE_HEADERS)
    )

    await _collect(
        GoogleAdapter(http_client=http_client),
        _request(f"google/{GEMINI_MODEL}"),
        GEMINI_MODEL,
        _google_key(),
    )

    assert ":streamGenerateContent" in str(route.calls[0].request.url)
    assert route.calls[0].request.url.params["alt"] == "sse"


@respx.mock
async def test_google_key_travels_in_a_header_not_the_url(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GEMINI_STREAM, headers=SSE_HEADERS)
    )

    await _collect(
        GoogleAdapter(http_client=http_client),
        _request(f"google/{GEMINI_MODEL}"),
        GEMINI_MODEL,
        _google_key(),
    )

    assert route.calls[0].request.headers["x-goog-api-key"] == "test-google-key"
    assert "test-google-key" not in str(route.calls[0].request.url)


@respx.mock
async def test_google_drops_events_that_carry_nothing(http_client: httpx.AsyncClient) -> None:
    """An event with no candidate, no text, no finish and no usage is not content."""
    body = "\n\n".join(
        [
            'data: {"candidates": [], "usageMetadata": {}}',
            'data: {"candidates": [{"content": {"parts": [{"text": "hi"}]}, '
            '"finishReason": "STOP"}]}',
        ]
    )
    respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(200, text=body, headers=SSE_HEADERS)
    )

    chunks = await _collect(
        GoogleAdapter(http_client=http_client),
        _request(f"google/{GEMINI_MODEL}"),
        GEMINI_MODEL,
        _google_key(),
    )

    assert len(chunks) == 1
    assert chunks[0].delta == "hi"


@respx.mock
async def test_google_does_not_send_stream_flags_it_has_no_use_for(
    http_client: httpx.AsyncClient,
) -> None:
    """Streaming is a URL choice for Gemini, so the body stays the unary body."""
    route = respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GEMINI_STREAM, headers=SSE_HEADERS)
    )

    await _collect(
        GoogleAdapter(http_client=http_client),
        _request(f"google/{GEMINI_MODEL}"),
        GEMINI_MODEL,
        _google_key(),
    )

    sent: dict[str, Any] = json.loads(route.calls[0].request.content)
    assert "stream" not in sent
    assert "stream_options" not in sent


# --- Mistral ---


@respx.mock
async def test_mistral_streams_text_finish_and_usage(http_client: httpx.AsyncClient) -> None:
    respx.post(MISTRAL_STREAM_URL).mock(
        return_value=httpx.Response(200, text=MISTRAL_STREAM, headers=SSE_HEADERS)
    )

    chunks = await _collect(
        MistralAdapter(http_client=http_client),
        _request(f"mistral/{MISTRAL_MODEL}", include_usage=True),
        MISTRAL_MODEL,
        _mistral_key(),
    )

    assert "".join(chunk.delta for chunk in chunks) == "Hel"
    assert any(chunk.finish_reason == "stop" for chunk in chunks)
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 5


@respx.mock
async def test_mistral_requests_streaming_in_the_body(http_client: httpx.AsyncClient) -> None:
    route = respx.post(MISTRAL_STREAM_URL).mock(
        return_value=httpx.Response(200, text=MISTRAL_STREAM, headers=SSE_HEADERS)
    )

    await _collect(
        MistralAdapter(http_client=http_client),
        _request(f"mistral/{MISTRAL_MODEL}", include_usage=True),
        MISTRAL_MODEL,
        _mistral_key(),
    )

    sent: dict[str, Any] = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True


@respx.mock
async def test_mistral_asks_for_usage_only_when_the_client_did(
    http_client: httpx.AsyncClient,
) -> None:
    """B7: the flag is honoured by making the number exist, not by hiding it.

    Mistral reports usage in a stream only when asked, so forwarding the flag
    unconditionally would change the provider payload for no client request.
    """
    route = respx.post(MISTRAL_STREAM_URL).mock(
        return_value=httpx.Response(200, text=MISTRAL_STREAM, headers=SSE_HEADERS)
    )

    await _collect(
        MistralAdapter(http_client=http_client),
        _request(f"mistral/{MISTRAL_MODEL}"),
        MISTRAL_MODEL,
        _mistral_key(),
    )

    sent: dict[str, Any] = json.loads(route.calls[0].request.content)
    assert "stream_options" not in sent


class _StallingStream(httpx.AsyncByteStream):
    """A provider that sends one usable event and then stops responding.

    Used to pin the mid-stream timeout classification: the request was accepted,
    so a silence is a 504-class fault even though the response has started.

    Args:
        first_event: A provider-shaped event the adapter under test recognises.
    """

    def __init__(self, first_event: str) -> None:
        self._first_event = first_event

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield f"data: {self._first_event}\n\n".encode()
        raise httpx.ReadTimeout("stalled")


@respx.mock
async def test_google_mid_stream_timeout_is_a_timeout_not_a_server_error(
    http_client: httpx.AsyncClient,
) -> None:
    from free_router.core.exceptions import ProviderTimeoutError

    respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            stream=_StallingStream('{"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}'),
            headers=SSE_HEADERS,
        )
    )

    iterator = GoogleAdapter(http_client=http_client).stream_chat_completion(
        _request(f"google/{GEMINI_MODEL}"), GEMINI_MODEL, _google_key()
    )

    assert await anext(iterator) is not None
    with pytest.raises(ProviderTimeoutError):
        await anext(iterator)


@respx.mock
async def test_mistral_mid_stream_timeout_is_a_timeout_not_a_server_error(
    http_client: httpx.AsyncClient,
) -> None:
    from free_router.core.exceptions import ProviderTimeoutError

    respx.post(MISTRAL_STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            stream=_StallingStream('{"choices": [{"delta": {"content": "hi"}}]}'),
            headers=SSE_HEADERS,
        )
    )

    iterator = MistralAdapter(http_client=http_client).stream_chat_completion(
        _request(f"mistral/{MISTRAL_MODEL}"), MISTRAL_MODEL, _mistral_key()
    )

    assert await anext(iterator) is not None
    with pytest.raises(ProviderTimeoutError):
        await anext(iterator)
