"""The provider read timeout is configuration, not a constant.

The read bound spans the provider's *generation*, which is unbounded in
principle and varies by orders of magnitude. It used to be 10 seconds baked into
a module constant, so when a provider answered more slowly than that, **every**
request returned 504 and no configuration could change it.

These tests pin the three things that defect consisted of: the value is
configurable, it is validated, and configuring it actually changes what the
router does.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from free_router.api.main import http_client_timeout
from free_router.core.config import Config, ModelConfig
from free_router.providers.google.adapter import GOOGLE_API_BASE

MODEL = "google/gemini-3.5-flash-lite"
PROVIDER_MODEL_ID = "gemini-3.5-flash-lite"
GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/{PROVIDER_MODEL_ID}:generateContent"

BASE_CONFIG = """\
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
"""


def body() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": "hello"}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3, "totalTokenCount": 7},
    }


def minimal_config(**overrides: Any) -> Config:
    """A config with one model, plus any override under test."""
    return Config(
        models=[ModelConfig(id=MODEL, provider="google", provider_id=PROVIDER_MODEL_ID)],
        **overrides,
    )


# --- the value itself --------------------------------------------------------


def test_the_read_timeout_defaults_to_two_minutes() -> None:
    """Covering the workload, not picked for comfort.

    The router reserves against a 1025-token output assumption; at the 15-30
    tokens/second a loaded free tier delivers, a full response takes 34-68 s.
    A smaller bound would refuse work the provider was willing to do.
    """
    assert minimal_config().provider_read_timeout_seconds == 120.0


@pytest.mark.parametrize("value", [0.0, -1.0, -0.001])
def test_a_non_positive_read_timeout_is_rejected(value: float) -> None:
    """A zero bound would fail every request, so it is refused at startup."""
    with pytest.raises(ValidationError):
        minimal_config(provider_read_timeout_seconds=value)


def test_the_read_timeout_is_configurable() -> None:
    assert minimal_config(provider_read_timeout_seconds=90.0).provider_read_timeout_seconds == 90.0


# --- the timeout object ------------------------------------------------------


def test_only_read_comes_from_configuration() -> None:
    """The other phases are stable constants, not settings.

    ``connect``, ``pool`` and ``write`` describe the router's own local
    connection behaviour; ``read`` spans the provider's generation. Conflating
    them is how a 400 ms routing budget once ended up bounding a provider call.
    """
    timeout = http_client_timeout(minimal_config(provider_read_timeout_seconds=45.0))

    assert timeout.read == 45.0
    assert timeout.connect == 0.25
    assert timeout.pool == 0.05
    assert timeout.write == 1.0


def test_the_timeout_tracks_the_configured_value() -> None:
    first = http_client_timeout(minimal_config(provider_read_timeout_seconds=11.0))
    second = http_client_timeout(minimal_config(provider_read_timeout_seconds=12.0))

    assert first.read == 11.0
    assert second.read == 12.0


# --- the app actually uses it ------------------------------------------------


@pytest.fixture
def app_factory(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str], FastAPI]]:
    """A factory for a started app whose config can be varied per test."""
    from free_router.api.main import create_app

    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    def _make(contents: str = BASE_CONFIG) -> FastAPI:
        write_config(contents)
        return create_app()

    yield _make


def test_the_app_client_uses_the_configured_timeout(
    app_factory: Callable[[str], FastAPI],
) -> None:
    """The wiring that was missing: config reaches the shared client."""
    with TestClient(app_factory(BASE_CONFIG + "\nprovider_read_timeout_seconds: 77\n")) as client:
        assert client.app.state.http_client.timeout.read == 77.0


@respx.mock
def test_a_slow_response_within_the_configured_budget_succeeds(
    app_factory: Callable[[str], FastAPI],
) -> None:
    """A slow body is waited for rather than abandoned.

    The delay is short in absolute terms so the suite stays fast; what is proven
    is that the *client* is built from the configured bound and that a lopsided
    response still completes.

    **What this cannot prove:** that a response *exceeding* the configured bound
    is refused. ``respx`` replaces the transport, so the delay happens inside
    the mock and httpx's read timer never runs. That was verified against the
    real provider instead: at ``read=10`` a 15-50 s response returned ``504``
    after 10.27 s, and at ``read=120`` the same traffic returned ``200``.
    """
    started = time.monotonic()

    async def delay(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=body())

    respx.post(GENERATE_URL).mock(side_effect=delay)

    with TestClient(app_factory(BASE_CONFIG + "\nprovider_read_timeout_seconds: 5\n")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    # The delay really happened: the mock was not short-circuited.
    assert time.monotonic() - started >= 0.3


# --- the flush interval is configuration too ---------------------------------


def test_the_flush_interval_is_configurable(app_factory: Callable[[str], FastAPI]) -> None:
    """``quota_flush_interval_seconds`` (B2) reaches the coordinator.

    Declared, validated and — until this change — silently ignored, so the 20 s
    default was the only value that ever took effect.
    """
    with TestClient(app_factory(BASE_CONFIG + "\nquota_flush_interval_seconds: 3\n")) as client:
        assert client.app.state.persistence.flush_interval == 3.0
