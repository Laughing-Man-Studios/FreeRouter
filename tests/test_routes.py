"""Ingress routing: normalization, model validation, and error envelopes."""

import json
from pathlib import Path

import pytest
from conftest import LogRecorder
from fastapi.testclient import TestClient

VALID_BODY = {
    "model": "google/gemini-3.5-flash-lite",
    "messages": [{"role": "user", "content": "hello"}],
}


def _post(client: TestClient, body: object) -> dict[str, object]:
    response = client.post("/v1/chat/completions", json=body)
    assert response.headers["content-type"].startswith("application/json")
    payload: dict[str, object] = response.json()
    return payload


def test_chat_completion_returns_openai_object(client: TestClient) -> None:
    """A valid request produces a standard chat.completion envelope."""
    response = client.post("/v1/chat/completions", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "google/gemini-3.5-flash-lite"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["id"].startswith("chatcmpl-")


def test_response_is_repeatable(client: TestClient) -> None:
    """The same request yields a stable id, since ids derive from model identity only."""
    first = client.post("/v1/chat/completions", json=VALID_BODY).json()["id"]
    second = client.post("/v1/chat/completions", json=VALID_BODY).json()["id"]

    assert first == second


def test_empty_messages_returns_openai_error(client: TestClient) -> None:
    """Scenario 6: an empty messages array is a 400 with an OpenAI error body."""
    response = client.post(
        "/v1/chat/completions", json={"model": VALID_BODY["model"], "messages": []}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "messages"
    assert error["code"] == "empty_messages"


def test_unsupported_model_is_rejected(client: TestClient) -> None:
    """Scenario 7: an unconfigured model is refused without dispatching."""
    response = client.post(
        "/v1/chat/completions", json={"model": "gpt-4", "messages": VALID_BODY["messages"]}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_model"
    assert error["param"] == "model"
    assert "gpt-4" in error["message"]


def test_missing_model_field_is_rejected(client: TestClient) -> None:
    """A body without a model names the missing field."""
    response = client.post("/v1/chat/completions", json={"messages": VALID_BODY["messages"]})

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "model"


def test_malformed_json_returns_openai_error(client: TestClient) -> None:
    """A non-JSON body produces the OpenAI envelope, not FastAPI's HTML page."""
    response = client.post(
        "/v1/chat/completions",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_json"


def test_non_object_body_is_rejected(client: TestClient) -> None:
    """A JSON array body is refused as a non-object payload."""
    response = client.post("/v1/chat/completions", json=[VALID_BODY])

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"


def test_unsupported_parameters_are_stripped_not_rejected(
    client: TestClient,
) -> None:
    """Scenario 5: temperature and max_tokens are ignored, not fatal."""
    response = client.post(
        "/v1/chat/completions",
        json={**VALID_BODY, "temperature": 0.7, "max_tokens": 100, "stream": True},
    )

    assert response.status_code == 200


def test_unknown_route_returns_openai_error(client: TestClient) -> None:
    """Even a 404 uses the OpenAI error envelope."""
    response = client.get("/v1/nope")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_request_id_header_is_returned(client: TestClient) -> None:
    """A caller-supplied request id is echoed for correlation."""
    response = client.post(
        "/v1/chat/completions", json=VALID_BODY, headers={"X-Request-ID": "abc123"}
    )

    assert response.headers["X-Request-ID"] == "abc123"


def test_logs_contain_no_prompt_text(client: TestClient, json_logs: LogRecorder) -> None:
    """Prompt text must not reach the operational log stream (spec 9)."""
    client.post(
        "/v1/chat/completions",
        json={
            "model": VALID_BODY["model"],
            "messages": [{"role": "user", "content": "secret-prompt-text"}],
        },
    )

    events = json_logs.records
    assert events, "expected the request to produce at least one log record"
    assert "secret-prompt-text" not in json.dumps(events)


def test_configured_model_is_the_only_one_served(client: TestClient, minimal_config: str) -> None:
    """Only the configured model resolves; nothing is silently substituted."""
    from free_router.api.routes import resolve_model
    from free_router.core.exceptions import UnsupportedModelError

    config = client.app.state.config

    assert resolve_model(config, "google/gemini-3.5-flash-lite") == "gemini-3.5-flash-lite"
    with pytest.raises(UnsupportedModelError):
        resolve_model(config, "google/gemini-3.5-pro")
    assert minimal_config  # the fixture documents the shipped configuration


def test_log_output_is_valid_json_with_telemetry(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Operational records stay parseable JSON and carry the expected fields."""
    client.post("/v1/chat/completions", json=VALID_BODY)

    by_name = {event.get("event"): event for event in json_logs.records}

    assert "request_completed" in by_name
    assert by_name["request_completed"]["http_status"] == 200
    assert "latency_ms" in by_name["request_completed"]
    assert "request_id" in by_name["request_completed"]
    assert "provider_dispatched" in by_name
    assert by_name["provider_dispatched"]["model"] == VALID_BODY["model"]


def test_db_file_is_created_in_working_directory(client: TestClient, workdir: Path) -> None:
    """Startup persists the database under the configured path."""
    assert Path(client.app.state.db_engine.url.database).exists()
