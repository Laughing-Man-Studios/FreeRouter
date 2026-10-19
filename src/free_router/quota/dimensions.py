"""Level 1 quota dimensions: what is limited, by how much, and over what window.

CONSTITUTION §8.2 requires the router to estimate, reserve, dispatch, and
reconcile against quota, but it does not say which quota. Google applies limits
per *project* with a daily reset at midnight Pacific; Mistral applies per-second
request ceilings and per-minute token ceilings. The two do not share a shape,
and neither publishes the numbers.

So this module holds a deliberately **generic** representation. A dimension is a
(resource, limit, window, reset) tuple. Requests-per-second and
requests-per-minute are not different kinds — they are the same resource over
different windows — which means a new provider's limits are a configuration
change rather than a code change (ROADMAP §2, and the "new dimension requires no
tracker change" requirement in T027).

Three decisions here are load-bearing and were made against alternatives.

**Counters are keyed on the shared capacity unit, never on a credential.**
Issue #51 established that *"Rate limits are applied per project, not per API
key"* — two keys in one Google project share one bucket. A tracker keyed on the
key would report headroom that does not exist: two keys each reading 40% while
the shared project bucket sits at 80%. The key is a credential for reaching
capacity, not capacity itself, so capacity is tracked against
``(provider, model)`` and never against an alias.

**A limit must be a positive number; there is no way to express "unlimited".**
RPD is unverified for both providers — Google does not publish it and Mistral's
console shows no daily ceiling. The tempting encoding is an optional ``limit``
meaning unlimited, but that converts "we do not know this limit" into "this
limit is infinite", and the router would then sail past a real ceiling while
reporting full headroom. Absence of evidence is not evidence of absence. An
unknown dimension is **omitted**, which means it is not enforced — a visible gap
rather than a silent, false guarantee.

The limit is a **float** because fractional ceilings are real: Mistral permits
0.50 requests/second on ``ministral-14b-2512``, meaning one request every two
seconds. An integer field cannot express that, and rounding 0.5 to 1 would
dispatch at twice the permitted rate.

**``provenance`` is required rather than defaulted.** Google's values are
conservative **placeholders** and Mistral's were **measured** from a workspace
console; neither is provider-published. Requiring the marker means no dimension
can be added without someone stating where the number came from, so a guess can
never be silently upgraded into an authority by a later reader.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "SECONDS_PER_DAY",
    "DimensionResource",
    "Provenance",
    "QuotaDimension",
    "WindowReset",
]

SECONDS_PER_DAY = 86_400.0
"""A daily window. Google's RPD resets at midnight Pacific, so a daily
dimension is a FIXED window rather than a rolling 24 hours — the two differ at
the boundary, where a request admitted just before midnight Pacific can be
followed immediately by one just after."""


class DimensionResource(StrEnum):
    """What a dimension counts.

    Two resources cover every Level 1 limit either provider documents. A third
    would be added here, and nowhere else — the tracker consumes dimensions
    generically and has no per-resource branch.
    """

    REQUESTS = "requests"
    TOKENS = "tokens"


class WindowReset(StrEnum):
    """How a window's usage returns to zero.

    These are genuinely different behaviours, not naming variants. A rolling
    window decays continuously, so 30 requests in the last 60 seconds is the
    whole story. A fixed window does not decay: all of the window's usage
    vanishes at once when the boundary passes, so a request can be admitted at
    23:59:59 and the next admitted immediately at 00:00:00.
    """

    ROLLING = "rolling"
    FIXED = "fixed"
    """Resets at a boundary, rather than decaying continuously.

    **The boundary is aligned to the monotonic clock's epoch, not to midnight
    Pacific.** Google's daily allowance really does reset at midnight Pacific,
    and that is the intent, but nothing in the router knows about Pacific time,
    so a fixed window currently resets at an arbitrary offset from boot.

    The persisted boundary is this one, expressed in wall-clock terms, so the
    durable record is truthful about the behaviour the code actually has. Making
    the window reset at a *specified* wall-clock boundary is outstanding work,
    and it belongs in the window implementation rather than in the persistence
    layer -- which should not decide what a bucket means.

    No fixed window is currently configured, so nothing misbehaves today.
    """


class Provenance(StrEnum):
    """Where a configured limit came from, and therefore how much to trust it.

    Recorded per dimension rather than per provider because the two providers
    are in genuinely different positions, and collapsing them would present a
    guess as an authority.
    """

    PUBLISHED = "published"
    """Stated in the provider's current documentation."""

    MEASURED = "measured"
    """Read from a workspace console or measured against the live API."""

    PLACEHOLDER = "placeholder"
    """A conservative guess. Unverified, and must be tuned from observation."""


class QuotaDimension(BaseModel):
    """One limit: a resource, a ceiling, and the window the ceiling applies over."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: DimensionResource
    limit: float
    window_seconds: float
    reset: WindowReset = WindowReset.ROLLING
    provenance: Provenance

    @field_validator("limit")
    @classmethod
    def _limit_must_be_positive(cls, value: float) -> float:
        """Refuse a limit that would enforce the wrong thing.

        A zero or negative limit refuses every request. It is rejected rather
        than clamped because the intent behind one is almost always a mistake,
        and the fix is to omit the dimension.

        The limit is a **float**, not an integer, because fractional ceilings
        are real: Mistral's free tier permits 0.50 requests/second for
        ``ministral-14b-2512`` and 0.63 for ``labs-leanstral-1-5-1``, meaning one
        request every two seconds and every 1.6 seconds. An integer-only field
        cannot express either, and rounding 0.5 up to 1 would dispatch at twice
        the permitted rate.
        """
        if value <= 0:
            raise ValueError(
                f"limit must be positive, got {value}. An unknown or unlimited limit is "
                "expressed by omitting the dimension entirely, not by setting an "
                "impossible value."
            )
        return value

    @field_validator("window_seconds")
    @classmethod
    def _window_must_be_positive(cls, value: float) -> float:
        """Refuse a zero window, which would divide by zero in a rolling window."""
        if value <= 0:
            raise ValueError(f"window_seconds must be positive, got {value}.")
        return value

    @property
    def is_requests(self) -> bool:
        """True when this dimension counts requests rather than tokens."""
        return self.resource is DimensionResource.REQUESTS

    @property
    def per_minute(self) -> float:
        """The limit normalised to a per-minute rate.

        Useful for comparing dimensions across providers that express the same
        ceiling differently — Mistral's 12.50 RPS and a hypothetical 750 RPM are
        the same ceiling — and for logging a limit in the unit an operator
        usually reads. This is a *derived* rate for comparison and display; a
        rolling window must still be evaluated against real usage inside the
        real window.
        """
        return self.limit * 60.0 / self.window_seconds

    def describe(self) -> str:
        """Render the dimension for a log line.

        Includes provenance deliberately. An operator reading ``limit=15`` has no
        way to know it is a guess unless the log says so.
        """
        return (
            f"{self.limit:g} {self.resource.value}/{self.window_seconds:g}s "
            f"({self.provenance.value}, {self.reset.value})"
        )
