"""T060: provider error classification, all four A4 improvements.

Everything here is about a failure staying *actionable*. The wire is unhelpful
on purpose: Gemini reports a bad key as ``400 INVALID_ARGUMENT``, a burst 429 and
a daily-cap 429 are byte-identical, and a 503 from an overloaded model and a 500
from a bug differ only in a status digit. So each test pins one decision about
which evidence the router trusts, and what it refuses to guess:

- the provider's own code travels to the client as ``provider_code``, beside the
  router's stable ``code`` rather than replacing it;
- a retry hint is read from the header *or* from ``google.rpc.RetryInfo``,
  because Gemini often sends only the latter;
- ``retryable`` is decided in the adapter from what the provider said, and the
  dispatcher merely honours it -- a 500 is not retried, a 503 is;
- a 429 is called a daily cap only on evidence: an exhausted daily request
  window, or a hint of an hour or more.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from free_router.core.dispatch import Dispatcher, is_daily_cap
from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderDailyQuotaExhaustedError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderValidationError,
    ValidationError,
)
from free_router.core.normalization import NormalizedRequest
from free_router.keys.manager import KeyManager, KeyRef
from free_router.providers.google.adapter import API_VERSION, GOOGLE_API_BASE, GoogleAdapter
from free_router.providers.mistral.adapter import (
    MISTRAL_API_BASE,
    MISTRAL_CHAT_PATH,
    MistralAdapter,
)
from free_router.quota.breaker import ContextBreaker
from free_router.quota.cooldown import CooldownManager
from free_router.quota.dimensions import DimensionResource, Provenance, QuotaDimension
from free_router.quota.tracker import QuotaTracker

GOOGLE_MODEL = "gemini-3.5-flash-lite"
MISTRAL_MODEL = "ministral-3b-2512"

GENERATE_URL = f"{GOOGLE_API_BASE}/{API_VERSION}/models/{GOOGLE_MODEL}:generateContent"
STREAM_URL = f"{GOOGLE_API_BASE}/{API_VERSION}/models/{GOOGLE_MODEL}:streamGenerateContent?alt=sse"
MISTRAL_URL = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"

SSE_HEADERS = {"content-type": "text/event-stream"}

SUCCESS_BODY: dict[str, Any] = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3, "totalTokenCount": 7},
}


def _google_error(status: int, provider_status: str, **extra: Any) -> dict[str, Any]:
    return {
        "error": {
            "code": status,
            "message": "upstream said no",
            "status": provider_status,
            **extra,
        }
    }


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


@pytest.fixture
def google_key() -> KeyRef:
    return KeyRef(
        alias="primary",
        provider="google",
        model_id=GOOGLE_MODEL,
        secret=SecretStr("test-google-key-primary"),
    )


@pytest.fixture
def mistral_key() -> KeyRef:
    return KeyRef(
        alias="primary",
        provider="mistral",
        model_id=MISTRAL_MODEL,
        secret=SecretStr("test-mistral-key-primary"),
    )


def _request(provider: str, model: str) -> NormalizedRequest:
    return NormalizedRequest(
        model=f"{provider}/{model}", messages=[{"role": "user", "content": "hi"}]
    )


# --- Item 1: the provider's own code reaches the client -----------------------


def test_provider_code_is_omitted_when_absent() -> None:
    """The common case stays byte-identical to OpenAI's four-key envelope."""
    payload = ValidationError("bad request", code="invalid_request", param="messages")

    assert payload.to_payload() == {
        "error": {
            "message": "bad request",
            "type": "invalid_request_error",
            "param": "messages",
            "code": "invalid_request",
        }
    }


def test_provider_code_sits_beside_the_router_code() -> None:
    """Additive, not a replacement: a client switching on ``code`` still works."""
    error = ProviderValidationError(
        "The upstream provider rejected the request payload.",
        provider_code="INVALID_ARGUMENT",
    )

    envelope = error.to_payload()["error"]

    assert envelope["code"] == "provider_validation_error"
    assert envelope["provider_code"] == "INVALID_ARGUMENT"


