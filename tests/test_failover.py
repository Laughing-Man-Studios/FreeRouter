"""Dispatch orchestration (T031–T034): retry, failover, and capacity gating.

The guarantees under test are the ones that protect free-tier quota and
CONSTITUTION §6.1, and several are about what must *not* happen:

- A rate limit stops the request. No retry, no failover to a sibling key.
- A 4xx and an auth failure are never blindly retried.
- Retry budget exhaustion terminates rather than looping.
- An explicit model request never becomes a different model.
- Nothing is dispatched when there is no capacity — the refusal costs no quota.
"""

import asyncio

import httpx
import pytest
import respx
from pydantic import SecretStr

from free_router.core.config import DispatchConfig
from free_router.core.dispatch import Dispatcher
from free_router.core.exceptions import (
    NoAvailableCapacityError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderValidationError,
    RetriesExhaustedError,
)
from free_router.core.normalization import NormalizedRequest
from free_router.keys.manager import KeyManager
from free_router.providers.google.adapter import GOOGLE_API_BASE
from free_router.quota.breaker import ContextBreaker
from free_router.quota.cooldown import CooldownManager
from free_router.quota.dimensions import (
    DimensionResource,
    Provenance,
    QuotaDimension,
)
from free_router.quota.tracker import QuotaTracker

PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"
EXTERNAL = f"{PROVIDER}/{MODEL}"
GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/{MODEL}:generateContent"

SUCCESS_BODY = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3, "totalTokenCount": 7},
}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _request(content: str = "hello") -> NormalizedRequest:
    return NormalizedRequest(model=EXTERNAL, messages=[{"role": "user", "content": content}])


def _rps(limit: float) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.REQUESTS,
        limit=limit,
        window_seconds=60.0,
        provenance=Provenance.MEASURED,
    )


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


def _dispatcher(
    http_client: httpx.AsyncClient,
    *,
    aliases: tuple[str, ...] = ("primary",),
    quota: list[QuotaDimension] | None = None,
    max_attempts: int = 2,
    backoff: float = 0.0,
    budget: float = 10.0,
    clock: FakeClock | None = None,
) -> Dispatcher:
    key_manager = KeyManager(
        {(PROVIDER, MODEL): {a: SecretStr(f"secret-{a}") for a in aliases}},
        clock=clock or FakeClock(),
    )
    return Dispatcher(
        key_manager=key_manager,
        quota=QuotaTracker({(PROVIDER, MODEL): quota or []}, clock=clock or FakeClock()),
        cooldown=CooldownManager(durations={(PROVIDER, MODEL): 60.0}, clock=clock or FakeClock()),
        breaker=ContextBreaker(clock=clock or FakeClock()),
        http_client=http_client,
        max_attempts=max_attempts,
        attempt_budget_seconds=budget,
        backoff_seconds=backoff,
    )


# --- Happy path ---


@respx.mock
async def test_a_single_attempt_succeeds(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))

    outcome = await _dispatcher(http_client).dispatch(_request(), PROVIDER, MODEL)

    assert outcome.response.content == "hello"
    assert outcome.attempts == 1
    assert outcome.failovers == 0
    assert outcome.key_aliases == ("primary",)
    assert route.call_count == 1


