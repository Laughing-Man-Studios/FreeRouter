"""Token estimation, the output assumption, and provider-reported usage.

Batch 3 (T049–T051). The reservation cycle is CONSTITUTION §8.2, and this
milestone makes it *accurate* rather than merely present:

- The reservation covers the **input** plus an **assumed output**, because
  under-reserving output is what breaches a token-per-minute ceiling.
- The **request** dimension stays exact. A provider's token count says nothing
  about how many requests were made.
- A **provider-reported** figure supersedes the estimate where present, and its
  absence leaves the estimate standing rather than releasing capacity that may
  well have been used.
"""

from __future__ import annotations

from free_router.core.normalization import NormalizedRequest, NormalizedResponse, TokenUsage
from free_router.providers.google.adapter import from_gemini_response
from free_router.providers.mistral.adapter import from_mistral_response
from free_router.quota.dimensions import (
    DimensionResource,
    Provenance,
    QuotaDimension,
    WindowReset,
)
from free_router.quota.estimation import (
    DEFAULT_OUTPUT_TOKEN_ASSUMPTION,
    estimate_input_tokens,
    estimate_total_tokens,
)
from free_router.quota.tracker import QuotaTracker

PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"


def _tpm(limit: float) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.TOKENS,
        limit=limit,
        window_seconds=60.0,
        reset=WindowReset.ROLLING,
        provenance=Provenance.MEASURED,
    )


def _rps(limit: float) -> QuotaDimension:
    return QuotaDimension(
        resource=DimensionResource.REQUESTS,
        limit=limit,
        window_seconds=60.0,
        reset=WindowReset.ROLLING,
        provenance=Provenance.MEASURED,
    )


def _request(text: str = "hello world") -> NormalizedRequest:
    return NormalizedRequest(model=PROVIDER, messages=[{"role": "user", "content": text}])


def _tracker(*dimensions: QuotaDimension, **kwargs: object) -> QuotaTracker:
    return QuotaTracker({(PROVIDER, MODEL): dimensions}, **kwargs)  # type: ignore[arg-type]


# --- the estimator -----------------------------------------------------------


def test_the_output_assumption_defaults_to_1025() -> None:
    """The agreed value, recorded so a change is deliberate rather than drift."""
    assert DEFAULT_OUTPUT_TOKEN_ASSUMPTION == 1025


def test_the_input_estimate_is_never_zero() -> None:
    assert estimate_input_tokens(_request("")) >= 1
    assert estimate_input_tokens(_request("a")) >= 1


def test_the_input_estimate_is_monotonic_in_content_length() -> None:
    short = _request("hello")
    long = _request("hello " * 100)

    assert estimate_input_tokens(long) > estimate_input_tokens(short)


def test_the_total_estimate_is_input_plus_output() -> None:
    request = _request("hello world")

    assert estimate_total_tokens(request, 1025) == estimate_input_tokens(request) + 1025
    assert estimate_total_tokens(request, 0) == estimate_input_tokens(request)


# --- the reservation ---------------------------------------------------------


async def test_the_reservation_covers_the_output_assumption() -> None:
    """The reservation must cover the response, not just the request.

    This is the regression M1's estimator would have permitted: M1 reserved the
    input only, which silently assumed the response cost nothing.
    """
    tracker = _tracker(_tpm(100_000))
    request = _request("hello world")

    reservation = await tracker.reserve(PROVIDER, MODEL, estimate_input_tokens(request))

    assert reservation is not None
    assert reservation.estimated_input_tokens == estimate_input_tokens(request)
    assert reservation.estimated_tokens == estimate_input_tokens(request) + 1025


async def test_the_output_assumption_is_configurable() -> None:
    """A smaller assumption reserves less, and that is the operator's call."""
    request = _request("hello world")
    input_tokens = estimate_input_tokens(request)

    generous = _tracker(_tpm(100_000), output_token_assumption=1025)
    stingy = _tracker(_tpm(100_000), output_token_assumption=0)

    big = await generous.reserve(PROVIDER, MODEL, input_tokens)
    small = await stingy.reserve(PROVIDER, MODEL, input_tokens)

    assert big is not None and small is not None
    assert big.reserved_tokens > small.reserved_tokens


async def test_the_safety_margin_multiplies_the_total() -> None:
    """The margin applies to input *and* output, not just the input."""
    tracker = _tracker(_tpm(100_000), safety_margin=2.0, output_token_assumption=1000)
    request = _request("hello world")
    input_tokens = estimate_input_tokens(request)

    reservation = await tracker.reserve(PROVIDER, MODEL, input_tokens)

    assert reservation is not None
    assert reservation.reserved_tokens == (input_tokens + 1000) * 2


async def test_a_tight_token_budget_refuses_when_the_output_would_not_fit() -> None:
    """The reservation is refused when the response would not fit.

    Under-reserving output is what breaches a ceiling, so a budget that cannot
    cover the assumption must refuse rather than admit.
    """
    tracker = _tracker(_tpm(1000), output_token_assumption=1025)
    request = _request("hello world")
    input_tokens = estimate_input_tokens(request)

    assert await tracker.reserve(PROVIDER, MODEL, input_tokens) is None


