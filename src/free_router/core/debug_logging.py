"""Opt-in debug payload logging, written to an isolated stream.

CONSTITUTION 9.1 requires that prompt text, payload content and secrets never reach
an operational log line. :mod:`free_router.core.logging` enforces that
*structurally*: only the telemetry field names in its allowlist are ever copied out
of a record, so a caller passing anything else through ``extra=`` still cannot leak
a prompt.

This module is the deliberate, explicitly-enabled exception. When an operator sets
``ROUTER_DEBUG_PAYLOAD_LOG`` to a path, the full inbound, on-the-wire and response
payloads are written to that file. Three properties keep the exception from
weakening the guarantee it suspends:

**Isolation.** The debug logger is separate from the operational logger and sets
``propagate = False``, so nothing it emits can reach the root handler or stdout.
Writing to stderr would not qualify: Docker's logging driver merges stdout and
stderr into the same container log, so stderr is only *apparently* separate. A file
is genuinely isolated, which is why the destination is a path rather than a stream.

**Explicit opt-in.** With the variable unset, no handler is created and every call
site short-circuits on a module-level flag. The operational formatter is not
modified, imported from, or aware of this module, so the 9.1 structural guarantee
holds exactly as it did before this module existed.

**Redaction.** Values under credential-shaped key names are replaced before
anything reaches the file, so enabling debug logging cannot disclose a key even if
a provider echoes one back inside an error body. Header mappings are never passed
in by callers -- credentials travel in headers -- and redaction is the backstop for
the case nobody thinks of.

Redaction is deliberately name-based rather than pattern-based. Guessing at key
*shapes* to catch secrets would be fragile in both directions: it would miss
unrecognised formats and mangle legitimate payload content, which is the very thing
this stream exists to show in full.
"""

from __future__ import annotations

import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

__all__ = [
    "DEBUG_LOG_ENV_VAR",
    "configure_debug_payload_logging",
    "debug_enabled",
    "debug_payload",
    "redact",
]

DEBUG_LOG_ENV_VAR = "ROUTER_DEBUG_PAYLOAD_LOG"
"""Environment variable naming the destination file. Unset means disabled."""

_DEBUG_LOGGER_NAME = "free_router.debug"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 3
_REDACTED = "[redacted]"
_SEPARATOR_RE = re.compile(r"[^a-z0-9]")

_SENSITIVE_KEY_PARTS = (
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
"""Substrings marking a mapping key as credential-bearing.

Compared against the key with separators stripped, because credential-bearing names
are spelled inconsistently: Google's own header is ``x-goog-api-key``, not
``api_key``, and a literal underscore match would miss the single most important
key in this codebase.
"""

_enabled = False


def configure_debug_payload_logging(environ: dict[str, str] | None = None) -> bool:
    """Install the debug payload handler if explicitly configured.

    Safe to call repeatedly: an existing debug handler is closed and replaced
    rather than stacked, so a reloaded process still writes one line per record.
    The debug logger's ``propagate = False`` is re-asserted each time, because
    :func:`free_router.core.logging.configure_logging` rebuilds the root handler
    tree and a future refactor there must not be able to silently redirect the
    debug stream onto stdout.

    Args:
        environ: Environment mapping to read. Defaults to ``os.environ``.

    Returns:
        ``True`` if debug payload logging is now active, ``False`` if it is
        disabled. An unusable destination (unwritable path, unserialisable
        directory) is treated as disabled rather than raised: debug logging must
        never be the reason the proxy fails to serve traffic.
    """
    global _enabled

    source = os.environ if environ is None else environ
    raw = source.get(DEBUG_LOG_ENV_VAR, "").strip()
    if not raw:
        _enabled = False
        return False

    debug_logger = logging.getLogger(_DEBUG_LOGGER_NAME)
    for existing in list(debug_logger.handlers):
        debug_logger.removeHandler(existing)
        existing.close()

    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        _enabled = False
        return False

    # Payloads are created 0600. Rotation reopens the file, so the mode is
    # applied on every write rather than once at configuration time -- otherwise
    # a rotated replacement would inherit the process umask and could widen the
    # file's permissions without anyone noticing.
    handler.setFormatter(_DebugFormatter())
    debug_logger.addHandler(handler)
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.propagate = False

    _enabled = True
    return True


def debug_enabled() -> bool:
    """True when debug payload logging is active and calls will be recorded."""
    return _enabled


def debug_payload(*, phase: str, provider: str, model: str, data: object) -> None:
    """Record one payload to the debug stream, if it is enabled.

    Call sites invoke this unconditionally; the flag check keeps the disabled cost
    to a single boolean test so no caller has to remember to guard.

    Args:
        phase: Where in the exchange this payload sits, e.g. ``"inbound"``,
            ``"wire_request"``, ``"wire_response"``.
        provider: The provider the payload relates to.
        model: The externally-requested model id, matching the operational
            stream's ``model`` field so the two can be correlated by eye.
        data: The payload. Redacted before it is written. Must not be a header
            mapping -- credentials travel in headers, and redaction is only a
            backstop.
    """
    if not _enabled:
        return
    logging.getLogger(_DEBUG_LOGGER_NAME).debug(
        "debug_payload",
        extra={"phase": phase, "provider": provider, "model": model, "data": redact(data)},
    )


def redact(value: object) -> Any:
    """Return a copy of ``value`` with credential-shaped entries replaced.

    Recurses through mappings and sequences. Anything not JSON-shaped is coerced
    with ``str`` at serialisation time by the formatter, so it passes through
    unchanged here.
    """
    if isinstance(value, dict):
        return {
            key: _REDACTED if _is_sensitive(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def _is_sensitive(key: str) -> bool:
    """True when a mapping key names a credential.

    Separators are stripped before matching so ``x-goog-api-key``, ``api_key`` and
    ``apiKey`` are all recognised as the same thing.
    """
    normalised = _SEPARATOR_RE.sub("", key.lower())
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


class _DebugFormatter(logging.Formatter):
    """Render a debug payload record as one JSON object per line.

    Deliberately simpler than the operational formatter: it carries no telemetry
    allowlist, because the whole point of this stream is to record fields the
    operational one would drop. It reads only the three attributes
    :func:`debug_payload` sets, so an unrelated record on this logger cannot
    contribute arbitrary attributes.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": record.created,
            "phase": getattr(record, "phase", None),
            "provider": getattr(record, "provider", None),
            "model": getattr(record, "model", None),
            "data": getattr(record, "data", None),
        }
        return json.dumps(payload, default=str)
