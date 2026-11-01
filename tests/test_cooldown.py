"""Cooldown state (T029): what to do after a provider says no.

CONSTITUTION §6.1 forbids retrying into a rate limit, so the central guarantee
here is that ``is_cooling`` is a hard stop and that no hint can shorten the wait
below the configured floor.

The retry-hint fixtures are the shapes actually observed in provider errors:

- Google's quota errors carry ``google.rpc.RetryInfo`` with a protobuf Duration
  such as ``"16s"``, and a message reading ``Please retry in 45.06576809s.``.
- ``Retry-After`` is a standard header with two documented forms, seconds and
  an HTTP date.

Time is injected throughout; nothing here sleeps.
"""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from free_router.core.config import ModelConfig
from free_router.quota.cooldown import (
    DEFAULT_COOLDOWN_SECONDS,
    CooldownManager,
    CooldownStatus,
    parse_retry_after_seconds,
    parse_retry_delay_seconds,
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


class FakeHeaders:
    """Minimal stand-in for httpx headers."""

    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, key: str) -> str | None:
        return self._values.get(key)


def _manager(
    clock: FakeClock,
    *,
    default: float = 60.0,
    durations: dict[tuple[str, str], float] | None = None,
) -> CooldownManager:
    return CooldownManager(durations=durations, default_duration=default, clock=clock)


# --- Entry and exit ---


def test_fresh_manager_is_available() -> None:
    clock = FakeClock()
    manager = _manager(clock)

    assert manager.status(PROVIDER, MODEL) is CooldownStatus.AVAILABLE
    assert not manager.is_cooling(PROVIDER, MODEL)
    assert manager.remaining(PROVIDER, MODEL) == 0.0


async def test_rate_limit_marks_the_model_cooling() -> None:
    clock = FakeClock()
    manager = _manager(clock)

    await manager.enter(PROVIDER, MODEL, reason="rate_limited")

    assert manager.is_cooling(PROVIDER, MODEL)
    assert manager.status(PROVIDER, MODEL) is CooldownStatus.COOLING


async def test_cooldown_expires_without_operator_action() -> None:
    clock = FakeClock()
    manager = _manager(clock)
    await manager.enter(PROVIDER, MODEL)

    clock.advance(59.9)
    assert manager.is_cooling(PROVIDER, MODEL)

    clock.advance(0.2)
    assert not manager.is_cooling(PROVIDER, MODEL)
    assert manager.status(PROVIDER, MODEL) is CooldownStatus.AVAILABLE


async def test_remaining_counts_down_and_floors_at_zero() -> None:
    clock = FakeClock()
    manager = _manager(clock)
    await manager.enter(PROVIDER, MODEL)

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(60.0)

    clock.advance(45)
    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(15.0)

    clock.advance(600)
    assert manager.remaining(PROVIDER, MODEL) == 0.0


async def test_clear_returns_a_model_to_available() -> None:
    """Operator recovery, and M2's seam for invalidating persisted state."""
    clock = FakeClock()
    manager = _manager(clock)
    await manager.enter(PROVIDER, MODEL)

    await manager.clear(PROVIDER, MODEL)

    assert not manager.is_cooling(PROVIDER, MODEL)


async def test_clearing_an_available_model_is_harmless() -> None:
    clock = FakeClock()
    await _manager(clock).clear(PROVIDER, MODEL)


async def test_expired_cooldown_is_discarded_not_retained() -> None:
    """Expiry is lazy, so a read must not leave dead state accumulating."""
    clock = FakeClock()
    manager = _manager(clock)
    await manager.enter(PROVIDER, MODEL)

    clock.advance(120)
    assert manager.record(PROVIDER, MODEL) is None


# --- The wait is the longest applicable hint, never the shortest ---


async def test_a_longer_provider_hint_is_honoured() -> None:
    """Google telling us to wait 45s must beat a 60s floor only if longer."""
    clock = FakeClock()
    manager = _manager(clock, default=10.0)

    await manager.enter(PROVIDER, MODEL, provider_hint=45.0)

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(45.0)


