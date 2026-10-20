"""Quota accounting: estimate, reserve, dispatch, reconcile (CONSTITUTION §8.2).

Every dispatch follows the same four steps. **Estimate** what the request will
cost, **reserve** that capacity locally before anything is sent, **dispatch**,
then **reconcile** the reservation against what the provider actually reported.
Reserving first is the whole point: a check-then-send sequence lets N concurrent
requests each observe the same headroom and each proceed, and the limit is
exceeded by exactly N.

Three decisions here are load-bearing.

**Usage is tracked per ``(provider, model)``, never per key.** Google's limits
apply per *project*, so two keys in one project share one bucket. A per-key
counter would report headroom that does not exist -- two keys each reading 40%
while the shared bucket sits at 80%. See :mod:`free_router.quota.dimensions`.

**A reservation counts at its estimate until reconciled.** Entries carry
``reserved`` and an optional ``actual``. Effective usage is ``actual`` when the
provider reported one and ``reserved`` otherwise, so an in-flight request
occupies capacity immediately and a provider that reports nothing leaves the
estimate standing. Reconciliation never *reduces* usage below what was reserved
unless the provider said so, and never double-counts: it overwrites one entry
rather than appending another.

**Reconciliation cannot fail a request that already succeeded.** If a provider
reports more tokens than estimated, the window is recorded as genuinely full and
``available()`` reports False from then on. The overshoot is not hidden and not
unwound -- the tokens were spent. Unwinding would let the router keep dispatching
against a limit it has demonstrably passed.

**State is in memory and will not survive a restart** (ADR-0004, a recorded
deviation from CONSTITUTION §8.3). A reader expecting persistence should look
here first: after a restart this tracker reports an empty window, so it may
briefly exceed a limit it had already reached. Persistence is M2, and lands
*behind this interface* -- callers do not change.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any

from free_router.core.timestamps import from_iso, to_iso
from free_router.quota.dimensions import DimensionResource, QuotaDimension, WindowReset
from free_router.quota.estimation import (
    DEFAULT_OUTPUT_TOKEN_ASSUMPTION,
)

__all__ = [
    "QuotaTracker",
    "Reservation",
]

# Rough characters-per-token for English text. Deliberately a constant rather
# than a tokenizer dependency: adding a tokenizer would pull a model-specific
# vocabulary into the router core, and the safety margin exists precisely to
# absorb the error an estimate makes. CONSTITUTION §8.2 asks for an estimate
# with a buffer, not an exact count.
_CHARS_PER_TOKEN = 4

_IMPLAUSIBLE_ACTUAL_FACTOR = 10
"""How far above a reservation a reported figure may be before it is doubted.