@respx.mock
async def test_google_payload_rejection_carries_its_code(
    http_client: httpx.AsyncClient, google_key: KeyRef
) -> None:
    """A payload problem and a credential problem both arrive as 400."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json=_google_error(400, "INVALID_ARGUMENT"))
    )

    with pytest.raises(ProviderValidationError) as raised:
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request("google", GOOGLE_MODEL), GOOGLE_MODEL, google_key
        )

    assert raised.value.provider_code == "INVALID_ARGUMENT"
    assert raised.value.context_length is False


@respx.mock
async def test_mistral_credential_rejection_carries_its_code(
    http_client: httpx.AsyncClient, mistral_key: KeyRef
) -> None:
    body = {
        "object": "error",
        "message": "Unauthorized",
        "type": "authentication_error",
        "param": None,
        "code": "invalid_api_key",
    }
    respx.post(MISTRAL_URL).mock(return_value=httpx.Response(401, json=body))

    with pytest.raises(ProviderAuthError) as raised:
        await MistralAdapter(http_client=http_client).chat_completion(
            _request("mistral", MISTRAL_MODEL), MISTRAL_MODEL, mistral_key
        )

    assert raised.value.provider_code == "invalid_api_key"
    # The alias is what may be logged; the secret never is.
    assert "test-mistral-key-primary" not in str(raised.value)


# --- Items 3 and 4: retryability comes from the provider ----------------------


@pytest.mark.parametrize(
    ("status", "provider_status", "expected"),
    [
        (503, "UNAVAILABLE", True),
        (502, "UNAVAILABLE", True),
        (504, "DEADLINE_EXCEEDED", True),
        (500, "INTERNAL", False),
    ],
)
@respx.mock
async def test_google_5xx_retryability_follows_the_status_and_signal(
    http_client: httpx.AsyncClient,
    google_key: KeyRef,
    status: int,
    provider_status: str,
    expected: bool,
) -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(status, json=_google_error(status, provider_status))
    )

    with pytest.raises(ProviderServerError) as raised:
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request("google", GOOGLE_MODEL), GOOGLE_MODEL, google_key
        )

    assert raised.value.retryable is expected
    assert raised.value.provider_code == provider_status


@pytest.mark.parametrize(("status", "expected"), [(503, True), (502, True), (500, False)])
@respx.mock
async def test_mistral_5xx_retryability_follows_the_status(
    http_client: httpx.AsyncClient,
    mistral_key: KeyRef,
    status: int,
    expected: bool,
) -> None:
    """Mistral supplies no in-body retryable signal, so the status is the signal."""
    respx.post(MISTRAL_URL).mock(return_value=httpx.Response(status, json={"message": "oops"}))

    with pytest.raises(ProviderServerError) as raised:
        await MistralAdapter(http_client=http_client).chat_completion(
            _request("mistral", MISTRAL_MODEL), MISTRAL_MODEL, mistral_key
        )

    assert raised.value.retryable is expected


# --- Item 4: the provider's own retry hint is trusted -------------------------


@respx.mock
async def test_google_retry_hint_is_read_from_the_header(
    http_client: httpx.AsyncClient, google_key: KeyRef
) -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            429,
            json=_google_error(429, "RESOURCE_EXHAUSTED"),
            headers={"Retry-After": "42"},
        )
    )

    with pytest.raises(ProviderRateLimitError) as raised:
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request("google", GOOGLE_MODEL), GOOGLE_MODEL, google_key
        )

    assert raised.value.retry_after == 42.0
    assert raised.value.provider_code == "RESOURCE_EXHAUSTED"


@respx.mock
async def test_google_retry_hint_is_read_from_retry_info_when_the_header_is_absent(
    http_client: httpx.AsyncClient, google_key: KeyRef
) -> None:
    """Gemini routinely omits ``Retry-After`` and says it in the body instead."""
    body = _google_error(
        429,
        "RESOURCE_EXHAUSTED",
        details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
        ],
    )
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(429, json=body))

    with pytest.raises(ProviderRateLimitError) as raised:
        await GoogleAdapter(http_client=http_client).chat_completion(
            _request("google", GOOGLE_MODEL), GOOGLE_MODEL, google_key
        )

    assert raised.value.retry_after == 37.0


@respx.mock
async def test_mistral_retry_hint_is_read_from_the_header(
    http_client: httpx.AsyncClient, mistral_key: KeyRef
) -> None:
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            429, json={"message": "slow down"}, headers={"Retry-After": "5"}
        )
    )

    with pytest.raises(ProviderRateLimitError) as raised:
        await MistralAdapter(http_client=http_client).chat_completion(
            _request("mistral", MISTRAL_MODEL), MISTRAL_MODEL, mistral_key
        )

    assert raised.value.retry_after == 5.0


# --- Item 2: burst 429 versus daily-cap 429 -----------------------------------


class _Quota:
    """The one method :func:`is_daily_cap` asks for."""

    def __init__(self, *, daily_exhausted: bool) -> None:
        self._daily_exhausted = daily_exhausted

    def daily_requests_exhausted(self, provider: str, model_id: str) -> bool:
        return self._daily_exhausted


@pytest.mark.parametrize(
    ("daily_exhausted", "hint", "expected"),
    [
        (True, None, True),
        (True, 1.0, True),
        (False, None, False),
        (False, 60.0, False),
        (False, 3_600.0, True),
        (False, 86_400.0, True),
    ],
)
def test_daily_cap_is_decided_on_evidence(
    daily_exhausted: bool, hint: float | None, expected: bool
) -> None:
    """No evidence is not evidence of a daily cap -- it is a burst.

    A wrong "daily" tells a client to give up for the rest of a day it could
    still use, which is the worse error.
    """
    error = ProviderRateLimitError("limited", retry_after=hint)

    assert is_daily_cap(_Quota(daily_exhausted=daily_exhausted), "google", "m", error) is expected


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _daily_dimension(limit: float) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.REQUESTS,
        limit=limit,
        window_seconds=86_400.0,
        provenance=Provenance.PUBLISHED,
    )


async def test_tracker_reports_an_exhausted_daily_window() -> None:
    tracker = QuotaTracker({("google", GOOGLE_MODEL): [_daily_dimension(1)]}, clock=FakeClock())

    assert tracker.daily_requests_exhausted("google", GOOGLE_MODEL) is False

    reservation = await tracker.reserve("google", GOOGLE_MODEL, 1)
    assert reservation is not None
    await tracker.reconcile(reservation, 1)

    assert tracker.daily_requests_exhausted("google", GOOGLE_MODEL) is True


def test_tracker_does_not_call_an_unconfigured_limit_daily() -> None:
    """An unverified limit stays unenforced rather than being guessed at."""
    tracker = QuotaTracker({("google", GOOGLE_MODEL): []}, clock=FakeClock())

    assert tracker.daily_requests_exhausted("google", GOOGLE_MODEL) is False


# --- The dispatcher honours ``retryable`` -------------------------------------


def _dispatcher(http_client: httpx.AsyncClient, *, quota: list[QuotaDimension]) -> Dispatcher:
    clock = FakeClock()
    return Dispatcher(
        key_manager=KeyManager(
            {("google", GOOGLE_MODEL): {"primary": SecretStr("secret-primary")}}, clock=clock
        ),
        quota=QuotaTracker({("google", GOOGLE_MODEL): quota}, clock=clock),
        cooldown=CooldownManager(durations={("google", GOOGLE_MODEL): 60.0}, clock=clock),
        breaker=ContextBreaker(clock=clock),
        http_client=http_client,
        max_attempts=3,
        backoff_seconds=0.0,
    )


@respx.mock
async def test_a_500_is_not_retried(http_client: httpx.AsyncClient) -> None:
    """A provider bug fails identically the second time; the retry only costs quota."""
    route = respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(500, json=_google_error(500, "INTERNAL"))
    )

    with pytest.raises(ProviderServerError) as raised:
        await _dispatcher(http_client, quota=[]).dispatch(
            _request("google", GOOGLE_MODEL), "google", GOOGLE_MODEL
        )

    assert route.call_count == 1
    assert raised.value.provider_code == "INTERNAL"


@respx.mock
async def test_a_503_is_still_retried(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GENERATE_URL)
    route.side_effect = [
        httpx.Response(503, json=_google_error(503, "UNAVAILABLE")),
        httpx.Response(200, json=SUCCESS_BODY),
    ]

    outcome = await _dispatcher(http_client, quota=[]).dispatch(
        _request("google", GOOGLE_MODEL), "google", GOOGLE_MODEL
    )

    assert outcome.attempts == 2
    assert route.call_count == 2


@respx.mock
async def test_a_rate_limit_with_the_daily_budget_gone_is_reported_as_a_daily_cap(
    http_client: httpx.AsyncClient,
) -> None:
    """The reservation itself fills the day's window, so the 429 can only be the cap."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(429, json=_google_error(429, "RESOURCE_EXHAUSTED"))
    )
    dispatcher = _dispatcher(http_client, quota=[_daily_dimension(1)])

    with pytest.raises(ProviderDailyQuotaExhaustedError) as raised:
        await dispatcher.dispatch(_request("google", GOOGLE_MODEL), "google", GOOGLE_MODEL)

    assert raised.value.retry_after is None


