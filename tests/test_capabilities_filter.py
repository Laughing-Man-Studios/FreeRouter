"""Tests for capability filtering, substitution, and error handling (Batch 3, T071-T073).

Covers:
- mismatch() predicate under fail-closed semantics (provenance, capability flags, context fit).
- eligible_models() ordering and filtering.
- resolve_target() substitution rules, single-hop, fallback ordering, and local refusals.
- Integration tests for Scenarios 1–12 of specs/004-capability-aware-router/spec.md.
- Zero outbound requests asserted on every refusal.
- Correct response `model` and model_substituted logging on substitution.
"""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from conftest import LogRecorder, capabilities_yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free_router.core.capabilities import (
    RequestRequirements,
    eligible_models,
    mismatch,
    resolve_target,
)
from free_router.core.config import (
    Capabilities,
    CapabilityValue,
    Config,
    ModelConfig,
    load_config,
)
from free_router.core.exceptions import (
    CapabilityUnsatisfiableError,
    ContextWindowExceededError,
    NoAvailableCapacityError,
    ProviderRateLimitError,
    RouterBaseError,
    UnsupportedModelError,
)
from free_router.providers.google.adapter import GOOGLE_API_BASE

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"
GENERATE_PRO_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-pro:generateContent"

GEMINI_RESP = {
    "candidates": [
        {
            "content": {"parts": [{"text": "hello from gemini"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ]
}


def _make_model(
    model_id: str = "test/model-1",
    provider: str = "google",
    provider_id: str = "gemini-3.5-flash-lite",
    context_window: int = 1000,
    context_provenance: str = "published",
    streaming: bool = True,
    streaming_provenance: str = "measured",
    tools: bool = False,
    tools_provenance: str = "assumed",
    vision: bool = False,
    vision_provenance: str = "assumed",
    json_schema: bool = False,
    json_schema_provenance: str = "assumed",
    fallback_models: list[str] | None = None,
) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        provider=provider,
        provider_id=provider_id,
        fallback_models=fallback_models or [],
        capabilities=Capabilities(
            context_window=CapabilityValue[int](
                value=context_window,
                provenance=context_provenance,  # type: ignore[arg-type]
                source="test" if context_provenance != "assumed" else None,
            ),
            supports_streaming=CapabilityValue[bool](
                value=streaming,
                provenance=streaming_provenance,  # type: ignore[arg-type]
                source="test" if streaming_provenance != "assumed" else None,
            ),
            supports_tools=CapabilityValue[bool](
                value=tools,
                provenance=tools_provenance,  # type: ignore[arg-type]
                source="test" if tools_provenance != "assumed" else None,
            ),
            supports_vision=CapabilityValue[bool](
                value=vision,
                provenance=vision_provenance,  # type: ignore[arg-type]
                source="test" if vision_provenance != "assumed" else None,
            ),
            supports_json_schema=CapabilityValue[bool](
                value=json_schema,
                provenance=json_schema_provenance,  # type: ignore[arg-type]
                source="test" if json_schema_provenance != "assumed" else None,
            ),
        ),
    )


def _make_req(
    tools: bool = False,
    vision: bool = False,
    json_schema: bool = False,
    streaming: bool = False,
    input_tokens: int = 100,
    reserved_output: int = 50,
) -> RequestRequirements:
    return RequestRequirements(
        requires_tools=tools,
        requires_vision=vision,
        requires_json_schema=json_schema,
        requires_streaming=streaming,
        input_tokens=input_tokens,
        reserved_output=reserved_output,
    )


# ==============================================================================
# Unit Tests: mismatch()
# ==============================================================================


def test_mismatch_exact_fit_succeeds() -> None:
    """Inclusive comparison: input + output == context_window fits."""
    model = _make_model(context_window=150)
    req = _make_req(input_tokens=100, reserved_output=50)
    assert mismatch(model, req) is None


def test_mismatch_one_token_over_fails_context() -> None:
    """One token over window fails with context_window_exceeded."""
    model = _make_model(context_window=150)
    req = _make_req(input_tokens=101, reserved_output=50)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "context_window_exceeded"
    assert m.param == "messages"


def test_mismatch_assumed_context_window_is_unusable() -> None:
    """Assumed context window is unknown, not too small: capability_unsatisfiable."""
    model = _make_model(context_window=1000, context_provenance="assumed")
    req = _make_req(input_tokens=10, reserved_output=10)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "capability_unsatisfiable"
    assert m.param == "messages"
    assert "not verified" in m.detail


def test_mismatch_tools_required_but_assumed() -> None:
    """Assumed tools capability does not satisfy tools requirement."""
    model = _make_model(tools=True, tools_provenance="assumed")
    req = _make_req(tools=True)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "capability_unsatisfiable"
    assert m.param == "tools"


def test_mismatch_tools_published_false_fails() -> None:
    """Published tools: false fails requirement."""
    model = _make_model(tools=False, tools_provenance="published")
    req = _make_req(tools=True)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "capability_unsatisfiable"
    assert m.param == "tools"


def test_mismatch_tools_published_true_succeeds() -> None:
    """Published tools: true satisfies tools requirement."""
    model = _make_model(tools=True, tools_provenance="published")
    req = _make_req(tools=True)
    assert mismatch(model, req) is None


def test_mismatch_vision_required_assumed_fails() -> None:
    """Assumed vision does not satisfy vision requirement."""
    model = _make_model(vision=True, vision_provenance="assumed")
    req = _make_req(vision=True)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "capability_unsatisfiable"
    assert m.param == "messages"


def test_mismatch_json_schema_required_assumed_fails() -> None:
    """Assumed json_schema does not satisfy json_schema requirement."""
    model = _make_model(json_schema=True, json_schema_provenance="assumed")
    req = _make_req(json_schema=True)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "capability_unsatisfiable"
    assert m.param == "response_format"


def test_mismatch_streaming_required_not_supported_fails() -> None:
    """Model not supporting streaming fails with streaming_unsupported."""
    model = _make_model(streaming=False, streaming_provenance="measured")
    req = _make_req(streaming=True)
    m = mismatch(model, req)
    assert m is not None
    assert m.code == "streaming_unsupported"
    assert m.param == "stream"


# ==============================================================================
# Unit Tests: eligible_models() & resolve_target()
# ==============================================================================


def test_eligible_models_preserves_order() -> None:
    m1 = _make_model(model_id="m1", context_window=100)
    m2 = _make_model(model_id="m2", context_window=200)
    m3 = _make_model(model_id="m3", context_window=300)
    cfg = Config(models=[m1, m2, m3])

    req = _make_req(input_tokens=100, reserved_output=50)  # total 150
    eligible = eligible_models(cfg, req)
    assert [m.id for m in eligible] == ["m2", "m3"]


def test_resolve_target_requested_model_eligible() -> None:
    m1 = _make_model(model_id="m1", context_window=200)
    cfg = Config(models=[m1])
    target = resolve_target(cfg, _make_req(input_tokens=50, reserved_output=50), "m1")
    assert target.model.id == "m1"
    assert target.substituted_from is None
    assert target.forcing_requirement is None


def test_resolve_target_unknown_model_raises_unsupported() -> None:
    m1 = _make_model(model_id="m1")
    cfg = Config(models=[m1])
    with pytest.raises(UnsupportedModelError) as exc_info:
        resolve_target(cfg, _make_req(), "unknown-model")
    assert exc_info.value.param == "model"


def test_resolve_target_context_exceeded_no_fallback_raises_spec_error() -> None:
    m1 = _make_model(model_id="m1", context_window=500)
    cfg = Config(models=[m1])
    req = _make_req(input_tokens=600, reserved_output=100)
    with pytest.raises(ContextWindowExceededError) as exc_info:
        resolve_target(cfg, req, "m1")
    err = exc_info.value
    assert err.code == "context_window_exceeded"
    assert err.param == "messages"
    assert "No configured model can accept this request" in err.message
    assert "600 input tokens plus 100 reserved output tokens" in err.message
    assert "largest eligible context window is 500" in err.message
    assert "Considered: m1." in err.message


def test_resolve_target_opt_in_fallback_substitution() -> None:
    m2 = _make_model(model_id="m2", context_window=1000)
    m1 = _make_model(model_id="m1", context_window=200, fallback_models=["m2"])
    cfg = Config(models=[m1, m2])

    req = _make_req(input_tokens=250, reserved_output=50)  # total 300 > 200, <= 1000
    target = resolve_target(cfg, req, "m1")
    assert target.model.id == "m2"
    assert target.substituted_from == "m1"
    assert target.forcing_requirement == "context_window_exceeded"


def test_resolve_target_fallback_order_skips_ineligible_first_fallback() -> None:
    m3 = _make_model(model_id="m3", context_window=2000)
    m2 = _make_model(model_id="m2", context_window=500)
    m1 = _make_model(model_id="m1", context_window=200, fallback_models=["m2", "m3"])
    cfg = Config(models=[m1, m2, m3])

    req = _make_req(input_tokens=600, reserved_output=100)  # total 700: m1 fails, m2 fails, m3 fits
    target = resolve_target(cfg, req, "m1")
    assert target.model.id == "m3"
    assert target.substituted_from == "m1"
    assert target.forcing_requirement == "context_window_exceeded"


def test_resolve_target_assumed_context_reports_capability_not_size() -> None:
    """An unverified context window must not be reported as an oversized request."""
    m1 = _make_model(model_id="m1", context_window=1000, context_provenance="assumed")
    cfg = Config(models=[m1])
    req = _make_req(input_tokens=10, reserved_output=10)

    with pytest.raises(CapabilityUnsatisfiableError) as exc_info:
        resolve_target(cfg, req, "m1")

    err = exc_info.value
    assert err.code == "capability_unsatisfiable"
    assert err.param == "messages"
    assert "not verified" in err.message
    assert "largest eligible context window" not in err.message


def test_resolve_target_skips_capability_ineligible_fallback() -> None:
    """A fallback missing a required capability is skipped, and is never probed."""
    m1 = _make_model(model_id="m1", context_window=100, fallback_models=["m2", "m3"])
    m2 = _make_model(model_id="m2", context_window=2000, tools=True, tools_provenance="assumed")
    m3 = _make_model(model_id="m3", context_window=2000, tools=True, tools_provenance="published")
    cfg = Config(models=[m1, m2, m3])
    req = _make_req(tools=True, input_tokens=200, reserved_output=50)
    probed: list[str] = []

    def probe(model: ModelConfig, estimated_tokens: int) -> RouterBaseError | None:
        probed.append(model.id)
        return None

    target = resolve_target(cfg, req, "m1", capacity_probe=probe)
    assert target.model.id == "m3"
    assert target.forcing_requirement == "capability_unsatisfiable"
    assert probed == ["m3"]


def test_resolve_target_skips_capacity_unavailable_fallback() -> None:
    """A capability-eligible but cooling fallback is skipped for the next one."""
    m1 = _make_model(model_id="m1", context_window=100, fallback_models=["m2", "m3"])
    m2 = _make_model(model_id="m2", context_window=2000)
    m3 = _make_model(model_id="m3", context_window=2000)
    cfg = Config(models=[m1, m2, m3])
    req = _make_req(input_tokens=200, reserved_output=50)
    probed: list[str] = []

    def probe(model: ModelConfig, estimated_tokens: int) -> RouterBaseError | None:
        probed.append(model.id)
        if model.id == "m2":
            return ProviderRateLimitError("cooling")
        return None

    target = resolve_target(cfg, req, "m1", capacity_probe=probe)
    assert target.model.id == "m3"
    assert target.substituted_from == "m1"
    assert probed == ["m2", "m3"]


def test_resolve_target_preserves_capacity_error_when_no_fallback_available() -> None:
    """All capable fallbacks unavailable: keep the M2 capacity outcome, not a 400."""
    m1 = _make_model(model_id="m1", context_window=100, fallback_models=["m2", "m3"])
    m2 = _make_model(model_id="m2", context_window=2000)
    m3 = _make_model(model_id="m3", context_window=2000)
    cfg = Config(models=[m1, m2, m3])
    req = _make_req(input_tokens=200, reserved_output=50)

    def probe(model: ModelConfig, estimated_tokens: int) -> RouterBaseError | None:
        return NoAvailableCapacityError("out of quota")

    with pytest.raises(NoAvailableCapacityError):
        resolve_target(cfg, req, "m1", capacity_probe=probe)


def test_resolve_target_does_not_probe_requested_model() -> None:
    """Capacity on the requested model keeps its M2 outcome; selection must not pre-empt it."""
    m1 = _make_model(model_id="m1", context_window=1000)
    cfg = Config(models=[m1])
    probed: list[str] = []

    def probe(model: ModelConfig, estimated_tokens: int) -> RouterBaseError | None:
        probed.append(model.id)
        return ProviderRateLimitError("cooling")

    target = resolve_target(
        cfg, _make_req(input_tokens=50, reserved_output=50), "m1", capacity_probe=probe
    )
    assert target.model.id == "m1"
    assert probed == []


# ==============================================================================
# Integration Tests: Spec Scenarios 1–12
# ==============================================================================


@pytest.fixture
def two_model_app(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], FastAPI]:
    """App configured with two models: flash-lite (fallback to pro) and pro."""
    from free_router.api.main import create_app
    from free_router.core.config import key_env_var

    # Key for pro
    monkeypatch.setenv(key_env_var("google", "primary"), "test-google-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    config_yaml_content = (
        "models:\n"
        '  - id: "google/gemini-3.5-flash-lite"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        + capabilities_yaml(context_window=1_048_576, fallback_models=("google/gemini-3.5-pro",))
        + '  - id: "google/gemini-3.5-pro"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-pro"\n' + capabilities_yaml(context_window=2_097_152)
    )
    write_config(config_yaml_content)
    return create_app


@pytest.fixture
def no_fallback_app(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], FastAPI]:
    """App configured with standard single model and no fallback."""
    from free_router.api.main import create_app

    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))
    config_yaml_content = (
        "models:\n"
        '  - id: "google/gemini-3.5-flash-lite"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        + capabilities_yaml(context_window=1_048_576, fallback_models=())
    )
    write_config(config_yaml_content)
    return create_app