@respx.mock
async def test_quota_is_reconciled_after_success(http_client: httpx.AsyncClient) -> None:
    """Reconciliation must settle the reservation, not leave it pending."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))
    dispatcher = _dispatcher(http_client, quota=[_rps(10)])

    await dispatcher.dispatch(_request(), PROVIDER, MODEL)

    # The request is counted, so a 10-per-minute budget shows 1 used.
    assert dispatcher._quota.used(PROVIDER, MODEL, DimensionResource.REQUESTS) == 1


# --- T032: bounded retry on transient faults ---


@respx.mock
async def test_a_transient_5xx_is_retried_once(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GENERATE_URL)
    route.side_effect = [httpx.Response(503, json={}), httpx.Response(200, json=SUCCESS_BODY)]

    outcome = await _dispatcher(http_client).dispatch(_request(), PROVIDER, MODEL)

    assert outcome.response.content == "hello"
    assert outcome.attempts == 2
    assert route.call_count == 2


@respx.mock
async def test_a_timeout_is_retried(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GENERATE_URL)
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=SUCCESS_BODY)]

    outcome = await _dispatcher(http_client).dispatch(_request(), PROVIDER, MODEL)

    assert outcome.attempts == 2


@respx.mock
async def test_retry_budget_exhaustion_terminates_rather_than_looping(
    http_client: httpx.AsyncClient,
) -> None:
    """The attempt budget is a hard stop. This is the anti-infinite-loop gate."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(503, json={}))

    dispatcher = _dispatcher(http_client, max_attempts=3)

    with pytest.raises(ProviderServerError):
        await dispatcher.dispatch(_request(), PROVIDER, MODEL)

    assert route.call_count == 3, "must not exceed max_attempts"


@respx.mock
async def test_attempts_are_bounded_by_configuration(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(500, json={}))

    with pytest.raises(ProviderServerError):
        await _dispatcher(http_client, max_attempts=1).dispatch(_request(), PROVIDER, MODEL)

    assert route.call_count == 1, "max_attempts=1 permits no retry"


@respx.mock
async def test_a_sustained_5xx_surfaces_as_502_not_503(http_client: httpx.AsyncClient) -> None:
    """Spec "Error Behaviour": a 5xx that survived its retries is still a 5xx.

    `retries_exhausted` (503) is reserved for never having dispatched at all.
    Reporting an upstream fault as a capacity problem would send an operator
    looking at quota instead of at the provider.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(500, json={}))

    with pytest.raises(ProviderServerError):
        await _dispatcher(http_client).dispatch(_request(), PROVIDER, MODEL)


# --- T034: what must never be retried ---


@respx.mock
async def test_a_400_is_never_retried(http_client: httpx.AsyncClient) -> None:
    """The provider will not accept this payload; a second attempt fails identically."""
    route = respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )

    with pytest.raises(ProviderValidationError):
        await _dispatcher(http_client).dispatch(_request(), PROVIDER, MODEL)

    assert route.call_count == 1


@respx.mock
async def test_a_422_is_never_retried(http_client: httpx.AsyncClient) -> None:
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(422, json={"detail": []}))

    with pytest.raises(ProviderValidationError):
        await _dispatcher(http_client).dispatch(_request(), PROVIDER, MODEL)

    assert route.call_count == 1


@respx.mock
async def test_retrying_into_a_rate_limit_is_structurally_impossible(
    http_client: httpx.AsyncClient,
) -> None:
    """A 429 stops the request. No retry, no failover to the sibling key.

    This is the gate that matters most. Issue #51 established that Google's
    limits are per project, so a second key draws on the same exhausted bucket:
    dispatching to it would spend another request to learn nothing. The refusal
    is structural -- the 429 marks the whole capacity unit cooling and the next
    iteration's gate refuses -- rather than a guard someone can forget.
    """
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(429, json={}))

    with pytest.raises(ProviderRateLimitError):
        await _dispatcher(http_client, aliases=("primary", "secondary")).dispatch(
            _request(), PROVIDER, MODEL
        )

    assert route.call_count == 1, "must not dispatch a second request into a rate limit"


@respx.mock
async def test_a_429_surfaces_as_429_with_the_providers_own_hint(
    http_client: httpx.AsyncClient,
) -> None:
    """The retry hint the provider sent must reach the cooldown, not be discarded."""
    body = {
        "error": {
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}]
        }
    }
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(429, json=body))
    dispatcher = _dispatcher(http_client)

    with pytest.raises(ProviderRateLimitError):
        await dispatcher.dispatch(_request(), PROVIDER, MODEL)

    assert dispatcher._cooldown.is_cooling(PROVIDER, MODEL)


@respx.mock
async def test_the_next_request_is_refused_while_cooling(http_client: httpx.AsyncClient) -> None:
    """Cooldown must persist beyond the request that triggered it."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(429, json={}))
    dispatcher = _dispatcher(http_client)
    with pytest.raises(ProviderRateLimitError):
        await dispatcher.dispatch(_request(), PROVIDER, MODEL)

    with pytest.raises(ProviderRateLimitError):
        await dispatcher.dispatch(_request(), PROVIDER, MODEL)

    assert route.call_count == 1, "the second request must never reach the provider"