@respx.mock
async def test_a_rate_limit_with_the_daily_budget_intact_is_a_burst(
    http_client: httpx.AsyncClient,
) -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(429, json=_google_error(429, "RESOURCE_EXHAUSTED"))
    )
    dispatcher = _dispatcher(http_client, quota=[_daily_dimension(100)])

    with pytest.raises(ProviderRateLimitError) as raised:
        await dispatcher.dispatch(_request("google", GOOGLE_MODEL), "google", GOOGLE_MODEL)

    assert not isinstance(raised.value, ProviderDailyQuotaExhaustedError)


# --- The same answers over HTTP, unary and streaming --------------------------


CONFIG = f"""\
models:
  - id: "google/{GOOGLE_MODEL}"
    provider: "google"
    provider_id: "{GOOGLE_MODEL}"
"""

CHAT_BODY = {
    "model": f"google/{GOOGLE_MODEL}",
    "messages": [{"role": "user", "content": "hello"}],
}


@pytest.fixture
def client(workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from free_router.api.main import create_app

    (workdir / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "test-google-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    with TestClient(create_app()) as started:
        yield started


@respx.mock
def test_unary_500_reaches_the_client_as_502_with_one_attempt(client: TestClient) -> None:
    route = respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(500, json=_google_error(500, "INTERNAL"))
    )

    response = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "provider_server_error"
    assert error["provider_code"] == "INTERNAL"
    assert route.call_count == 1


@respx.mock
def test_streaming_500_reaches_the_client_as_502_with_one_attempt(client: TestClient) -> None:
    """The streaming path honours the same ``retryable`` flag, from the same flag."""
    route = respx.post(STREAM_URL).mock(
        return_value=httpx.Response(500, json=_google_error(500, "INTERNAL"))
    )

    response = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_server_error"
    assert route.call_count == 1


@respx.mock
def test_unary_400_carries_the_provider_code_and_does_not_degrade(client: TestClient) -> None:
    route = respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json=_google_error(400, "INVALID_ARGUMENT"))
    )

    first = client.post("/v1/chat/completions", json=CHAT_BODY)
    second = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert first.status_code == 400
    assert first.json()["error"]["code"] == "provider_validation_error"
    assert first.json()["error"]["provider_code"] == "INVALID_ARGUMENT"
    # An ordinary 400 must not degrade the model: the next request still dispatches.
    assert second.status_code == 400
    assert route.call_count == 2


# A guard against the streaming loop silently losing the shared verdict.
def test_streaming_and_unary_rate_limit_verdicts_share_one_function() -> None:
    import free_router.api.stream_dispatch as stream_dispatch

    assert stream_dispatch.is_daily_cap is is_daily_cap