@respx.mock
def test_scenario_1_oversized_request_refused_locally(
    no_fallback_app: Callable[[], FastAPI],
) -> None:
    """Scenario 1: Oversized request refused with context_window_exceeded, no outbound call."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    # Request with ~1,100,000 estimated input tokens (4.4 chars/token -> ~4,840,000 chars)
    large_text = "a" * 4_840_000
    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": large_text}],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "context_window_exceeded"
    assert data["error"]["param"] == "messages"
    assert "1,048,576" in data["error"]["message"]
    # Zero outbound requests
    assert route.call_count == 0


@respx.mock
def test_scenario_2_request_that_just_fits_dispatched(
    no_fallback_app: Callable[[], FastAPI],
) -> None:
    """Scenario 2: Request within context window is dispatched normally."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "hello within context"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "google/gemini-3.5-flash-lite"
    assert route.call_count == 1


@respx.mock
def test_scenario_3_tools_request_refused_not_stripped(
    no_fallback_app: Callable[[], FastAPI],
) -> None:
    """Scenario 3: Non-empty tools array refused locally with 400 and ZERO outbound calls."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "call a tool"}],
                "tools": [{"type": "function", "function": {"name": "test_fn"}}],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "capability_unsatisfiable"
    assert data["error"]["param"] == "tools"
    assert route.call_count == 0


@respx.mock
def test_scenario_4_image_request_refused(no_fallback_app: Callable[[], FastAPI]) -> None:
    """Scenario 4: Request with image_url part refused locally with 400 and ZERO outbound calls."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look at this"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/img.png"},
                            },
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "capability_unsatisfiable"
    assert data["error"]["param"] == "messages"
    assert route.call_count == 0


