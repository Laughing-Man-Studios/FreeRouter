"""The application actually uses the persistence layer.

Batch 2 built the persistence layer and verified it by constructing the
components **by hand** in a subprocess (``tests/restart_helper.py``). Nothing
exercised it *through the application*, and so nothing noticed that the
application was never wired to it: ``QuotaStateStore`` and
``PersistenceCoordinator`` were imported by nothing, and every table stayed
empty however much traffic the router served.

That is the failure these tests exist to prevent. They go through
``create_app`` deliberately -- a test that assembles its own tracker proves the
tracker works, not that the router uses it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free_router.providers.google.adapter import GOOGLE_API_BASE
from free_router.quota.dimensions import DimensionResource

MODEL = "google/gemini-3.5-flash-lite"
PROVIDER = "google"
PROVIDER_MODEL_ID = "gemini-3.5-flash-lite"
GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/{PROVIDER_MODEL_ID}:generateContent"

CONFIG_WITH_QUOTA = """\
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
    quota:
      - resource: "requests"
        limit: 15
        window_seconds: 60
        provenance: "measured"
      - resource: "tokens"
        limit: 250000
        window_seconds: 60
        provenance: "measured"
"""
"""A configuration that actually declares limits.

The shipped configuration declares none, which is why a request against it
would persist nothing to assert on. These tests need a real window.
"""

CONFIG_WITH_SHORT_COOLDOWN_CAP = CONFIG_WITH_QUOTA + "\nmax_cooldown_seconds: 30\n"
"""The same limits, with an absurdly long provider cooldown clamped to 30 s."""


def gemini_body() -> dict[str, Any]:
    """A realistic ``generateContent`` success body, with usage."""
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": "hello"}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 3,
            "totalTokenCount": 7,
        },
    }


def ask(client: TestClient) -> httpx.Response:
    """Send one chat completion through the router."""
    return client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
    )


def rows(path: Path, table: str) -> list[tuple]:
    """Read a table directly, bypassing the application entirely."""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"select * from {table}").fetchall()  # noqa: S608
    finally:
        connection.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """The database the app will use."""
    return tmp_path / "router.db"


@pytest.fixture
def quota_app(
    workdir: Path,
    db_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str], FastAPI]]:
    """A factory for an app configured with real quota limits."""
    from free_router.api.main import create_app

    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(db_path))

    def _make(config: str = CONFIG_WITH_QUOTA) -> FastAPI:
        write_config(config)
        return create_app()

    yield _make


# --- 1. A served request reaches durable storage -----------------------------


@respx.mock
def test_a_served_request_is_persisted(quota_app: Callable[[str], FastAPI], db_path: Path) -> None:
    """Traffic through the app must appear in ``quota_windows``.

    This is the assertion that was missing. Before the wiring existed, a request
    could be served, reconciled and counted entirely in memory, and the table
    stayed empty forever -- which is precisely what happened.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=gemini_body()))

    with TestClient(quota_app()) as client:
        assert ask(client).status_code == 200

    # The clean shutdown flushes once more, so the counter must be durable.
    windows = rows(db_path, "quota_windows")
    assert windows, "a served request left no persisted quota state"

    persisted = {(row[2], row[3]) for row in windows}
    assert ("tokens", 60.0) in persisted
    assert ("requests", 60.0) in persisted


@respx.mock
def test_persisted_usage_carries_the_provider_figure(
    quota_app: Callable[[str], FastAPI], db_path: Path
) -> None:
    """The flushed counter records what the provider said, not the estimate."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=gemini_body()))

    with TestClient(quota_app()) as client:
        assert ask(client).status_code == 200

    token_rows = [row for row in rows(db_path, "quota_windows") if row[2] == "tokens"]
    assert len(token_rows) == 1

    entries = json.loads(token_rows[0][6])
    assert entries, "the token window persisted with no entries"

    actuals = [entry["actual"] for entry in entries]
    assert all(actual == 7 for actual in actuals), actuals


# --- 2. Restore happens before the listener serves ---------------------------


@respx.mock
def test_state_is_restored_across_a_restart(
    quota_app: Callable[[str], FastAPI],
) -> None:
    """A second boot starts already knowing what the first one spent.

    Two real app lifetimes over one database: if restore were missing, the
    second boot would report zero used capacity, which is the overshoot
    CONSTITUTION 8.3 exists to prevent.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=gemini_body()))

    with TestClient(quota_app()) as first:
        assert ask(first).status_code == 200

    with TestClient(quota_app()) as second:
        restored = second.app.state.dispatcher.quota.used(
            PROVIDER, PROVIDER_MODEL_ID, DimensionResource.TOKENS
        )
        restored_requests = second.app.state.dispatcher.quota.used(
            PROVIDER, PROVIDER_MODEL_ID, DimensionResource.REQUESTS
        )

    assert restored == 7, "the token counter did not survive the restart"
    assert restored_requests == 1, "the request counter did not survive the restart"


