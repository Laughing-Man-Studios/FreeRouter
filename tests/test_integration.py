"""End-to-end integration tests: ingress to egress through the full stack.

These deliberately overlap the unit suites at each layer. The point is not to
re-test the normalizer or the adapter in isolation, but to prove that the
seams between them hold: an OpenAI-shaped body in, a Gemini-shaped request on
the wire, a normalized response back, and an OpenAI completion out.

The provider is mocked at the HTTP boundary, so these assert the *contract*
between layers without touching the network or spending quota.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import LogRecorder
from fastapi.testclient import TestClient

from free_router.core.config import load_config
from free_router.providers.google.adapter import GOOGLE_API_BASE
from free_router.providers.mistral.adapter import MISTRAL_API_BASE, MISTRAL_CHAT_PATH

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"

MODEL = "google/gemini-3.5-flash-lite"

VALID_LIST_BODY = [{"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}]
"""A JSON array body, which is valid JSON but not a valid request object."""


def gemini_response(text: str = "hello from gemini", finish_reason: str = "STOP") -> dict[str, Any]:
    """Build a realistic ``generateContent`` success body."""
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}], "role": "model"},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 3,
            "totalTokenCount": 7,
        },
    }


def mock_google(status: int = 200, json_body: Any = None, text: str | None = None) -> Any:
    """Install a single ``respx`` route for the Gemini endpoint."""
    if text is not None:
        route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(status, text=text))
    else:
        route = respx.post(GENERATE_URL).mock(
            return_value=httpx.Response(status, json=json_body or gemini_response())
        )
    return route


# --- Scenario 1: the full happy path ---


@respx.mock
def test_full_ingress_to_egress_round_trip(client: TestClient) -> None:
    """An OpenAI request becomes a Gemini call and an OpenAI completion.

    This is M0's exit criterion in miniature: every layer participates, and the
    only thing mocked is the provider itself.
    """
    route = mock_google()

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    body = response.json()

    # Egress is a valid OpenAI chat completion.
    assert body["object"] == "chat.completion"
    assert body["model"] == MODEL
    assert body["id"].startswith("chatcmpl-")
    assert isinstance(body["created"], int)
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello from gemini"},
            "finish_reason": "stop",
        }
    ]
    assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}

    # The provider actually received a Gemini-shaped request.
    assert route.called
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}


@respx.mock
def test_auth_header_is_present_and_key_is_not_in_url(client: TestClient) -> None:
    """The key travels in a header, so it cannot leak via a URL."""
    route = mock_google()

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "test-gemini-key"
    assert "test-gemini-key" not in str(request.url)
    assert "key=" not in str(request.url)


# --- Translation across the boundary ---


@respx.mock
def test_system_message_reaches_provider_as_system_instruction(client: TestClient) -> None:
    """An OpenAI system turn becomes Gemini's top-level systemInstruction."""
    route = mock_google()

    client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        },
    )

    sent = json.loads(route.calls[0].request.content)
    assert sent["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert sent["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


@respx.mock
def test_multi_turn_conversation_preserves_order(client: TestClient) -> None:
    """A full conversation arrives at the provider in the order sent."""
    route = mock_google()

    client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ],
        },
    )

    sent = json.loads(route.calls[0].request.content)
    assert [item["parts"][0]["text"] for item in sent["contents"]] == [
        "one",
        "two",
        "three",
    ]
    assert [item["role"] for item in sent["contents"]] == ["user", "assistant", "user"]


@respx.mock
def test_unsupported_parameters_never_reach_the_provider(client: TestClient) -> None:
    """Scenario 5: stripped parameters are absent from the provider payload."""
    route = mock_google()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "max_tokens": 100,
            "top_p": 0.9,
            "stream": True,
        },
    )

    assert response.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    assert set(sent) == {"contents"}
    for stripped in ("temperature", "max_tokens", "top_p", "stream", "generationConfig"):
        assert stripped not in json.dumps(sent)


