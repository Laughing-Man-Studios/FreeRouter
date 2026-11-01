"""Cooldown state: what to do after a provider says "no" (CONSTITUTION §6.1).

CONSTITUTION §6.1 requires that retrying into a rate limit is prohibited. That
rule is the reason this module exists, and it shapes every decision in it.

**Cooldown is keyed on the shared capacity unit, not on a key.** Issue #51
established that Google applies limits per *project*, so cooling one key just
moves the same request to a sibling key drawing on the same exhausted bucket.
Google's own quota identifier confirms the granularity:
``GenerateRequestsPerMinutePerProjectPerModel``, with dimensions
``{location: 'global', model: ...}``. So a rate limit is a property of
``(provider, model)`` and is recorded there.

**The wait is the longest applicable hint, never the shortest.** A provider that
says "retry in 16s" is authoritative about its own window, so that is honoured.
A configured minimum is honoured too. Neither is a licence to retry earlier than
the other, so the effective wait is the maximum of the two. Taking the minimum
would be the "retry aggressively and hope" behaviour §6.1 exists to prevent.

**A rate limit is not a transient blip.** There is no exponential backoff here
and no jitter. Backing off and retrying the same bucket is retrying into a rate
limit with extra steps; the correct response is to stop issuing and either wait
out the window or move to capacity that is not in one.

**State is in memory and will not survive a restart** (ADR-0004, a recorded
deviation from CONSTITUTION §8.3). After a restart every cooldown is forgotten
and a key that was cooling may be dispatched immediately. A reader expecting
persistence should look here first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

from free_router.core.timestamps import from_iso, to_iso

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "CooldownManager",
    "CooldownRecord",
    "CooldownStatus",
    "parse_retry_after_seconds",
    "parse_retry_delay_seconds",
]

DEFAULT_COOLDOWN_SECONDS = 60.0
"""Fallback wait when a provider gives no hint and none is configured.

Sixty seconds because it spans the shortest window either provider documents
without assuming one: Mistral's per-minute ceilings and Google's per-minute
metric both roll over within a minute. A shorter default would re-dispatch into
a window that has not reset.
"""


logger = logging.getLogger(__name__)

DEFAULT_MAX_COOLDOWN_SECONDS = 86_400.0
"""Upper bound on a single cooldown, in seconds (clarification B4).