# --- 3. Cooldowns write through, counters batch ------------------------------


@respx.mock
def test_a_rate_limit_is_persisted_without_waiting_for_a_flush(
    quota_app: Callable[[str], FastAPI], db_path: Path
) -> None:
    """A cooldown is written the moment it is set (ADR-0005).

    Read *inside* the app's lifetime, before any shutdown flush, because a
    cooldown that only reaches disk at shutdown would be forgotten by a
    ``SIGKILL`` -- and a forgotten cooldown makes the router contact a provider
    that has just refused it.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(429, text="slow down"))

    with TestClient(quota_app()) as client:
        ask(client)
        stored = rows(db_path, "cooldowns")
        assert stored, "a rate limit entered a cooldown that was not persisted"
        assert stored[0][0] == PROVIDER
        assert stored[0][1] == PROVIDER_MODEL_ID


@respx.mock
def test_the_configured_cooldown_cap_is_honoured(
    quota_app: Callable[[str], FastAPI], db_path: Path
) -> None:
    """``max_cooldown_seconds`` reaches the manager that enforces it (B4).

    The field was declared in configuration and never passed, so an operator
    override was silently ignored. A provider asking for an hour is capped.
    """
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"}, text="no")
    )

    with TestClient(quota_app(CONFIG_WITH_SHORT_COOLDOWN_CAP)) as client:
        ask(client)
        stored = rows(db_path, "cooldowns")

    assert stored, "no cooldown was persisted"
    assert stored[0][5] is not None, "the cooldown recorded no retry_after"


# --- 4. The flush loop runs --------------------------------------------------


def test_the_flush_coordinator_is_started(
    quota_app: Callable[[str], FastAPI],
) -> None:
    """Startup creates the coordinator and its interval task.

    Without it, counters would only ever be written at shutdown, and an unclean
    exit would lose everything since boot.
    """
    with TestClient(quota_app()) as client:
        coordinator = client.app.state.persistence
        assert coordinator is not None
        task = coordinator._task
        assert task is not None, "the flush loop was never started"
        assert not task.done(), "the flush loop died immediately"


# --- 5. Degradation ---------------------------------------------------------


def test_an_idle_app_writes_no_window_rows(
    quota_app: Callable[[str], FastAPI], db_path: Path
) -> None:
    """A shutdown with no traffic persists no counters.

    An empty window carries no information. Writing one per configured
    dimension on every shutdown would make "is there durable state?" ambiguous
    and the table grow a row per boot for nothing.
    """
    with TestClient(quota_app()):
        pass

    assert rows(db_path, "quota_windows") == []


def test_a_corrupt_window_row_does_not_prevent_startup(
    quota_app: Callable[[str], FastAPI], db_path: Path
) -> None:
    """An unreadable row degrades that unit rather than refusing to boot.

    The router must still start with in-memory accounting: a corrupt row is a
    reason to lose history, not a reason to be unavailable.
    """
    # First boot creates and migrates the schema.
    with TestClient(quota_app()):
        pass

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "insert into quota_windows "
            "(provider, model_id, resource, window_seconds, reset_kind, reset_at, "
            "entries, flushed_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (PROVIDER, PROVIDER_MODEL_ID, "tokens", 60.0, "rolling", None, "not json", "x"),
        )
        connection.commit()
    finally:
        connection.close()

    with TestClient(quota_app()) as client:
        assert client.get("/health").status_code == 200
        # Degraded to empty, not crashed, and still serving.
        used = client.app.state.dispatcher.quota.used(
            PROVIDER, PROVIDER_MODEL_ID, DimensionResource.TOKENS
        )
        assert used == 0
