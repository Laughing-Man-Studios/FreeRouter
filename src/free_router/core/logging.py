"""Structured JSON logging for operational events.

Every operational record is emitted as a single JSON object on stdout, so log
consumers never have to parse free-form text.

Privacy (CONSTITUTION 9.1, spec 9): only the field names listed in
``_ALLOWED_EXTRA_FIELDS`` are ever copied out of a log record. Prompt text,
message content, completion text, payloads, and secrets are therefore
structurally unable to reach a log line, regardless of what a caller passes via
``extra=``. Request-scoped values are attached with context variables, so no
logger object or metadata mapping has to be threaded through call stacks.
"""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# The module deliberately shares its name with the standard library module it
# wraps. `import logging` above resolves to the stdlib: Python 3 uses absolute
# imports, so there is no shadowing.

_ALLOWED_EXTRA_FIELDS = (
    "event",
    "request_id",
    "provider",
    "model",
    "key_alias",
    "routing_decision",
    "selection_reason",
    "estimated_tokens",
    "attempt",
    "http_status",
    "latency_ms",
    "cooldown_state",
    "error_category",
    "error",
)
"""Operational telemetry fields permitted in a log record (CONSTITUTION 9.1)."""

_CONTEXT_VARS: dict[str, ContextVar[Any]] = {
    name: ContextVar(f"free_router_{name}", default=None) for name in _ALLOWED_EXTRA_FIELDS
}
"""Request-scoped storage backing :func:`log_context`."""

NORMALISED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")
"""Third-party loggers forced onto the root JSON handler, to avoid duplicate output."""

__all__ = [
    "NORMALISED_LOGGERS",
    "JsonFormatter",
    "configure_logging",
    "current_context",
    "log_context",
]


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind request-scoped telemetry for the duration of the block.

    Unknown field names are ignored, so a caller cannot smuggle arbitrary data
    into the log stream. Every bound variable is reset on exit, which keeps one
    request's context from leaking into the next.

    Args:
        **fields: Telemetry values keyed by a name in ``_ALLOWED_EXTRA_FIELDS``.
    """
    tokens: list[tuple[ContextVar[Any], Any]] = []
    try:
        for name, value in fields.items():
            var = _CONTEXT_VARS.get(name)
            if var is not None:
                tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_context() -> dict[str, Any]:
    """Return the bound request-scoped telemetry, omitting unset fields."""
    context: dict[str, Any] = {}
    for name, var in _CONTEXT_VARS.items():
        value = var.get()
        if value is not None:
            context[name] = value
    return context


class JsonFormatter(logging.Formatter):
    """Render a log record as one JSON object.

    Unrecognised attributes on the record are dropped rather than serialised.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context())
        for name in _ALLOWED_EXTRA_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger.

    Safe to call repeatedly: existing root handlers are removed first, so a
    reconfigured process still emits exactly one line per record.

    Args:
        level: Log level name, e.g. ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``.

    Raises:
        ValueError: If the level name is not recognised.
    """
    resolved = _resolve_level(level)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(resolved)

    for name in NORMALISED_LOGGERS:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.setLevel(logging.WARNING)
        logger.propagate = True


def _resolve_level(level: str) -> int:
    """Translate a level name into its numeric stdlib logging level."""
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        expected = ", ".join(sorted(logging.getLevelNamesMapping()))
        raise ValueError(f"Unknown log level '{level}'. Expected one of: {expected}.")
    return resolved