@respx.mock
@pytest.mark.parametrize(
    ("gemini_reason", "openai_reason"),
    [("STOP", "stop"), ("MAX_TOKENS", "length"), ("SAFETY", "content_filter")],
)
def test_finish_reason_survives_the_round_trip(
    client: TestClient, gemini_reason: str, openai_reason: str
) -> None:
    """Gemini's finish vocabulary is translated on the way out."""
    mock_google(json_body=gemini_response(finish_reason=gemini_reason))

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == openai_reason


@respx.mock
def test_multi_part_response_is_joined(client: TestClient) -> None:
    """Several content parts concatenate into one assistant message."""
    body = {
        "candidates": [
            {
                "content": {"parts": [{"text": "part one "}, {"text": "part two"}]},
                "finishReason": "STOP",
            }
        ]
    }
    mock_google(json_body=body)

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    content = response.json()["choices"][0]["message"]["content"]
    assert content == "part one part two"


# --- Error mapping pipeline (Scenarios 6, 7, 8, 9, 10) ---


@pytest.mark.parametrize(
    ("exception_name", "expected_status"),
    [
        ("ProviderAuthError", 401),
        ("ProviderRateLimitError", 429),
        ("ProviderValidationError", 400),
        ("ProviderServerError", 502),
        ("ProviderTimeoutError", 504),
        ("ValidationError", 400),
        ("UnsupportedModelError", 400),
    ],
)
def test_every_exception_maps_to_its_documented_status(
    exception_name: str, expected_status: int
) -> None:
    """Spec 10 requires an exact status for each error class.

    Direct coverage of the mapping table, because the table's failure mode is
    silent: an exception missing from it inherits a neighbour's status instead
    of raising. That is how a provider 400 came to return 502, which the
    adapter-level tests could not catch because they never assert the HTTP
    status the client sees.

    Every concrete subclass of RouterBaseError must appear here. Adding one
    without a mapping fails this test only if its status is wrong, so the
    completeness check below is what actually prevents a regression.
    """
    from free_router.api.main import _status_for
    from free_router.core import exceptions

    error_type = getattr(exceptions, exception_name)
    assert _status_for(error_type("boom")) == expected_status


def test_no_exception_is_missing_from_the_status_map() -> None:
    """Every concrete RouterBaseError subclass must declare a status.

    Guards the specific bug this file exists to prevent: a new exception type
    added to the hierarchy without a mapping entry silently reports the wrong
    HTTP status, and no single test would necessarily notice.
    """
    from free_router.api.main import PROVIDER_ERROR_STATUS_MAP
    from free_router.core import exceptions as exceptions_module
    from free_router.core.exceptions import ProviderBaseError, RouterBaseError

    # Base classes are excluded: they are never raised directly, and
    # _status_for already has a fallback for them.
    bases = {RouterBaseError, ProviderBaseError}
    concrete = {
        name
        for name, obj in vars(exceptions_module).items()
        if isinstance(obj, type)
        and issubclass(obj, RouterBaseError)
        and obj not in bases
        and not name.startswith("_")
    }
    mapped = {error_type.__name__ for error_type in PROVIDER_ERROR_STATUS_MAP}

    assert concrete == mapped, (
        "every concrete RouterBaseError needs an explicit status. "
        f"Missing: {sorted(concrete - mapped)}. Extra: {sorted(mapped - concrete)}."
    )


@respx.mock
def test_provider_401_maps_to_client_401(client: TestClient) -> None:
    """Scenario 8: an upstream auth failure surfaces as 401, not 500."""
    mock_google(status=401, json_body={"error": {"message": "API key not valid"}})

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "provider_auth_error"
    assert error["type"] == "api_error"
    assert error["param"] is None


@respx.mock
def test_provider_400_maps_to_client_400(client: TestClient) -> None:
    """An upstream payload rejection surfaces as 400."""
    mock_google(status=400, json_body={"error": {"message": "bad field"}})

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "provider_validation_error"


