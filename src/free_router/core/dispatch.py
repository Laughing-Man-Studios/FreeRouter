"""Dispatch orchestration: capacity gates, retry, and key failover.

This module owns the loop between "a request arrived" and "here is a completion".
It is the first place every piece of M1's capacity machinery meets: the key
pool, the quota tracker, the cooldown manager, and the circuit breaker.

Four decisions shape it, and three of them correct something a more obvious
implementation would get wrong.

**A rate limit stops the request. It does not fail over.** On a ``429`` the
cooldown manager marks the whole ``(provider, model)`` capacity unit, and the
next loop iteration sees that and refuses to dispatch. Retry-to-a-different-key
after a rate limit is therefore impossible *structurally* rather than by a guard
someone can forget. This is the direct consequence of Issue #51: Google's limits
are per project, so a second key draws on the same exhausted bucket, and
dispatching to it would burn another request to learn nothing. CONSTITUTION §6.1
forbids retrying into a rate limit, and a bypass guard is not enforcement.

**Transient faults are the only thing retry recovers.** A 5xx or a timeout is an
upstream problem that a second attempt can genuinely fix, so those retry within
a budget. A 4xx the provider will never accept and an authentication failure
will not fix itself are terminal for the *attempt*; the loop moves on, and
exhausting it produces a distinct error rather than a retried request.

**Every dispatched attempt consumes quota, including the ones that fail.** The
request reached the provider, so it counts against the per-minute and per-day
ceilings regardless of what came back. A reservation is therefore reconciled in
every terminal path, never released on the assumption that a failed call was
free. Assuming otherwise would let a run of 5xx quietly drain a 500-per-day
budget.

**Failover is credential-only, never model.** CONSTITUTION §7.2 forbids
substituting a different model for an explicitly requested one, so the loop
holds the model fixed for its entire life. A request for a Google model that
fails does not become a Mistral request; it becomes a ``503`` or a ``502``.

The context-overload circuit breaker is consulted before dispatch, not after: a
request that is known to be too large for a degraded model is refused without
consuming a provider call, which is the point of the breaker (CONSTITUTION §8.2).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from free_router.core.exceptions import (
    NoAvailableCapacityError,
    PersistenceUnavailableError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
    RetriesExhaustedError,
    RouterBaseError,
)
from free_router.core.normalization import NormalizedRequest, NormalizedResponse
from free_router.keys.manager import KeyManager
from free_router.providers.base import build_adapter
from free_router.quota.breaker import ContextBreaker
from free_router.quota.cooldown import CooldownManager
from free_router.quota.estimation import estimate_input_tokens
from free_router.quota.tracker import QuotaTracker

__all__ = ["DispatchOutcome", "Dispatcher"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What a dispatch cost, for logging and for the milestone measurement.

    Attributes:
        response: The successful completion.
        attempts: Total dispatches made, including the successful one.
        key_aliases: The credentials used, in order. Aliases only, never material
            (CONSTITUTION §9.1).
        failovers: How many times a different credential was tried.
    """

    response: NormalizedResponse
    attempts: int
    key_aliases: tuple[str, ...]
    failovers: int


