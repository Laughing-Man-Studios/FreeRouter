"""The streaming capacity gate must agree with the dispatcher's.

A2 keeps :mod:`free_router.core.dispatch` untouched, so the streaming path
repeats its capacity gates instead of sharing them. Repetition rots silently --
the dispatcher could gain a condition and streaming would keep dispatching into
it -- so this module drives both implementations through the same state and
requires the same error type from each.

It is deliberately a *parity* test rather than a behavioural one: the individual
conditions are covered where they belong (``test_failover.py``). What is new here
is the claim that the two copies agree.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from free_router.api.stream_dispatch import _check_capacity as stream_check_capacity
from free_router.core.dispatch import Dispatcher
from free_router.core.exceptions import (
    NoAvailableCapacityError,
    PersistenceUnavailableError,
    ProviderRateLimitError,
)
from free_router.keys.manager import KeyManager
from free_router.quota.breaker import ContextBreaker
from free_router.quota.cooldown import CooldownManager
from free_router.quota.dimensions import DimensionResource, Provenance, QuotaDimension
from free_router.quota.tracker import QuotaTracker


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0))


PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"
ESTIMATE = 100


def _rps(limit: float) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.REQUESTS,
        limit=limit,
        window_seconds=60.0,
        provenance=Provenance.MEASURED,
    )


def _dispatcher(
    http_client: httpx.AsyncClient, *, quota: list[QuotaDimension] | None = None
) -> Dispatcher:
    return Dispatcher(
        key_manager=KeyManager({(PROVIDER, MODEL): {"primary": SecretStr("secret")}}),
        quota=QuotaTracker({(PROVIDER, MODEL): quota or []}),
        cooldown=CooldownManager(durations={(PROVIDER, MODEL): 60.0}),
        breaker=ContextBreaker(),
        http_client=http_client,
    )


def _both(dispatcher: Dispatcher) -> tuple[type[Exception] | None, type[Exception] | None]:
    """Run both gates and report what each raised, if anything."""

    def run(gate: object) -> type[Exception] | None:
        try:
            gate(PROVIDER, MODEL, ESTIMATE)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - the type is the assertion
            return type(exc)
        return None

    return run(dispatcher._check_capacity), run(
        lambda provider, model, estimate: stream_check_capacity(
            dispatcher, provider, model, estimate
        )
    )


async def test_both_gates_allow_a_healthy_model(http_client: httpx.AsyncClient) -> None:
    dispatcher = _dispatcher(http_client, quota=[_rps(10)])

    assert _both(dispatcher) == (None, None)


async def test_both_gates_refuse_a_cooling_model_with_429(
    http_client: httpx.AsyncClient,
) -> None:
    dispatcher = _dispatcher(http_client)
    await dispatcher.cooldown.enter(PROVIDER, MODEL, reason="rate_limited")

    assert _both(dispatcher) == (ProviderRateLimitError, ProviderRateLimitError)


async def test_both_gates_refuse_a_degraded_model(http_client: httpx.AsyncClient) -> None:
    dispatcher = _dispatcher(http_client)
    await dispatcher.breaker.record_failure(PROVIDER, MODEL, ESTIMATE)

    assert _both(dispatcher) == (NoAvailableCapacityError, NoAvailableCapacityError)


async def test_both_gates_refuse_an_exhausted_quota(http_client: httpx.AsyncClient) -> None:
    dispatcher = _dispatcher(http_client, quota=[_rps(1)])
    reservation = await dispatcher.quota.reserve(PROVIDER, MODEL, ESTIMATE)
    assert reservation is not None
    await dispatcher.quota.reconcile(reservation, None)

    assert _both(dispatcher) == (NoAvailableCapacityError, NoAvailableCapacityError)


async def test_both_gates_refuse_a_degraded_persistence_record(
    http_client: httpx.AsyncClient,
) -> None:
    """The condition that makes a restart forget a rate limit must be mirrored too.

    Exercised directly because a degraded cooldown manager needs a store whose
    writes fail, which is heavier than the point requires here.
    """
    dispatcher = _dispatcher(http_client)
    dispatcher.cooldown._degraded[(PROVIDER, MODEL)] = "cooldown_write_failed"

    with pytest.raises(PersistenceUnavailableError):
        stream_check_capacity(dispatcher, PROVIDER, MODEL, ESTIMATE)
    with pytest.raises(PersistenceUnavailableError):
        dispatcher._check_capacity(PROVIDER, MODEL, ESTIMATE)