@respx.mock
def test_scenario_5_assumed_capability_does_not_satisfy(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 5: Model with supports_tools: true but provenance: assumed is rejected."""
    from free_router.api.main import create_app

    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    # Config with tools declared true but assumed
    config_yaml_content = (
        "models:\n"
        '  - id: "google/gemini-3.5-flash-lite"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        "    capabilities:\n"
        "      context_window:\n"
        "        value: 1048576\n"
        "        provenance: published\n"
        '        source: "docs"\n'
        "      supports_streaming:\n"
        "        value: true\n"
        "        provenance: measured\n"
        '        source: "test"\n'
        "      supports_tools:\n"
        "        value: true\n"
        "        provenance: assumed\n"
        "      supports_vision:\n"
        "        value: false\n"
        "        provenance: assumed\n"
        "      supports_json_schema:\n"
        "        value: false\n"
        "        provenance: assumed\n"
    )
    write_config(config_yaml_content)
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "use a tool"}],
                "tools": [{"type": "function", "function": {"name": "test_fn"}}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "capability_unsatisfiable"
    assert route.call_count == 0


@respx.mock
def test_scenario_6_substitution_off_by_default(no_fallback_app: Callable[[], FastAPI]) -> None:
    """Scenario 6: Ineligible requested model without fallbacks is refused with 400."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    # Request requiring tools against model without tools & no fallbacks
    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "need tools"}],
                "tools": [{"type": "function", "function": {"name": "fn"}}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "capability_unsatisfiable"
    assert route.call_count == 0


@respx.mock
def test_scenario_7_opt_in_fallback_serves_request(
    two_model_app: Callable[[], FastAPI], log_recorder: LogRecorder
) -> None:
    """Scenario 7: Oversized request is served by gemini-3.5-pro with substitution logging."""
    lite_route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    pro_route = respx.post(GENERATE_PRO_URL).mock(
        return_value=httpx.Response(200, json=GEMINI_RESP)
    )

    # Request with ~1,100,000 tokens: > 1,048,576 (flash-lite), <= 2,097,152 (pro)
    large_text = "a" * 4_840_000
    with TestClient(two_model_app()) as test_client:
        import logging

        root = logging.getLogger()
        root.addHandler(log_recorder.handler)
        try:
            response = test_client.post(
                "/v1/chat/completions",
                json={
                    "model": "google/gemini-3.5-flash-lite",
                    "messages": [{"role": "user", "content": large_text}],
                },
            )
        finally:
            root.removeHandler(log_recorder.handler)

    assert response.status_code == 200
    data = response.json()
    assert data["model"] == "google/gemini-3.5-pro"
    assert lite_route.call_count == 0
    assert pro_route.call_count == 1

    # Verify INFO log
    sub_records = [r for r in log_recorder.records if r.get("event") == "model_substituted"]
    assert len(sub_records) == 1
    msg = sub_records[0].get("message", "")
    assert "google/gemini-3.5-flash-lite" in msg
    assert "google/gemini-3.5-pro" in msg
    assert "context_window_exceeded" in msg


@respx.mock
def test_scenario_8_cooldown_does_not_trigger_substitution(
    two_model_app: Callable[[], FastAPI],
) -> None:
    """Scenario 8: A rate limit / cooldown on flash-lite does NOT trigger fallback substitution."""
    # First request gets a 429 to place flash-lite in cooldown
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    pro_route = respx.post(GENERATE_PRO_URL).mock(
        return_value=httpx.Response(200, json=GEMINI_RESP)
    )

    with TestClient(two_model_app()) as test_client:
        # Request 1: hits 429
        resp1 = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp1.status_code == 429

        # Request 2: while cooling, client asks for flash-lite again
        resp2 = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "hi again"}],
            },
        )
        assert resp2.status_code == 429
        # Fallback was NEVER contacted
        assert pro_route.call_count == 0


