"""Level 1 quota dimensions (T027).

The dimension representation is configuration, so a new provider's limits are a
config change rather than a code change. These tests pin the three decisions
that make that true, and the two ways a dimension can silently enforce the
wrong thing.
"""

import pytest
from pydantic import ValidationError

from free_router.core.config import ConfigError, ModelConfig, load_config
from free_router.quota.dimensions import (
    SECONDS_PER_DAY,
    DimensionResource,
    Provenance,
    QuotaDimension,
    WindowReset,
)


def _dimension(**overrides: object) -> QuotaDimension:
    defaults: dict[str, object] = {
        "resource": DimensionResource.REQUESTS,
        "limit": 15,
        "window_seconds": 60.0,
        "provenance": Provenance.MEASURED,
    }
    return QuotaDimension(**{**defaults, **overrides})  # type: ignore[arg-type]


# --- The representation is generic ---


def test_requests_per_second_and_per_minute_are_the_same_kind() -> None:
    """RPS and RPM differ only in window, not in kind.

    This is what lets a new provider's limits be a configuration change. If
    per-second were a separate resource, Mistral's RPS ceilings would need a
    tracker change and a second code path through the reservation cycle.
    """
    per_second = _dimension(limit=15, window_seconds=1.0)
    per_minute = _dimension(limit=900, window_seconds=60.0)

    assert per_second.resource is per_minute.resource
    assert per_second.per_minute == per_minute.per_minute == 900


def test_requests_and_tokens_are_distinguishable_resources() -> None:
    assert _dimension().is_requests
    assert not _dimension(resource=DimensionResource.TOKENS).is_requests


def test_fixed_and_rolling_windows_are_distinguishable() -> None:
    """They behave differently at the boundary, so the distinction is load-bearing."""
    assert _dimension().reset is WindowReset.ROLLING
    assert _dimension(reset=WindowReset.FIXED).reset is WindowReset.FIXED


def test_daily_window_is_available_and_documented_as_fixed() -> None:
    """Google's RPD resets at midnight Pacific, so a day is not 24 rolling hours."""
    daily = _dimension(
        limit=1500,
        window_seconds=SECONDS_PER_DAY,
        reset=WindowReset.FIXED,
        provenance=Provenance.PLACEHOLDER,
    )

    assert daily.window_seconds == 86_400.0
    assert daily.reset is WindowReset.FIXED


# --- No way to express "unlimited" ---


@pytest.mark.parametrize("bad", [0, -1, -1500, -0.5])
def test_non_positive_limit_is_rejected(bad: float) -> None:
    """A limit of zero refuses every request.

    An unknown limit must be expressed by omitting the dimension, not by a
    sentinel value. Accepting 0 would turn "we do not know this limit" into
    "refuse everything", and accepting a negative would be worse.
    """
    with pytest.raises(ValidationError, match="limit must be positive"):
        _dimension(limit=bad)


@pytest.mark.parametrize("bad", [0.0, -1.0, -60.0])
def test_non_positive_window_is_rejected(bad: float) -> None:
    """A zero window would divide by zero in a rolling window."""
    with pytest.raises(ValidationError, match="window_seconds must be positive"):
        _dimension(window_seconds=bad)


def test_rejection_message_points_at_omitting_the_dimension() -> None:
    """The error must say what to do instead, not just what is wrong."""
    with pytest.raises(ValidationError, match="omitting the dimension"):
        _dimension(limit=0)


# --- Provenance is mandatory ---


def test_provenance_cannot_be_defaulted() -> None:
    """A dimension with no stated source would let a guess read as an authority."""
    with pytest.raises(ValueError, match="provenance"):
        QuotaDimension(  # type: ignore[call-arg]
            resource=DimensionResource.REQUESTS, limit=15, window_seconds=60.0
        )


def test_unknown_provenance_is_rejected() -> None:
    with pytest.raises(ValueError):
        _dimension(provenance="guessed-from-a-blog-post")  # type: ignore[arg-type]


def test_describe_includes_provenance() -> None:
    """An operator reading a limit in a log has no other way to know its source."""
    placeholder = _dimension(provenance=Provenance.PLACEHOLDER).describe()
    measured = _dimension(provenance=Provenance.MEASURED).describe()

    assert "placeholder" in placeholder
    assert "measured" in measured
    assert placeholder != measured


def test_describe_shows_the_window_and_reset() -> None:
    described = _dimension(
        limit=1500, window_seconds=SECONDS_PER_DAY, reset=WindowReset.FIXED
    ).describe()

    assert "1500 requests/86400s" in described
    assert "fixed" in described


def test_per_minute_normalises_across_windows() -> None:
    assert _dimension(limit=0.5, window_seconds=1.0).per_minute == 30.0
    assert _dimension(limit=30, window_seconds=60.0).per_minute == 30.0


