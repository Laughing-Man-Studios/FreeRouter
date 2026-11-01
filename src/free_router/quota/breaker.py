"""Context-overload circuit breaker (CONSTITUTION §8.2).

> "If a provider returns a hard `400 Context Length Exceeded` error, the router
> must automatically mark the affected model as temporarily degraded or
> unavailable for large-context requests to protect remaining quota."

Three decisions here are load-bearing.

**Degradation is size-conditional, not total.** A model that rejects a 900k-token
prompt can still serve a 200-token one, and the point of the breaker is to
protect the remaining quota by routing small requests around the failure. A
breaker that disabled the model outright would satisfy the letter of the rule by
making the router useless.

**The threshold is learned from the failure, not from a configured context
window.** Neither provider publishes per-model context lengths (Issue #51), and
they change. Recording the size of the request that actually failed yields a
bound that is true by construction, and it only ever tightens: a later, smaller
failure lowers the threshold.

**The threshold is expressed in *our* estimated tokens, never the provider's.**
Google's error message carries the real counts — ``The input token count
(5911388) exceeds the maximum number of tokens allowed (1048576)`` — and using
those to compare against our own pre-dispatch estimate would compare two
different tokenizers. Our estimate is ~4 characters per token; Gemini's is not
that. The provider figure is recorded for diagnostics only, explicitly labelled
as a different scale.

**An unrecognised `400` does not trip the breaker.** Failing closed would mean
every malformed request degraded a working model; failing open means a shape we
have not seen is treated as M0 already treated it. Google's wording is the only
one matched, because it is the only one observed.

**State is in memory and will not survive a restart** (ADR-0004, a recorded
deviation from CONSTITUTION §8.3). A reader expecting persistence should look
here first.

This error is also **intermittent on Google's side**: the same model and project
have been observed serving larger requests seconds after a 32,768-token refusal,
which Google was investigating at the time of writing. That is why the breaker is
time-bounded and why it degrades *for large contexts* rather than disabling the
model — an intermittent backend limit must not cost permanent availability.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from free_router.core.timestamps import to_iso

__all__ = [
    "DEFAULT_BREAKER_SECONDS",
    "ContextBreaker",
    "ContextBreakerRecord",
    "ContextLengthInfo",
    "is_context_length_error",
]

DEFAULT_BREAKER_SECONDS = 300.0
"""How long a model stays degraded for large contexts.