@respx.mock
def test_scenario_9_ineligible_first_fallback_skipped(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 9: If first fallback also cannot serve the request, second fallback serves it."""
    from free_router.api.main import create_app
    from free_router.core.config import key_env_var

    monkeypatch.setenv(key_env_var("google", "primary"), "test-google-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    config_yaml_content = (
        "models:\n"
        '  - id: "model-small"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        + capabilities_yaml(context_window=100, fallback_models=("model-med", "model-large"))
        + '  - id: "model-med"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        + capabilities_yaml(context_window=500)
        + '  - id: "model-large"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n' + capabilities_yaml(context_window=2000)
    )
    write_config(config_yaml_content)

    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    # Request requiring ~600 tokens (chars ~2700)
    text = "a" * 2700
    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "model-small",
                "messages": [{"role": "user", "content": text}],
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "model-large"


def test_scenario_10_invalid_capability_config_prevents_startup(
    workdir: Path, write_config: Callable[..., Path]
) -> None:
    """Scenario 10: Capability declaration without provenance fails startup."""
    from free_router.core.config import ConfigError

    config_yaml = (
        "models:\n"
        '  - id: "google/gemini-3.5-flash-lite"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        "    capabilities:\n"
        "      context_window:\n"
        "        value: 1048576\n"
        "        provenance: published\n"
        '        source: "docs"\n'
        "      supports_streaming:\n"
        "        value: true\n"
        "        provenance: measured\n"
        '        source: "test"\n'
        "      supports_tools:\n"
        "        value: false\n"
        "        provenance: assumed\n"
        "      supports_vision:\n"
        "        value: false\n"
        # missing provenance
        "      supports_json_schema:\n"
        "        value: false\n"
        "        provenance: assumed\n"
    )
    write_config(config_yaml)
    with pytest.raises(ConfigError) as exc_info:
        load_config(resolve_keys=False)
    assert "provenance" in str(exc_info.value)


def test_scenario_11_unknown_capability_keys_refused(
    workdir: Path, write_config: Callable[..., Path]
) -> None:
    """Scenario 11: Unknown capability key fails config loading."""
    from free_router.core.config import ConfigError

    config_yaml = (
        "models:\n"
        '  - id: "google/gemini-3.5-flash-lite"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        "    capabilities:\n"
        "      context_window:\n"
        "        value: 1048576\n"
        "        provenance: published\n"
        '        source: "docs"\n'
        "      supports_streaming:\n"
        "        value: true\n"
        "        provenance: measured\n"
        '        source: "test"\n'
        "      supports_tools:\n"
        "        value: false\n"
        "        provenance: assumed\n"
        "      supports_vision:\n"
        "        value: false\n"
        "        provenance: assumed\n"
        "      supports_json_schema:\n"
        "        value: false\n"
        "        provenance: assumed\n"
        "      supports_audio:\n"
        "        value: true\n"
    )
    write_config(config_yaml)
    with pytest.raises(ConfigError) as exc_info:
        load_config(resolve_keys=False)
    assert "supports_audio" in str(exc_info.value)


@respx.mock
def test_scenario_12_streaming_unchanged_for_eligible_model(
    no_fallback_app: Callable[[], FastAPI],
) -> None:
    """Scenario 12: Streaming eligible request streams normally with chunks reporting model."""
    chunk1 = (
        'data: {"candidates": [{"content": {"parts": [{"text": "chunk 1"}], "role": "model"}, '
        '"index": 0}]}'
    )
    chunk2 = (
        'data: {"candidates": [{"content": {"parts": [{"text": "chunk 2"}], "role": "model"}, '
        '"finishReason": "STOP", "index": 0}], "usageMetadata": {"promptTokenCount": "5", '
        '"candidatesTokenCount": "5", "totalTokenCount": "10"}}'
    )
    sse_body = f"{chunk1}\n\n{chunk2}"
    stream_url = (
        f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse"
    )
    respx.post(stream_url).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "stream": True,
                "messages": [{"role": "user", "content": "stream this"}],
            },
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    lines = [
        line
        for line in response.text.split("\n")
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert len(lines) >= 1
    first_chunk = json.loads(lines[0][6:])
    assert first_chunk["model"] == "google/gemini-3.5-flash-lite"


@respx.mock
def test_streaming_with_substitution_echoes_effective_model(
    two_model_app: Callable[[], FastAPI],
) -> None:
    """Streaming with fallback substitution reports effective model in chunk events."""
    chunk1 = (
        'data: {"candidates": [{"content": {"parts": [{"text": "chunk 1"}], "role": "model"}, '
        '"index": 0}]}'
    )
    chunk2 = (
        'data: {"candidates": [{"content": {"parts": [{"text": "chunk 2"}], "role": "model"}, '
        '"finishReason": "STOP", "index": 0}], "usageMetadata": {"promptTokenCount": "5", '
        '"candidatesTokenCount": "5", "totalTokenCount": "10"}}'
    )
    sse_body = f"{chunk1}\n\n{chunk2}"
    pro_stream_url = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-pro:streamGenerateContent?alt=sse"
    respx.post(pro_stream_url).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    large_text = "a" * 4_840_000
    with TestClient(two_model_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "stream": True,
                "messages": [{"role": "user", "content": large_text}],
            },
        )

    assert response.status_code == 200
    lines = [
        line
        for line in response.text.split("\n")
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert len(lines) >= 1
    first_chunk = json.loads(lines[0][6:])
    assert first_chunk["model"] == "google/gemini-3.5-pro"


@respx.mock
def test_response_format_refused_locally(no_fallback_app: Callable[[], FastAPI]) -> None:
    """Structured output (response_format) is refused with capability_unsatisfiable."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    with TestClient(no_fallback_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "return json"}],
                "response_format": {"type": "json_object"},
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "capability_unsatisfiable"
    assert data["error"]["param"] == "response_format"
    assert route.call_count == 0


@respx.mock
def test_streaming_unsupported_refused_with_zero_outbound(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming to a non-streaming model returns streaming_unsupported, param 'stream'."""
    from free_router.api.main import create_app

    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))
    config_yaml_content = (
        "models:\n"
        '  - id: "google/gemini-3.5-flash-lite"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        + capabilities_yaml(context_window=1_048_576, streaming=False)
    )
    write_config(config_yaml_content)

    stream_url = (
        f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse"
    )
    route = respx.post(stream_url).mock(return_value=httpx.Response(200, text=""))

    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-flash-lite",
                "stream": True,
                "messages": [{"role": "user", "content": "stream this"}],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "streaming_unsupported"
    assert data["error"]["param"] == "stream"
    assert route.call_count == 0


@respx.mock
def test_cooldown_fallback_skipped_for_next_eligible(
    workdir: Path,
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cooling fallback is skipped and a later eligible fallback serves the request."""
    from free_router.api.main import create_app
    from free_router.core.config import key_env_var

    monkeypatch.setenv(key_env_var("google", "primary"), "test-google-key-primary")
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))

    config_yaml_content = (
        "models:\n"
        '  - id: "model-small"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
        + capabilities_yaml(context_window=100, fallback_models=("model-med", "model-large"))
        + '  - id: "model-med"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-pro"\n'
        + capabilities_yaml(context_window=2000)
        + '  - id: "model-large"\n'
        '    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n' + capabilities_yaml(context_window=2000)
    )
    write_config(config_yaml_content)

    pro_route = respx.post(GENERATE_PRO_URL).mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    lite_route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    with TestClient(create_app()) as test_client:
        # Put model-med's capacity unit (google/gemini-3.5-pro) into cooldown.
        cooled = test_client.post(
            "/v1/chat/completions",
            json={"model": "model-med", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert cooled.status_code == 429

        # Oversized for model-small. model-med is capable but cooling; model-large serves.
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "model-small",
                "messages": [{"role": "user", "content": "a" * 2700}],
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "model-large"
    assert pro_route.call_count == 1
    assert lite_route.call_count == 1
