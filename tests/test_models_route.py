"""``GET /v1/models`` -- a projection of configuration (T053, T054).

The endpoint's whole claim is that it has **no second source of truth**: the ids
it reports are ``config.models``, the same list the dispatcher routes over. These
tests pin that, the B5 response shape decisions, and scenario 8's requirement that
a cooling model is still *listed*.

The cooldown in the scenario-8 test is produced by a real rate-limited dispatch
rather than by inserting a record directly, because the point is that provider
state reaches the listing through the running application -- a test that built a
``CooldownManager`` itself would pass even if nothing were wired.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from free_router.providers.google.adapter import GOOGLE_API_BASE
from free_router.providers.mistral.adapter import MISTRAL_API_BASE, MISTRAL_CHAT_PATH

GOOGLE_MODEL = "gemini-3.5-flash-lite"
MISTRAL_MODEL = "ministral-3b-2512"

GOOGLE_GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/{GOOGLE_MODEL}:generateContent"
MISTRAL_CHAT_URL = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"

TWO_MODEL_CONFIG = f"""\
models:
  - id: "google/{GOOGLE_MODEL}"
    provider: "google"
    provider_id: "{GOOGLE_MODEL}"
  - id: "mistral/{MISTRAL_MODEL}"
    provider: "mistral"
    provider_id: "{MISTRAL_MODEL}"
"""


@pytest.fixture
def two_model_client(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A started app declaring one model per provider.

    Both credentials must be present: a declared model makes its key mandatory at
    startup, which is the deployment contract ADR-0002 describes.
    """
    from free_router.api.main import create_app

    (workdir / "config.yaml").write_text(TWO_MODEL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "test-google-key-primary")
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "test-mistral-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    with TestClient(create_app()) as started:
        yield started


# --- shape (T053) ------------------------------------------------------------


def test_every_configured_model_is_listed(two_model_client: TestClient) -> None:
    """Ids come from configuration, in declaration order.

    Order is preserved rather than sorted because it is the operator's stated
    preference in ``config.yaml``; there is nothing more authoritative to sort by.
    """
    response = two_model_client.get("/v1/models")

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["data"]] == [
        f"google/{GOOGLE_MODEL}",
        f"mistral/{MISTRAL_MODEL}",
    ]


def test_the_response_is_an_openai_list_envelope(two_model_client: TestClient) -> None:
    body = two_model_client.get("/v1/models").json()

    assert body["object"] == "list"
    assert isinstance(body["data"], list)


def test_each_entry_is_an_openai_model_object(two_model_client: TestClient) -> None:
    """``object`` is required by spec section 5 and easy to omit by accident."""
    for entry in two_model_client.get("/v1/models").json()["data"]:
        assert entry["object"] == "model"
        assert isinstance(entry["id"], str)
        assert isinstance(entry["created"], int)
        assert isinstance(entry["owned_by"], str)


def test_permission_and_default_are_emitted(two_model_client: TestClient) -> None:
    """B5: the keys are present even though their values carry nothing.

    Omitting them changes client behaviour, so their absence is a compatibility
    bug rather than a tidiness question.
    """
    for entry in two_model_client.get("/v1/models").json()["data"]:
        assert entry["permission"] == []
        assert entry["default"] is None


def test_owned_by_is_the_deployment_owner_not_the_provider(
    two_model_client: TestClient,
) -> None:
    """The provider is already in the id; repeating it would add nothing (B5)."""
    owners = {entry["owned_by"] for entry in two_model_client.get("/v1/models").json()["data"]}

    assert owners == {"free-router"}