Five minutes, deliberately short. §8.2 says *temporarily* degraded, and Google's
context refusals have been observed clearing on their own within seconds. A long
breaker would turn a transient backend limit into a sustained outage; a short one
costs at most a few rejected large requests while it is actually still failing.
"""


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContextLengthInfo:
    """What a provider's context-refusal told us.

    Attributes:
        attempted_tokens: The provider's own count of the rejected input.
        maximum_tokens: The ceiling the provider stated, when it stated one.
        message: The provider's message. **Never logged** — it can echo prompt
            content, so it is captured for classification only and callers must
            not propagate it (CONSTITUTION §9.1).
    """

    attempted_tokens: int | None
    maximum_tokens: int | None
    message: str


@dataclass(frozen=True, slots=True)
class ContextBreakerRecord:
    """A model degraded for requests at or above a learned size.

    Attributes:
        provider: Provider owning the model.
        model_id: The provider-native model id.
        threshold_tokens: Smallest request size known to fail, in **our** token
            estimate. ``None`` means nothing is currently blocked.
        until: Monotonic timestamp at which the breaker resets.
        provider_reported_max: The ceiling the provider stated, if any. Recorded
            for diagnostics only — it is a different tokenizer from ours and is
            never used in a comparison.
    """

    provider: str
    model_id: str
    threshold_tokens: int
    until: float
    provider_reported_max: int | None = None

    def describe(self) -> str:
        """Render for a log line. Carries no payload and no key material."""
        reported = (
            ""
            if self.provider_reported_max is None
            else f" provider_max={self.provider_reported_max}"
        )
        return f"model={self.model_id} threshold_tokens={self.threshold_tokens}{reported}"


class ContextBreaker:
    """Blocks requests at or above a learned failing size, temporarily."""

    def __init__(
        self,
        *,
        durations: dict[tuple[str, str], float] | None = None,
        default_duration: float = DEFAULT_BREAKER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        store: Any = None,
    ) -> None:
        """Build a breaker.

        Args:
            durations: Per-model degraded duration keyed by ``(provider, model_id)``.
            default_duration: Duration for a model with no configured value.
            clock: Monotonic time source, injected so tests do not sleep.
        """
        self._durations = dict(durations or {})
        self._default_duration = default_duration
        self._clock = clock
        self._wall_clock = wall_clock
        self._store = store
        self._records: dict[tuple[str, str], ContextBreakerRecord] = {}

    def configured_duration(self, provider: str, model_id: str) -> float:
        """The configured degraded duration for a model, or the default."""
        return self._durations.get((provider, model_id), self._default_duration)

    async def record_failure(
        self,
        provider: str,
        model_id: str,
        estimated_tokens: int,
        *,
        provider_reported_max: int | None = None,
        now: float | None = None,
    ) -> ContextBreakerRecord:
        """Degrade a model for requests at or above ``estimated_tokens``.

        The threshold only ever **tightens**. A later failure on a smaller
        request means the earlier bound was too generous, and using the smaller
        one is what keeps a 900k-token-only failure from blocking a 4k-token
        request that would also have failed.

        Args:
            provider: Provider owning the model.
            model_id: The provider-native model id.
            estimated_tokens: Our estimate of the rejected request's size.
            provider_reported_max: Ceiling the provider stated, for diagnostics.
            now: Override for the current time, for tests.

        Returns:
            The resulting :class:`ContextBreakerRecord`.
        """
        moment = self._clock() if now is None else now
        key = (provider, model_id)
        existing = self._active(provider, model_id)

        threshold = estimated_tokens
        if existing is not None and existing.threshold_tokens <= threshold:
            threshold = existing.threshold_tokens
            reported = existing.provider_reported_max
            if provider_reported_max is not None:
                reported = provider_reported_max
        else:
            reported = provider_reported_max

        record = ContextBreakerRecord(
            provider=provider,
            model_id=model_id,
            threshold_tokens=threshold,
            until=moment + self.configured_duration(provider, model_id),
            provider_reported_max=reported,
        )
        self._records[key] = record
        await self._persist(provider, model_id, record)
        return record

    def blocks(self, provider: str, model_id: str, estimated_tokens: int) -> bool:
        """True when this request is too large for a degraded model.

        Requests *below* the threshold are always admitted: the model has only
        been shown to fail at larger sizes, and blocking small requests would
        make the breaker cost more quota than it protects.
        """
        record = self._active(provider, model_id)
        if record is None:
            return False
        return estimated_tokens >= record.threshold_tokens

    def is_degraded(self, provider: str, model_id: str) -> bool:
        """True when the model is currently degraded for large contexts."""
        return self._active(provider, model_id) is not None

    def threshold(self, provider: str, model_id: str) -> int | None:
        """The learned failing size, or ``None`` when not degraded."""
        record = self._active(provider, model_id)
        return None if record is None else record.threshold_tokens

    def remaining(self, provider: str, model_id: str) -> float:
        """Seconds until the breaker resets, or ``0.0`` when healthy."""
        record = self._active(provider, model_id)
        if record is None:
            return 0.0
        return max(0.0, record.until - self._clock())

    def record(self, provider: str, model_id: str) -> ContextBreakerRecord | None:
        """The active record, or ``None``. Diagnostic only."""
        return self._active(provider, model_id)

    def clear(self, provider: str, model_id: str) -> None:
        """Clear the breaker, returning the model to full availability."""
        self._records.pop((provider, model_id), None)

    def _active(self, provider: str, model_id: str) -> ContextBreakerRecord | None:
        """The record if still in force, expiring and removing it if elapsed."""
        key = (provider, model_id)
        record = self._records.get(key)
        if record is None:
            return None
        if record.until <= self._clock():
            del self._records[key]
            return None
        return record

    # Observed Google messages. Matched as whole phrases rather than loose keywords,
    # so a prompt that happens to contain "token" cannot degrade a model.
    #
    #   "The input token count (5911388) exceeds the maximum number of tokens
    #    allowed (1048576)."
    #   "Unable to submit request because the input token count is 40000 but model
    #    only supports up to 32768"
    #
    # Mistral's wording has not been observed and is deliberately NOT guessed. An
    # unrecognised body leaves the breaker closed, which is M0's existing behaviour.

    def export_status(self) -> list[dict[str, Any]]:
        """Serialise the breaker's state for persistence.

        Only a *degraded* model is written: the absence of a row means healthy,
        which is the common case and does not deserve a row per model.
        """
        rows: list[dict[str, Any]] = []
        for (provider, model_id), record in self._records.items():
            rows.append(
                {
                    "provider": provider,
                    "model_id": model_id,
                    "status": "degraded",
                    "detail": record.provider_reported_max is not None
                    and f"provider_max={record.provider_reported_max}"
                    or None,
                    "threshold_tokens": record.threshold_tokens,
                    "updated_at": to_iso(self._wall_clock()),
                }
            )
        return rows

    def import_status(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Restore degraded models from persisted rows.

        A row that is not degraded carries no threshold and so nothing to
        restore; an unhealthy row is recorded for inspection but does not block
        requests, because the breaker blocks by *size* and a model with no
        threshold is not degraded for any size.
        """
        now = self._clock()
        restored = 0
        for row in rows:
            provider = row.get("provider")
            model_id = row.get("model_id")
            threshold = row.get("threshold_tokens")
            if not provider or not model_id or threshold is None:
                continue
            record = ContextBreakerRecord(
                provider=str(provider),
                model_id=str(model_id),
                threshold_tokens=int(threshold),
                until=now + self.configured_duration(str(provider), str(model_id)),
                provider_reported_max=None,
            )
            self._records[(provider, model_id)] = record
            restored += 1
        return restored

    async def _persist(self, provider: str, model_id: str, record: ContextBreakerRecord) -> None:
        """Write the degraded status through.

        A degraded model that is forgotten on restart would keep serving
        requests it has already been shown to fail, which spends quota to
        rediscover a limit the router already knows.
        """
        if self._store is None:
            return
        try:
            await self._store.save_model_status(
                {
                    "provider": provider,
                    "model_id": model_id,
                    "status": "degraded",
                    "detail": (
                        f"provider_max={record.provider_reported_max}"
                        if record.provider_reported_max is not None
                        else None
                    ),
                    "threshold_tokens": record.threshold_tokens,
                    "updated_at": to_iso(self._wall_clock()),
                }
            )
        except Exception as exc:
            logger.warning(
                "model_status_persist_failed",
                extra={
                    "event": "model_status_persist_failed",
                    "provider": provider,
                    "model_id": model_id,
                    "error_category": type(exc).__name__,
                },
            )