async def test_a_shorter_provider_hint_never_shortens_the_wait() -> None:
    """CONSTITUTION §6.1: retrying into a rate limit is prohibited.

    A provider asking for 5s against a 60s floor must not get 5s. Honouring the
    shorter value would re-dispatch into a window that has not reset.
    """
    clock = FakeClock()
    manager = _manager(clock, default=60.0)

    record = await manager.enter(PROVIDER, MODEL, provider_hint=5.0)

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(60.0)
    assert record.source == "configured"


async def test_hint_source_is_recorded_for_diagnostics() -> None:
    clock = FakeClock()
    manager = _manager(clock, default=10.0)

    assert (await manager.enter(PROVIDER, MODEL, provider_hint=45.0)).source == "provider_hint"


async def test_no_hint_falls_back_to_the_configured_duration() -> None:
    clock = FakeClock()
    manager = _manager(clock, default=25.0)

    record = await manager.enter(PROVIDER, MODEL)

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(25.0)
    assert record.source == "configured"


async def test_zero_hint_does_not_produce_a_zero_wait() -> None:
    """A hint of zero must not be treated as 'no wait at all'."""
    clock = FakeClock()
    manager = _manager(clock, default=60.0)

    await manager.enter(PROVIDER, MODEL, provider_hint=0.0)

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(60.0)


# --- Per-model configuration ---


def test_duration_is_per_model_not_global() -> None:
    """A long-reset provider must not inherit another provider's short wait."""
    clock = FakeClock()
    manager = _manager(
        clock,
        default=60.0,
        durations={("google", MODEL): 300.0, ("mistral", "ministral-3b-2512"): 15.0},
    )

    assert manager.configured_duration("google", MODEL) == 300.0
    assert manager.configured_duration("mistral", "ministral-3b-2512") == 15.0
    assert manager.configured_duration("google", "unconfigured") == 60.0


async def test_cooldown_is_isolated_per_model() -> None:
    clock = FakeClock()
    manager = _manager(clock)
    await manager.enter(PROVIDER, MODEL)

    assert not manager.is_cooling(PROVIDER, "gemini-3.5-pro")
    assert not manager.is_cooling("mistral", MODEL)


async def test_re_entering_extends_the_wait() -> None:
    """A second rate limit while cooling must not shorten the cooldown."""
    clock = FakeClock()
    manager = _manager(clock, default=60.0)
    await manager.enter(PROVIDER, MODEL)

    clock.advance(50)
    await manager.enter(PROVIDER, MODEL)

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(60.0)


# --- Window-derived cooldown ---


async def test_enter_until_covers_a_daily_window() -> None:
    """A daily budget exhausted at 23:50 cannot be retried in sixty seconds.

    Google's free tier is dominated by a requests-per-day ceiling, so a
    duration-based wait is the wrong shape for the common case.
    """
    clock = FakeClock()
    manager = _manager(clock)
    midnight = clock.now + 600

    await manager.enter_until(PROVIDER, MODEL, midnight, reason="window_exhausted")

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(600.0)
    clock.advance(599)
    assert manager.is_cooling(PROVIDER, MODEL)
    clock.advance(2)
    assert not manager.is_cooling(PROVIDER, MODEL)


async def test_enter_until_in_the_past_is_immediately_available() -> None:
    clock = FakeClock()
    manager = _manager(clock)

    await manager.enter_until(PROVIDER, MODEL, clock.now - 1)

    assert not manager.is_cooling(PROVIDER, MODEL)


# --- Logging safety ---


async def test_record_never_carries_provider_text() -> None:
    """CONSTITUTION §9.1: provider bodies can echo prompt content."""
    clock = FakeClock()
    manager = _manager(clock)

    record = await manager.enter(PROVIDER, MODEL, reason="rate_limited")
    described = record.describe()

    assert "rate_limited" in described
    assert MODEL in described