class Dispatcher:
    """Runs one request to completion against a single provider and model."""

    def __init__(
        self,
        *,
        key_manager: KeyManager,
        quota: QuotaTracker,
        cooldown: CooldownManager,
        breaker: ContextBreaker,
        http_client: httpx.AsyncClient,
        max_attempts: int = 2,
        attempt_budget_seconds: float = 10.0,
        backoff_seconds: float = 0.2,
    ) -> None:
        """Wire the dispatcher's collaborators.

        Args:
            key_manager: Credential pool and LRU selection.
            quota: Reservation cycle counters.
            cooldown: Rate-limit windows, keyed on ``(provider, model)``.
            breaker: Context-overload circuit breaker.
            http_client: Shared client passed to the adapter.
            max_attempts: Total dispatches permitted, including the first.
            attempt_budget_seconds: Wall-clock ceiling for the whole request.
            backoff_seconds: Delay before retrying a transient fault. Never
                applied to a rate limit.
        """
        self._keys = key_manager
        self._quota = quota
        self._cooldown = cooldown
        self._breaker = breaker
        self._http_client = http_client
        self._max_attempts = max_attempts
        self._attempt_budget = attempt_budget_seconds
        self._backoff = backoff_seconds

    @property
    def quota(self) -> QuotaTracker:
        """The quota tracker. Read-only, for the persistence layer."""
        return self._quota

    @property
    def cooldown(self) -> CooldownManager:
        """The cooldown manager. Read-only, for the persistence layer."""
        return self._cooldown

    @property
    def breaker(self) -> ContextBreaker:
        """The context breaker. Read-only, for the persistence layer."""
        return self._breaker

    @property
    def key_manager(self) -> KeyManager:
        """The credential pool. Read-only, for the persistence layer."""
        return self._keys

    async def dispatch(
        self, request: NormalizedRequest, provider: str, model_id: str
    ) -> DispatchOutcome:
        """Dispatch one request, retrying transient faults and failing over keys.

        Args:
            request: The normalised request.
            provider: The configured provider.
            model_id: The provider-native model id.

        Returns:
            A :class:`DispatchOutcome` describing the successful completion.

        Raises:
            NoAvailableCapacityError: If the model is cooling, degraded for a
                request of this size, out of quota, or has no eligible credential.
                Nothing was dispatched in that case.
            ProviderRateLimitError: If the provider rate limited and there is no
                capacity to move to.
            ProviderAuthError: If every credential was rejected.
            ProviderValidationError: If the provider rejected the payload. Never
                retried.
            RetriesExhaustedError: If attempts were made and all failed.
        """
        # The *input* estimate. The tracker adds the output assumption and
        # the safety margin; the breaker compares against this figure,
        # because a context-length refusal is about the prompt.
        estimated = estimate_input_tokens(request)
        aliases: list[str] = []
        failovers = 0
        last_error: RouterBaseError | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._attempt_budget

        for attempt in range(1, self._max_attempts + 1):
            # --- Capacity gates, before anything is sent. -------------------
            self._check_capacity(provider, model_id, estimated)

            if loop.time() >= deadline:
                break

            selection = await self._keys.select(provider, model_id)
            if selection.key is None:
                # Every credential is suspended. If a provider error started
                # this, that error is the honest answer -- an upstream that
                # rejected every credential we hold is an auth problem, not a
                # capacity problem, and 503 would misattribute it.
                if last_error is not None:
                    raise last_error
                raise NoAvailableCapacityError(
                    f"No eligible credential is available for model '{model_id}'."
                )

            key = selection.key
            aliases.append(key.alias)
            reservation = await self._quota.reserve(provider, model_id, estimated)
            if reservation is None:
                if last_error is not None:
                    raise RetriesExhaustedError(
                        "Quota was exhausted while retrying after an upstream fault."
                    ) from last_error
                raise NoAvailableCapacityError(f"No quota remains for model '{model_id}'.")

            logger.info(
                "provider_dispatched",
                extra={
                    "event": "provider_dispatched",
                    "key_alias": key.alias,
                    "selection_reason": selection.reason.value,
                    "attempt": attempt,
                },
            )

            try:
                adapter = build_adapter(provider, self._http_client)
                response = await adapter.chat_completion(request, model_id, key)
            except ProviderRateLimitError as exc:
                # The request was sent, so its quota is spent regardless.
                await self._quota.reconcile(reservation, None)
                await self._cooldown.enter(
                    provider, model_id, provider_hint=exc.retry_after, reason="rate_limited"
                )
                last_error = exc
                logger.info(
                    "provider_rate_limited",
                    extra={
                        "event": "provider_rate_limited",
                        "key_alias": key.alias,
                        "attempt": attempt,
                        "retry_after": exc.retry_after,
                    },
                )
                # No failover, no retry. The capacity unit is now cooling, and the
                # next iteration's gate refuses to dispatch. See the module
                # docstring for why a second key would learn nothing.
                raise

            except ProviderAuthError as exc:
                # The credential itself is dead. Suspend it so later requests do
                # not walk into the same wall; the model may still be fine on a
                # different credential.
                await self._quota.reconcile(reservation, None)
                await self._keys.suspend(provider, model_id, key.alias)
                last_error = exc
                failovers += 1
                logger.info(
                    "credential_suspended",
                    extra={
                        "event": "credential_suspended",
                        "key_alias": key.alias,
                        "attempt": attempt,
                    },
                )
                continue

            except ProviderValidationError as exc:
                # Never retried: the provider will not accept this payload, and a
                # second attempt would fail identically. The reservation is
                # reconciled because the call did consume quota.
                await self._quota.reconcile(reservation, None)
                if exc.context_length:
                    # CONSTITUTION 8.2: degrade the model for requests of this
                    # size rather than rediscovering the limit on every one of
                    # them. Recorded in OUR token estimate, not the provider's
                    # count -- different tokenizers are not comparable.
                    await self._breaker.record_failure(provider, model_id, estimated)
                    logger.info(
                        "model_degraded_for_large_context",
                        extra={
                            "event": "model_degraded_for_large_context",
                            "key_alias": key.alias,
                            "attempt": attempt,
                            "estimated_tokens": estimated,
                        },
                    )
                raise

            except (ProviderServerError, ProviderTimeoutError) as exc:
                await self._quota.reconcile(reservation, None)
                last_error = exc
                failovers += 1
                logger.info(
                    "provider_transient_fault",
                    extra={
                        "event": "provider_transient_fault",
                        "key_alias": key.alias,
                        "attempt": attempt,
                        "error_category": type(exc).__name__,
                    },
                )
                if self._backoff and attempt < self._max_attempts:
                    await asyncio.sleep(self._backoff)
                continue

            await self._quota.reconcile(reservation, _usage_from(response))
            await self._keys.mark_used(key)
            logger.info(
                "provider_responded",
                extra={"event": "provider_responded", "key_alias": key.alias, "attempt": attempt},
            )
            return DispatchOutcome(
                response=response,
                attempts=attempt,
                key_aliases=tuple(aliases),
                failovers=failovers,
            )

        # The exit reason determines the status, and spec "Error Behaviour" draws
        # a distinction that matters: a provider fault that survived its retries
        # is reported as *that fault* (5xx -> 502, timeout -> 504, auth -> 401),
        # because the client needs to know the upstream failed. `retries_exhausted`
        # is reserved for the case where nothing was ever dispatched -- the
        # attempt budget ran out before a fault -- which is a 503 capacity signal.
        if last_error is not None:
            raise last_error
        raise RetriesExhaustedError(
            f"The {self._attempt_budget}s attempt budget elapsed before a dispatch "
            f"could be made for model '{model_id}'."
        )

    def _check_capacity(self, provider: str, model_id: str, estimated_tokens: int) -> None:
        """Refuse to dispatch when no capacity is available.

        Checked once per attempt and entirely in memory, so a request that is
        refused on capacity grounds **never reaches the provider** and never
        consumes quota.

        Raises:
            NoAvailableCapacityError: If the model is cooling, degraded for a
                request this size, or out of quota.
            ProviderRateLimitError: If a rate limit is in force, so the client is
                told ``429`` rather than a generic ``503``.
        """
        if self._cooldown.persistence_degraded(provider, model_id):
            # The router knows a limit on this unit and could not record it, so
            # it cannot promise to remember it. Dispatching anyway would mean a
            # restart forgets a limit it was told about -- the overshoot
            # CONSTITUTION 8.3 exists to prevent.
            raise PersistenceUnavailableError(
                f"Model '{model_id}' cannot be served: its rate limit could not be "
                f"recorded, so the router cannot guarantee to respect it."
            )

        if self._cooldown.is_cooling(provider, model_id):
            remaining = round(self._cooldown.remaining(provider, model_id), 3)
            if remaining > 0:
                raise ProviderRateLimitError(
                    f"Model '{model_id}' is rate limited for another {remaining}s."
                )
            raise NoAvailableCapacityError(f"Model '{model_id}' has no capacity available.")

        if self._breaker.blocks(provider, model_id, estimated_tokens):
            raise NoAvailableCapacityError(
                f"Model '{model_id}' is temporarily degraded for requests of this size "
                "after a context-length rejection (CONSTITUTION 8.2)."
            )

        if not self._quota.available(provider, model_id):
            raise NoAvailableCapacityError(f"No quota remains for model '{model_id}'.")


def _usage_from(response: NormalizedResponse) -> int | None:
    """Token usage reported alongside a completion.

    The provider's figure is authoritative when it reports one, and the
    reservation is settled against it.

    ``None`` when the provider reported no usage, and that is the conservative
    direction: the estimate stands rather than capacity being released on the
    assumption it was unused. Every dispatched attempt consumes quota whether or
    not it produced output.
    """
    if response.usage is None:
        return None
    return response.usage.resolved_total()