# --- T033: key failover, same model only ---


@respx.mock
async def test_an_auth_failure_fails_over_to_another_key(http_client: httpx.AsyncClient) -> None:
    """A rejected credential is the one genuinely per-key failure, so it fails over."""
    route = respx.post(GENERATE_URL)
    route.side_effect = [httpx.Response(401, json={}), httpx.Response(200, json=SUCCESS_BODY)]

    outcome = await _dispatcher(http_client, aliases=("primary", "secondary")).dispatch(
        _request(), PROVIDER, MODEL
    )

    assert outcome.attempts == 2
    assert outcome.failovers == 1
    assert outcome.key_aliases == ("primary", "secondary")


@respx.mock
async def test_a_rejected_credential_is_suspended_for_later_requests(
    http_client: httpx.AsyncClient,
) -> None:
    """Otherwise every future request walks into the same rejected credential."""
    route = respx.post(GENERATE_URL)
    route.side_effect = [
        httpx.Response(401, json={}),
        httpx.Response(200, json=SUCCESS_BODY),
        httpx.Response(200, json=SUCCESS_BODY),
    ]
    dispatcher = _dispatcher(http_client, aliases=("primary", "secondary"))

    await dispatcher.dispatch(_request(), PROVIDER, MODEL)
    assert dispatcher._keys.is_suspended(PROVIDER, MODEL, "primary")

    await dispatcher.dispatch(_request(), PROVIDER, MODEL)
    # Second request went straight to the healthy credential.
    assert route.call_count == 3


@respx.mock
async def test_failover_never_changes_model(http_client: httpx.AsyncClient) -> None:
    """CONSTITUTION 7.2 forbids substituting a different model.

    A request that fails must not become a request for another model, however
    attractive the alternative looks. The dispatcher's signature takes exactly one
    (provider, model) pair and never varies it across attempts.
    """
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(401, json={}))

    with pytest.raises((ProviderAuthError, RetriesExhaustedError, NoAvailableCapacityError)):
        await _dispatcher(http_client, aliases=("primary", "secondary")).dispatch(
            _request(), PROVIDER, MODEL
        )

    for call in route.calls:
        body = call.request.read().decode()
        # The provider-native model id lives in the URL path, not the body.
        assert f"models/{MODEL}:generateContent" in str(call.request.url)
        assert "mistral" not in str(call.request.url).lower()
        assert "mistral" not in body.lower()


# --- Capacity gating: nothing dispatched, nothing spent ---


async def test_no_eligible_key_dispatches_nothing(http_client: httpx.AsyncClient) -> None:
    """A refusal on capacity grounds must never reach the provider."""
    with respx.mock:
        route = respx.post(GENERATE_URL)
        dispatcher = _dispatcher(http_client, aliases=("primary",))
        await dispatcher._keys.suspend(PROVIDER, MODEL, "primary")

        with pytest.raises(NoAvailableCapacityError):
            await dispatcher.dispatch(_request(), PROVIDER, MODEL)

        assert not route.called


async def test_exhausted_quota_dispatches_nothing(http_client: httpx.AsyncClient) -> None:
    with respx.mock:
        # The first request must SUCCEED and consume the budget. A bare
        # respx.post() returns an unparseable body, which would fail the first
        # dispatch, retry, and exhaust the budget before this test's subject.
        route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))
        dispatcher = _dispatcher(http_client, quota=[_rps(1)])
        await dispatcher.dispatch(_request(), PROVIDER, MODEL)
        route.reset()

        with pytest.raises(NoAvailableCapacityError):
            await dispatcher.dispatch(_request(), PROVIDER, MODEL)

        assert not route.called


