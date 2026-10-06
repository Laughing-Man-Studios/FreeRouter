"""Telemetry completeness on the request_completed record.

Issue #48. The M0 epic requires the operational log stream to emit request id,
provider, model, HTTP status, and latency. All five were emitted, but split
across two records: ``request_completed`` carried only the first, status, and
latency, because the route's ``log_context`` binding was unwound before the
middleware logged.

These tests assert the completion record is self-contained, which is what the
criterion means in practice.
"""

import json

import httpx
import respx
from conftest import LogRecorder
from fastapi.testclient import TestClient

from free_router.providers.google.adapter import GOOGLE_API_BASE

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"
MODEL = "google/gemini-3.5-flash-lite"

GEMINI_BODY = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello from gemini"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ]
}

REQUIRED_FIELDS = ("request_id", "provider", "model", "http_status", "latency_ms")


def _completed(json_logs: LogRecorder) -> dict[str, object]:
    """Return the single request_completed record, failing if absent or duplicated."""
    records = [record for record in json_logs.records if record.get("event") == "request_completed"]
    assert len(records) == 1, f"expected exactly one request_completed, got {len(records)}"
    return records[0]


@respx.mock
def test_completion_record_carries_all_required_telemetry(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """The M0 epic's telemetry fields all appear on one record (issue #48)."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    record = _completed(json_logs)
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    assert not missing, f"request_completed is missing {missing}"


@respx.mock
def test_completion_record_values_are_correct(client: TestClient, json_logs: LogRecorder) -> None:
    """The fields must describe this request, not a neighbouring one."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Request-ID": "trace-xyz"},
    )

    record = _completed(json_logs)
    assert record["provider"] == "google"
    assert record["model"] == MODEL
    assert record["request_id"] == "trace-xyz"
    assert record["http_status"] == 200
    assert isinstance(record["latency_ms"], (int, float))


@respx.mock
def test_sequential_requests_do_not_share_telemetry(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Each completion record must carry its own request's context.

    Guards the reset-on-exit behaviour of log_context: a binding leaking from
    one request into the next would show here as a record whose request_id
    disagrees with its provider or model.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    for index in (1, 2):
        client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": f"req{index}"}]},
            headers={"X-Request-ID": f"trace-{index}"},
        )

    records = [r for r in json_logs.records if r.get("event") == "request_completed"]
    assert [r["request_id"] for r in records] == ["trace-1", "trace-2"]
    assert all(r["provider"] == "google" for r in records)


@respx.mock
def test_provider_error_completion_record_still_carries_telemetry(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """A provider failure is the case where attribution matters most."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(500, text="upstream boom"))

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    record = _completed(json_logs)
    assert record["http_status"] == 502
    assert record["provider"] == "google"
    assert record["model"] == MODEL


@respx.mock
def test_completion_record_for_rejected_request_omits_unknown_fields(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """A request rejected before provider resolution logs cleanly.

    There is no provider or model to attribute, so those fields are absent
    rather than null or misleading. The record must still be valid JSON and
    still carry the status.
    """
    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
    )

    record = _completed(json_logs)
    assert record["http_status"] == 400
    assert record.get("model") is None
    assert record.get("provider") is None
    # Never the string "None", which would be an easy way to mislead a reader.
    assert "None" not in {value for value in record.values()}


@respx.mock
def test_enriched_record_still_excludes_prompt_text(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Adding telemetry must not weaken the privacy guarantee (spec 9)."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "secret-prompt-text"}]},
    )

    rendered = json.dumps(json_logs.records)
    assert "secret-prompt-text" not in rendered
    assert "hello from gemini" not in rendered


@respx.mock
def test_completion_record_survives_a_malformed_request(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Malformed JSON still produces a well-formed completion record."""
    client.post(
        "/v1/chat/completions",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )

    record = _completed(json_logs)
    assert record["http_status"] == 400
    assert "request_id" in record
    assert "latency_ms" in record


def test_health_request_completion_record_is_attributable(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """/health has no provider or model, and must not invent them."""
    client.get("/health")

    record = _completed(json_logs)
    assert record["http_status"] == 200
    assert record.get("provider") is None
    assert record.get("model") is None


@respx.mock
def test_records_remain_single_line_json(client: TestClient, json_logs: LogRecorder) -> None:
    """Every emitted line must still parse as one JSON object.

    The enrichment happens by re-binding context, not by changing the format, so
    this guards against a refactor that breaks the one-record-per-line contract
    log consumers depend on.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert json_logs.lines, "expected log output"
    for line in json_logs.lines:
        assert isinstance(json.loads(line), dict)