# --- the request dimension is exact ------------------------------------------


async def test_a_token_count_never_inflates_the_request_dimension() -> None:
    """One dispatched request is one request, whatever the response contained.

    A provider's token count says nothing about how many requests were made. If
    reconciliation wrote the token figure into the request window, a single
    request would consume seven request-slots, and an RPM budget would be
    exhausted seven times faster than the traffic warranted.
    """
    tracker = _tracker(_rps(10), _tpm(100_000))
    request = _request("hello world")

    reservation = await tracker.reserve(PROVIDER, MODEL, estimate_input_tokens(request))
    assert reservation is not None

    await tracker.reconcile(reservation, 7)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.REQUESTS) == 1
    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 7


# --- provider-reported usage -------------------------------------------------


async def test_a_reported_figure_supersedes_the_estimate() -> None:
    """The provider's number is authoritative where it reports one."""
    tracker = _tracker(_tpm(100_000))
    request = _request("hello world")

    reservation = await tracker.reserve(PROVIDER, MODEL, estimate_input_tokens(request))
    assert reservation is not None

    await tracker.reconcile(reservation, 42)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 42


async def test_no_reported_figure_leaves_the_estimate_standing() -> None:
    """Absence of usage must not release capacity.

    Every dispatched attempt consumes quota whether or not it produced output, so
    a run of 5xx reporting no usage would otherwise drain a daily budget while
    the router believed it had capacity left.
    """
    tracker = _tracker(_tpm(100_000))
    request = _request("hello world")

    reservation = await tracker.reserve(PROVIDER, MODEL, estimate_input_tokens(request))
    assert reservation is not None

    await tracker.reconcile(reservation, None)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == reservation.reserved_tokens


async def test_an_implausible_figure_is_not_believed() -> None:
    """A cumulative counter reported as per-request must not be believed.

    A cumulative figure would inflate reconciliation without bound, so one more
    than an order of magnitude above the reservation is treated as implausible
    and the estimate stands.
    """
    tracker = _tracker(_tpm(100_000))
    request = _request("hello world")

    reservation = await tracker.reserve(PROVIDER, MODEL, estimate_input_tokens(request))
    assert reservation is not None

    await tracker.reconcile(reservation, reservation.reserved_tokens * 1000)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == reservation.reserved_tokens


async def test_reconciliation_is_idempotent() -> None:
    """A repeated settle cannot double-count."""
    tracker = _tracker(_tpm(100_000))
    request = _request("hello world")

    reservation = await tracker.reserve(PROVIDER, MODEL, estimate_input_tokens(request))
    assert reservation is not None

    await tracker.reconcile(reservation, 42)
    await tracker.reconcile(reservation, 42)

    assert tracker.used(PROVIDER, MODEL, DimensionResource.TOKENS) == 42


# --- the adapters ------------------------------------------------------------


def test_gemini_usage_is_parsed_from_its_string_counts() -> None:
    """Gemini reports counts as strings, so they are parsed rather than trusted."""
    body = {
        "candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"}}],
        "usageMetadata": {
            "promptTokenCount": "28",
            "candidatesTokenCount": "146",
            "totalTokenCount": "174",
        },
    }

    response = from_gemini_response(body, MODEL)

    assert response.usage is not None
    assert response.usage.input_tokens == 28
    assert response.usage.output_tokens == 146
    assert response.usage.resolved_total() == 174


def test_gemini_without_usage_reports_none() -> None:
    """No usage block means no figure, and the estimate stands."""
    body = {"candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"}}]}

    response = from_gemini_response(body, MODEL)

    assert response.usage is None


def test_mistral_usage_is_parsed_from_its_numeric_counts() -> None:
    """Mistral reports counts as numbers."""
    body = {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 33, "completion_tokens": 178, "total_tokens": 211},
    }

    response = from_mistral_response(body, MODEL)

    assert response.usage is not None
    assert response.usage.input_tokens == 33
    assert response.usage.output_tokens == 178
    assert response.usage.resolved_total() == 211


def test_mistral_without_usage_reports_none() -> None:
    body = {"choices": [{"message": {"content": "hi"}}]}

    response = from_mistral_response(body, MODEL)

    assert response.usage is None


def test_a_response_carries_no_usage_by_default() -> None:
    """The field is optional, so a provider that reports nothing needs no change."""
    response = NormalizedResponse(content="hi", model_used=MODEL)

    assert response.usage is None


def test_token_usage_derives_a_total_when_the_provider_gave_none() -> None:
    """A provider that reports input and output but no total still yields one."""
    usage = TokenUsage(input_tokens=10, output_tokens=20)

    assert usage.resolved_total() == 30


# --- privacy -----------------------------------------------------------------


def test_usage_is_metadata_and_never_content() -> None:
    """Token counts say how much was spent, not what was said.

    That is what makes it safe to reconcile against and to persist: the count is
    operational metadata, and no text is recoverable from it.
    """
    usage = TokenUsage(input_tokens=100, output_tokens=200)

    payload = usage.model_dump()

    assert set(payload) == {"input_tokens", "output_tokens", "total_tokens"}
    assert all(isinstance(value, int) or value is None for value in payload.values())