async def test_a_refused_request_costs_no_quota(http_client: httpx.AsyncClient) -> None:
    """Refusing early is the whole point: it must not spend the daily budget."""
    dispatcher = _dispatcher(http_client, quota=[_rps(10)])
    await dispatcher._keys.suspend(PROVIDER, MODEL, "primary")

    with pytest.raises(NoAvailableCapacityError):
        await dispatcher.dispatch(_request(), PROVIDER, MODEL)

    assert dispatcher._quota.used(PROVIDER, MODEL, DimensionResource.REQUESTS) == 0


# --- Circuit breaker wiring ---


@respx.mock
async def test_a_context_length_rejection_degrades_the_model(
    http_client: httpx.AsyncClient,
) -> None:
    """CONSTITUTION 8.2: degrade for large requests rather than rediscovering the limit."""
    body = {
        "error": {
            "message": (
                "The input token count (999999) exceeds the maximum number of "
                "tokens allowed (32768)."
            ),
            "status": "INVALID_ARGUMENT",
        }
    }
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(400, json=body))
    dispatcher = _dispatcher(http_client)

    with pytest.raises(ProviderValidationError):
        await dispatcher.dispatch(_request("x" * 4000), PROVIDER, MODEL)

    assert route.call_count == 1, "a 400 must not be retried"
    assert dispatcher._breaker.is_degraded(PROVIDER, MODEL)


@respx.mock
async def test_a_degraded_model_refuses_large_requests_without_dispatching(
    http_client: httpx.AsyncClient,
) -> None:
    """The breaker earns its keep only if it stops the request before the call."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))
    dispatcher = _dispatcher(http_client)

    await dispatcher._breaker.record_failure(PROVIDER, MODEL, 1000)
    route.reset()

    with pytest.raises(NoAvailableCapacityError):
        await dispatcher.dispatch(_request("x" * 4000), PROVIDER, MODEL)

    assert not route.called, "a known-too-large request must not reach the provider"


@respx.mock
async def test_a_degraded_model_still_serves_small_requests(http_client: httpx.AsyncClient) -> None:
    """Degradation is size-conditional; blocking small requests would cost quota."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))
    dispatcher = _dispatcher(http_client)
    await dispatcher._breaker.record_failure(PROVIDER, MODEL, 1000)

    outcome = await dispatcher.dispatch(_request("hi"), PROVIDER, MODEL)

    assert outcome.response.content == "hello"
    assert route.called


@respx.mock
async def test_an_ordinary_400_does_not_degrade_the_model(http_client: httpx.AsyncClient) -> None:
    """A malformed payload says nothing about the model's context capacity."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad field"}})
    )
    dispatcher = _dispatcher(http_client)

    with pytest.raises(ProviderValidationError):
        await dispatcher.dispatch(_request("x" * 4000), PROVIDER, MODEL)

    assert not dispatcher._breaker.is_degraded(PROVIDER, MODEL)


# --- Concurrency ---


@respx.mock
async def test_concurrent_dispatches_respect_a_tight_quota(http_client: httpx.AsyncClient) -> None:
    """Many simultaneous requests must not collectively exceed the configured ceiling."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=SUCCESS_BODY))
    dispatcher = _dispatcher(http_client, quota=[_rps(3)])

    results = await asyncio.gather(
        *(dispatcher.dispatch(_request(), PROVIDER, MODEL) for _ in range(20)),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, BaseException)]

    assert len(succeeded) == 3
    assert dispatcher._quota.used(PROVIDER, MODEL, DimensionResource.REQUESTS) == 3


# --- Configuration ---


def test_retry_defaults_are_conservative() -> None:
    """Every retry spends real quota, so the default must be one retry."""
    config = DispatchConfig()

    assert config.max_attempts == 2
    assert config.attempt_budget_seconds > 0
    assert config.backoff_seconds >= 0


@pytest.mark.parametrize("attempts", [0, -1, 11])
async def test_invalid_attempt_counts_are_refused(attempts: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DispatchConfig(max_attempts=attempts)


def test_a_zero_budget_is_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DispatchConfig(attempt_budget_seconds=0)
