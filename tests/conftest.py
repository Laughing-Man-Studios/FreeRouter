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

from free_router.core.config import Capabilities, CapabilityValue
from free_router.core.logging import NORMALISED_LOGGERS, JsonFormatter

MANAGED_ENV_VARS = (
    "GOOGLE_KEY_PRIMARY",
    "GOOGLE_KEY_SECONDARY",
    "MISTRAL_KEY_PRIMARY",
    "LOG_LEVEL",
    "ROUTER_CONFIG_PATH",
    "ROUTER_DB_PATH",
    "ROUTER_DEBUG_PAYLOAD_LOG",
)
"""Environment variables that must not leak into a test.

Covers every ``<PROVIDER>_KEY_<ALIAS>`` slot the shipped configuration can name,
so a developer shell with real credentials cannot influence a result."""
"""Environment variables that must not leak into a test."""


def capabilities_yaml(
    *,
    context_window: int = 1_048_576,
    streaming: bool = True,
    fallback_models: tuple[str, ...] = (),
    indent: str = "    ",
) -> str:
    """A complete capability block, for tests that build their own config.

    One definition rather than a copy in every fixture: the block is required on
    every model, so a schema change would otherwise touch a dozen files. The
    values here are synthetic test data; the shipped file's values are asserted
    separately in ``test_shipped_config.py``.
    """
    fallback = (
        "[]" if not fallback_models else "[" + ", ".join(f'"{m}"' for m in fallback_models) + "]"
    )
    return (
        f"{indent}capabilities:\n"
        f"{indent}  context_window:\n"
        f"{indent}    value: {context_window}\n"
        f"{indent}    provenance: published\n"
        f'{indent}    source: "https://example.test/model-docs, retrieved 2026-11-04"\n'
        f"{indent}  supports_streaming:\n"
        f"{indent}    value: {str(streaming).lower()}\n"
        f"{indent}    provenance: measured\n"
        f'{indent}    source: "test measurement, 1/1 streamed, 2026-11-04"\n'
        f"{indent}  supports_tools:\n"
        f"{indent}    value: false\n"
        f"{indent}    provenance: assumed\n"
        f"{indent}  supports_vision:\n"
        f"{indent}    value: false\n"
        f"{indent}    provenance: assumed\n"
        f"{indent}  supports_json_schema:\n"
        f"{indent}    value: false\n"
        f"{indent}    provenance: assumed\n"
        f"{indent}fallback_models: {fallback}\n"
    )


def sample_capabilities(
    *,
    context_window: int = 1_048_576,
    streaming: bool = True,
) -> Capabilities:
    """The typed equivalent of :func:`capabilities_yaml`, for direct model construction."""
    return Capabilities(
        context_window=CapabilityValue[int](
            value=context_window,
            provenance="published",
            source="https://example.test/model-docs, retrieved 2026-11-04",
        ),
        supports_streaming=CapabilityValue[bool](
            value=streaming,
            provenance="measured",
            source="test measurement, 1/1 streamed, 2026-11-04",
        ),
        supports_tools=CapabilityValue[bool](value=False, provenance="assumed"),
        supports_vision=CapabilityValue[bool](value=False, provenance="assumed"),
        supports_json_schema=CapabilityValue[bool](value=False, provenance="assumed"),
    )


MINIMAL_CONFIG = (
    "models:\n"
    '  - id: "google/gemini-3.5-flash-lite"\n'
    '    provider: "google"\n'
    '    provider_id: "gemini-3.5-flash-lite"\n' + capabilities_yaml()
)
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
    """Set a dummy key in the slot the shipped configuration declares.

    The variable name is derived by ``key_env_var("google", "primary")``, so a
    change to the derivation breaks these fixtures loudly rather than silently
    leaving every test without a credential.
    """
    from free_router.core.config import key_env_var

    variable = key_env_var("google", "primary")

    def _set(value: str = "test-google-key-primary") -> str:
        monkeypatch.setenv(variable, value)
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
def reset_debug_logging() -> Iterator[None]:
    """Restore the debug payload logger to disabled after every test.

    ``debug_logging`` keeps its enabled flag in a module global, because it is
    read on every call site in the request path. Without this, a test that enables
    payload logging would silently enable it for every test that follows, and the
    "disabled by default" guarantee would only ever be verified once per run.
    """
    from free_router.core.debug_logging import configure_debug_payload_logging

    yield
    configure_debug_payload_logging({})


@pytest.fixture(autouse=True)
def _isolate_debug_logging(reset_debug_logging: None) -> None:
    """Apply :func:`reset_debug_logging` to every test in the suite."""
    assert reset_debug_logging is None


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
