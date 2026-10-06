"""Structured logging tests.

The privacy assertions here are the enforcement mechanism for CONSTITUTION 9.1
and spec 9: prompt, completion, and secret material must never reach a log line.
"""

import json
import logging
import sys
from typing import Any

import pytest
from conftest import LogRecorder

from free_router.core.logging import (
    NORMALISED_LOGGERS,
    JsonFormatter,
    configure_logging,
    current_context,
    log_context,
)

pytestmark = pytest.mark.usefixtures("root_logger_guard")


def test_formatter_emits_single_line_valid_json(log_recorder: LogRecorder):
    """Every record is one parseable JSON object with the required fields."""
    record = log_recorder.emit("request received", event="request_received")

    assert len(log_recorder.lines) == 1
    assert isinstance(record, dict)
    assert record["level"] == "INFO"
    assert record["logger"] == "free_router.test_capture"
    assert record["message"] == "request received"
    assert record["event"] == "request_received"
    assert record["timestamp"].endswith("Z")
    json.dumps(record)  # the line is serialisable, not a Python repr


def test_contextvars_injected_without_explicit_extras(log_recorder: LogRecorder):
    """Request-scoped metadata reaches logs without threading a logger."""
    with log_context(request_id="req-1", model="google/gemini-3.5-flash-lite"):
        record = log_recorder.emit("request received")

    assert record["request_id"] == "req-1"
    assert record["model"] == "google/gemini-3.5-flash-lite"


def test_contextvars_reset_after_scope(log_recorder: LogRecorder):
    """One request's context must not leak into the next."""
    with log_context(request_id="req-1", model="google/gemini-3.5-flash-lite"):
        pass

    record = log_recorder.emit("later event")

    assert "request_id" not in record
    assert "model" not in record
    assert current_context() == {}


def test_contextvars_reset_on_exception(log_recorder: LogRecorder):
    """Context is unwound even when the block raises."""
    with pytest.raises(RuntimeError), log_context(request_id="req-boom"):
        raise RuntimeError("boom")

    assert current_context() == {}


def test_unknown_context_field_ignored(log_recorder: LogRecorder):
    """log_context cannot be used to smuggle arbitrary data into the log stream."""
    with log_context(request_id="req-1", prompt="secret prompt text"):
        record = log_recorder.emit("request received")

    assert record["request_id"] == "req-1"
    assert "prompt" not in record
    assert "secret prompt text" not in json.dumps(record)


def test_latency_and_status_types(log_recorder: LogRecorder):
    """Timing and status keep their numeric types for log consumers."""
    record = log_recorder.emit("request completed", http_status=200, latency_ms=12.5)

    assert record["http_status"] == 200
    assert isinstance(record["http_status"], int)
    assert record["latency_ms"] == 12.5
    assert isinstance(record["latency_ms"], float)


def test_prompt_and_completion_fields_dropped(log_recorder: LogRecorder):
    """Prompt and completion text can never reach a log line (spec 9)."""
    record = log_recorder.emit(
        "request received",
        prompt="THE_USER_SECRET_PROMPT",
        messages=[{"role": "user", "content": "ANOTHER_SECRET"}],
        completion="THE_MODEL_SECRET_COMPLETION",
        payload={"content": "PAYLOAD_SECRET"},
    )

    serialised = json.dumps(record)
    assert "THE_USER_SECRET_PROMPT" not in serialised
    assert "ANOTHER_SECRET" not in serialised
    assert "THE_MODEL_SECRET_COMPLETION" not in serialised
    assert "PAYLOAD_SECRET" not in serialised
    for forbidden in ("prompt", "messages", "completion", "payload"):
        assert forbidden not in record


def test_secret_and_config_fields_dropped(log_recorder: LogRecorder):
    """Secrets and serialised configuration never reach a log line."""
    record = log_recorder.emit(
        "startup",
        api_key="sk-should-never-appear",
        config='{"gemini_api_key": "sk-should-never-appear"}',
    )

    serialised = json.dumps(record)
    assert "sk-should-never-appear" not in serialised
    assert "api_key" not in record
    assert "config" not in record


def test_exception_record_includes_error(log_recorder: LogRecorder):
    """A traceback is rendered into the error field, not dropped."""
    try:
        raise ValueError("kaboom")
    except ValueError:
        record = log_recorder.emit("request_failed", level=logging.ERROR, exc_info=True)

    assert record["level"] == "ERROR"
    assert "ValueError: kaboom" in record["error"]


def test_configure_logging_is_idempotent(capfd: pytest.CaptureFixture[str]):
    """Reconfiguring leaves exactly one handler, so records are not duplicated."""
    configure_logging("INFO")
    configure_logging("INFO")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.handlers[0].stream is sys.stdout

    logging.getLogger("free_router.probe").info("only once", extra={"event": "probe"})
    lines = [line for line in capfd.readouterr().out.splitlines() if line.strip()]

    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "probe"


def test_configure_logging_sets_level(capfd: pytest.CaptureFixture[str]):
    configure_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING

    logging.getLogger("free_router.probe").info("suppressed")
    logging.getLogger("free_router.probe").warning("emitted")

    lines = [line for line in capfd.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "WARNING"


def test_configure_logging_rejects_unknown_level():
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging("CHATTY")


def test_third_party_loggers_normalised():
    """uvicorn and httpx must not emit duplicate or non-JSON lines."""
    configure_logging("INFO")

    for name in NORMALISED_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True
        assert logger.level == logging.WARNING


def test_extra_context_precedence(log_recorder: LogRecorder):
    """An explicit record field wins over a bound context value."""
    with log_context(request_id="from-context"):
        record = log_recorder.emit("request completed", request_id="from-record")

    assert record["request_id"] == "from-record"


def test_record_without_event_omits_key(log_recorder: LogRecorder):
    """A record with no event simply has no event field."""
    record: dict[str, Any] = log_recorder.emit("bare message")

    assert record["message"] == "bare message"
    assert "event" not in record
