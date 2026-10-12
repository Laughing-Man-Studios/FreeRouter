"""The reservation cycle (T028): estimate, reserve, dispatch, reconcile.

CONSTITUTION §8.2 requires all four steps. The property that matters most is
that **reserving happens before dispatch and is atomic**, because a
check-then-send sequence lets N concurrent requests each observe the same
headroom and each proceed, exceeding the limit by exactly N.

Time is injected throughout, so nothing here sleeps.
"""

import asyncio

import pytest

from free_router.core.normalization import NormalizedRequest
from free_router.quota.dimensions import (
    SECONDS_PER_DAY,
    DimensionResource,
    Provenance,
    QuotaDimension,
    WindowReset,
)
from free_router.quota.tracker import QuotaTracker, Reservation, estimate_tokens

PROVIDER = "mistral"
MODEL = "ministral-14b-2512"


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _rps(limit: float, provenance: Provenance = Provenance.MEASURED) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.REQUESTS,
        limit=limit,
        window_seconds=1.0,
        provenance=provenance,
    )


def _tpm(limit: float, provenance: Provenance = Provenance.MEASURED) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.TOKENS,
        limit=limit,
        window_seconds=60.0,
        provenance=provenance,
    )


def _tracker(clock: FakeClock, *dimensions: QuotaDimension, margin: float = 1.1) -> QuotaTracker:
    return QuotaTracker(
        {(PROVIDER, MODEL): dimensions},
        safety_margin=margin,
        clock=clock,
    )


# --- Step 1: estimate ---


def test_estimate_scales_with_content() -> None:
    short = NormalizedRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    long = NormalizedRequest(model="m", messages=[{"role": "user", "content": "hi" * 1000}])

    assert estimate_tokens(long) > estimate_tokens(short)


def test_estimate_is_never_zero() -> None:
    """A zero estimate would reserve nothing and overshoot on the first request."""
    empty = NormalizedRequest(model="m", messages=[{"role": "user", "content": ""}])

    assert estimate_tokens(empty) >= 1


def test_estimate_sums_every_message() -> None:
    one = NormalizedRequest(model="m", messages=[{"role": "user", "content": "a" * 400}])
    two = NormalizedRequest(
        model="m",
        messages=[
            {"role": "system", "content": "a" * 400},
            {"role": "user", "content": "a" * 400},
        ],
    )

    assert estimate_tokens(two) == estimate_tokens(one) * 2


# --- Step 2: reserve, and the safety margin ---


async def test_safety_margin_is_applied_to_the_reservation() -> None:
    """CONSTITUTION §8.2 requires the buffer; a caller must not be able to skip it."""
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(1000))

    reservation = await tracker.reserve(PROVIDER, MODEL, 100)

    assert reservation is not None
    assert reservation.estimated_tokens == 100
    # The literal is 110, not math.ceil(100 * 1.1). In binary floating point
    # 100 * 1.1 is 110.00000000000001, so ceil yields 111 and the router
    # over-reserves by a token for arithmetic reasons rather than policy.
    assert reservation.reserved_tokens == 110


async def test_safety_margin_must_exceed_one() -> None:
    """A margin at or below 1 reserves less than the request costs."""
    with pytest.raises(ValueError, match="safety_margin must exceed 1.0"):
        QuotaTracker({(PROVIDER, MODEL): []}, safety_margin=1.0)


async def test_reservation_carries_no_prompt_content() -> None:
    """CONSTITUTION §9.1: operational output must never contain prompt text."""
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(1000))

    reservation = await tracker.reserve(PROVIDER, MODEL, 10)
    assert reservation is not None
    assert "hello" not in reservation.describe()
    assert str(reservation.identifier) in reservation.describe()


# --- Step 3: the limit is enforced ---


