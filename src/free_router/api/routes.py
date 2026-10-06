"""OpenAI-compatible HTTP ingress.

Route handlers are thin: they resolve singletons from ``app.state``, validate
the request, call the adapter, and serialise the result. Routing policy and
provider translation live elsewhere by design (CONSTITUTION 3).
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, Request

from free_router.core.config import Config
from free_router.core.exceptions import UnsupportedModelError, ValidationError
from free_router.core.logging import log_context
from free_router.core.normalization import (
    NormalizedRequest,
    NormalizedResponse,
    normalize_chat_completion_request,
    to_openai_completion,
)
from free_router.providers.base import build_adapter

__all__ = [
    "chat_completions",
    "get_config",
    "get_http_client",
    "resolve_model",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])


def get_config(request: Request) -> Config:
    """Return the validated configuration attached during startup."""
    return cast("Config", request.app.state.config)


def get_http_client(request: Request) -> httpx.AsyncClient:
    """Return the shared ``httpx.AsyncClient`` singleton."""
    return cast("httpx.AsyncClient", request.app.state.http_client)


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


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    config: Config = Depends(get_config),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    """Handle an OpenAI-compatible chat completion request.

    The payload is read as raw JSON rather than through a FastAPI body model.
    That is intentional: M0 honours only ``model`` and ``messages``, and a
    strict body model would reject requests carrying ``temperature`` instead of
    stripping those parameters as the spec requires (spec 3). The normalizer is
    the only place that decides which fields are meaningful.

    Raises:
        ValidationError: If the payload is not a JSON object, or fails ingress
            normalization.
        UnsupportedModelError: If the requested model is not configured.
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

    started = time.perf_counter()
    adapter = build_adapter(provider, http_client)
    with log_context(model=normalized.model, provider=provider):
        logger.info("provider_dispatched", extra={"event": "provider_dispatched"})
        response: NormalizedResponse = await adapter.chat_completion(normalized, provider_id)
        logger.info(
            "provider_responded",
            extra={
                "event": "provider_responded",
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )

    return to_openai_completion(
        response,
        request_model=normalized.model,
        response_id=f"chatcmpl-{_completion_id(normalized, response, provider_id)}",
        created=int(datetime.now(tz=UTC).timestamp()),
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
