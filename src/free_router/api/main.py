"""FastAPI application: lifespan, error handling, and request logging middleware.

Startup is fail-fast by design. Configuration, the database, and the HTTP
client are all initialised before the server accepts traffic, and any failure
aborts the process rather than serving degraded requests (spec 6 and 8,
Scenarios 2, 3, and 4).

Every error leaving this application is rendered as an OpenAI-compatible JSON
envelope, including FastAPI's own validation and HTTP errors, so a client never
receives an HTML error page.
"""

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from free_router.api.routes import router
from free_router.core.config import ConfigError, load_config
from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderBaseError,
    ProviderServerError,
    ProviderTimeoutError,
    RouterBaseError,
    ValidationError,
)
from free_router.core.logging import configure_logging, log_context
from free_router.db.engine import DatabaseInitializationError, init_db_engine

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

HTTP_CLIENT_TIMEOUT = httpx.Timeout(connect=0.25, pool=0.05, write=1.0, read=10.0)
"""Aggressive client timeouts so a hung provider cannot stall the event loop."""

PROVIDER_ERROR_STATUS_MAP: dict[type[RouterBaseError], int] = {
    ProviderAuthError: 401,
    ValidationError: 400,
    ProviderServerError: 502,
    ProviderTimeoutError: 504,
}
"""Exception-to-status mapping required by spec 10."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise logging, configuration, the database, and the HTTP client.

    Each step is guarded so the failure is reported as structured JSON before
    the exception propagates and the process exits. Logging is configured first
    so that a fatal error in any later step is itself logged correctly.
    """
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    config = _startup_step("config_invalid", lambda: load_config())
    app.state.config = config

    database_path = os.environ.get("ROUTER_DB_PATH")
    app.state.db_engine = _startup_step(
        "database_init_failed",
        lambda: init_db_engine(database_path),
    )

    app.state.http_client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)
    logger.info("startup_completed", extra={"event": "startup_completed"})

    try:
        yield
    finally:
        client: httpx.AsyncClient | None = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()
        engine = getattr(app.state, "db_engine", None)
        if engine is not None:
            engine.dispose()
        logger.info("shutdown_completed", extra={"event": "shutdown_completed"})


def _startup_step(error_category: str, step: Callable[[], Any]) -> Any:
    """Run a startup step, logging a fatal error and re-raising on failure."""
    try:
        return step()
    except (ConfigError, DatabaseInitializationError) as exc:
        logger.critical(
            "startup_failed",
            extra={"event": "startup_failed", "error_category": error_category, "error": str(exc)},
        )
        raise


async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind a request id for the duration of the call and log the outcome.

    The request body is never read and never logged.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
    request.app.state.request_id = request_id
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


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so every error is an OpenAI-compatible JSON envelope."""

    @app.exception_handler(RouterBaseError)
    async def _router_error(_: Request, exc: RouterBaseError) -> JSONResponse:
        status = _status_for(exc)
        logger.info(
            "request_rejected",
            extra={"event": "request_rejected", "error_category": exc.code},
        )
        return JSONResponse(status_code=status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_envelope(
                "Request payload failed validation.", "invalid_request_error", "invalid_request"
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(str(exc.detail), "invalid_request_error", "http_error"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_error",
            extra={"event": "unhandled_error", "error_category": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500,
            content=_envelope("Internal server error.", "api_error", "internal_error"),
        )


def _status_for(exc: RouterBaseError) -> int:
    """Resolve the HTTP status for a router exception.

    Unknown exception types fall back to 500 rather than being treated as a
    client error.
    """
    for error_type, status in PROVIDER_ERROR_STATUS_MAP.items():
        if isinstance(exc, error_type):
            return status
    if isinstance(exc, ProviderBaseError):
        return 502
    return 500


def _envelope(message: str, error_type: str, code: str) -> dict[str, Any]:
    """Build an OpenAI error envelope for handlers outside the exception tree."""
    return {"error": {"message": message, "type": error_type, "param": None, "code": code}}


def create_app() -> FastAPI:
    """Build the ASGI application."""
    app = FastAPI(title="Free LLM Router", lifespan=lifespan)
    app.middleware("http")(request_context_middleware)
    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()

__all__ = [
    "HTTP_CLIENT_TIMEOUT",
    "PROVIDER_ERROR_STATUS_MAP",
    "REQUEST_ID_HEADER",
    "app",
    "create_app",
    "lifespan",
    "register_exception_handlers",
    "request_context_middleware",
]
