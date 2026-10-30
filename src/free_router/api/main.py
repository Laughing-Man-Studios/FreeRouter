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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from free_router.api.health import health_router
from free_router.api.models_route import router as models_router
from free_router.api.routes import router
from free_router.core.config import Config, ConfigError, load_config
from free_router.core.debug_logging import configure_debug_payload_logging
from free_router.core.dispatch import Dispatcher
from free_router.core.exceptions import (
    NoAvailableCapacityError,
    PersistenceUnavailableError,
    ProviderAuthError,
    ProviderBaseError,
    ProviderDailyQuotaExhaustedError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
    RetriesExhaustedError,
    RouterBaseError,
    StreamingUnsupportedError,
    UnsupportedModelError,
    ValidationError,
)
from free_router.core.logging import configure_logging, log_context
from free_router.db.engine import DatabaseInitializationError, init_db_engine
from free_router.keys.manager import KeyManager
from free_router.quota.breaker import ContextBreaker
from free_router.quota.cooldown import CooldownManager
from free_router.quota.dimensions import QuotaDimension
from free_router.quota.persistence import (
    PersistenceCoordinator,
    QuotaStateStore,
    RestoreReport,
)
from free_router.quota.tracker import QuotaTracker

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

CONNECT_TIMEOUT_SECONDS = 0.25
"""How long a TCP+TLS connection to a provider may take.

A constant, not configuration: it describes the router's own local connection
behaviour, which does not vary by orders of magnitude between deployments.
"""

POOL_TIMEOUT_SECONDS = 0.05
"""How long to wait for a free connection from the pool."""

WRITE_TIMEOUT_SECONDS = 1.0
"""How long sending the request body may take.

The bodies here are small -- a chat completion request, not an upload -- so this
is generous by an order of magnitude.
"""


def http_client_timeout(config: Config) -> httpx.Timeout:
    """Build the provider client's timeout.

    **Only ``read`` is configuration.** ``connect``, ``pool`` and ``write``
    describe the router's own connection behaviour and are stable constants.
    ``read`` spans the provider's *generation*, which is unbounded in principle
    and is the only phase that varies by orders of magnitude -- a value baked
    into the source meant a slow provider produced a 504 on every request with
    no way to adjust it short of a rebuild.

    Args:
        config: The validated configuration.

    Returns:
        The timeout applied to the shared provider client.
    """
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        pool=POOL_TIMEOUT_SECONDS,
        write=WRITE_TIMEOUT_SECONDS,
        read=config.provider_read_timeout_seconds,
    )


