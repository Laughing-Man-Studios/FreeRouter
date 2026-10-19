"""Opt-in debug payload logging and its isolation from the operational stream.

Issue #60 / T038. CONSTITUTION 9.1 requires that prompt text, payload content and
secrets never reach an operational log line. Debug payload logging suspends that
requirement on purpose, so the tests here are about the boundary of the suspension:

* **disabled** -- the guarantee is untouched and no payload is recorded anywhere;
* **enabled** -- the payload is recorded, but *only* in a separate file, and the
  operational stream is byte-for-byte what it would have been.

The second point is the one worth defending. "Debug logging is off by default" is
not a privacy control if turning it on merges into the operational stream, so
isolation is asserted positively -- the payload is present in the debug file and
absent from the operational records of the very same request.

Payload visibility is a deliberate trade, so these tests also pin the two things
that make it defensible: it is off unless explicitly configured, and it redacts
credential-shaped values even when a provider echoes one back.
"""

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from conftest import LogRecorder
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free_router.core.debug_logging import (
    _DEBUG_LOGGER_NAME,
    DEBUG_LOG_ENV_VAR,
    configure_debug_payload_logging,
    debug_enabled,
    debug_payload,
    redact,
)
from free_router.core.logging import JsonFormatter, configure_logging
from free_router.providers.google.adapter import GOOGLE_API_BASE

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"
MODEL = "google/gemini-3.5-flash-lite"

# Distinctive enough that finding it in the wrong stream cannot be a coincidence.
SECRET_PROMPT = "the quick brown fox jumps over 13 lazy dogs"

GEMINI_BODY = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello from gemini"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ]
}


def _chat(client: TestClient, content: str = SECRET_PROMPT) -> None:
    """Issue one chat completion carrying a recognisable prompt."""
    client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": content}]},
    )


def _debug_records(path: Path) -> list[dict[str, object]]:
    """Parse the debug stream, failing if the file is absent or malformed."""
    assert path.exists(), f"expected a debug payload file at {path}"
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _operational_text(json_logs: LogRecorder) -> str:
    """Everything the operational stream emitted, as one searchable string."""
    return json_logs.stream.getvalue()


@pytest.fixture
def debug_file(tmp_path: Path) -> Path:
    return tmp_path / "debug-payloads.log"


@pytest.fixture
def ready_app_with_debug(
    ready_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch, debug_file: Path
) -> Iterator[FastAPI]:
    """An app started with debug payload logging enabled, as an operator would."""
    monkeypatch.setenv(DEBUG_LOG_ENV_VAR, str(debug_file))
    app = ready_app()
    with TestClient(app):
        yield app


# --- disabled: the structural guarantee holds unchanged -----------------------


def test_debug_is_disabled_when_the_variable_is_unset() -> None:
    """Absence of the variable is the default, and it is an explicit one."""
    assert configure_debug_payload_logging({}) is False
    assert debug_enabled() is False


