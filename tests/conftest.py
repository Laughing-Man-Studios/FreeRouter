"""Shared pytest fixtures.

Every test runs in a throwaway working directory with the configuration
environment variables cleared, so a developer's real ``.env`` or
``config.yaml`` can never influence a result.
"""

import json
import logging
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free_router.core.logging import NORMALISED_LOGGERS, JsonFormatter

MANAGED_ENV_VARS = ("GEMINI_API_KEY", "LOG_LEVEL", "ROUTER_CONFIG_PATH", "ROUTER_DB_PATH")
"""Environment variables that must not leak into a test."""

MINIMAL_CONFIG = """\
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
"""
"""The shipped M0 configuration."""


@pytest.fixture
def workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty working directory, with configuration variables cleared."""
    for name in MANAGED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "work"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def minimal_config() -> str:
    """The shipped M0 configuration, as a YAML document."""
    return MINIMAL_CONFIG


@pytest.fixture
def write_config(workdir: Path) -> Callable[..., Path]:
    """Write a ``config.yaml`` into the isolated working directory."""

    def _write(contents: str = MINIMAL_CONFIG) -> Path:
        path = workdir / "config.yaml"
        path.write_text(contents, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def set_api_key(monkeypatch: pytest.MonkeyPatch) -> Callable[..., str]:
    """Set a dummy ``GEMINI_API_KEY`` for the duration of a test."""

    def _set(value: str = "test-gemini-key") -> str:
        monkeypatch.setenv("GEMINI_API_KEY", value)
        return value

    return _set


@pytest.fixture
def ready_app(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[], FastAPI]]:
    """A factory for an app whose startup steps all succeed.

    The database is redirected away from the working directory so a test can
    assert on its lifecycle without picking up the default ``./data`` path.
    """
    from free_router.api.main import create_app

    write_config()
    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    yield create_app


@pytest.fixture
def client(ready_app: Callable[[], FastAPI]) -> Iterator[TestClient]:
    """A started :class:`TestClient` with a throwaway database and configuration."""
    with TestClient(ready_app()) as started:
        yield started


class LogRecorder:
    """Capture log records rendered by :class:`JsonFormatter`.

    ``caplog`` is deliberately not used: it bypasses formatters, so it cannot
    verify the JSON contract.
    """

    def __init__(self) -> None:
        self.stream = StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(JsonFormatter())
        self.logger = logging.getLogger("free_router.test_capture")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def close(self) -> None:
        self.logger.removeHandler(self.handler)
        self.handler.close()

    @property
    def lines(self) -> list[str]:
        """Every emitted line."""
        return [line for line in self.stream.getvalue().splitlines() if line.strip()]

    @property
    def records(self) -> list[dict[str, Any]]:
        """Every emitted line parsed as JSON."""
        return [json.loads(line) for line in self.lines]

    def emit(
        self,
        message: str,
        level: int = logging.INFO,
        exc_info: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        """Emit one record and return it parsed."""
        self.logger.log(level, message, exc_info=exc_info, extra=extra)
        return self.records[-1]


@pytest.fixture
def log_recorder() -> Iterator[LogRecorder]:
    """A :class:`LogRecorder` for the duration of a test."""
    recorder = LogRecorder()
    try:
        yield recorder
    finally:
        recorder.close()


@pytest.fixture
def json_logs(log_recorder: LogRecorder) -> LogRecorder:
    """A :class:`LogRecorder` wired to the root logger.

    ``caplog`` is unusable for the router's own records: it only captures at
    WARNING by default, and ``configure_logging`` clears root handlers during
    startup. This fixture attaches after startup and restores the root logger
    afterwards, so tests can assert on the real JSON contract at INFO level.
    """
    root = logging.getLogger()
    root.addHandler(log_recorder.handler)
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield log_recorder
    finally:
        root.removeHandler(log_recorder.handler)
        root.setLevel(previous_level)


@pytest.fixture
def root_logger_guard() -> Iterator[None]:
    """Restore root and third-party logger state after a test mutates it."""
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in NORMALISED_LOGGERS
    }
    try:
        yield
    finally:
        root.handlers = root_handlers
        root.setLevel(root_level)
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
