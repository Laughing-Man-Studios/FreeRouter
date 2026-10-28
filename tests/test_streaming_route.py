"""Streaming through the full stack: SSE delivery, fail-fast, and settlement.

These are route-level tests, not adapter or renderer tests. They exist to pin the
four claims Batch 5 makes, each of which is about what must *not* happen:

- An interruption after the first delivered chunk ends the stream and never
  reaches a second provider (ADR-0007, spec Scenario 9).
- A zero-chunk stream has delivered nothing, so it retries inside the budget.
- An interrupted stream settles its reservation; no request is left in flight.
- A provider that cannot stream raises rather than returning an empty iterator.

The provider is mocked at the HTTP boundary with ``respx``, because the failure
being tested is a *provider* failure and a fake adapter would bypass the
classification the route depends on.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from free_router.core.normalization import NormalizedRequest
from free_router.providers.google.adapter import API_VERSION, GOOGLE_API_BASE
from free_router.providers.mistral.adapter import MISTRAL_API_BASE, MISTRAL_CHAT_PATH

GOOGLE_MODEL = "gemini-3.5-flash-lite"
MISTRAL_MODEL = "ministral-3b-2512"

GOOGLE_STREAM_URL = (
    f"{GOOGLE_API_BASE}/{API_VERSION}/models/{GOOGLE_MODEL}:streamGenerateContent?alt=sse"
)
MISTRAL_CHAT_URL = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"

TWO_MODEL_CONFIG = f"""\
models:
  - id: "google/{GOOGLE_MODEL}"
    provider: "google"
    provider_id: "{GOOGLE_MODEL}"
  - id: "mistral/{MISTRAL_MODEL}"
    provider: "mistral"
    provider_id: "{MISTRAL_MODEL}"
"""

GOOGLE_STREAM_BODY = "\n\n".join(
    [
        'data: {"candidates": [{"content": {"parts": [{"text": "Hel"}], "role": "model"}, '
        '"index": 0}]}',
        'data: {"candidates": [{"content": {"parts": [{"text": "lo"}], "role": "model"}, '
        '"finishReason": "STOP", "index": 0}], '
        '"usageMetadata": {"promptTokenCount": "8", "candidatesTokenCount": "2", '
        '"totalTokenCount": "10"}}',
    ]
)

SSE_HEADERS = {"content-type": "text/event-stream"}

STREAM_BODY = {
    "model": f"google/{GOOGLE_MODEL}",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": True,
}


@pytest.fixture
def two_model_client(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A started app declaring one model per provider.

    Two models are declared so "the model never varies" is observable: a second
    provider's endpoint is reachable, and the test asserts it is never called.
    """
    from free_router.api.main import create_app

    (workdir / "config.yaml").write_text(TWO_MODEL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "test-google-key-primary")
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "test-mistral-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    with TestClient(create_app()) as started:
        yield started


def _data_events(body: str) -> list[str]:
    """The SSE payloads, excluding the terminator, in order."""
    return [
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


# --- Happy path ---------------------------------------------------------------


@respx.mock
def test_streaming_returns_an_event_stream(two_model_client: TestClient) -> None:
    """The response is SSE, not JSON, and it is terminated by ``[DONE]``."""
    respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GOOGLE_STREAM_BODY, headers=SSE_HEADERS)
    )

    response = two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")


@respx.mock
def test_streaming_emits_role_then_content_then_finish(two_model_client: TestClient) -> None:
    """B7: the role delta comes first, and one chunk terminates the stream."""
    respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GOOGLE_STREAM_BODY, headers=SSE_HEADERS)
    )

    body = two_model_client.post("/v1/chat/completions", json=STREAM_BODY).text
    events = [json.loads(payload) for payload in _data_events(body)]

    deltas = [event["choices"][0]["delta"] for event in events]
    assert deltas[0] == {"role": "assistant"}
    assert "".join(delta.get("content", "") for delta in deltas) == "Hello"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"]["total_tokens"] == 10
    assert {event["model"] for event in events} == {f"google/{GOOGLE_MODEL}"}


@respx.mock
def test_include_usage_is_forwarded_but_usage_is_always_reported(
    two_model_client: TestClient,
) -> None:
    """B7 read literally: Gemini reports usage whether or not the client asked.

    The flag matters for Mistral, which needs asking. For Gemini it changes
    nothing, which is why the payload carries no ``stream_options``.
    """
    route = respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GOOGLE_STREAM_BODY, headers=SSE_HEADERS)
    )

    body = two_model_client.post(
        "/v1/chat/completions",
        json={**STREAM_BODY, "stream_options": {"include_usage": True}},
    ).text

    assert json.loads(_data_events(body)[-1])["usage"]["total_tokens"] == 10
    assert len(route.calls) == 1


def test_stream_field_must_be_a_boolean(two_model_client: TestClient) -> None:
    """``"stream": "true"`` is a client bug; coercing it would hide the bug."""
    response = two_model_client.post("/v1/chat/completions", json={**STREAM_BODY, "stream": "true"})

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "stream"


# --- Scenario 9: fail-fast ----------------------------------------------------