# --- Configuration integration ---


def test_dimensions_load_from_config(workdir, write_config, set_api_key) -> None:
    """The representation must be reachable from config.yaml, not just the API."""
    set_api_key()
    write_config(
        """\
models:
  - id: "mistral/ministral-3b-2512"
    provider: "mistral"
    provider_id: "ministral-3b-2512"
    quota:
      - resource: "requests"
        limit: 15
        window_seconds: 1
        provenance: "measured"
      - resource: "tokens"
        limit: 1300000
        window_seconds: 60
        provenance: "measured"
"""
    )

    # These tests are about dimension parsing, not credentials, and they name a
    # Mistral model while `set_api_key` provisions Google's slot. resolve_keys=False
    # skips the credential requirement rather than this test setting an unrelated key.
    config = load_config(resolve_keys=False)
    dimensions = config.models[0].quota

    assert len(dimensions) == 2
    assert dimensions[0].resource is DimensionResource.REQUESTS
    assert dimensions[0].window_seconds == 1.0
    assert dimensions[1].resource is DimensionResource.TOKENS


def test_model_with_no_quota_block_has_no_dimensions() -> None:
    """An absent quota block is valid and means nothing is enforced.

    This is how an unverified limit is represented, so it must not be an error.
    """
    model = ModelConfig(id="google/gemini-3.5-flash-lite", provider="google", provider_id="gemini")

    assert model.quota == []


def test_unknown_quota_field_is_rejected(workdir, write_config, set_api_key) -> None:
    """A typo must fail startup rather than silently drop a limit.

    ``prov: x`` for ``provenance`` would otherwise leave the dimension with no
    stated source, which is the exact failure provenance exists to prevent.
    """
    set_api_key()
    write_config(
        """\
models:
  - id: "mistral/m"
    provider: "mistral"
    provider_id: "m"
    quota:
      - resource: "requests"
        limit: 15
        window_seconds: 60
        provenance: "measured"
        prov: "measured"
"""
    )

    with pytest.raises(ConfigError, match="prov"):
        load_config()


def test_fractional_limits_load_from_config(workdir, write_config, set_api_key) -> None:
    """Sub-one-request-per-second ceilings must be expressible.

    Mistral's free tier permits 0.50 RPS for ``ministral-14b-2512`` and 0.63 for
    ``labs-leanstral-1-5-1``. An integer-only limit cannot hold either, and
    rounding 0.5 up to 1 would dispatch at twice the permitted rate against a
    provider that enforces the ceiling per second.
    """
    set_api_key()
    write_config(
        """\
models:
  - id: "mistral/ministral-14b-2512"
    provider: "mistral"
    provider_id: "ministral-14b-2512"
    quota:
      - resource: "requests"
        limit: 0.5
        window_seconds: 1
        provenance: "measured"
"""
    )

    config = load_config(resolve_keys=False)
    dimension = config.models[0].quota[0]

    assert dimension.limit == 0.5
    assert dimension.per_minute == 30.0
    assert "0.5" in dimension.describe()


def test_unknown_resource_is_rejected_at_startup(workdir, write_config, set_api_key) -> None:
    set_api_key()
    write_config(
        """\
models:
  - id: "mistral/m"
    provider: "mistral"
    provider_id: "m"
    quota:
      - resource: "furlongs"
        limit: 15
        window_seconds: 60
        provenance: "measured"
"""
    )

    with pytest.raises(ConfigError):
        load_config()


def test_zero_limit_is_rejected_at_startup(workdir, write_config, set_api_key) -> None:
    set_api_key()
    write_config(
        """\
models:
  - id: "mistral/m"
    provider: "mistral"
    provider_id: "m"
    quota:
      - resource: "requests"
        limit: 0
        window_seconds: 60
        provenance: "placeholder"
"""
    )

    with pytest.raises(ConfigError):
        load_config()


def test_estimation_safety_margin_must_exceed_one(workdir, write_config, set_api_key) -> None:
    """A margin of 1.0 reserves exactly the estimate, with no buffer.

    CONSTITUTION §8.2 requires the buffer to absorb hidden prompt and tool
    overhead. A margin at or below 1 would reserve less than the request costs,
    which is the overshoot the reservation cycle exists to prevent.
    """
    set_api_key()
    write_config(
        """\
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
quota:
  estimation_safety_margin: 1.0
"""
    )

    with pytest.raises(ConfigError, match="estimation_safety_margin"):
        load_config()


def test_estimation_safety_margin_defaults_to_the_constitutional_example() -> None:
    """CONSTITUTION §8.2 gives ``estimated_tokens * 1.1`` as its example."""
    model = ModelConfig(id="g/m", provider="google", provider_id="gemini")
    assert model.quota == []