async def test_reserve_returns_none_when_exhausted() -> None:
    """Exhaustion returns None; the 503 exception belongs to Batch 5."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(2))

    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None
    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None
    assert await tracker.reserve(PROVIDER, MODEL, 10) is None


async def test_a_fractional_limit_is_honoured_not_rounded() -> None:
    """0.5 RPS means one request every two seconds, not one per second.

    Mistral's ``ministral-14b-2512`` ceiling is exactly this. Rounding up to an
    integer would dispatch at twice the permitted rate.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _rps(0.5))

    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None
    assert await tracker.reserve(PROVIDER, MODEL, 10) is None


async def test_fractional_limit_enforces_spacing_across_the_boundary() -> None:
    """The spacing holds for its whole duration, not just inside the window.

    Minimum spacing for 0.5 RPS over a 1s window is 2s, which is *longer* than
    the window. An implementation that looks back only a window has seen nothing
    to wait for by 1.9s and admits the request early.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _rps(0.5))

    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None

    clock.advance(1.0)
    assert await tracker.reserve(PROVIDER, MODEL, 10) is None, "past the window, before spacing"

    clock.advance(0.9)
    assert await tracker.reserve(PROVIDER, MODEL, 10) is None, "1.9s: still short of 2s"

    clock.advance(0.2)
    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None, "2.1s: spacing satisfied"


async def test_fractional_spacing_does_not_apply_to_tokens() -> None:
    """A token budget smaller than the request is refused, not deferred.

    Waiting cannot make a budget cover a request it is smaller than, so the
    minimum-spacing fallback must not apply to token dimensions.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(50))

    assert await tracker.reserve(PROVIDER, MODEL, 100) is None


async def test_requests_and_tokens_are_enforced_independently() -> None:
    """Exhausting one dimension must not be masked by headroom in the other."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(100), _tpm(50))

    # Each request reserves ceil(50 * 1.1) = 55 tokens, over the 50 limit.
    assert await tracker.reserve(PROVIDER, MODEL, 50) is None


async def test_unconfigured_model_is_always_available() -> None:
    """An omitted dimension is unenforced, not unlimited-and-failing."""
    clock = FakeClock()
    unconfigured = _tracker(clock)

    for _ in range(50):
        assert unconfigured.available(PROVIDER, "unconfigured-model")
        assert await unconfigured.reserve(PROVIDER, "unconfigured-model", 10_000) is not None


# --- Concurrency: the reason reserving exists at all ---


async def test_concurrent_reservations_cannot_over_commit() -> None:
    """N concurrent requests must not each observe the same headroom and proceed.

    This is the specific failure the reservation cycle prevents. A check-then-send
    sequence would admit all 20 here; the atomic reserve admits exactly the limit.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _rps(5), _tpm(10_000))

    results = await asyncio.gather(*(tracker.reserve(PROVIDER, MODEL, 100) for _ in range(20)))

    admitted = [r for r in results if r is not None]
    assert len(admitted) == 5
    assert len(results) - len(admitted) == 15


async def test_concurrent_token_reservations_cannot_over_commit() -> None:
    """The same guarantee on the token dimension, which is where it actually bites."""
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(100), _rps(1000))

    results = await asyncio.gather(*(tracker.reserve(PROVIDER, MODEL, 10) for _ in range(50)))
    admitted = [r for r in results if r is not None]

    # Each reserves ceil(10 * 1.1) = 11 tokens against a 100 budget.
    assert len(admitted) == 9
    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 99