def test_debug_records_nothing_when_disabled(
    debug_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call site that forgets to guard still writes nothing."""
    monkeypatch.delenv(DEBUG_LOG_ENV_VAR, raising=False)
    configure_debug_payload_logging()

    debug_payload(phase="inbound", provider="google", model=MODEL, data={"secret": "value"})

    assert not debug_file.exists()
    assert list(debug_file.parent.glob("debug-payloads.log*")) == []


@respx.mock
def test_operational_stream_carries_no_payload_when_disabled(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """With debug logging off, the prompt reaches no log stream at all.

    This is CONSTITUTION 9.1 as it behaves in practice, not as it is documented.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    _chat(client)

    assert client and json_logs.records, "the request should still have been served and logged"
    assert SECRET_PROMPT not in _operational_text(json_logs)


# --- enabled: recorded, but isolated ------------------------------------------


@respx.mock
def test_payload_is_recorded_in_the_debug_file(
    ready_app_with_debug: FastAPI, debug_file: Path
) -> None:
    """The payload is captured in full, which is the entire point of the feature."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    with TestClient(ready_app_with_debug) as client:
        _chat(client)

    records = _debug_records(debug_file)
    assert records, "debug payload logging is enabled but recorded nothing"

    phases = [record["phase"] for record in records]
    assert "inbound" in phases, f"no inbound payload recorded; saw {phases}"
    assert "wire_request" in phases, f"no wire request recorded; saw {phases}"
    assert "wire_response" in phases, f"no wire response recorded; saw {phases}"

    inbound = next(record for record in records if record["phase"] == "inbound")
    messages = inbound["data"]["normalized"]["messages"]
    assert messages[0]["content"] == SECRET_PROMPT


@respx.mock
def test_debug_output_never_reaches_the_operational_stream(
    ready_app_with_debug: FastAPI, debug_file: Path, json_logs: LogRecorder
) -> None:
    """The isolation guarantee, asserted as a difference rather than an absence.

    The same request produces both streams, so finding the payload in one and not
    the other is a positive result. An assertion that the operational stream merely
    "has no debug records" would pass even if the feature were broken.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    with TestClient(ready_app_with_debug) as client:
        _chat(client)

    assert SECRET_PROMPT in debug_file.read_text(encoding="utf-8"), (
        "precondition: the debug file should contain the payload"
    )
    operational = _operational_text(json_logs)
    assert SECRET_PROMPT not in operational, "payload leaked into the operational stream"
    assert "debug_payload" not in operational
    assert "wire_request" not in operational


def test_debug_logger_does_not_propagate(debug_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``propagate = False`` is the mechanism, asserted directly.

    If this ever flips, a debug record reaches the root handler and therefore
    stdout, which is exactly the leak the isolation test above would catch -- but
    only if that test happens to run.
    """
    monkeypatch.setenv(DEBUG_LOG_ENV_VAR, str(debug_file))
    configure_debug_payload_logging()
    try:
        assert logging.getLogger(_DEBUG_LOGGER_NAME).propagate is False
    finally:
        configure_debug_payload_logging({})


@respx.mock
def test_operational_formatter_still_drops_unknown_fields_when_debug_is_enabled(
    ready_app_with_debug: FastAPI, json_logs: LogRecorder
) -> None:
    """Enabling debug logging does not widen the operational allowlist.

    The operational formatter is unaware of the debug module, so a caller passing
    payload-shaped ``extra=`` still gets nothing through. This is the guarantee
    being *unmodified*, as opposed to merely satisfied.
    """
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    with TestClient(ready_app_with_debug) as client:
        _chat(client, content="payload-shaped probe")

    operational = _operational_text(json_logs)
    assert "payload-shaped probe" not in operational
    assert "messages" not in operational
    assert "content" not in operational


def test_debug_survives_operational_reconfiguration(debug_file: Path) -> None:
    """Rebuilding the root handler tree must not redirect the debug stream.

    ``configure_logging`` removes and reinstalls root handlers. The debug logger is
    re-asserted as non-propagating at configuration time precisely so that a future
    refactor there cannot quietly repoint it at stdout.
    """
    configure_logging("INFO")
    configure_debug_payload_logging({DEBUG_LOG_ENV_VAR: str(debug_file)})
    try:
        assert logging.getLogger(_DEBUG_LOGGER_NAME).propagate is False
        debug_payload(phase="inbound", provider="google", model=MODEL, data={"a": 1})
        assert _debug_records(debug_file)
    finally:
        configure_debug_payload_logging({})


# --- redaction: what makes the trade defensible -------------------------------


def test_redact_replaces_credential_shaped_keys() -> None:
    """Credential-shaped keys are replaced wherever they appear in the structure."""
    redacted = redact(
        {
            "headers": {"Authorization": "Bearer sk-secret", "x-goog-api-key": "AIzaSecret"},
            "nested": [{"api_key": "k"}, {"harmless": "kept"}],
            "apiKey": "k2",
            "user_password": "p",
            "messages": [{"role": "user", "content": "kept in full"}],
        }
    )

    assert redacted["headers"]["Authorization"] == "[redacted]"
    assert redacted["headers"]["x-goog-api-key"] == "[redacted]"
    assert redacted["apiKey"] == "[redacted]"
    assert redacted["user_password"] == "[redacted]"
    assert redacted["nested"][0]["api_key"] == "[redacted]"
    # The point of the stream survives redaction.
    assert redacted["nested"][1]["harmless"] == "kept"
    assert redacted["messages"][0]["content"] == "kept in full"


def test_redact_replaces_private_key_and_other_key_material() -> None:
    """``private_key`` and the other key-material names are redacted.

    The previous rule matched the bare substring ``token``, which mangled
    legitimate usage fields while missing ``private_key`` entirely. A rule that
    produces both kinds of failure is not a rule.
    """
    for key in (
        "private_key",
        "privateKey",
        "signing_key",
        "encryption_key",
        "client_key",
        "client_secret",
        "passphrase",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
        "api_token",
    ):
        redacted = redact({key: "sk-proj-abcdefghijklmnopqrstuvwxyz"})
        assert redacted[key] == "[redacted]", f"{key} was not redacted"


def test_redact_preserves_legitimate_token_usage_fields() -> None:
    """Usage and parameter fields whose names contain "token" survive.

    These are exactly the fields the previous rule destroyed. ``promptTokenCount``
    and its siblings are Gemini's usage metadata, and ``max_tokens`` is a request
    parameter -- the stream exists to show them.
    """
    for key in (
        "promptTokenCount",
        "candidatesTokenCount",
        "totalTokenCount",
        "promptTokensDetails",
        "max_tokens",
        "token_count",
        "tokens",
        "tokenBudget",
    ):
        redacted = redact({key: 1234})
        assert redacted[key] == 1234, f"{key} was wrongly redacted"


def test_redact_leaves_ambiguous_keys_alone_when_the_value_is_not_opaque() -> None:
    """An ambiguous terminal word is only a credential when its value looks like one.

    This is the narrow value-shape check, and it has to leave counts and short
    identifiers alone or it re-creates the over-redaction the phrase rule fixed.
    """
    assert redact({"token": 42})["token"] == 42
    assert redact({"token": "42"})["token"] == "42"
    assert redact({"tokens": 999})["tokens"] == 999
    assert redact({"key_alias": "primary"})["key_alias"] == "primary"
    assert redact({"max_tokens": 512})["max_tokens"] == 512


def test_redact_replaces_an_ambiguous_key_holding_an_opaque_value() -> None:
    """A bare ``token`` holding a JWT-shaped value is still a credential."""
    opaque = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    assert redact({"token": opaque})["token"] == "[redacted]"
    assert redact({"key": opaque})["key"] == "[redacted]"
    assert redact({"bearer": opaque})["bearer"] == "[redacted]"


def test_redact_leaves_ordinary_payloads_untouched() -> None:
    """Redaction must not mangle the content an operator is trying to read."""
    payload = {"contents": [{"parts": [{"text": "hello"}], "role": "user"}], "model": MODEL}
    assert redact(payload) == payload


def test_gemini_usage_metadata_survives_redaction() -> None:
    """The regression that motivated the fix, asserted end to end.

    Gemini reports usage as ``usageMetadata`` with ``promptTokenCount``,
    ``candidatesTokenCount`` and ``totalTokenCount``. The previous rule replaced
    all three with ``[redacted]``, so a debug session could not show what a
    request actually cost.
    """
    body = {
        "candidates": [{"content": {"parts": [{"text": "hi"}], "role": "model"}}],
        "usageMetadata": {
            "promptTokenCount": "8",
            "candidatesTokenCount": "1",
            "totalTokenCount": "9",
            "serviceTier": "standard",
        },
    }

    redacted = redact(body)

    usage = redacted["usageMetadata"]
    assert usage["promptTokenCount"] == "8"
    assert usage["candidatesTokenCount"] == "1"
    assert usage["totalTokenCount"] == "9"
    assert usage["serviceTier"] == "standard"


def test_redaction_applies_to_what_is_actually_written(debug_file: Path) -> None:
    """Redaction is enforced on the write path, not left to the caller."""
    configure_debug_payload_logging({DEBUG_LOG_ENV_VAR: str(debug_file)})
    try:
        debug_payload(
            phase="wire_response",
            provider="mistral",
            model=MODEL,
            data={"body": {"api_key": "sk-live-should-not-appear"}},
        )
    finally:
        configure_debug_payload_logging({})

    text = debug_file.read_text(encoding="utf-8")
    assert "sk-live-should-not-appear" not in text
    assert "[redacted]" in text


@respx.mock
def test_provider_credential_never_reaches_the_debug_file(
    ready_app_with_debug: FastAPI, debug_file: Path
) -> None:
    """Google's credential travels in a header; the wire payload must not carry it."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_BODY))

    with TestClient(ready_app_with_debug) as client:
        _chat(client)

    text = debug_file.read_text(encoding="utf-8")
    assert "x-goog-api-key" not in text
    assert "test-google-key-primary" not in text


# --- operational robustness ---------------------------------------------------


def test_unwritable_destination_disables_rather_than_raising(tmp_path: Path) -> None:
    """Debug logging must never be the reason the proxy fails to serve.

    A path that cannot be created -- here a directory, which cannot be opened as a
    file -- degrades to disabled instead of aborting startup.
    """
    target = tmp_path / "a-directory"
    target.mkdir()
    assert configure_debug_payload_logging({DEBUG_LOG_ENV_VAR: str(target)}) is False
    assert debug_enabled() is False


def test_repeated_configuration_does_not_stack_handlers(debug_file: Path, tmp_path: Path) -> None:
    """Reconfiguring replaces the handler, so a reloaded process writes one line."""
    configure_debug_payload_logging({DEBUG_LOG_ENV_VAR: str(debug_file)})
    configure_debug_payload_logging({DEBUG_LOG_ENV_VAR: str(debug_file)})
    try:
        assert len(logging.getLogger(_DEBUG_LOGGER_NAME).handlers) == 1
        debug_payload(phase="inbound", provider="google", model=MODEL, data={"a": 1})
        assert len(_debug_records(debug_file)) == 1
    finally:
        configure_debug_payload_logging({})


def test_empty_variable_is_treated_as_disabled() -> None:
    """An empty or whitespace value is a misconfiguration, not an instruction."""
    assert configure_debug_payload_logging({DEBUG_LOG_ENV_VAR: "   "}) is False
    assert debug_enabled() is False


def test_debug_formatter_carries_no_arbitrary_record_attributes() -> None:
    """The debug formatter reads only what ``debug_payload`` sets.

    Unlike the operational formatter it has no allowlist, because recording dropped
    fields is its purpose -- so the discipline has to come from reading a fixed set
    of attributes rather than from serialising the record.
    """
    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_debug_formatter())

    record = logging.LogRecord(
        name="free_router.debug",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="debug_payload",
        args=(),
        exc_info=None,
    )
    record.injected = "should not appear"  # type: ignore[attr-defined]
    handler.emit(record)

    payload = json.loads(stream.getvalue())
    assert "injected" not in payload
    assert payload["phase"] is None


def _debug_formatter() -> object:
    from free_router.core.debug_logging import _DebugFormatter

    return _DebugFormatter()


def test_operational_formatter_allowlist_is_unchanged() -> None:
    """CONSTITUTION 9.1's allowlist is a closed tuple and stayed closed.

    T038 added no field to it. This test fails if a later change widens the
    operational stream to accommodate debugging, which is the way that guarantee
    would quietly erode.
    """
    import free_router.core.logging as operational

    assert operational._ALLOWED_EXTRA_FIELDS == (
        "event",
        "request_id",
        "provider",
        "model",
        "key_alias",
        "routing_decision",
        "selection_reason",
        "estimated_tokens",
        "attempt",
        "http_status",
        "latency_ms",
        "cooldown_state",
        "error_category",
        "error",
    )
    assert issubclass(JsonFormatter, logging.Formatter)