@respx.mock
def test_interruption_after_the_first_chunk_never_reaches_another_provider(
    two_model_client: TestClient,
) -> None:
    """Spec Scenario 9 and CONSTITUTION §7.2, asserted at the wire.

    The Google stream delivers one chunk and then sends a malformed event. The
    response must end there. If the route retried, the mistral endpoint would be
    reachable and the ``(provider, model)`` pair would have varied after
    delivery -- the exact splice ADR-0007 forbids.
    """
    google = respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            text="\n\n".join(
                [
                    'data: {"candidates": [{"content": {"parts": [{"text": "partial"}], '
                    '"role": "model"}, "index": 0}]}',
                    "data: {not json",
                ]
            ),
            headers=SSE_HEADERS,
        )
    )
    mistral = respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(200, text="", headers=SSE_HEADERS)
    )

    response = two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    assert response.status_code == 200
    # The partial answer is delivered; the stream ends without claiming success.
    assert "partial" in response.text
    assert "[DONE]" not in response.text

    assert len(google.calls) == 1
    assert mistral.called is False
    # Every call that happened used the requested model, not a substitute.
    assert all(":streamGenerateContent" in str(call.request.url) for call in google.calls)
    assert GOOGLE_MODEL in str(google.calls[0].request.url)
    assert json.loads(google.calls[0].request.content)["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]}
    ]


@respx.mock
def test_a_zero_chunk_stream_is_retried(two_model_client: TestClient) -> None:
    """Nothing was delivered, so the failure is still retryable (ADR-0007).

    A provider that answers ``200`` and then sends nothing is a transient fault,
    not a partial response. Treating it as "after stream start" would turn a
    recoverable empty answer into a permanent one.
    """
    route = respx.post(GOOGLE_STREAM_URL).mock(
        side_effect=[
            httpx.Response(200, text="data: [DONE]", headers=SSE_HEADERS),
            httpx.Response(200, text=GOOGLE_STREAM_BODY, headers=SSE_HEADERS),
        ]
    )

    response = two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    assert response.status_code == 200
    contents = "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in (json.loads(payload) for payload in _data_events(response.text))
    )
    assert contents == "Hello"
    assert len(route.calls) == 2


@respx.mock
def test_a_provider_that_only_ever_returns_nothing_is_reported_as_a_fault(
    two_model_client: TestClient,
) -> None:
    """Exhausting the budget on empty streams is a 502, not a 200 with no body."""
    respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(200, text="data: [DONE]", headers=SSE_HEADERS)
    )

    response = two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")


# --- Reservation lifecycle (T058) ---------------------------------------------


@respx.mock
def test_an_interrupted_stream_leaves_no_pending_reservation(
    two_model_client: TestClient,
) -> None:
    """A leaked reservation would hold capacity for the rest of the window."""
    respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            text="\n\n".join(
                [
                    'data: {"candidates": [{"content": {"parts": [{"text": "partial"}], '
                    '"role": "model"}, "index": 0}]}',
                    "data: {not json",
                ]
            ),
            headers=SSE_HEADERS,
        )
    )

    two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    tracker = two_model_client.app.state.dispatcher.quota
    assert tracker._pending == {}


@respx.mock
def test_a_completed_stream_leaves_no_pending_reservation(
    two_model_client: TestClient,
) -> None:
    """The success path settles too, against the usage the provider reported."""
    respx.post(GOOGLE_STREAM_URL).mock(
        return_value=httpx.Response(200, text=GOOGLE_STREAM_BODY, headers=SSE_HEADERS)
    )

    two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    tracker = two_model_client.app.state.dispatcher.quota
    assert tracker._pending == {}


# --- Capability (T055) --------------------------------------------------------


def test_a_provider_that_cannot_stream_is_refused_not_emptied(
    two_model_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsupported stream must not look like a stream that produced nothing."""

    class _Unstreamable:
        supports_streaming = False

    monkeypatch.setattr(
        "free_router.api.stream_dispatch.build_adapter", lambda provider, client: _Unstreamable()
    )

    response = two_model_client.post("/v1/chat/completions", json=STREAM_BODY)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "streaming_unsupported"
    assert error["param"] == "stream"


# --- The unary path is untouched (T059) ---------------------------------------


@respx.mock
def test_a_non_streaming_request_still_returns_one_json_object(
    two_model_client: TestClient,
) -> None:
    """The M1 path that measured 100/100 must not have gained a stream branch."""
    respx.post(f"{GOOGLE_API_BASE}/{API_VERSION}/models/{GOOGLE_MODEL}:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "hi"}], "role": "model"},
                        "finishReason": "STOP",
                    }
                ]
            },
        )
    )

    response = two_model_client.post(
        "/v1/chat/completions",
        json={
            "model": f"google/{GOOGLE_MODEL}",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["choices"][0]["message"]["content"] == "hi"


def test_stream_flag_is_carried_on_the_normalized_request() -> None:
    """The normalizer, not the route, decides what the flag means."""
    from free_router.core.normalization import normalize_chat_completion_request

    normalized = normalize_chat_completion_request(
        {**STREAM_BODY, "stream_options": {"include_usage": True}}
    )

    assert isinstance(normalized, NormalizedRequest)
    assert normalized.stream is True
    assert normalized.include_usage is True
