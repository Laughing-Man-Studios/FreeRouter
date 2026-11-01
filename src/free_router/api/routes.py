"""OpenAI-compatible HTTP ingress.

Route handlers are thin: they resolve singletons from ``app.state``, validate
the request, hand off to the dispatcher, and serialise the result. Retry,
failover, capacity gating and provider translation all live elsewhere by design
(CONSTITUTION 3).
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from free_router.api import stream_dispatch
from free_router.core.config import Config
from free_router.core.debug_logging import debug_payload
from free_router.core.dispatch import Dispatcher
from free_router.core.exceptions import UnsupportedModelError, ValidationError
from free_router.core.logging import log_context
from free_router.core.normalization import (
    NormalizedRequest,
    NormalizedResponse,
    normalize_chat_completion_request,
    to_openai_completion,
)

__all__ = [
    "chat_completions",
    "get_config",
    "get_dispatcher",
    "resolve_model",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])


def get_config(request: Request) -> Config:
    """Return the validated configuration attached during startup."""
    return cast("Config", request.app.state.config)


def get_dispatcher(request: Request) -> Dispatcher:
    """Return the dispatcher built during startup.

    The dispatcher owns the key pool, quota tracker, cooldown manager, circuit
    breaker and HTTP client. Building them once at startup rather than per
    request matters: every one of them holds in-memory state that would be lost
    if a request constructed its own, defeating the whole quota reservation cycle.
    """
    return cast("Dispatcher", request.app.state.dispatcher)


def resolve_model(config: Config, model: str) -> tuple[str, str]:
    """Confirm the requested model is configured and return its provider mapping.

    The router honours explicit model selection and never substitutes a
    different model, so an unknown id is rejected before dispatch
    (spec "No Automatic Fallback", Scenario 7).

    Args:
        config: The validated application configuration.
        model: The externally-requested model id.

    Returns:
        A ``(provider, provider_model_id)`` pair: which adapter to use and the
        provider-native model id to send it.

    Raises:
        UnsupportedModelError: If no configured model matches.
    """
    for entry in config.models:
        if entry.id == model:
            return entry.provider, entry.provider_id
    available = ", ".join(entry.id for entry in config.models)
    raise UnsupportedModelError(
        f"Model '{model}' is not available. Configured models: {available}.",
        param="model",
    )


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    config: Config = Depends(get_config),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> dict[str, Any] | StreamingResponse:
    """Handle an OpenAI-compatible chat completion request.

    The payload is read as raw JSON rather than through a FastAPI body model.
    That is intentional: the normalizer is the only place that decides which
    fields are meaningful, and a strict body model would reject requests carrying
    ``temperature`` instead of stripping those parameters as the spec requires
    (spec 3).

    ``stream`` is the one behavioural branch in this handler, and it branches
    **after** model resolution so both paths reject an unknown model identically.
    The unary path below is deliberately unchanged: it is the path M1 measured at
    100/100, and a streaming change that altered it would invalidate that.

    Raises:
        ValidationError: If the payload is not a JSON object, or fails ingress
            normalization.
        UnsupportedModelError: If the requested model is not configured.
        StreamingUnsupportedError: If streaming was requested for a model whose
            provider cannot stream. Raised before anything is dispatched.
        NoAvailableCapacityError: If no capacity is available. Raised *before*
            anything is dispatched.
        ProviderRateLimitError: If the provider rate limited the request.
        RetriesExhaustedError: If attempts were made and all of them failed.
    """
    payload: Any = await _read_json_object(request)
    normalized = normalize_chat_completion_request(payload)

    # Model validation happens here, inside the route handler, so no dispatch
    # can occur for a model the router does not serve (spec Behavioral Rules).
    provider, provider_id = resolve_model(config, normalized.model)

    # Published on request.state so the logging middleware can attribute the
    # request_completed record. The middleware binds its context around
    # call_next and logs after the route returns, so a log_context made inside
    # the handler is already unwound by then and would not reach that record.
    request.state.model = normalized.model
    request.state.provider = provider

    debug_payload(
        phase="inbound",
        provider=provider,
        model=normalized.model,
        data={"client_request": payload, "normalized": normalized.model_dump()},
    )

    if normalized.stream:
        return await _stream_response(
            request=request,
            config=config,
            dispatcher=dispatcher,
            normalized=normalized,
            provider=provider,
            provider_id=provider_id,
        )

    # The context is opened here rather than inside the dispatcher so that every
    # record the dispatcher emits -- provider_dispatched, provider_rate_limited,
    # credential_suspended, and the rest -- carries the externally-requested
    # `model` alongside `provider`. The dispatcher knows only the provider-native
    # id, and CONSTITUTION 9.1 lists both as expected operational metadata.
    started = time.perf_counter()
    with log_context(model=normalized.model, provider=provider):
        outcome = await dispatcher.dispatch(normalized, provider, provider_id)
        logger.info(
            "dispatch_completed",
            extra={
                "event": "dispatch_completed",
                "attempt": outcome.attempts,
                "failovers": outcome.failovers,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )

    response = outcome.response
    completion = to_openai_completion(
        response,
        request_model=normalized.model,
        response_id=f"chatcmpl-{_completion_id(normalized, response, provider_id)}",
        created=int(datetime.now(tz=UTC).timestamp()),
    )
    debug_payload(
        phase="client_response",
        provider=provider,
        model=normalized.model,
        data=completion,
    )
    return completion


async def _stream_response(
    *,
    request: Request,
    config: Config,
    dispatcher: Dispatcher,
    normalized: NormalizedRequest,
    provider: str,
    provider_id: str,
) -> StreamingResponse:
    """Open an SSE response, or raise before any bytes are committed.

    The first provider chunk is consumed here, inside the handler, **before** a
    ``StreamingResponse`` exists. That ordering is what makes fail-fast precise:
    while nothing has been written the failure can still become a retry or a JSON
    error, and once the response object is returned the status line is already
    decided. See :mod:`free_router.api.stream_dispatch`.

    Returns:
        A ``text/event-stream`` response.

    Raises:
        StreamingUnsupportedError: If the provider cannot stream.
        RouterBaseError: Any pre-delivery failure, mapped to JSON by the handlers
            in :mod:`free_router.api.main`.
    """
    http_client = cast("httpx.AsyncClient", request.app.state.http_client)
    started = time.perf_counter()

    with log_context(model=normalized.model, provider=provider):
        prepared = await stream_dispatch.start_stream(
            dispatcher=dispatcher,
            policy=config.dispatch,
            http_client=http_client,
            request=normalized,
            provider=provider,
            model_id=provider_id,
        )
        logger.info(
            "dispatch_completed",
            extra={
                "event": "dispatch_completed",
                "attempt": prepared.attempts,
                "failovers": prepared.failovers,
                "stream": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )

    return StreamingResponse(
        prepared.events,
        media_type="text/event-stream",
        headers={
            # No buffering anywhere: a buffered SSE response is an unstreamed one.
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _read_json_object(request: Request) -> dict[str, Any]:
    """Decode the request body as a JSON object.

    A malformed body must not surface as an HTML error page, so JSON decoding
    failures are translated into the router's own error type (spec Edge Cases).

    Raises:
        ValidationError: If the body is not valid JSON or not an object.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise ValidationError(
            "Request body must be a valid JSON object.",
            code="malformed_json",
        ) from exc

    if not isinstance(payload, dict):
        raise ValidationError(
            "Request body must be a JSON object.",
            code="invalid_body",
        )
    return payload


def _completion_id(
    normalized: NormalizedRequest, response: NormalizedResponse, provider_id: str
) -> str:
    """Derive a stable, content-free completion id for this exchange.

    The id is hashed from model identity alone: it must correlate log lines
    without ever being derived from prompt or completion text.
    """
    digest = hashlib.sha256(
        f"{normalized.model}:{provider_id}:{response.model_used}".encode()
    ).hexdigest()
    return digest[:24]