@respx.mock
def test_provider_invalid_key_maps_to_client_401(client: TestClient) -> None:
    """A bad API key surfaces as 401 even though Google reports 400.

    Verified live: Gemini answers an invalid key with 400 INVALID_ARGUMENT.
    Without classification the client would see 400 and debug its payload
    rather than its credentials.
    """
    mock_google(
        status=400,
        json_body={
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
            }
        },
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_auth_error"


@respx.mock
def test_provider_500_maps_to_client_502(client: TestClient) -> None:
    """Scenario 9: a 5xx becomes 502 Bad Gateway, never a 5xx passthrough."""
    mock_google(status=500, text="upstream exploded")

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_server_error"


@respx.mock
def test_provider_timeout_maps_to_client_504(client: TestClient) -> None:
    """Scenario 10: a timeout becomes 504, not 500."""
    respx.post(GENERATE_URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["code"] == "provider_timeout"
    assert error["type"] == "timeout_error"


@respx.mock
def test_empty_candidates_maps_to_client_502(client: TestClient) -> None:
    """A safety-filtered empty response is a clean error, not a 500.

    Gemini can return zero candidates when a safety filter suppresses the
    reply. Indexing into that list would raise IndexError and surface as an
    opaque 500; it must be a mapped provider error instead.
    """
    mock_google(json_body={"candidates": []})

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_server_error"


@respx.mock
def test_undecodable_provider_body_maps_to_client_502(client: TestClient) -> None:
    """A non-JSON provider reply is an upstream fault, reported as one."""
    mock_google(status=200, text="<html>gateway</html>")

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_server_error"


@respx.mock
def test_empty_messages_never_reaches_the_provider(client: TestClient) -> None:
    """Scenario 6: rejection happens before any outbound call."""
    route = mock_google()

    response = client.post("/v1/chat/completions", json={"model": MODEL, "messages": []})

    assert response.status_code == 400
    assert not route.called
    assert response.json()["error"]["param"] == "messages"


@respx.mock
def test_unsupported_model_never_reaches_the_provider(client: TestClient) -> None:
    """Scenario 7: no fallback dispatch for an unconfigured model."""
    route = mock_google()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code in (400, 404)
    assert not route.called
    assert response.json()["error"]["code"] == "unsupported_model"


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ({"messages": [{"role": "user", "content": "hi"}]}, "missing_required_field"),
        ({"model": MODEL, "messages": []}, "empty_messages"),
        ({"model": MODEL, "messages": [{"content": "hi"}]}, "invalid_message"),
        (VALID_LIST_BODY, "invalid_body"),
    ],
)
def test_malformed_requests_return_openai_errors(
    client: TestClient, body: Any, expected_code: str
) -> None:
    """Every malformed shape yields the OpenAI envelope, never an HTML page."""
    response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == expected_code
    assert set(error) == {"message", "type", "param", "code"}


@respx.mock
def test_no_error_response_ever_leaks_the_key_or_prompt(client: TestClient) -> None:
    """Across every failure mode, neither secret nor prompt is echoed back.

    The provider body is not included in router errors, so even a provider that
    reflects the prompt cannot get it into the client response or the logs.
    """
    secret_prompt = "my-very-secret-prompt"
    mock_google(status=400, json_body={"error": {"message": f"bad: {secret_prompt}"}})

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": secret_prompt}]},
    )

    body = response.text
    assert secret_prompt not in body
    assert "test-gemini-key" not in body


# --- Operational behaviour across the stack ---


@respx.mock
def test_request_id_flows_through_to_logs_and_header(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """A caller-supplied request id ties the client, header, and logs together."""
    mock_google()

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Request-ID": "trace-abc"},
    )

    assert response.headers["X-Request-ID"] == "trace-abc"
    completed = [event for event in json_logs.records if event.get("event") == "request_completed"]
    assert completed, "expected a request_completed record"
    assert all(event["request_id"] == "trace-abc" for event in completed)


