"""The streaming dispatch path: capacity, retry, and fail-fast.

This module is the streaming counterpart to :mod:`free_router.core.dispatch`, and
it exists *because* the dispatcher must not change (A2). The streaming path
re-implements model resolution, key eligibility, capacity gating and the bounded
retry budget, identically, and adds the one thing only the route layer can know:
whether anything has reached the client yet.

**Why the duplication is acceptable and bounded.** The dispatcher is the most
safety-critical component in the router and the one M1 measured at 100/100.
Streaming does not need a different dispatch policy -- it needs the *same* policy
with a different response shape -- so the copy is mechanical, not a second design.
It is kept honest by tests that drive both paths through the same capacity
conditions and require the same error type.

**Fail-fast is structural, not a guard.** The route consumes the adapter's first
chunk *before* it returns a streaming response. Until that chunk arrives nothing
has been written, so a failure can still become a retry or a JSON error. After it
arrives the response is committed, and the only remaining option is to end the
stream. There is no code path that could try a second provider after delivery,
which is what CONSTITUTION §7.2 and ADR-0007 require: the pair ``(provider,
model)`` is fixed for the whole call and never varies.

**A zero-chunk stream is not a delivered response.** Gemini and Mistral can both
answer ``200`` and then send nothing usable. That is treated as a transient fault
and retried inside the existing budget, because no client has seen anything yet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from free_router.api.streaming import StreamRenderer
from free_router.core.config import DispatchConfig
from free_router.core.dispatch import Dispatcher
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
    StreamingUnsupportedError,
)
from free_router.core.logging import log_context
from free_router.core.normalization import NormalizedChunk, NormalizedRequest
from free_router.keys.manager import KeyRef
from free_router.providers.base import build_adapter
from free_router.quota.estimation import estimate_input_tokens
from free_router.quota.tracker import Reservation

__all__ = ["PreparedStream", "start_stream"]

logger = logging.getLogger(__name__)


@dataclass
class PreparedStream:
    """A stream that has produced its first chunk and is therefore committed.

    Attributes:
        events: Renderable SSE events. Consuming this iterator settles the
            reservation, whichever way consumption ends.
        renderer: The renderer behind ``events``, whose ``finish_reason`` and
            ``usage`` describe how the provider ended.
        attempts: Dispatches made, including the successful one.
        failovers: How many times a different credential was tried.
        key_aliases: Credentials used, in order. Aliases only, never material.
    """

    events: AsyncIterator[str]
    renderer: StreamRenderer
    attempts: int
    failovers: int
    key_aliases: tuple[str, ...]


async def start_stream(
    *,
    dispatcher: Dispatcher,
    policy: DispatchConfig,
    http_client: httpx.AsyncClient,
    request: NormalizedRequest,
    provider: str,
    model_id: str,
) -> PreparedStream:
    """Open a streamed completion, retrying anything that happens before delivery.

    Args:
        dispatcher: Supplies the shared key pool, quota tracker, cooldown manager
            and breaker. Only its public collaborators are used.
        policy: The retry budget: attempts, wall-clock ceiling, backoff.
        http_client: The lifespan-owned client the adapters share.
        request: The normalized request, with ``stream`` set.
        provider: The configured provider.
        model_id: The provider-native model id.

    Returns:
        A :class:`PreparedStream` whose first chunk has already arrived.

    Raises:
        NoAvailableCapacityError: If capacity, cooldown or the breaker refuse the
            request. Nothing was dispatched.
        ProviderRateLimitError: If the provider rate limited the request. Never
            retried.
        ProviderAuthError: If every credential was rejected.
        ProviderValidationError: If the provider rejected the payload. Never
            retried.
        RetriesExhaustedError: If the attempt budget elapsed before a dispatch.
        ProviderServerError: If every attempt produced no content at all, so
            nothing can be delivered.
    """
    estimate = estimate_input_tokens(request)
    adapter = build_adapter(provider, http_client)

    if not adapter.supports_streaming:
        # A capability refusal, not a provider fault: nothing is sent, and
        # buffering a unary answer into SSE would fake streaming while keeping
        # its latency. An empty iterator would be worse still -- it is
        # indistinguishable from a provider that returned nothing.
        raise StreamingUnsupportedError(
            f"Model '{request.model}' does not support streaming.",
            param="stream",
        )

    aliases: list[str] = []
    failovers = 0
    last_error: RouterBaseError | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + policy.attempt_budget_seconds

    for attempt in range(1, policy.max_attempts + 1):
        _check_capacity(dispatcher, provider, model_id, estimate)

        if loop.time() >= deadline:
            break

        selection = await dispatcher.key_manager.select(provider, model_id)
        if selection.key is None:
            if last_error is not None:
                raise last_error
            raise NoAvailableCapacityError(
                f"No eligible credential is available for model '{model_id}'."
            )

        key = selection.key
        aliases.append(key.alias)
        reservation = await dispatcher.quota.reserve(provider, model_id, estimate)
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
                "stream": True,
            },
        )

        chunks = adapter.stream_chat_completion(request, model_id, key)

        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            # Zero chunks. A 200 with nothing in it is a provider fault, and
            # because nothing was written it is still retryable -- this is the
            # distinction ADR-0007 calls "not after stream start".
            await dispatcher.quota.reconcile(reservation, None)
            last_error = ProviderServerError("The upstream provider returned an empty stream.")
            failovers += 1
            logger.info(
                "provider_empty_stream",
                extra={
                    "event": "provider_empty_stream",
                    "key_alias": key.alias,
                    "attempt": attempt,
                },
            )
            if policy.backoff_seconds and attempt < policy.max_attempts:
                await asyncio.sleep(policy.backoff_seconds)
            continue
        except ProviderRateLimitError as exc:
            await dispatcher.quota.reconcile(reservation, None)
            await dispatcher.cooldown.enter(
                provider, model_id, provider_hint=exc.retry_after, reason="rate_limited"
            )
            logger.info(
                "provider_rate_limited",
                extra={
                    "event": "provider_rate_limited",
                    "key_alias": key.alias,
                    "attempt": attempt,
                    "retry_after": exc.retry_after,
                    "stream": True,
                },
            )
            raise
        except ProviderAuthError as exc:
            await dispatcher.quota.reconcile(reservation, None)
            await dispatcher.key_manager.suspend(provider, model_id, key.alias)
            last_error = exc
            failovers += 1
            logger.info(
                "credential_suspended",
                extra={
                    "event": "credential_suspended",
                    "key_alias": key.alias,
                    "attempt": attempt,
                    "stream": True,
                },
            )
            continue
        except ProviderValidationError as exc:
            await dispatcher.quota.reconcile(reservation, None)
            if exc.context_length:
                await dispatcher.breaker.record_failure(provider, model_id, estimate)
                logger.info(
                    "model_degraded_for_large_context",
                    extra={
                        "event": "model_degraded_for_large_context",
                        "key_alias": key.alias,
                        "attempt": attempt,
                        "estimated_tokens": estimate,
                        "stream": True,
                    },
                )
            raise
        except (ProviderServerError, ProviderTimeoutError) as exc:
            await dispatcher.quota.reconcile(reservation, None)
            last_error = exc
            failovers += 1
            logger.info(
                "provider_transient_fault",
                extra={
                    "event": "provider_transient_fault",
                    "key_alias": key.alias,
                    "attempt": attempt,
                    "error_category": type(exc).__name__,
                    "stream": True,
                },
            )
            if policy.backoff_seconds and attempt < policy.max_attempts:
                await asyncio.sleep(policy.backoff_seconds)
            continue

        # A chunk arrived, so the response is committed from here on.
        logger.info(
            "provider_responded",
            extra={
                "event": "provider_responded",
                "key_alias": key.alias,
                "attempt": attempt,
                "stream": True,
                "first_chunk": True,
            },
        )
        return _prepared(
            dispatcher=dispatcher,
            first=first,
            rest=chunks,
            reservation=reservation,
            estimate=estimate,
            key=key,
            request=request,
            provider=provider,
            model_id=model_id,
            attempts=attempt,
            failovers=failovers,
            aliases=aliases,
        )

    if last_error is not None:
        raise last_error
    raise RetriesExhaustedError(
        f"The {policy.attempt_budget_seconds}s attempt budget elapsed before a dispatch "
        f"could be made for model '{model_id}'."
    )


def _prepared(
    *,
    dispatcher: Dispatcher,
    first: NormalizedChunk,
    rest: AsyncIterator[NormalizedChunk],
    reservation: Reservation,
    estimate: int,
    key: KeyRef,
    request: NormalizedRequest,
    provider: str,
    model_id: str,
    attempts: int,
    failovers: int,
    aliases: list[str],
) -> PreparedStream:
    """Wrap a primed iterator in the reservation settlement it must perform."""
    renderer = StreamRenderer(model=request.model)
    events = _settled_events(
        renderer=renderer,
        dispatcher=dispatcher,
        model=request.model,
        first=first,
        rest=rest,
        reservation=reservation,
        estimate=estimate,
        key=key,
        provider=provider,
        attempts=attempts,
    )
    return PreparedStream(
        events=events,
        renderer=renderer,
        attempts=attempts,
        failovers=failovers,
        key_aliases=tuple(aliases),
    )


async def _settled_events(
    *,
    renderer: StreamRenderer,
    dispatcher: Dispatcher,
    model: str,
    first: NormalizedChunk,
    rest: AsyncIterator[NormalizedChunk],
    reservation: Reservation,
    estimate: int,
    key: KeyRef,
    provider: str,
    attempts: int,
) -> AsyncIterator[str]:
    """Render the stream and settle its reservation exactly once.

    The ``finally`` is the guarantee T058 asks for: a client that disconnects
    mid-stream, a provider that dies mid-stream and a provider that finishes all
    leave through it. A leaked reservation would not merely misreport tokens --
    it would hold capacity for the rest of the window, and a streamed request can
    sit in flight far longer than a unary one.

    A provider failure after delivery is logged and ends the stream. It is not
    re-raised into ``StreamingResponse``, which could only turn a clean truncation
    into a half-written traceback in the client's stream.
    """
    started = time.perf_counter()
    completed = False
    # The generator runs after the handler has returned, so the handler's
    # log_context has already unwound by the time it emits anything. Rebinding
    # it here keeps stream_interrupted and stream_settled attributable.
    with log_context(model=model, provider=provider):
        try:
            async for event in renderer.events(_chain(first, rest)):
                yield event
            completed = True
        except RouterBaseError as exc:
            # After delivery the only honest options are "stop" and "stop".
            # Sending [DONE] would claim success, and re-dispatching would splice
            # a second provider's answer onto the first (ADR-0007).
            logger.warning(
                "stream_interrupted",
                extra={
                    "event": "stream_interrupted",
                    "error_category": type(exc).__name__,
                    "stream": True,
                    "delivered": True,
                    "attempt": attempts,
                },
            )
        finally:
            await dispatcher.quota.reconcile(reservation, _usage_total(renderer))
            logger.info(
                "stream_settled",
                extra={
                    "event": "stream_settled",
                    "attempt": attempts,
                    "completed": completed,
                    "stream": True,
                    "estimated_tokens": estimate,
                    "reported_tokens": _usage_total(renderer),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
            if completed:
                await dispatcher.key_manager.mark_used(key)


async def _chain(
    first: NormalizedChunk, rest: AsyncIterator[NormalizedChunk]
) -> AsyncIterator[NormalizedChunk]:
    """Yield the primed first chunk, then the remainder of the provider stream."""
    yield first
    async for chunk in rest:
        yield chunk


def _usage_total(renderer: StreamRenderer) -> int | None:
    """Tokens to settle against, or ``None`` when the provider reported none.

    ``None`` leaves the estimate standing, which is the conservative direction:
    an unreported stream may still have consumed its whole reservation.
    """
    if renderer.usage is None:
        return None
    return renderer.usage.resolved_total()


def _check_capacity(
    dispatcher: Dispatcher, provider: str, model_id: str, estimated_tokens: int
) -> None:
    """Refuse to dispatch when no capacity is available.

    A deliberate mirror of ``Dispatcher._check_capacity``. The dispatcher keeps
    that method private and A2 keeps the file untouched, so the streaming path
    repeats it rather than reaching into another object's internals. The two are
    held together by tests that require the same error type for the same
    condition.

    Raises:
        PersistenceUnavailableError: If a known rate limit could not be recorded.
        ProviderRateLimitError: If a rate limit is in force.
        NoAvailableCapacityError: If the model is degraded for this size, or out
            of quota.
    """
    if dispatcher.cooldown.persistence_degraded(provider, model_id):
        raise PersistenceUnavailableError(
            f"Model '{model_id}' cannot be served: its rate limit could not be "
            f"recorded, so the router cannot guarantee to respect it."
        )

    if dispatcher.cooldown.is_cooling(provider, model_id):
        remaining = round(dispatcher.cooldown.remaining(provider, model_id), 3)
        if remaining > 0:
            raise ProviderRateLimitError(
                f"Model '{model_id}' is rate limited for another {remaining}s."
            )
        raise NoAvailableCapacityError(f"Model '{model_id}' has no capacity available.")

    if dispatcher.breaker.blocks(provider, model_id, estimated_tokens):
        raise NoAvailableCapacityError(
            f"Model '{model_id}' is temporarily degraded for requests of this size "
            "after a context-length rejection (CONSTITUTION 8.2)."
        )

    if not dispatcher.quota.available(provider, model_id):
        raise NoAvailableCapacityError(f"No quota remains for model '{model_id}'.")
