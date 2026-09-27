"""FastAPI application: startup/shutdown lifespan and request logging middleware.

Scope for Batch 1 is deliberately minimal. The lifespan validates configuration
and fails fast before the server accepts traffic; T007 (Batch 2) extends it with
the SQLite engine and the ``httpx.AsyncClient`` singleton, and T008 adds routes.
"""

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from free_router.core.config import ConfigError, load_config
from free_router.core.logging import configure_logging, log_context

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate configuration on startup; anything invalid aborts the process.

    Logging is configured first so that a fatal configuration error is itself
    reported as structured JSON (spec Scenarios 2 and 3).
    """
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        config = load_config()
    except ConfigError as exc:
        logger.critical(
            "startup_failed",
            extra={
                "event": "startup_failed",
                "error_category": "config_invalid",
                "error": str(exc),
            },
        )
        raise

    app.state.config = config
    logger.info("startup_completed", extra={"event": "startup_completed"})

    yield


async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind a request id for the duration of the call and log the outcome.

    The request body is never read and never logged.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
    started = time.perf_counter()

    with log_context(request_id=request_id):
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception(
                "request_failed",
                extra={"event": "request_failed", "error_category": type(exc).__name__},
            )
            raise

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            extra={
                "event": "request_completed",
                "http_status": response.status_code,
                "latency_ms": latency_ms,
            },
        )
        return response


def create_app() -> FastAPI:
    """Build the ASGI application."""
    app = FastAPI(title="Free LLM Router", lifespan=lifespan)
    app.middleware("http")(request_context_middleware)
    return app


app = create_app()

__all__ = ["REQUEST_ID_HEADER", "app", "create_app", "lifespan", "request_context_middleware"]