@respx.mock
def test_full_stack_logs_are_json_and_carry_telemetry(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Dispatch and completion are logged with operational fields, no payload."""
    mock_google()

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "sensitive"}]},
    )

    by_event = {event.get("event"): event for event in json_logs.records}
    assert "provider_dispatched" in by_event
    assert "provider_responded" in by_event
    assert "request_completed" in by_event

    assert by_event["provider_dispatched"]["model"] == MODEL
    assert by_event["provider_dispatched"]["provider"] == "google"
    assert by_event["request_completed"]["http_status"] == 200

    # Nothing anywhere in the log stream carries message content.
    assert "sensitive" not in json.dumps(json_logs.records)


@respx.mock
def test_repeated_requests_are_independent(client: TestClient) -> None:
    """Two requests produce two complete responses, not shared state."""
    route = mock_google()

    first = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "one"}]},
    )
    second = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "two"}]},
    )

    assert first.status_code == second.status_code == 200
    assert route.call_count == 2
    contents = [
        json.loads(call.request.content)["contents"][0]["parts"][0]["text"] for call in route.calls
    ]
    assert contents == ["one", "two"]


@respx.mock
def test_database_is_ready_before_traffic_is_served(client: TestClient) -> None:
    """Startup completes migrations, so a request never hits a missing table.

    Ties the migration work to observable behaviour: the engine is migrated
    during lifespan, before this request is accepted.
    """
    mock_google()

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200

    # Read through a plain synchronous sqlite3 connection rather than the app's
    # async engine: TestClient drives the app on its own event loop, so the
    # engine cannot be awaited from this thread. Opening the file directly also
    # proves the schema is committed on disk, not merely visible in-app.
    from free_router.db.engine import journal_mode_of

    db_path = str(client.app.state.db_engine.sync_engine.url.database)
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table';")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version;").fetchone()
    finally:
        connection.close()

    assert {"models", "request_logs", "alembic_version"} <= tables
    assert revision is not None
    assert journal_mode_of(db_path).lower() == "wal"


@respx.mock
def test_provider_503_is_not_passthrough(client: TestClient) -> None:
    """Any 5xx maps to 502; no upstream status leaks to the client."""
    mock_google(status=503, text="unavailable")

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502


@respx.mock
def test_network_error_maps_to_a_client_error(client: TestClient) -> None:
    """A connection failure is reported as an upstream fault, not a crash.

    httpx raises transport errors as exceptions rather than returning a
    response, so they bypass the status-code mapping entirely and must be
    caught explicitly. A raw ConnectError escaping to the generic handler would
    surface as a 500 with an unhandled-error log, which misattributes a network
    problem to the router itself.
    """
    respx.post(GENERATE_URL).mock(side_effect=httpx.ConnectError("dns failure"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code in (502, 504)
    error = response.json()["error"]
    assert error["code"].startswith("provider_")
    assert error["type"] in ("api_error", "timeout_error")


# --- Second provider: the seam holds (T036) ---


MISTRAL_MODEL = "ministral-3b-2512"
"""Per Issue #7, the most permissive model on the free tier by a wide margin."""

MISTRAL_CONFIG = f"""\
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
  - id: "mistral/{MISTRAL_MODEL}"
    provider: "mistral"
    provider_id: "{MISTRAL_MODEL}"
"""

MISTRAL_CHAT_URL = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"


def mistral_response(text: str = "hello from mistral") -> dict[str, Any]:
    """Build a realistic ``chat.completions`` success body."""
    return {
        "id": "cmpl-e5cc70bb28c444948073e77776eb30ef",
        "object": "chat.completion",
        "created": 1702256327,
        "model": MISTRAL_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


@pytest.fixture
def mistral_client(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A started app configured with both providers.

    ``gemini_api_key`` is still a required configuration field, so a Mistral-only
    deployment is not expressible yet. That is a Batch 5 (key pool) concern; the
    tests here use both providers so the constraint does not hide the behaviour
    being verified.
    """
    from free_router.api.main import create_app

    (workdir / "config.yaml").write_text(MISTRAL_CONFIG, encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    with TestClient(create_app()) as started:
        yield started


@respx.mock
def test_mistral_model_serves_through_the_unchanged_route(mistral_client: TestClient) -> None:
    """T036: a second provider is a configuration change, not a code change.

    This is the acceptance criterion for the adapter. Both providers are in one
    config, only the Mistral one is requested, and the identical ingress route
    returns an identical OpenAI response envelope. Nothing in the ingress layer,
    the routing core, or the Google adapter is exercised differently.
    """
    route = respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(200, json=mistral_response())
    )

    response = mistral_client.post(
        "/v1/chat/completions",
        json={"model": f"mistral/{MISTRAL_MODEL}", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == f"mistral/{MISTRAL_MODEL}"
    assert body["choices"][0]["message"]["content"] == "hello from mistral"
    assert body["choices"][0]["finish_reason"] == "stop"

    # The Google endpoint was never touched: dispatch followed the config. Only
    # the Mistral route is mocked, and respx.mock asserts every outbound request
    # matches a route, so a call to Gemini would have failed this test outright.
    assert route.call_count == 1

    # The provider-native id is what was sent, not the router's external id.
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == MISTRAL_MODEL


@respx.mock
def test_both_providers_serve_from_one_running_router(mistral_client: TestClient) -> None:
    """Google and Mistral coexist in one process, selected purely by model id.

    The point of the adapter seam is that a second provider costs a config
    entry. If this needed anything beyond a different model id in the request,
    the seam would not be doing its job.
    """
    google_route = respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(200, json=gemini_response())
    )
    mistral_route = respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(200, json=mistral_response("hello from mistral"))
    )

    google_response = mistral_client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    mistral_http_response = mistral_client.post(
        "/v1/chat/completions",
        json={"model": f"mistral/{MISTRAL_MODEL}", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert google_response.status_code == 200
    assert mistral_http_response.status_code == 200
    assert google_response.json()["choices"][0]["message"]["content"] == "hello from gemini"
    assert mistral_http_response.json()["choices"][0]["message"]["content"] == "hello from mistral"
    assert google_route.called
    assert mistral_route.called


@respx.mock
def test_mistral_rate_limit_surfaces_as_429_not_400(mistral_client: TestClient) -> None:
    """A rate limit is capacity, not a malformed request.

    Reporting it as 400 invalid_request_error tells the client operator that
    their payload is wrong, which sends them to debug the wrong thing entirely.
    """
    respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(429, json={"object": "error", "type": "rate_limit_error"})
    )

    response = mistral_client.post(
        "/v1/chat/completions",
        json={"model": f"mistral/{MISTRAL_MODEL}", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "provider_rate_limited"
    assert error["type"] == "rate_limit_error"


@respx.mock
def test_google_rate_limit_surfaces_as_429_not_400(client: TestClient) -> None:
    """The same fix applies to the provider that shipped in M0.

    M0 routed every 4xx to ProviderValidationError, so a Google 429 was
    reported to the client as a 400 payload error.
    """
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "provider_rate_limited"


@respx.mock
def test_mistral_auth_failure_surfaces_as_401(mistral_client: TestClient) -> None:
    respx.post(MISTRAL_CHAT_URL).mock(
        return_value=httpx.Response(401, json={"object": "error", "type": "authentication_error"})
    )

    response = mistral_client.post(
        "/v1/chat/completions",
        json={"model": f"mistral/{MISTRAL_MODEL}", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_auth_error"


def test_mistral_model_requires_no_schema_change(
    workdir: Path, set_api_key: Callable[[], str]
) -> None:
    """A Mistral entry is accepted by the existing model configuration unchanged.

    T024 introduces a general provider/model identity registry. Until then this
    asserts what is true now: ``ModelConfig`` already carries provider and
    provider_id, so a second provider is a config entry rather than a change to
    the configuration schema.
    """
    set_api_key()
    path = workdir / "config.yaml"
    path.write_text(MISTRAL_CONFIG, encoding="utf-8")

    config = load_config()

    assert [entry.id for entry in config.models] == [
        "google/gemini-3.5-flash-lite",
        f"mistral/{MISTRAL_MODEL}",
    ]
    assert config.models[1].provider == "mistral"