PROVIDER_ERROR_STATUS_MAP: dict[type[RouterBaseError], int] = {
    # Order is significant: _status_for returns the first isinstance match, so
    # specific types precede the generic ones they would otherwise be swallowed
    # by. ProviderValidationError must precede ProviderBaseError's 502 fallback,
    # and ProviderAuthError must precede ProviderValidationError's 400.
    ProviderTimeoutError: 504,
    ProviderDailyQuotaExhaustedError: 429,
    ProviderRateLimitError: 429,
    NoAvailableCapacityError: 503,
    PersistenceUnavailableError: 503,
    RetriesExhaustedError: 503,
    ProviderAuthError: 401,
    ProviderValidationError: 400,
    ProviderServerError: 502,
    StreamingUnsupportedError: 400,
    ValidationError: 400,
    UnsupportedModelError: 400,
}
"""Exception-to-status mapping required by spec 10.

Every concrete subclass of :class:`RouterBaseError` belongs here. An exception
missing from this map silently inherits a neighbour's status, which is how a
provider 400 once returned 502.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise logging, configuration, the database, and the HTTP client.

    Each step is guarded so the failure is reported as structured JSON before
    the exception propagates and the process exits. Logging is configured first
    so that a fatal error in any later step is itself logged correctly.
    """
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    # Configured separately and strictly after the operational stream, so the
    # debug handler can never be the reason the operational one is misconfigured.
    configure_debug_payload_logging()

    app.state.config = await _startup_step("config_invalid", _load_config)
    app.state.db_engine = await _startup_step("database_init_failed", _init_database)

    # The store is built over the lifespan-owned engine, so every component that
    # writes through to durable state shares one connection pool and one file.
    quota_store = QuotaStateStore(app.state.db_engine)

    # Built after the HTTP client so the dispatcher can hold it, and after config
    # so a missing credential has already aborted startup. Every collaborator is
    # constructed once and shared: each holds in-memory state that a per-request
    # construction would silently discard, which would defeat the quota
    # reservation cycle entirely.
    app.state.http_client = httpx.AsyncClient(timeout=http_client_timeout(app.state.config))
    app.state.dispatcher = _build_dispatcher(app.state.config, app.state.http_client, quota_store)

    # Restored **before** the listener accepts anything. A request dispatched
    # against an unrestored counter would spend capacity the router believes it
    # has, which is the overshoot CONSTITUTION 8.3 exists to prevent. Nothing
    # below this line can run in parallel with a served request, because the
    # listener does not start until the lifespan yields.
    restore_report = await _restore_quota_state(quota_store, app.state.dispatcher)
    # The counts go in the message rather than in ``extra``: CONSTITUTION 9.1's
    # operational allowlist is closed and deliberately so, and a restore summary
    # is a startup line rather than request telemetry. Degradation is the part
    # that needs to be machine-readable, and it is logged below with
    # ``error_category``, which the allowlist does carry.
    logger.info(
        "quota_state_restored %s",
        restore_report.describe(),
        extra={"event": "quota_state_restored"},
    )

    # Counters and LRU ordinals batch behind the flush interval (B2). Cooldowns
    # and status transitions do not: they write through the moment they are set,
    # because a forgotten cooldown makes the router contact a provider that has
    # just refused it.
    app.state.persistence = PersistenceCoordinator(
        quota_store,
        snapshot=lambda: _quota_snapshot(app.state.dispatcher),
        flush_interval=app.state.config.quota_flush_interval_seconds,
    )
    app.state.persistence.start()

    logger.info("startup_completed", extra={"event": "startup_completed"})

    try:
        yield
    finally:
        # The final flush happens here: a clean shutdown should not be able to
        # lose the last interval of counters.
        coordinator: PersistenceCoordinator | None = getattr(app.state, "persistence", None)
        if coordinator is not None:
            await coordinator.stop()
        client: httpx.AsyncClient | None = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()
        engine: AsyncEngine | None = getattr(app.state, "db_engine", None)
        if engine is not None:
            # AsyncEngine.dispose is a coroutine; a bare call would leak the pool.
            await engine.dispose()
        logger.info("shutdown_completed", extra={"event": "shutdown_completed"})


async def _load_config() -> Config:
    """Load configuration. Exists to give every startup step one signature."""
    return load_config()


def _build_dispatcher(
    config: Config, http_client: httpx.AsyncClient, store: QuotaStateStore
) -> Dispatcher:
    """Construct the dispatcher's whole object graph from configuration.

    Runs after configuration so a declared alias with no credential has already
    failed startup: an unusable key is discovered before the process serves
    traffic, not as a 401 on the first request.

    Args:
        config: The validated configuration.
        http_client: The lifespan-owned client shared by every adapter.
        store: The durable store. Cooldowns and status transitions write through
            it the moment they are set, so a restart cannot resurrect a provider
            the router had already learned to avoid.
    """
    dimensions: Mapping[tuple[str, str], tuple[QuotaDimension, ...]] = {
        (entry.provider, entry.provider_id): tuple(entry.quota) for entry in config.models
    }
    durations = {
        (entry.provider, entry.provider_id): entry.cooldown_seconds for entry in config.models
    }
    return Dispatcher(
        key_manager=KeyManager(config.resolve_key_material()),
        quota=QuotaTracker(
            dimensions,
            safety_margin=config.quota.estimation_safety_margin,
            output_token_assumption=config.quota.output_token_assumption,
        ),
        cooldown=CooldownManager(
            durations=durations,
            store=store,
            max_cooldown_seconds=config.max_cooldown_seconds,
        ),
        breaker=ContextBreaker(store=store),
        http_client=http_client,
        max_attempts=config.dispatch.max_attempts,
        attempt_budget_seconds=config.dispatch.attempt_budget_seconds,
        backoff_seconds=config.dispatch.backoff_seconds,
    )