Twenty-four hours accommodates the longest *legitimate* provider hint --
a daily cap resetting at midnight -- while still bounding a lockout caused
by a clock that jumped backwards. A backwards jump is better detected than
patched: restoring a cooldown compares it against the system clock, and this
is the backstop for the case that check cannot cover.
"""


class CooldownStatus(StrEnum):
    """Where a capacity unit stands right now."""

    AVAILABLE = "available"
    COOLING = "cooling"
    """Rate limited; must not be dispatched until the window resets."""


@dataclass(frozen=True, slots=True)
class CooldownRecord:
    """A recorded cooldown.

    Attributes:
        provider: Provider owning the capacity.
        model_id: The provider-native model id.
        until: Monotonic timestamp at which the cooldown expires.
        reason: A short machine-readable code, never provider-supplied text.
            Provider error bodies can echo prompt content, so nothing from a
            response body may reach a log line (CONSTITUTION §9.1).
        source: What the duration came from, for diagnostics.
    """

    provider: str
    model_id: str
    until: float
    reason: str
    source: str
    provider_hint: float | None = None
    """Seconds the provider asked us to wait, if it said.

    Kept because it is the provider's own answer about how long the limit
    lasts, and persisting only our derived wait loses it.
    """

    def describe(self) -> str:
        """Render for a log line. Carries no payload and no key material."""
        return f"model={self.model_id} reason={self.reason} source={self.source}"


class CooldownManager:
    """Records and expires per-``(provider, model)`` cooldowns."""

    def __init__(
        self,
        *,
        durations: dict[tuple[str, str], float] | None = None,
        default_duration: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        store: Any = None,
        max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS,
    ) -> None:
        """Build a manager over per-model configured durations.

        Args:
            durations: Configured wait in seconds, keyed by ``(provider, model_id)``.
                Per provider/model rather than global, so a provider with a long
                reset does not inherit a short default from another.
            default_duration: Wait for a model with no configured duration.
            clock: Monotonic time source, injected so tests do not sleep.
        """
        self._durations = dict(durations or {})
        self._default_duration = default_duration
        self._clock = clock
        self._wall_clock = wall_clock
        self._store = store
        self._max_cooldown_seconds = max_cooldown_seconds
        self._records: dict[tuple[str, str], CooldownRecord] = {}
        self._degraded: dict[tuple[str, str], str] = {}

    def configured_duration(self, provider: str, model_id: str) -> float:
        """The configured wait for a model, or the default."""
        return self._durations.get((provider, model_id), self._default_duration)

    async def enter(
        self,
        provider: str,
        model_id: str,
        *,
        provider_hint: float | None = None,
        reason: str = "rate_limited",
        now: float | None = None,
    ) -> CooldownRecord:
        """Place a model into cooldown.

        The effective wait is the **maximum** of the provider's hint and the
        configured duration, never the minimum. See the module docstring.

        Args:
            provider: Provider owning the capacity.
            model_id: The provider-native model id.
            provider_hint: Seconds the provider asked us to wait, if it said.
            reason: Short machine-readable cause, e.g. ``rate_limited``.
            now: Override for the current time, for tests.

        Returns:
            The recorded :class:`CooldownRecord`.
        """
        moment = self._clock() if now is None else now
        configured = self.configured_duration(provider, model_id)

        if provider_hint is None:
            wait = configured
            source = "configured"
        else:
            wait = max(configured, provider_hint)
            source = "provider_hint" if provider_hint >= configured else "configured"

        record = CooldownRecord(
            provider=provider,
            model_id=model_id,
            until=moment + wait,
            reason=reason,
            source=source,
            provider_hint=provider_hint,
        )
        self._records[(provider, model_id)] = record
        await self._persist(provider, model_id, record)
        return record

    async def enter_until(
        self,
        provider: str,
        model_id: str,
        until: float,
        *,
        reason: str = "window_exhausted",
    ) -> CooldownRecord:
        """Place a model into cooldown until a specific monotonic time.

        Used when a window is known to roll over later than any duration-based
        wait would cover -- a daily budget exhausted at 23:50 must not be
        retried in sixty seconds.

        Args:
            provider: Provider owning the capacity.
            model_id: The provider-native model id.
            until: Monotonic timestamp at which the cooldown expires. A value in
                the past yields an already-expired cooldown, which reads as
                "the window has already reset".
            reason: Short machine-readable cause.

        Returns:
            The recorded :class:`CooldownRecord`.
        """
        record = CooldownRecord(
            provider=provider,
            model_id=model_id,
            until=until,
            reason=reason,
            source="window",
        )
        self._records[(provider, model_id)] = record
        await self._persist(provider, model_id, record)
        return record

    def status(self, provider: str, model_id: str) -> CooldownStatus:
        """Current status, expiring any elapsed cooldown as a side effect."""
        record = self._active(provider, model_id)
        return CooldownStatus.COOLING if record is not None else CooldownStatus.AVAILABLE

    def is_cooling(self, provider: str, model_id: str) -> bool:
        """True when the model must not be dispatched.

        The single gate CONSTITUTION §6.1 relies on. The dispatch path must
        consult this and treat ``True`` as a hard stop, not a warning.
        """
        return self._active(provider, model_id) is not None

    def remaining(self, provider: str, model_id: str) -> float:
        """Seconds until the cooldown expires, or ``0.0`` when available."""
        record = self._active(provider, model_id)
        if record is None:
            return 0.0
        return max(0.0, record.until - self._clock())

    def record(self, provider: str, model_id: str) -> CooldownRecord | None:
        """The active record, or ``None``. Diagnostic only."""
        return self._active(provider, model_id)

    async def clear(self, provider: str, model_id: str) -> None:
        """Drop any cooldown, returning the model to available.

        Exists for operator recovery and for M2, where cooldown state becomes
        persisted and therefore needs a way to be invalidated deliberately. The
        removal is written through, so a deliberate clearing is not undone by
        the next restore.
        """
        self._records.pop((provider, model_id), None)
        self._degraded.pop((provider, model_id), None)
        if self._store is not None:
            try:
                await self._store.save_cooldown(
                    {
                        "provider": provider,
                        "model_id": model_id,
                        "cooldown_until": None,
                        "reason": None,
                        "retry_after": None,
                        "updated_at": to_iso(self._wall_clock()),
                    }
                )
            except Exception as exc:
                self._degrade(provider, model_id, exc)

    def _active(self, provider: str, model_id: str) -> CooldownRecord | None:
        """The record if still in force, expiring and removing it if elapsed.

        Expiry is evaluated lazily on read rather than by a timer: a background
        task would be a second source of truth about the same state and would
        have to be cancelled on shutdown.
        """
        key = (provider, model_id)
        record = self._records.get(key)
        if record is None:
            return None
        if record.until <= self._clock():
            del self._records[key]
            return None
        return record

    def persistence_degraded(self, provider: str, model_id: str) -> bool:
        """True when a limit on this unit could not be recorded durably.

        The in-memory cooldown still refuses dispatch, so the unit is not
        serving traffic while the cooldown holds. The degradation matters for
        the moment the cooldown *expires*: by then the limit has reset, but if
        the record of it was lost the router would not know that, and a restart
        in between would forget a limit it had been told about.
        """
        return (provider, model_id) in self._degraded

    def degradation_reason(self, provider: str, model_id: str) -> str | None:
        """Why the unit is degraded, or ``None``."""
        return self._degraded.get((provider, model_id))

    def export_cooldowns(self) -> list[dict[str, Any]]:
        """Serialise the active cooldowns for persistence.

        Timestamps are wall-clock: a monotonic value is only meaningful against
        the current boot, and persisting one would restore nonsense after a
        reboot.
        """
        now = self._clock()
        offset = self._wall_clock() - now
        rows: list[dict[str, Any]] = []
        for (provider, model_id), record in self._records.items():
            rows.append(
                {
                    "provider": provider,
                    "model_id": model_id,
                    "cooldown_until": to_iso(record.until + offset),
                    "reason": record.reason,
                    "retry_after": record.provider_hint,
                    "updated_at": to_iso(self._wall_clock()),
                }
            )
        return rows

    def import_cooldowns(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Restore cooldowns from persisted rows.

        An **expired** cooldown is not restored. A cooldown is time-bounded, so
        restoring one that has already elapsed would refuse capacity that is
        actually free.

        A ``cooldown_until`` in the future is restored, because that is exactly
        the case CONSTITUTION 8.3 names: a restart must not retry a key that is
        cooling. A value absurdly far in the future is clamped by the maximum
        cooldown, which bounds a lockout caused by a clock that jumped
        backwards.
        """
        now = self._clock()
        offset = self._wall_clock() - now
        restored = 0
        for row in rows:
            provider = row.get("provider")
            model_id = row.get("model_id")
            until_text = row.get("cooldown_until")
            if not provider or not model_id or not until_text:
                continue
            try:
                until = from_iso(str(until_text)) - offset
            except ValueError:
                continue
            if until <= now:
                continue
            if until - now > self._max_cooldown_seconds:
                until = now + self._max_cooldown_seconds
            self._records[(provider, model_id)] = CooldownRecord(
                provider=str(provider),
                model_id=str(model_id),
                until=until,
                reason=str(row.get("reason") or "restored"),
                source="restored",
                provider_hint=row.get("retry_after"),
            )
            restored += 1
        return restored

    async def _persist(self, provider: str, model_id: str, record: CooldownRecord) -> None:
        """Write a cooldown through, immediately.

        A cooldown is the one piece of state that cannot wait for a flush: a
        forgotten cooldown makes the router contact a provider that has just
        refused it, which spends quota to learn nothing.
        """
        if self._store is None:
            return
        offset = self._wall_clock() - self._clock()
        try:
            await self._store.save_cooldown(
                {
                    "provider": provider,
                    "model_id": model_id,
                    "cooldown_until": to_iso(record.until + offset),
                    "reason": record.reason,
                    "retry_after": record.provider_hint,
                    "updated_at": to_iso(self._wall_clock()),
                }
            )
        except Exception as exc:
            self._degrade(provider, model_id, exc)
        else:
            self._degraded.pop((provider, model_id), None)

    def _degrade(self, provider: str, model_id: str, exc: Exception) -> None:
        self._degraded[(provider, model_id)] = type(exc).__name__
        logger.warning(
            "cooldown_persist_failed",
            extra={
                "event": "cooldown_persist_failed",
                "provider": provider,
                "model_id": model_id,
                "error_category": type(exc).__name__,
            },
        )