_CONTEXT_LENGTH_PHRASES = (
    "exceeds the maximum number of tokens allowed",
    "model only supports up to",
)

_TOKEN_COUNT = re.compile(r"token count[^0-9]*([0-9][0-9,]*)")
_MAXIMUM_TOKENS = re.compile(r"(?:allowed|supports up to)\s*\(?([0-9][0-9,]*)\)?", re.IGNORECASE)


def _to_int(raw: str) -> int:
    return int(raw.replace(",", ""))


def is_context_length_error(body: Any) -> ContextLengthInfo | None:
    """Classify a provider error body as a context-length refusal.

    Args:
        body: A decoded JSON error body, or ``None``.

    Returns:
        A :class:`ContextLengthInfo` when the body is a recognised context
        refusal, otherwise ``None`. The caller must then treat the ``400``
        exactly as M0 did, rather than degrading the model.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    message = str(error.get("message", ""))

    if not any(phrase in message for phrase in _CONTEXT_LENGTH_PHRASES):
        return None

    attempted = _TOKEN_COUNT.search(message)
    maximum = _MAXIMUM_TOKENS.search(message)
    return ContextLengthInfo(
        attempted_tokens=_to_int(attempted.group(1)) if attempted else None,
        maximum_tokens=_to_int(maximum.group(1)) if maximum else None,
        message=message,
    )