async def _restore_quota_state(store: QuotaStateStore, dispatcher: Dispatcher) -> RestoreReport:
    """Restore every component's durable state before the listener serves.

    Each table is loaded and handed to the component that owns it; the store
    never interprets a field. A failure to load one table degrades that unit to
    in-memory accounting and is recorded, rather than failing startup -- an
    unreadable row must not make the router unavailable.

    Args:
        store: The durable store.
        dispatcher: The object graph whose components own the restored state.

    Returns:
        What was actually recovered, so startup can report it rather than
        assume it.
    """
    report = RestoreReport()

    try:
        rows = await store.load_windows()
        dispatcher.quota.import_windows(rows)
        report.windows = len(rows)
        report.window_entries = sum(
            len(row.get("entries", ())) for row in rows if isinstance(row, dict)
        )
    except Exception as exc:
        report.degraded.append(f"windows:{type(exc).__name__}")

    try:
        report.cooldowns = dispatcher.cooldown.import_cooldowns(await store.load_cooldowns())
    except Exception as exc:
        report.degraded.append(f"cooldowns:{type(exc).__name__}")

    try:
        report.model_status = dispatcher.breaker.import_status(await store.load_model_status())
    except Exception as exc:
        report.degraded.append(f"model_status:{type(exc).__name__}")

    try:
        report.key_state = await dispatcher.key_manager.import_key_state(
            await store.load_key_state()
        )
    except Exception as exc:
        report.degraded.append(f"key_state:{type(exc).__name__}")

    for unit in report.degraded:
        logger.warning(
            "quota_state_degraded",
            extra={
                "event": "quota_state_degraded",
                "error_category": "persistence_degraded",
                "error": unit,
            },
        )

    return report


def _quota_snapshot(
    dispatcher: Dispatcher,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The batched state written on each flush: counters and LRU ordinals.

    Read at flush time rather than captured when the loop started, so the write
    reflects the state as it is now.
    """
    return dispatcher.quota.export_windows(), dispatcher.key_manager.export_key_state()


async def _init_database() -> AsyncEngine:
    """Initialise the database engine against ``ROUTER_DB_PATH``."""
    return await init_db_engine(os.environ.get("ROUTER_DB_PATH"))


async def _startup_step(error_category: str, step: Callable[[], Awaitable[Any]]) -> Any:
    """Await a startup step, logging a fatal error and re-raising on failure.

    Steps are coroutines so database initialisation does not block the loop.
    """
    try:
        return await step()
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

    The completion record is emitted from inside the ``log_context`` opened
    here, but ``model`` and ``provider`` are only known once a route has
    resolved them, and a route's own binding is unwound when it returns. They
    are therefore read back off ``request.state`` and rebound for the
    completion record, so a single line carries the request id, the provider,
    the model, the status, and the latency. An unset attribute yields ``None``,
    which the formatter omits, so a request that failed before reaching a
    provider still logs cleanly.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
    request.app.state.request_id = request_id
    started = time.perf_counter()

    with log_context(request_id=request_id):
        try:
            response = await call_next(request)
        except Exception as exc:
            with log_context(
                model=getattr(request.state, "model", None),
                provider=getattr(request.state, "provider", None),
            ):
                logger.exception(
                    "request_failed",
                    extra={"event": "request_failed", "error_category": type(exc).__name__},
                )
            raise

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        response.headers[REQUEST_ID_HEADER] = request_id
        with log_context(
            model=getattr(request.state, "model", None),
            provider=getattr(request.state, "provider", None),
        ):
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

    Order matters: the map is walked most-specific-first, so a
    ProviderValidationError (a ProviderBaseError, which would otherwise fall
    through to 502) is matched before the generic provider fallback. Every
    concrete exception in :mod:`free_router.core.exceptions` must appear here
    or it will be reported with the wrong status.
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
    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(router)
    return app


app = create_app()

__all__ = [
    "PROVIDER_ERROR_STATUS_MAP",
    "REQUEST_ID_HEADER",
    "app",
    "create_app",
    "http_client_timeout",
    "lifespan",
    "register_exception_handlers",
    "request_context_middleware",
]
