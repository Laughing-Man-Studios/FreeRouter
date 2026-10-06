"""Liveness endpoint used by the Docker healthcheck."""

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from free_router.core.config import ConfigError


def test_health_returns_ok(client: TestClient) -> None:
    """A started app reports healthy."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_needs_no_provider_call(client: TestClient) -> None:
    """The probe never touches a provider, so it cannot spend quota.

    A healthcheck pointed at a chat endpoint would dispatch a real request on
    every probe. This asserts nothing leaves the process.
    """
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent"
    ).mock(return_value=httpx.Response(200, json={}))

    with respx.mock:
        assert client.get("/health").status_code == 200

    assert not route.called


def test_health_does_not_expose_configuration(client: TestClient) -> None:
    """The response reveals no model list, provider detail, or secret."""
    body = client.get("/health").text

    assert "gemini" not in body.lower()
    assert "api_key" not in body.lower()
    assert set(client.get("/health").json()) == {"status"}


def test_health_is_not_under_the_openai_prefix(client: TestClient) -> None:
    """/v1/health does not exist: this is not an OpenAI-compatible surface.

    Advertising operational endpoints inside the OpenAI namespace would suggest
    capabilities an OpenAI client library would try to use.
    """
    assert client.get("/v1/health").status_code == 404


def test_health_is_unavailable_when_startup_fails(workdir: Path) -> None:
    """A probe distinguishes "crashed" from "healthy".

    Startup is fail-fast: the `workdir` fixture chdirs into an empty directory
    with the configuration environment variables cleared, so load_config() fails
    and the lifespan aborts before serving traffic. The health endpoint
    therefore never answers, which is how Docker tells a dead container from a
    working one.
    """
    from free_router.api.main import create_app

    with pytest.raises(ConfigError), TestClient(create_app()):
        pass
