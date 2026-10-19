"""Context-overload circuit breaker (T030).

CONSTITUTION §8.2 requires a hard context-length refusal to degrade the model
*for large-context requests* rather than being retried. The interesting property
is the size-conditionality: a model that rejects a 900k-token prompt can still
serve a 200-token one, and the point is to protect the remaining quota rather
than to disable the model.

Time is injected; nothing here sleeps.
"""

import pytest

from free_router.quota.breaker import (
    DEFAULT_BREAKER_SECONDS,
    ContextBreaker,
    is_context_length_error,
)

PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: FakeClock, **kwargs: float) -> ContextBreaker:
    return ContextBreaker(clock=clock, **kwargs)


# --- Size-conditional degradation, not total ---


def test_a_fresh_breaker_blocks_nothing() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)

    assert not breaker.is_degraded(PROVIDER, MODEL)
    assert not breaker.blocks(PROVIDER, MODEL, 1_000_000)
    assert breaker.threshold(PROVIDER, MODEL) is None


async def test_large_requests_are_blocked_after_a_failure() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    assert breaker.blocks(PROVIDER, MODEL, 900_000)
    assert breaker.blocks(PROVIDER, MODEL, 1_500_000)


async def test_small_requests_are_still_served() -> None:
    """The model only failed at large sizes; blocking small requests would cost
    more quota than the breaker protects."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    assert not breaker.blocks(PROVIDER, MODEL, 200)
    assert not breaker.blocks(PROVIDER, MODEL, 899_999)


async def test_the_threshold_is_inclusive() -> None:
    """A request the same size as the failing one is the same size, not a smaller one."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 4_000)

    assert breaker.blocks(PROVIDER, MODEL, 4_000)
    assert not breaker.blocks(PROVIDER, MODEL, 3_999)


async def test_is_degraded_is_true_even_when_a_small_request_passes() -> None:
    """Degraded and blocked are different questions, and both are needed."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    assert breaker.is_degraded(PROVIDER, MODEL)
    assert not breaker.blocks(PROVIDER, MODEL, 100)


# --- The threshold only tightens ---


async def test_a_smaller_failure_lowers_the_threshold() -> None:
    """The bound must reflect the smallest request actually shown to fail."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    await breaker.record_failure(PROVIDER, MODEL, 4_000)

    assert breaker.threshold(PROVIDER, MODEL) == 4_000
    assert breaker.blocks(PROVIDER, MODEL, 4_000)


async def test_a_larger_failure_does_not_raise_the_threshold() -> None:
    """Re-raising would un-block requests already known to fail."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 4_000)

    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    assert breaker.threshold(PROVIDER, MODEL) == 4_000


async def test_repeated_failures_stay_at_the_smallest_size() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    for size in (900_000, 800_000, 4_000, 500_000):
        await breaker.record_failure(PROVIDER, MODEL, size)

    assert breaker.threshold(PROVIDER, MODEL) == 4_000


# --- Temporary, and expiring on its own ---


async def test_the_breaker_expires_without_operator_action() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    clock.advance(DEFAULT_BREAKER_SECONDS - 1)
    assert breaker.blocks(PROVIDER, MODEL, 1_000_000)

    clock.advance(2)
    assert not breaker.is_degraded(PROVIDER, MODEL)
    assert not breaker.blocks(PROVIDER, MODEL, 1_000_000)


async def test_a_refusal_extends_the_window() -> None:
    """A model still refusing must stay degraded, not get a free pass halfway."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    clock.advance(DEFAULT_BREAKER_SECONDS - 10)
    await breaker.record_failure(PROVIDER, MODEL, 900_000)

    assert breaker.remaining(PROVIDER, MODEL) == pytest.approx(DEFAULT_BREAKER_SECONDS)


def test_the_default_is_short_because_google_refusals_are_intermittent() -> None:
    """The same request has been observed succeeding seconds after a refusal.

    A long breaker would convert a transient backend limit into an outage.
    """
    assert DEFAULT_BREAKER_SECONDS <= 600.0


async def test_duration_is_configurable_per_model() -> None:
    clock = FakeClock()
    breaker = ContextBreaker(
        durations={(PROVIDER, MODEL): 60.0}, default_duration=300.0, clock=clock
    )

    assert breaker.configured_duration(PROVIDER, MODEL) == 60.0
    assert breaker.configured_duration("google", "other") == 300.0