A per-request count cannot plausibly exceed the reservation by an order of
magnitude. The threshold is deliberately generous: a legitimate long
completion must not be mistaken for a cumulative counter, and the cost of
believing a bad figure is unbounded while the cost of doubting a good one is
one pessimistic count.
"""

_PRUNE_THRESHOLD = 256
"""Entry count above which a window is scanned for expired entries."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """Capacity held for one in-flight request.

    Attributes:
        identifier: Correlates this reservation with its reconciliation.
        provider: The provider whose capacity was reserved.
        model_id: The provider-native model id.
        estimated_tokens: The raw estimate, before the safety margin.
        reserved_tokens: What was actually reserved, after the margin.
        window_start: Monotonic timestamp at which the reservation was made.
    """

    identifier: int
    provider: str
    model_id: str
    estimated_input_tokens: int
    """The caller's estimate of the request's own tokens."""
    estimated_tokens: int
    """The total estimate -- input plus the assumed output -- before the margin."""
    reserved_tokens: int
    """What was actually reserved, after the safety margin."""
    window_start: float

    def describe(self) -> str:
        """Render the reservation for a log line.

        Carries no prompt or completion content (CONSTITUTION §9.1); only
        counts, which are already visible in the provider's own usage reporting.
        """
        return (
            f"reservation={self.identifier} estimated_tokens={self.estimated_tokens} "
            f"reserved_tokens={self.reserved_tokens}"
        )


@dataclass(slots=True)
class _Entry:
    """One reserved use of a window."""

    at: float
    reserved: float
    actual: float | None = None

    @property
    def effective(self) -> float:
        """Usage this entry contributes: the actual figure once known."""
        return self.reserved if self.actual is None else self.actual


@dataclass(slots=True)
class _Window:
    """Usage against one :class:`QuotaDimension`."""

    dimension: QuotaDimension
    entries: list[_Entry] = field(default_factory=list)

    def used(self, now: float) -> float:
        """Total usage inside the window containing ``now``.

        A rolling window counts everything within the trailing period. A fixed
        window counts only the current bucket, so its usage drops to zero the
        moment the boundary passes rather than decaying. That difference is
        real: at a boundary a request can be admitted immediately after one
        admitted a moment before.
        """
        if self.dimension.reset is WindowReset.FIXED:
            bucket = self._bucket(now)
            return sum(e.effective for e in self.entries if self._bucket(e.at) == bucket)

        window = self.dimension.window_seconds
        return sum(e.effective for e in self.entries if now - e.at < window)

    def has_room(self, now: float, additional: float) -> bool:
        """True when ``additional`` more can be consumed without exceeding the limit.

        Two regimes, because a fractional ceiling is a genuinely different
        constraint rather than a smaller version of the same one.

        When the limit can hold ``additional``, this is a token-bucket check: a
        full bucket permits a burst up to the limit and refills continuously, so
        summing usage inside the trailing window gives the same answer.

        When the limit is *smaller* than a single request -- Mistral's
        ``ministral-14b-2512`` permits 0.50 requests/second, meaning one request
        every two seconds -- the bucket can never hold a whole request, so
        summing would refuse every request forever. The real constraint is a
        minimum spacing of ``additional * window / limit`` seconds between
        requests, which is what a sub-one-per-second rate limit means in
        practice.

        That fallback is **request-rate semantics only**. A token dimension whose
        limit is smaller than a single request is a different situation: the
        budget genuinely cannot cover the request and no amount of waiting
        changes that, so it is refused. Applying spacing to tokens would let an
        over-budget request through after a pause.
        """
        limit = self.dimension.limit
        if additional <= limit:
            return self.used(now) + additional <= limit

        if not self.dimension.is_requests:
            return False

        window = self.dimension.window_seconds
        minimum_spacing = additional * window / limit
        # The lookback is the minimum spacing, which is longer than the window
        # whenever the limit is below one. Filtering by the window instead let a
        # request through at 1.9s under a 2s spacing: the only prior entry had
        # already aged out of the 1s window, so the check saw nothing to wait for.
        recent = [e.at for e in self.entries if now - e.at < minimum_spacing]
        if not recent:
            return True
        return (now - max(recent)) >= minimum_spacing

    def reserve(self, at: float, amount: float) -> _Entry:
        """Record a reservation and return its entry."""
        entry = _Entry(at=at, reserved=amount)
        self.entries.append(entry)
        return entry

    def prune(self, now: float) -> None:
        """Drop entries that can no longer affect any future window.

        Bounded rather than aggressive: the list is scanned only once it has
        grown past a threshold, because the usual case is a handful of live
        entries and pruning on every reservation would be wasted work on the
        latency-sensitive dispatch path.
        """
        if len(self.entries) < _PRUNE_THRESHOLD:
            return
        keep_from = now - self.dimension.window_seconds
        self.entries = [e for e in self.entries if e.at >= keep_from]

    def _bucket(self, at: float) -> int:
        return int(at // self.dimension.window_seconds)

    def bucket_of(self, at: float) -> int:
        """The bucket an instant falls in, for a fixed window.

        Public because persistence needs to reason about which bucket a
        restored reservation belongs to, and reimplementing the arithmetic
        outside this class is how the two would drift apart.
        """
        return self._bucket(at)

    def bucket_end(self, now: float) -> float:
        """The instant the bucket containing ``now`` ends, for a fixed window.

        This is the boundary the window *actually* honours. It is not a
        documented boundary it does not: Google's daily allowance resets at
        midnight Pacific, but nothing here knows about Pacific, so the honest
        thing to persist is the boundary the code uses.
        """
        return (self._bucket(now) + 1) * self.dimension.window_seconds


class QuotaTracker:
    """In-memory Level 1 quota accounting.

    Not thread-safe by design and not meant to be: it is guarded by a single
    ``asyncio.Lock`` so that check-and-reserve is atomic with respect to other
    coroutines. The lock is held only across in-memory updates and is released
    before anything awaits provider I/O (CONSTITUTION §8.3 forbids the database
    blocking the event loop; the same discipline applies to this lock).
    """

    def __init__(
        self,
        dimensions_by_model: Mapping[tuple[str, str], Sequence[QuotaDimension]],
        *,
        safety_margin: float = 1.1,
        output_token_assumption: int = DEFAULT_OUTPUT_TOKEN_ASSUMPTION,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Build a tracker over the configured dimensions.

        Args:
            dimensions_by_model: Quota dimensions keyed by ``(provider, model_id)``.
                The key is the **shared capacity unit**, not a credential -- see
                the module docstring.
            safety_margin: Multiplier applied to the token estimate before
                reserving. Must exceed 1.0; CONSTITUTION §8.2 gives
                ``estimated_tokens * 1.1`` as its example.
            clock: Monotonic time source, injected so tests do not sleep and so
                a future implementation is not tied to wall time.
            wall_clock: Wall-clock source, used only to translate instants to
                and from the durable form. Durations are always measured on the
                monotonic clock; the wall clock exists because a monotonic
                value is only meaningful against the current boot, so a
                persisted one would restore nonsense after a reboot.
            output_token_assumption: Tokens assumed for a response that has not
                been generated yet. The tracker adds it to the caller's *input*
                estimate, so the caller supplies the part it can see and the
                tracker owns the part that is policy.

        Raises:
            ValueError: If ``safety_margin`` is not greater than 1.0.
        """
        if safety_margin <= 1.0:
            raise ValueError(
                f"safety_margin must exceed 1.0, got {safety_margin}. A margin at or "
                "below 1 reserves less than the request will consume, which is the "
                "overshoot the reservation cycle exists to prevent (CONSTITUTION 8.2)."
            )
        self._dimensions = {
            key: [_Window(dimension) for dimension in dimensions]
            for key, dimensions in dimensions_by_model.items()
        }
        # Held as Decimal, not float. CONSTITUTION 8.2's example margin of 1.1 is
        # not representable in binary floating point, so 100 * 1.1 evaluates to
        # 110.00000000000001 and ceil() yields 111 -- one token of phantom
        # over-reservation, reproducible but for the wrong reason. Decimal keeps
        # the arithmetic exact so the configured margin is the applied margin.
        self._safety_margin = Decimal(str(safety_margin))
        self._output_token_assumption = max(0, output_token_assumption)
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = asyncio.Lock()
        self._next_identifier = 0
        self._pending: dict[int, list[tuple[_Window, _Entry]]] = {}

    def dimensions_for(self, provider: str, model_id: str) -> tuple[QuotaDimension, ...]:
        """The configured dimensions for a model, empty when none are declared."""
        return tuple(window.dimension for window in self._windows_for(provider, model_id))

    def available(self, provider: str, model_id: str) -> bool:
        """True when the model plausibly has room for another request.

        Advisory, and deliberately coarse. It asks whether each dimension can
        absorb the *smallest useful* dispatch -- one request and one token --
        rather than the cost of the request actually in hand, which is not known
        until :meth:`reserve`.

        The token check uses ``1.0``, not ``0.0``. Asking a token dimension
        whether it has room for zero tokens is true whenever the window is not
        over its limit, which made this report "available" while the budget was
        fully spent.

        It is a read without the lock, so a caller must still handle
        :meth:`reserve` returning ``None`` when concurrent requests took the
        remaining capacity between the check and the reservation.
        """
        now = self._clock()
        return all(window.has_room(now, 1.0) for window in self._windows_for(provider, model_id))

    async def reserve(
        self, provider: str, model_id: str, estimated_input_tokens: int
    ) -> Reservation | None:
        """Reserve capacity for one request, or return ``None`` if none is free.

        The caller supplies the **input** estimate, because that is the part it
        can see. The tracker adds the output assumption it holds, because that
        is the part that is policy, and applies the safety margin. Composing it
        here rather than in the caller means a caller cannot forget the output
        half the way it could forget the margin.

        Args:
            provider: The provider owning the capacity.
            model_id: The provider-native model id.
            estimated_input_tokens: The caller's estimate of the *request's* own
                tokens. The output assumption is added on top of it.

        Returns:
            The :class:`Reservation`, or ``None`` when a dimension is exhausted.
            ``None`` rather than an exception because the exhaustion exception
            (``NoAvailableCapacityError``) belongs to Batch 5, which attaches
            cooldown and failover semantics to it.
        """
        estimated_tokens = estimated_input_tokens + self._output_token_assumption
        reserved_tokens = max(
            1,
            int(
                (Decimal(estimated_tokens) * self._safety_margin).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        async with self._lock:
            now = self._clock()
            windows = self._windows_for(provider, model_id)

            for window in windows:
                additional = 1.0 if window.dimension.is_requests else float(reserved_tokens)
                if not window.has_room(now, additional):
                    return None

            identifier = self._next_identifier
            self._next_identifier += 1

            # One entry per window, all under a single identifier. Reconciliation
            # settles every entry for the reservation, so the request and token
            # dimensions cannot drift apart.
            entries: list[tuple[_Window, _Entry]] = []
            for window in windows:
                amount = 1.0 if window.dimension.is_requests else float(reserved_tokens)
                entries.append((window, window.reserve(now, amount)))
                window.prune(now)
            self._pending[identifier] = entries

        return Reservation(
            identifier=identifier,
            provider=provider,
            model_id=model_id,
            estimated_input_tokens=estimated_input_tokens,
            estimated_tokens=estimated_tokens,
            reserved_tokens=reserved_tokens,
            window_start=now,
        )

    async def reconcile(self, reservation: Reservation, actual_tokens: int | None) -> None:
        """Settle a reservation against what the provider reported.

        Args:
            reservation: The reservation returned by :meth:`reserve`.
            actual_tokens: Tokens the provider actually consumed, or ``None``
                when it reported no usage. ``None`` leaves the estimate standing
                rather than releasing capacity that may well have been used.
        """
        async with self._lock:
            entries = self._pending.get(reservation.identifier)
            if entries is None:
                # Already reconciled, or never reserved. Idempotent by design so
                # a retry of the reconcile step cannot double-count.
                return
            if actual_tokens is not None and self._is_plausible(reservation, actual_tokens):
                for window, entry in entries:
                    # Only the *token* dimension is corrected. A provider's
                    # token count says nothing about how many requests were
                    # made, and the request dimension is exact -- one dispatched
                    # attempt is one request, whatever the response contained.
                    # Overwriting it would turn one request into seven.
                    if not window.dimension.is_requests:
                        entry.actual = float(actual_tokens)
            del self._pending[reservation.identifier]

    def _is_plausible(self, reservation: Reservation, actual_tokens: int) -> bool:
        """True when a provider-reported figure looks like a per-request count.

        A cumulative counter reported as if it were per-request would inflate
        reconciliation without bound -- every request would add the running
        total to the window. A figure more than an order of magnitude above the
        reservation is not a per-request count, so the estimate stands.

        **This is a heuristic, and it is recorded as one.** The authoritative
        check on estimator drift is the console cross-check, which compares
        accumulated persisted counts against what the provider actually bills.
        This guard exists to stop one obviously-wrong figure from doing damage
        before that check runs, not to replace it.
        """
        if actual_tokens <= 0:
            return False
        return actual_tokens <= reservation.reserved_tokens * _IMPLAUSIBLE_ACTUAL_FACTOR

    def used(self, provider: str, model_id: str, resource: DimensionResource) -> float:
        """Current usage for one resource, for logging and diagnostics.

        Counts every configured dimension of that resource summed. Diagnostic
        only -- enforcement goes through :meth:`reserve`.
        """
        now = self._clock()
        return sum(
            window.used(now)
            for window in self._windows_for(provider, model_id)
            if window.dimension.resource is resource
        )

    def _windows_for(self, provider: str, model_id: str) -> list[_Window]:
        """Windows for a model, created empty on first touch.

        An unconfigured model has no windows, so it is always available. That is
        the deliberate encoding for an unverified limit: unenforced, not
        unlimited.
        """
        return self._dimensions.setdefault((provider, model_id), [])

    def export_windows(self) -> list[dict[str, Any]]:
        """Serialise the window state for persistence.

        The durable form is a list of one row per configured dimension, each
        carrying the reservations that window has recorded. A reservation keeps
        its *reserved* amount and its *reported* amount side by side, because
        collapsing them into one effective figure would make estimator drift
        unmeasurable after a restart -- and the console cross-check is the only
        independent check available.

        Timestamps are **wall-clock**, not the monotonic instants the window
        holds. A monotonic value is only meaningful against the current boot, so
        persisting one and restoring it after a reboot would restore nonsense.
        The conversion happens here, at the boundary, rather than leaking into
        the window logic.

        Returns:
            One row per configured dimension.
        """
        now = self._clock()
        offset = self._wall_clock() - now
        rows: list[dict[str, Any]] = []

        for (provider, model_id), windows in self._dimensions.items():
            for window in windows:
                dimension = window.dimension
                reset_at: str | None = None
                if dimension.reset is WindowReset.FIXED:
                    # The instant the *current* bucket ends, in wall-clock
                    # terms. This is the boundary the window actually uses, not
                    # a documented one it does not: the honest thing to persist
                    # is the boundary the code honours.
                    # Monotonic to wall-clock is an *addition*: the offset is
                    # wall_now - mono_now, so mono + offset is the wall
                    # instant. Subtracting it produced a boundary far in the
                    # past, which made every fixed window look already-reset
                    # and silently discarded its restored usage.
                    boundary = window.bucket_end(now) + offset
                    reset_at = to_iso(boundary)
                rows.append(
                    {
                        "provider": provider,
                        "model_id": model_id,
                        "resource": dimension.resource.value,
                        "window_seconds": dimension.window_seconds,
                        "reset_kind": dimension.reset.value,
                        "reset_at": reset_at,
                        "entries": [
                            {
                                "at": round(entry.at + offset, 6),
                                "reserved": entry.reserved,
                                "actual": entry.actual,
                            }
                            for entry in window.entries
                        ],
                        "flushed_at": to_iso(self._wall_clock()),
                    }
                )
        return rows

    def import_windows(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Restore window state from persisted rows.

        The *dimension* is taken from configuration, never from the row: the
        database records usage, not policy, and a limit that has changed must
        take effect. A row whose dimension is no longer configured is ignored,
        which is what ``spec.md`` requires -- state for a dimension that is
        gone is discarded, not applied to one that remains.

        Entries that fall outside the window they belong to are dropped rather
        than restored, because they cannot affect any future decision.

        Args:
            rows: Rows as produced by :meth:`export_windows`.

        Returns:
            The number of reservations restored.
        """
        now = self._clock()
        offset = self._wall_clock() - now
        by_key = {
            (
                row.get("provider"),
                row.get("model_id"),
                row.get("resource"),
                float(row.get("window_seconds", 0.0)),
            ): row
            for row in rows
        }

        restored = 0
        for (provider, model_id), windows in self._dimensions.items():
            for window in windows:
                dimension = window.dimension
                row = by_key.get(
                    (provider, model_id, dimension.resource.value, dimension.window_seconds)
                )
                if row is None:
                    continue

                if dimension.reset is WindowReset.FIXED:
                    reset_at = row.get("reset_at")
                    if not reset_at:
                        # No boundary recorded, so the current bucket's usage
                        # cannot be identified. Starting from zero is the safe
                        # direction: it may admit a request the limit has
                        # already refused, but it never refuses one it should
                        # admit.
                        continue
                    if self._wall_clock() >= from_iso(str(reset_at)):
                        # The bucket has rolled since the flush. The old usage
                        # belongs to a window that has reset.
                        continue

                for entry in row.get("entries") or []:
                    try:
                        wall_at = float(entry["at"])
                        reserved = float(entry["reserved"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    actual = entry.get("actual")
                    actual_value = None if actual is None else float(actual)
                    mono_at = wall_at - offset

                    if dimension.reset is WindowReset.FIXED:
                        if window.bucket_of(mono_at) != window.bucket_of(now):
                            continue
                    elif now - mono_at >= dimension.window_seconds:
                        continue

                    window.entries.append(
                        _Entry(at=mono_at, reserved=reserved, actual=actual_value)
                    )
                    restored += 1
        return restored
