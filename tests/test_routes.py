"""Ingress routing: normalization, model validation, and error envelopes."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from conftest import LogRecorder
from fastapi.testclient import TestClient

from free_router.providers.google.adapter import GOOGLE_API_BASE

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"

VALID_BODY = {
    "model": "google/gemini-3.5-flash-lite",
    "messages": [{"role": "user", "content": "hello"}],
}


def _post(client: TestClient, body: object) -> dict[str, object]:
    response = client.post("/v1/chat/completions", json=body)
    assert response.headers["content-type"].startswith("application/json")
    payload: dict[str, object] = response.json()
    return payload


GEMINI_BODY = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello from gemini"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ]
}


@respx.mock
def test_chat_completion_returns_openai_object(client: TestClient) -> None:
    """A valid request produces a standard chat.completion envelope."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    response = client.post("/v1/chat/completions", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "google/gemini-3.5-flash-lite"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello from gemini",
    }
    assert body["id"].startswith("chatcmpl-")


@respx.mock
def test_request_reaches_google_with_translated_body(client: TestClient) -> None:
    """The provider receives Gemini-shaped contents and a header-based key."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    client.post("/v1/chat/completions", json=VALID_BODY)

    request = route.calls[0].request
    sent = json.loads(request.content)
    assert sent["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert "generationConfig" not in sent
    assert request.headers["x-goog-api-key"] == "test-gemini-key"
    assert "test-gemini-key" not in str(request.url)


@respx.mock
def test_provider_auth_error_returns_401(client: TestClient) -> None:
    """Scenario 8 through the full stack: provider 401 becomes client 401."""
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )

    response = client.post("/v1/chat/completions", json=VALID_BODY)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "provider_auth_error"
    assert error["type"] == "api_error"


@respx.mock
def test_provider_server_error_returns_502(client: TestClient) -> None:
    """Scenario 9 through the full stack: provider 5xx becomes client 502."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(500, text="boom"))

    response = client.post("/v1/chat/completions", json=VALID_BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_server_error"


@respx.mock
def test_provider_timeout_returns_504(client: TestClient) -> None:
    """Scenario 10 through the full stack: timeout becomes client 504."""
    respx.post(GENERATE_URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    response = client.post("/v1/chat/completions", json=VALID_BODY)

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["code"] == "provider_timeout"
    assert error["type"] == "timeout_error"


@respx.mock
def test_response_is_repeatable(client: TestClient) -> None:
    """The same request yields a stable id, since ids derive from model identity only."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    first = client.post("/v1/chat/completions", json=VALID_BODY).json()["id"]
    second = client.post("/v1/chat/completions", json=VALID_BODY).json()["id"]

    assert first == second


@respx.mock
def test_empty_messages_returns_openai_error(client: TestClient) -> None:
    """Scenario 6: an empty messages array is a 400 with an OpenAI error body."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    response = client.post(
        "/v1/chat/completions", json={"model": VALID_BODY["model"], "messages": []}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "messages"
    assert error["code"] == "empty_messages"
    assert not route.called, "the request must be rejected before any provider call"


@respx.mock
def test_unsupported_model_is_rejected(client: TestClient) -> None:
    """Scenario 7: an unconfigured model is refused without dispatching."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    response = client.post(
        "/v1/chat/completions", json={"model": "gpt-4", "messages": VALID_BODY["messages"]}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_model"
    assert error["param"] == "model"
    assert "gpt-4" in error["message"]
    assert not route.called, "no dispatch may occur for an unconfigured model"


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


@respx.mock
def test_unsupported_parameters_are_stripped_not_rejected(client: TestClient) -> None:
    """Scenario 5: temperature and max_tokens are ignored, not fatal."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    response = client.post(
        "/v1/chat/completions",
        json={**VALID_BODY, "temperature": 0.7, "max_tokens": 100},
    )

    assert response.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    assert "generationConfig" not in sent
    assert "temperature" not in str(sent)


def test_unknown_route_returns_openai_error(client: TestClient) -> None:
    """Even a 404 uses the OpenAI error envelope."""
    response = client.get("/v1/nope")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


@respx.mock
def test_request_id_header_is_returned(client: TestClient) -> None:
    """A caller-supplied request id is echoed for correlation."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    response = client.post(
        "/v1/chat/completions", json=VALID_BODY, headers={"X-Request-ID": "abc123"}
    )

    assert response.headers["X-Request-ID"] == "abc123"


@respx.mock
def test_logs_contain_no_prompt_text(client: TestClient, json_logs: LogRecorder) -> None:
    """Prompt text must not reach the operational log stream (spec 9)."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

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


def test_configured_model_is_the_only_one_served(client: TestClient) -> None:
    """Only the configured model resolves; nothing is silently substituted."""
    from free_router.api.routes import resolve_model
    from free_router.core.exceptions import UnsupportedModelError

    config = client.app.state.config

    assert resolve_model(config, "google/gemini-3.5-flash-lite") == (
        "google",
        "gemini-3.5-flash-lite",
    )
    with pytest.raises(UnsupportedModelError):
        resolve_model(config, "google/gemini-3.5-pro")


@respx.mock
def test_log_output_is_valid_json_with_telemetry(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Operational records stay parseable JSON and carry the expected fields."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    client.post("/v1/chat/completions", json=VALID_BODY)

    by_name = {event.get("event"): event for event in json_logs.records}

    assert "request_completed" in by_name
    assert by_name["request_completed"]["http_status"] == 200
    assert "latency_ms" in by_name["request_completed"]
    assert "request_id" in by_name["request_completed"]
    assert "provider_dispatched" in by_name
    assert by_name["provider_dispatched"]["model"] == VALID_BODY["model"]
    assert by_name["provider_dispatched"]["provider"] == "google"


@respx.mock
def test_provider_timeout_log_has_no_secret_or_prompt(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """A provider failure log must not echo the key or the prompt (spec 9)."""
    respx.post(GENERATE_URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    client.post(
        "/v1/chat/completions",
        json={
            "model": VALID_BODY["model"],
            "messages": [{"role": "user", "content": "secret-prompt-text"}],
        },
    )

    rendered = json.dumps(json_logs.records)
    assert "secret-prompt-text" not in rendered
    assert "test-gemini-key" not in rendered


def test_db_file_is_created_in_working_directory(client: TestClient, workdir: Path) -> None:
    """Startup persists the database under the configured path."""
    assert Path(client.app.state.db_engine.url.database).exists()