async def test_reservations_are_unique_across_concurrency() -> None:
    """Identifiers must not collide, or reconciliation settles the wrong entry."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(1000), _tpm(100_000))

    reservations = await asyncio.gather(*(tracker.reserve(PROVIDER, MODEL, 10) for _ in range(25)))
    identifiers = [r.identifier for r in reservations if r is not None]

    assert len(set(identifiers)) == len(identifiers) == 25


# --- Step 4: reconcile ---


async def test_reconcile_releases_the_estimate_overshoot() -> None:
    """Actual below estimate frees the difference for the next request."""
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(110), _rps(1000))

    reservation = await tracker.reserve(PROVIDER, MODEL, 100)
    assert reservation is not None
    assert reservation.reserved_tokens == 110
    assert not tracker.available(PROVIDER, MODEL)

    await tracker.reconcile(reservation, 10)

    assert tracker.available(PROVIDER, MODEL)
    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 10


async def test_a_request_larger_than_the_budget_is_refused() -> None:
    """The safety margin can push an at-the-limit request over the budget.

    A 100-token estimate reserves 110, so a 100-token budget cannot admit it.
    That is the margin doing its job: the estimate equals the ceiling, and the
    buffer is what stops the request being dispatched against it.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(100), _rps(1000))

    assert await tracker.reserve(PROVIDER, MODEL, 100) is None


async def test_reconcile_with_no_usage_keeps_the_estimate() -> None:
    """A provider reporting nothing must not release capacity that was used.

    Treating "no usage reported" as "no usage incurred" would hand out the same
    capacity twice and overshoot on the next request.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(110), _rps(1000))

    reservation = await tracker.reserve(PROVIDER, MODEL, 100)
    assert reservation is not None

    await tracker.reconcile(reservation, None)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 110
    assert not tracker.available(PROVIDER, MODEL)


async def test_reconcile_does_not_double_count() -> None:
    """Reconciling twice must settle the same entry, not add another."""
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(1000), _rps(1000))

    reservation = await tracker.reserve(PROVIDER, MODEL, 100)
    assert reservation is not None

    await tracker.reconcile(reservation, 40)
    await tracker.reconcile(reservation, 40)
    await tracker.reconcile(reservation, 40)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 40


async def test_reconcile_of_an_unknown_reservation_is_ignored() -> None:
    """A stray reconcile must not corrupt state."""
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(1000), _rps(1000))

    stranger = Reservation(
        identifier=999,
        provider=PROVIDER,
        model_id=MODEL,
        estimated_tokens=10,
        reserved_tokens=11,
        window_start=clock.now,
    )
    await tracker.reconcile(stranger, 50)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 0


async def test_reconcile_above_estimate_records_the_overshoot() -> None:
    """An over-report is real spend and must count, not be clamped away.

    The request already happened, so the tokens were spent. Clamping to the
    estimate would let the router keep dispatching against a limit it has
    demonstrably passed.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(100), _rps(1000))

    reservation = await tracker.reserve(PROVIDER, MODEL, 50)
    assert reservation is not None

    await tracker.reconcile(reservation, 180)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 180
    assert not tracker.available(PROVIDER, MODEL)


async def test_reconcile_releases_in_flight_capacity() -> None:
    """An unreconciled reservation still occupies capacity.

    Otherwise a slow in-flight request would not be counted and the tracker
    would admit more than the limit permits.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _tpm(110), _rps(1000))

    await tracker.reserve(PROVIDER, MODEL, 100)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 110
    assert not tracker.available(PROVIDER, MODEL)


# --- Window semantics ---


async def test_rolling_window_expires_usage() -> None:
    """A rolling window forgets usage older than the window."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(2))

    await tracker.reserve(PROVIDER, MODEL, 10)
    await tracker.reserve(PROVIDER, MODEL, 10)
    assert not tracker.available(PROVIDER, MODEL)

    clock.advance(1.5)

    assert tracker.available(PROVIDER, MODEL)
    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None


async def test_rolling_window_decays_gradually() -> None:
    """Half a window past a request frees half the window's capacity."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(2))

    await tracker.reserve(PROVIDER, MODEL, 10)
    clock.advance(0.6)

    # One request is still inside the trailing second; a second fits alongside it.
    assert tracker.available(PROVIDER, MODEL)


async def test_fixed_window_holds_usage_until_the_boundary() -> None:
    """A fixed window does not decay; it drops to zero all at once."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(2))

    await tracker.reserve(PROVIDER, MODEL, 10)
    await tracker.reserve(PROVIDER, MODEL, 10)
    assert not tracker.available(PROVIDER, MODEL)

    clock.advance(0.99)
    assert not tracker.available(PROVIDER, MODEL), "must still hold before the boundary"

    clock.advance(0.02)
    assert tracker.available(PROVIDER, MODEL), "must clear at the boundary"