def parse_retry_after_seconds(headers: Any) -> float | None:
    """Read a standard ``Retry-After`` header.

    Handles both documented forms: a number of seconds, and an HTTP date. An
    HTTP-date form is interpreted against the current wall clock, which is why
    this takes the response headers rather than being given a duration.

    Args:
        headers: Anything with a ``get`` method, i.e. httpx headers.

    Returns:
        Seconds to wait, or ``None`` when the header is absent or unparseable.
        An unparseable value yields ``None`` rather than a guess: falling back
        to the configured duration is safe, inventing a number is not.
    """
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(str(raw).strip())
    except (TypeError, ValueError):
        return None

    now = datetime.now(UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - now).total_seconds())


def parse_retry_delay_seconds(body: Any) -> float | None:
    """Read Google's ``google.rpc.RetryInfo`` delay from an error body.

    Google's quota errors carry the provider's own answer in
    ``error.details``::

        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "16s"}

    The value is a protobuf Duration in JSON form -- seconds with an optional
    fraction -- so ``45.06576809s`` is a real observed figure.

    Args:
        body: A decoded JSON body, or ``None``.

    Returns:
        Seconds to wait, or ``None`` when absent or unparseable.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    details = error.get("details")
    if not isinstance(details, list):
        return None

    for detail in details:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("RetryInfo"):
            continue
        return _parse_duration_seconds(detail.get("retryDelay"))
    return None


def _parse_duration_seconds(raw: Any) -> float | None:
    """Parse a protobuf Duration JSON string such as ``'16s'`` or ``'1.5s'``."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return max(0.0, value)