def test_owned_by_and_created_come_from_configuration(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both B5 inputs are configuration-driven, not hardcoded.

    ``owned_by`` is an operator identity, and ``created`` must describe the file
    the models were read from -- so setting one and touching the other changes
    exactly one field.
    """
    from free_router.api.main import create_app

    config_path = workdir / "config.yaml"
    config_path.write_text(TWO_MODEL_CONFIG + "\nowned_by: acme-research\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "test-google-key-primary")
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "test-mistral-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    with TestClient(create_app()) as client:
        body = client.get("/v1/models").json()

    assert {entry["owned_by"] for entry in body["data"]} == {"acme-research"}
    assert {entry["created"] for entry in body["data"]} == {int(config_path.stat().st_mtime)}


def test_created_is_the_config_mtime_not_now(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stable ``created`` matters: a client seeing it move treats the model as updated.

    The config file's mtime is backdated, because a file written moments ago has a
    mtime indistinguishable from "now" and would let a ``time.time()`` bug pass.
    """
    from free_router.api.main import create_app

    config_path = workdir / "config.yaml"
    config_path.write_text(TWO_MODEL_CONFIG, encoding="utf-8")
    backdated = int(time.time()) - 400 * 86_400
    os.utime(config_path, (backdated, backdated))
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "test-google-key-primary")
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "test-mistral-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    with TestClient(create_app()) as client:
        first = client.get("/v1/models").json()["data"]
        second = client.get("/v1/models").json()["data"]

    assert {entry["created"] for entry in first} == {backdated}
    assert [entry["created"] for entry in first] == [entry["created"] for entry in second]


def test_there_is_no_model_detail_route(two_model_client: TestClient) -> None:
    """Spec section 5 defines the listing only.

    ``GET /v1/models/{id}`` is not in the specification, so it is not implemented
    -- an unspecified endpoint is surface area nobody asked for and nothing tests.
    """
    assert two_model_client.get(f"/v1/models/google/{GOOGLE_MODEL}").status_code == 404


# --- availability (T054) -----------------------------------------------------


def test_configured_models_are_available_when_healthy(two_model_client: TestClient) -> None:
    for entry in two_model_client.get("/v1/models").json()["data"]:
        assert entry["available"] is True


@respx.mock
def test_a_cooling_model_is_still_listed(two_model_client: TestClient) -> None:
    """Scenario 8: the mistral model is rate-limited, and still appears.

    The cooldown is produced by a real 429 through the dispatcher, so this fails
    if cooldown state never reaches the route.
    """
    respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(429, json={"object": "error", "type": "rate_limit_error"})
    )

    dispatched = two_model_client.post(
        "/v1/chat/completions",
        json={
            "model": f"mistral/{MISTRAL_MODEL}",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert dispatched.status_code == 429

    body = two_model_client.get("/v1/models").json()
    listed = {entry["id"]: entry for entry in body["data"]}

    # Still present -- that is the whole requirement.
    assert set(listed) == {f"google/{GOOGLE_MODEL}", f"mistral/{MISTRAL_MODEL}"}
    # ...and the state travels in its own field rather than by removal.
    assert listed[f"mistral/{MISTRAL_MODEL}"]["available"] is False
    assert listed[f"google/{GOOGLE_MODEL}"]["available"] is True


@respx.mock
def test_a_cooling_model_does_not_change_the_list_shape(two_model_client: TestClient) -> None:
    """Availability is a value, so the envelope and ids survive a rate limit.

    A client diffing two responses during a cooldown window must see identical
    ids; only the flag moves.
    """
    before = two_model_client.get("/v1/models").json()

    respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(429, json={"object": "error", "type": "rate_limit_error"})
    )
    two_model_client.post(
        "/v1/chat/completions",
        json={
            "model": f"mistral/{MISTRAL_MODEL}",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    after = two_model_client.get("/v1/models").json()

    assert before["object"] == after["object"] == "list"
    assert [entry["id"] for entry in before["data"]] == [entry["id"] for entry in after["data"]]
    assert [entry["created"] for entry in before["data"]] == [
        entry["created"] for entry in after["data"]
    ]


def test_the_listing_does_not_spend_quota(two_model_client: TestClient) -> None:
    """Discovery is a projection, so it must not touch a provider (spec section 5).

    Asserted through the registry rather than by counting connector calls, because
    the failure this guards against is a discovery call being added later.
    """
    dispatcher: Any = two_model_client.app.state.dispatcher
    before = dispatcher.quota.export_windows()

    two_model_client.get("/v1/models")

    assert dispatcher.quota.export_windows() == before