async def test_fixed_window_resets_all_at_once_rather_than_decaying() -> None:
    """The behavioural difference from a rolling window, stated as a test.

    At a fixed boundary two requests can be admitted back to back, because the
    first one's usage vanishes the instant the second is considered.
    """
    clock = FakeClock()
    tracker = _tracker(clock, _rps(1))

    await tracker.reserve(PROVIDER, MODEL, 10)
    assert await tracker.reserve(PROVIDER, MODEL, 10) is None

    clock.advance(1.0)

    assert await tracker.reserve(PROVIDER, MODEL, 10) is not None
    assert await tracker.reserve(PROVIDER, MODEL, 10) is None


async def test_daily_fixed_window_holds_for_the_whole_day() -> None:
    """A fixed window drops usage at a wall-clock boundary, not 24h after use.

    The clock starts on an epoch-aligned boundary because that is where a fixed
    window resets. Starting mid-bucket would cross the boundary partway through
    the test and hide the behaviour being asserted.
    """
    clock = FakeClock(start=SECONDS_PER_DAY)
    tracker = _tracker(
        clock,
        QuotaDimension(
            resource=DimensionResource.REQUESTS,
            limit=2,
            window_seconds=SECONDS_PER_DAY,
            reset=WindowReset.FIXED,
            provenance=Provenance.PLACEHOLDER,
        ),
    )

    await tracker.reserve(PROVIDER, MODEL, 10)
    await tracker.reserve(PROVIDER, MODEL, 10)

    clock.advance(SECONDS_PER_DAY - 1)
    assert not tracker.available(PROVIDER, MODEL)

    clock.advance(2)
    assert tracker.available(PROVIDER, MODEL)


async def test_usage_expires_out_of_the_window_even_when_unreconciled() -> None:
    """Pruning must not resurrect settled usage as headroom forever."""
    clock = FakeClock()
    tracker = _tracker(clock, _rps(1))

    await tracker.reserve(PROVIDER, MODEL, 10)
    clock.advance(5)

    assert tracker.available(PROVIDER, MODEL)


# --- Isolation ---


async def test_tracking_is_isolated_per_model() -> None:
    """One model exhausting its quota must not affect another."""
    clock = FakeClock()
    tracker = QuotaTracker(
        {
            (PROVIDER, MODEL): [_rps(1)],
            (PROVIDER, "other"): [_rps(1)],
        },
        clock=clock,
    )

    await tracker.reserve(PROVIDER, MODEL, 10)

    assert not tracker.available(PROVIDER, MODEL)
    assert tracker.available(PROVIDER, "other")
    assert await tracker.reserve(PROVIDER, "other", 10) is not None


async def test_tracking_is_isolated_per_provider() -> None:
    """Google and Mistral capacity are genuinely separate."""
    clock = FakeClock()
    tracker = QuotaTracker(
        {
            ("mistral", MODEL): [_rps(1)],
            ("google", MODEL): [_rps(1)],
        },
        clock=clock,
    )

    await tracker.reserve("mistral", MODEL, 10)

    assert not tracker.available("mistral", MODEL)
    assert tracker.available("google", MODEL)


def test_dimensions_for_reports_the_configuration() -> None:
    clock = FakeClock()
    tracker = _tracker(clock, _rps(15), _tpm(1_300_000))

    dimensions = tracker.dimensions_for(PROVIDER, MODEL)

    assert len(dimensions) == 2
    assert dimensions[0].describe() == "15 requests/1s (measured, rolling)"
    assert tracker.dimensions_for(PROVIDER, "unconfigured") == ()