async def test_clear_returns_the_model_to_full_availability() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 4_000)

    breaker.clear(PROVIDER, MODEL)

    assert not breaker.blocks(PROVIDER, MODEL, 1_000_000)


async def test_breakers_are_isolated_per_model_and_provider() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 4_000)

    assert not breaker.blocks("google", "gemini-3.5-pro", 1_000_000)
    assert not breaker.blocks("mistral", MODEL, 1_000_000)


async def test_remaining_floors_at_zero() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await breaker.record_failure(PROVIDER, MODEL, 4_000)

    assert breaker.remaining(PROVIDER, MODEL) == pytest.approx(DEFAULT_BREAKER_SECONDS)
    clock.advance(10_000)
    assert breaker.remaining(PROVIDER, MODEL) == 0.0


# --- Tokenizer scale: the provider's numbers are diagnostics only ---


async def test_the_provider_reported_maximum_is_recorded_but_not_compared() -> None:
    """Google's counts come from a different tokenizer than our estimate.

    Comparing our pre-dispatch estimate against the provider's real token count
    would compare two scales. The figure is kept for diagnostics and explicitly
    never used in a threshold comparison.
    """
    clock = FakeClock()
    breaker = _breaker(clock)

    record = await breaker.record_failure(
        PROVIDER, MODEL, estimated_tokens=900_000, provider_reported_max=1_048_576
    )

    assert record.threshold_tokens == 900_000
    assert record.provider_reported_max == 1_048_576
    assert "provider_max=1048576" in record.describe()


async def test_the_record_description_carries_no_provider_message() -> None:
    """CONSTITUTION §9.1: provider bodies can echo prompt content."""
    clock = FakeClock()
    breaker = _breaker(clock)
    record = await breaker.record_failure(PROVIDER, MODEL, 4_000, provider_reported_max=32_768)

    described = record.describe()

    assert MODEL in described
    assert "threshold_tokens=4000" in described
    assert "Unable to submit" not in described


# --- Classification ---


async def test_the_observed_google_message_is_recognised() -> None:
    body = {
        "error": {
            "code": 400,
            "message": (
                "The input token count (5911388) exceeds the maximum number of "
                "tokens allowed (1048576)."
            ),
            "status": "INVALID_ARGUMENT",
        }
    }

    info = is_context_length_error(body)

    assert info is not None
    assert info.attempted_tokens == 5_911_388
    assert info.maximum_tokens == 1_048_576


def test_the_second_observed_google_message_is_recognised() -> None:
    body = {
        "error": {
            "code": 400,
            "message": (
                "Unable to submit request because the input token count is 40000 "
                "but model only supports up to 32768"
            ),
            "status": "INVALID_ARGUMENT",
        }
    }

    info = is_context_length_error(body)

    assert info is not None
    assert info.attempted_tokens == 40_000
    assert info.maximum_tokens == 32_768


async def test_thousands_separators_are_handled() -> None:
    body = {
        "error": {
            "message": (
                "input token count (1,048,576) exceeds the maximum number of "
                "tokens allowed (1,000,000)"
            )
        }
    }

    info = is_context_length_error(body)

    assert info is not None
    assert info.attempted_tokens == 1_048_576
    assert info.maximum_tokens == 1_000_000


def test_an_unrecognised_400_does_not_trip_the_breaker() -> None:
    """Failing closed would degrade a working model on any malformed request."""
    body = {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}}

    assert is_context_length_error(body) is None


async def test_mistral_wording_is_not_guessed() -> None:
    """Mistral's shape has not been observed, so it is deliberately not matched."""
    body = {
        "object": "error",
        "message": "maximum context length exceeded",
        "type": "invalid_request_error",
    }

    assert is_context_length_error(body) is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"error": "not-an-object"},
        {"error": {}},
        {"error": {"message": ""}},
        ["a", "list"],
        "a string",
    ],
)
def test_malformed_bodies_are_handled(body: object) -> None:
    assert is_context_length_error(body) is None


async def test_counts_are_optional_when_the_message_is_recognised() -> None:
    """The phrase alone is enough to classify; the numbers are a bonus."""
    body = {"error": {"message": "exceeds the maximum number of tokens allowed"}}

    info = is_context_length_error(body)

    assert info is not None
    assert info.attempted_tokens is None
    assert info.maximum_tokens is None