def test_config_cooldown_default_is_configurable_per_model() -> None:
    model = ModelConfig(
        id="mistral/m",
        provider="mistral",
        provider_id="m",
        cooldown_seconds=300.0,
    )

    assert model.cooldown_seconds == 300.0


async def test_config_rejects_a_non_positive_cooldown() -> None:
    """A zero cooldown would mean 'do not wait', which is the prohibited retry."""
    with pytest.raises(ValueError):
        ModelConfig(id="m/m", provider="m", provider_id="m", cooldown_seconds=0.0)


def test_config_default_covers_a_rolling_window() -> None:
    """The default must span the shortest documented window, not be arbitrary."""
    assert DEFAULT_COOLDOWN_SECONDS >= 60.0
    assert (
        ModelConfig(id="g/m", provider="google", provider_id="m").cooldown_seconds
        == DEFAULT_COOLDOWN_SECONDS
    )


# --- Retry-After header ---


async def test_retry_after_seconds_form() -> None:
    assert parse_retry_after_seconds(FakeHeaders(**{"Retry-After": "30"})) == 30.0


def test_retry_after_whitespace_and_float() -> None:
    assert parse_retry_after_seconds(FakeHeaders(**{"Retry-After": " 45.5 "})) == 45.5


async def test_retry_after_absent_returns_none() -> None:
    assert parse_retry_after_seconds(FakeHeaders()) is None


def test_retry_after_unparseable_returns_none_rather_than_guessing() -> None:
    """Falling back to the configured floor is safe; inventing a number is not."""
    assert parse_retry_after_seconds(FakeHeaders(**{"Retry-After": "soon"})) is None
    assert parse_retry_after_seconds(None) is None


async def test_retry_after_http_date_form() -> None:
    """The second documented form. Evaluated against the wall clock."""
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)

    parsed = parse_retry_after_seconds(FakeHeaders(**{"Retry-After": future}))

    assert parsed is not None
    assert 110 <= parsed <= 125


def test_retry_after_http_date_in_the_past_floors_at_zero() -> None:
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=120), usegmt=True)

    assert parse_retry_after_seconds(FakeHeaders(**{"Retry-After": past})) == 0.0


# --- Google RetryInfo ---


async def test_retry_info_delay_is_parsed() -> None:
    body = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaMetric": "generate_requests_per_model"}],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "16s"},
            ],
        }
    }

    assert parse_retry_delay_seconds(body) == 16.0


def test_retry_info_parses_a_fractional_delay() -> None:
    """Observed live: 'Please retry in 45.06576809s.'"""
    body = {
        "error": {
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45.06576809s"}
            ]
        }
    }

    assert parse_retry_delay_seconds(body) == pytest.approx(45.06576809)


async def test_retry_info_absent_returns_none() -> None:
    body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.Help"}]}}

    assert parse_retry_delay_seconds(body) is None
    assert parse_retry_delay_seconds({"error": {}}) is None
    assert parse_retry_delay_seconds({"error": {"details": "not-a-list"}}) is None
    assert parse_retry_delay_seconds({}) is None
    assert parse_retry_delay_seconds(None) is None


def test_retry_info_malformed_duration_returns_none() -> None:
    body = {
        "error": {
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "soon"}]
        }
    }

    assert parse_retry_delay_seconds(body) is None


async def test_retry_info_non_string_duration_returns_none() -> None:
    body = {
        "error": {
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": 16}]
        }
    }

    assert parse_retry_delay_seconds(body) is None


async def test_the_longest_hint_wins_across_header_and_body() -> None:
    """Header and body are independent hints; the later wait governs."""
    clock = FakeClock()
    manager = _manager(clock, default=10.0)

    header = parse_retry_after_seconds(FakeHeaders(**{"Retry-After": "5"}))
    body = parse_retry_delay_seconds(
        {
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}
                ]
            }
        }
    )
    await manager.enter(PROVIDER, MODEL, provider_hint=max(x for x in (header, body) if x))

    assert manager.remaining(PROVIDER, MODEL) == pytest.approx(45.0)
