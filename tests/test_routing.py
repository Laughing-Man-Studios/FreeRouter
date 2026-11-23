"""Tests for routing policy, scoring, 'free' selection, explainability, and discovery (M4 Batch 5).

Covers:
- Spec Scenarios 1–9 and 16 (named tests).
- Pure-function scoring and rank_candidates deterministic tie-breaking.
- Latency above ceiling scores 0.0 and stays eligible.
- Weights normalization (relative weights, fractions, percentages, points).
- Hard eligibility exclusions (capabilities, capacity probe, unconfigured priors).
- Single resolution entry point parity between unary and streaming routes.
- routing_decision log record allowlist and privacy enforcement (no prompt/tool/content).
- Zero outbound requests on all refusals via respx.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
import respx
from conftest import LogRecorder, capabilities_yaml
from fastapi.testclient import TestClient

from free_router.core.capabilities import (
    RequestRequirements,
)
from free_router.core.config import (
    Capabilities,
    CapabilityValue,
    Config,
    ModelConfig,
    RoutingConfig,
    RoutingPrior,
    RoutingWeights,
)
from free_router.core.exceptions import (
    ProviderRateLimitError,
    RouterBaseError,
    UnsupportedModelError,
)
from free_router.core.routing import (
    FREE_MODEL_ID,
    rank_candidates,
    resolve_request,
    score_model,
    select_model,
)
from free_router.providers.google.adapter import GOOGLE_API_BASE

GENERATE_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:generateContent"
GENERATE_PRO_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-pro:generateContent"
STREAM_URL = f"{GOOGLE_API_BASE}/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse"

GEMINI_RESP = {
    "candidates": [
        {
            "content": {"parts": [{"text": "response from gemini"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
    ]
}


def _make_model(
    model_id: str,
    *,
    provider: str = "google",
    provider_id: str = "gemini-3.5-flash-lite",
    quality: int | None = 70,
    latency_ms: int | None = 1000,
    tools: bool = True,
    tools_provenance: str = "measured",
    streaming: bool = True,
    streaming_provenance: str = "measured",
    vision: bool = False,
    vision_provenance: str = "assumed",
    json_schema: bool = False,
    json_schema_provenance: str = "assumed",
    context_window: int = 100000,
    context_provenance: str = "published",
    fallback_models: list[str] | None = None,
) -> ModelConfig:
    routing = None
    if quality is not None and latency_ms is not None:
        routing = RoutingPrior(quality=quality, latency_ms=latency_ms)

    return ModelConfig(
        id=model_id,
        provider=provider,
        provider_id=provider_id,
        routing=routing,
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
    *,
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
# Pure Function Tests: score_model & rank_candidates
# ==============================================================================


def test_score_model_pure_function() -> None:
    """A candidate model produces the exact expected mathematical score."""
    policy = RoutingConfig(
        weights=RoutingWeights(quality=0.60, tool_support=0.30, latency=0.10),
        latency_ceiling_ms=5000,
    )
    # quality=70 -> 0.7; tools=True (measured) -> 1.0; latency=1000 -> 1 - 1000/5000 = 0.8
    # score = (0.6 * 0.7 + 0.3 * 1.0 + 0.1 * 0.8) / 1.0 = 0.42 + 0.30 + 0.08 = 0.80
    model = _make_model("test/model-a", quality=70, latency_ms=1000, tools=True)
    score = score_model(model, policy)

    assert score is not None
    assert pytest.approx(score.total, abs=1e-4) == 0.80
    assert pytest.approx(score.contributions["quality"], abs=1e-4) == 0.42
    assert pytest.approx(score.contributions["tool_support"], abs=1e-4) == 0.30
    assert pytest.approx(score.contributions["latency"], abs=1e-4) == 0.08
    assert pytest.approx(score.components["quality"], abs=1e-4) == 0.70
    assert pytest.approx(score.components["tool_support"], abs=1e-4) == 1.0
    assert pytest.approx(score.components["latency"], abs=1e-4) == 0.80


def test_score_model_latency_above_ceiling_scores_zero_and_stays_eligible() -> None:
    """A model exceeding the latency ceiling scores 0.0 on latency and stays eligible."""
    policy = RoutingConfig(
        weights=RoutingWeights(quality=0.60, tool_support=0.30, latency=0.10),
        latency_ceiling_ms=5000,
    )
    # latency_ms=6000 > 5000 ceiling -> latency_component = 0.0
    model = _make_model("test/model-slow", quality=80, latency_ms=6000, tools=True)
    score = score_model(model, policy)

    assert score is not None
    assert score.components["latency"] == 0.0
    assert score.contributions["latency"] == 0.0
    # total = 0.6 * 0.8 + 0.3 * 1.0 + 0.1 * 0 = 0.48 + 0.30 = 0.78
    assert pytest.approx(score.total, abs=1e-4) == 0.78


def test_score_model_weights_normalization() -> None:
    """Weights as fractions (0.6, 0.3, 0.1) and points (60, 30, 10) produce identical scores."""
    p1 = RoutingConfig(weights=RoutingWeights(quality=0.60, tool_support=0.30, latency=0.10))
    p2 = RoutingConfig(weights=RoutingWeights(quality=60.0, tool_support=30.0, latency=10.0))
    model = _make_model("test/model-a", quality=75, latency_ms=1200)

    s1 = score_model(model, p1)
    s2 = score_model(model, p2)
    assert s1 is not None and s2 is not None
    assert pytest.approx(s1.total, abs=1e-5) == s2.total


def test_score_model_unconfigured_model_returns_none() -> None:
    """A model without priors returns None from score_model rather than inventing a default."""
    policy = RoutingConfig(weights=RoutingWeights())
    model = _make_model("test/no-priors", quality=None, latency_ms=None)
    assert score_model(model, policy) is None


def test_score_model_assumed_tool_support_counts_as_zero() -> None:
    """Assumed tools capability counts as 0, consistent with fail-closed semantics."""
    policy = RoutingConfig(weights=RoutingWeights(quality=0.5, tool_support=0.5, latency=0.0))
    model = _make_model("test/assumed-tools", quality=100, tools=True, tools_provenance="assumed")
    score = score_model(model, policy)
    assert score is not None
    assert score.components["tool_support"] == 0.0


def test_rank_candidates_deterministic_tie_breaking() -> None:
    """Identical scores resolve to configuration order."""
    policy = RoutingConfig(weights=RoutingWeights())
    m1 = _make_model("test/first", quality=80, latency_ms=1000)
    m2 = _make_model("test/second", quality=80, latency_ms=1000)
    s1 = score_model(m1, policy)
    s2 = score_model(m2, policy)
    assert s1 is not None and s2 is not None

    ranked = rank_candidates([(0, m1, s1), (1, m2, s2)])
    assert [m.id for m, _ in ranked] == ["test/first", "test/second"]

    # Even if fed in reverse order, declaration index (0 before 1) breaks the tie
    ranked_reverse = rank_candidates([(1, m2, s2), (0, m1, s1)])
    assert [m.id for m, _ in ranked_reverse] == ["test/first", "test/second"]


def test_tool_support_weight_effect() -> None:
    """tool_support changes nothing on a tools request and matters on a tools-free one."""
    policy = RoutingConfig(
        weights=RoutingWeights(quality=0.60, tool_support=0.30, latency=0.10),
        latency_ceiling_ms=5000,
    )
    # Model A: lower quality (70), has tools
    mA = _make_model("test/mA", quality=70, tools=True, latency_ms=1000)
    # Model B: higher quality (85), no tools
    mB = _make_model("test/mB", quality=85, tools=False, latency_ms=1000)

    cfg = Config(models=[mA, mB], routing=policy)

    # 1. Tools-free request: mA wins because tool_support gives it an advantage
    # mA score: 0.6*0.7 + 0.3*1.0 + 0.1*0.8 = 0.42 + 0.30 + 0.08 = 0.80
    # mB score: 0.6*0.85 + 0.3*0.0 + 0.1*0.8 = 0.51 + 0.00 + 0.08 = 0.59
    req_no_tools = _make_req(tools=False)
    target_no_tools, _ = select_model(cfg, req_no_tools)
    assert target_no_tools.model.id == "test/mA"

    # 2. Tools request: mB is excluded by capability filter before scoring; mA is the only survivor
    req_tools = _make_req(tools=True)
    target_tools, decision_tools = select_model(cfg, req_tools)
    assert target_tools.model.id == "test/mA"
    excluded_reasons = {e.model: e.reason for e in decision_tools.excluded}
    assert excluded_reasons["test/mB"] == "tools"


# ==============================================================================
# Spec Scenarios 1–9 and 16
# ==============================================================================


def test_scenario_1_best_scoring_eligible_model_selected() -> None:
    """Scenario 1: Best-scoring eligible model is selected."""
    policy = RoutingConfig(
        weights=RoutingWeights(quality=0.6, tool_support=0.3, latency=0.1),
        latency_ceiling_ms=5000,
    )
    # Model A: quality 70, tools true, latency 900
    #   score = 0.6*0.7 + 0.3*1.0 + 0.1*(1-900/5000) = 0.42 + 0.3 + 0.082 = 0.802
    model_a = _make_model("test/model-a", quality=70, tools=True, latency_ms=900)
    # Model B: quality 90, tools false, latency 1200
    #   score = 0.6*0.9 + 0.3*0.0 + 0.1*(1-1200/5000) = 0.54 + 0.0 + 0.076 = 0.616
    model_b = _make_model("test/model-b", quality=90, tools=False, latency_ms=1200)

    cfg = Config(models=[model_a, model_b], routing=policy)
    req = _make_req(tools=False)

    target, decision = resolve_request(cfg, req, FREE_MODEL_ID)
    assert target.model.id == "test/model-a"
    assert target.selection == "auto"
    assert decision is not None
    assert decision.selected == "test/model-a"


def test_scenario_2_required_capability_removes_model_before_scoring() -> None:
    """Scenario 2: A required capability removes a model before scoring."""
    policy = RoutingConfig(weights=RoutingWeights(quality=0.6, tool_support=0.3, latency=0.1))
    model_a = _make_model("test/model-a", quality=70, tools=True, latency_ms=900)
    model_b = _make_model("test/model-b", quality=90, tools=False, latency_ms=1200)

    cfg = Config(models=[model_a, model_b], routing=policy)
    req = _make_req(tools=True)

    target, decision = resolve_request(cfg, req, FREE_MODEL_ID)
    assert target.model.id == "test/model-a"
    assert decision is not None
    assert any(e.model == "test/model-b" and e.reason == "tools" for e in decision.excluded)


def test_scenario_3_capacity_is_hard_constraint_not_score() -> None:
    """Scenario 3: Capacity is a hard constraint, not a score."""
    policy = RoutingConfig(weights=RoutingWeights())
    model_a = _make_model("test/model-a", quality=95, latency_ms=500)
    model_b = _make_model("test/model-b", quality=70, latency_ms=1000)

    cfg = Config(models=[model_a, model_b], routing=policy)
    req = _make_req()

    def probe(m: ModelConfig, est_tokens: int) -> RouterBaseError | None:
        if m.id == "test/model-a":
            return ProviderRateLimitError("Model cooling", retry_after=30.0)
        return None

    target, decision = resolve_request(cfg, req, FREE_MODEL_ID, capacity_probe=probe)
    assert target.model.id == "test/model-b"
    assert decision is not None
    assert any(e.model == "test/model-a" and e.reason == "cooling" for e in decision.excluded)


def test_scenario_4_nothing_eligible_fails_without_dispatch() -> None:
    """Scenario 4: every model cooling fails with the M2 capacity error naming them."""
    policy = RoutingConfig(weights=RoutingWeights())
    model_a = _make_model("test/model-a", quality=90, latency_ms=500)
    model_b = _make_model("test/model-b", quality=80, latency_ms=500)

    cfg = Config(models=[model_a, model_b], routing=policy)
    req = _make_req()

    def probe(m: ModelConfig, est_tokens: int) -> RouterBaseError | None:
        return ProviderRateLimitError(f"Model '{m.id}' is cooling.", retry_after=45.0)

    with pytest.raises(ProviderRateLimitError) as exc_info:
        resolve_request(cfg, req, FREE_MODEL_ID, capacity_probe=probe)

    msg = str(exc_info.value)
    assert "test/model-a (cooling)" in msg
    assert "test/model-b (cooling)" in msg
    assert exc_info.value.retry_after == 45.0


def test_scenario_5_selection_is_sticky_while_capacity_remains() -> None:
    """Scenario 5: Selection is sticky while capacity remains (no rotation)."""
    policy = RoutingConfig(weights=RoutingWeights())
    model_a = _make_model("test/model-a", quality=90, latency_ms=500)
    model_b = _make_model("test/model-b", quality=80, latency_ms=500)

    cfg = Config(models=[model_a, model_b], routing=policy)
    req = _make_req()

    for _ in range(5):
        target, _ = resolve_request(cfg, req, FREE_MODEL_ID)
        assert target.model.id == "test/model-a"


def test_scenario_6_ties_broken_by_configuration_order() -> None:
    """Scenario 6: Ties are broken by configuration order."""
    policy = RoutingConfig(weights=RoutingWeights())
    model_a = _make_model("test/model-a", quality=80, latency_ms=1000)
    model_b = _make_model("test/model-b", quality=80, latency_ms=1000)

    cfg = Config(models=[model_a, model_b], routing=policy)
    req = _make_req()

    target, decision = resolve_request(cfg, req, FREE_MODEL_ID)
    assert target.model.id == "test/model-a"
    assert decision is not None
    assert decision.candidates[0].score == decision.candidates[1].score


def test_scenario_7_free_is_unknown_without_routing_policy() -> None:
    """Scenario 7: 'free' is unknown without routing policy (400 unsupported_model)."""
    model_a = _make_model("test/model-a")
    cfg = Config(models=[model_a], routing=None)
    req = _make_req()

    with pytest.raises(UnsupportedModelError) as exc_info:
        resolve_request(cfg, req, FREE_MODEL_ID)

    assert exc_info.value.code == "unsupported_model"
    assert exc_info.value.param == "model"


@respx.mock
def test_scenario_8_decision_explainable_from_logs(
    client: TestClient, json_logs: LogRecorder
) -> None:
    """Scenario 8: Decision is explainable from logs with routing_decision event."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "free", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    # Response model is effective model
    assert resp.json()["model"] == "google/gemini-3.5-flash-lite"

    decision_records = [rec for rec in json_logs.records if rec.get("event") == "routing_decision"]
    assert len(decision_records) == 1
    rec = decision_records[0]
    assert rec["requested"] == "free"
    assert rec["selected"] == "google/gemini-3.5-flash-lite"
    assert "score" in rec
    assert "contributions" in rec
    assert "weights" in rec
    assert "candidates" in rec
    assert "excluded" in rec

    # Ensure no message content in log payload
    json_str = json.dumps(rec)
    assert "hello" not in json_str


@respx.mock
def test_scenario_9_explicit_model_not_ranked(client: TestClient, json_logs: LogRecorder) -> None:
    """Scenario 9: An explicit model is not ranked and emits no routing_decision record."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "google/gemini-3.5-flash-lite",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200

    decision_records = [rec for rec in json_logs.records if rec.get("event") == "routing_decision"]
    assert len(decision_records) == 0


def test_scenario_16_free_is_discoverable(client: TestClient) -> None:
    """Scenario 16: 'free' is discoverable in GET /v1/models when routing is configured."""
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    free_entries = [m for m in data if m["id"] == "free"]
    assert len(free_entries) == 1
    assert free_entries[0]["available"] is True


def test_scenario_16_free_is_absent_without_routing_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 16: 'free' is absent in GET /v1/models when no routing policy is configured."""
    config_yaml = """
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
    capabilities:
      context_window:
        value: 1048576
        provenance: published
        source: "docs"
      supports_streaming:
        value: true
        provenance: measured
        source: "test"
      supports_tools:
        value: false
        provenance: published
        source: "docs"
      supports_vision:
        value: false
        provenance: published
        source: "docs"
      supports_json_schema:
        value: false
        provenance: published
        source: "docs"
    fallback_models: []
"""
    p = tmp_path / "config.yaml"
    p.write_text(config_yaml)
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(p))
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "dummy")
    from free_router.api.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        resp = test_client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert all(m["id"] != "free" for m in data)


# ==============================================================================
# Refusal & Zero Outbound Requests Tests
# ==============================================================================


@respx.mock
def test_zero_outbound_requests_on_all_refusals(client: TestClient) -> None:
    """Every refusal path asserts zero outbound requests via respx."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    # 1. Invalid capability: a context window the request cannot fit.
    giant_message = "x" * 5_000_000
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "free", "messages": [{"role": "user", "content": giant_message}]},
    )
    assert resp.status_code == 400
    assert route.call_count == 0


@respx.mock
def test_unary_and_streaming_routes_select_same_model(client: TestClient) -> None:
    """The unary and streaming routes select the same model for the same config and request."""
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    stream_content = (
        b'data: {"candidates":[{"content":{"parts":[{"text":"chunk"}]},"finishReason":"STOP"}]}\n\n'
    )
    respx.post(STREAM_URL).mock(
        return_value=httpx.Response(
            200, content=stream_content, headers={"Content-Type": "text/event-stream"}
        )
    )

    # Unary request
    resp_unary = client.post(
        "/v1/chat/completions",
        json={"model": "free", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert resp_unary.status_code == 200
    assert resp_unary.json()["model"] == "google/gemini-3.5-flash-lite"

    # Streaming request
    resp_stream = client.post(
        "/v1/chat/completions",
        json={"model": "free", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp_stream.status_code == 200
    lines = [line for line in resp_stream.text.split("\n") if line.startswith("data: {")]
    first_chunk = json.loads(lines[0][6:])
    assert first_chunk["model"] == "google/gemini-3.5-flash-lite"


# ==============================================================================
# App-level refusals: every exclusion path makes zero outbound requests
# ==============================================================================

TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {"type": "object", "properties": {}},
    },
}

IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


def _routing_yaml() -> str:
    """The routing policy block used by the app-level tests."""
    return (
        "routing:\n"
        "  weights:\n"
        "    quality: 0.60\n"
        "    tool_support: 0.30\n"
        "    latency: 0.10\n"
        "  latency_ceiling_ms: 5000\n"
    )


def _model_yaml(
    model_id: str,
    *,
    provider_id: str = "gemini-3.5-flash-lite",
    quality: int | None = 75,
    latency_ms: int = 1200,
    extra: str = "",
    **capabilities: object,
) -> str:
    """One model entry, with optional priors and capability overrides."""
    priors = (
        ""
        if quality is None
        else f"    routing:\n      quality: {quality}\n      latency_ms: {latency_ms}\n"
    )
    return (
        f'  - id: "{model_id}"\n'
        '    provider: "google"\n'
        f'    provider_id: "{provider_id}"\n'
        + priors
        + capabilities_yaml(**capabilities)  # type: ignore[arg-type]
        + extra
    )


def _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text):
    """Write ``yaml_text`` and return an app factory for it."""
    from free_router.api.main import create_app

    set_api_key()
    monkeypatch.setenv("ROUTER_DB_PATH", str(tmp_path / "router.db"))
    write_config(yaml_text)
    return create_app()


@respx.mock
def test_scenario_7_free_request_is_unsupported_end_to_end(
    write_config, set_api_key, tmp_path, monkeypatch
) -> None:
    """Scenario 7 end-to-end: no policy means 400 unsupported_model, no dispatch."""
    yaml_text = "models:\n" + _model_yaml("google/gemini-3.5-flash-lite", quality=None)
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    app = _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text)

    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": "free", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_model"
    assert response.json()["error"]["param"] == "model"
    assert route.call_count == 0


@respx.mock
def test_free_is_case_sensitive(client: TestClient) -> None:
    """Edge case: "FREE" is an unknown model; ids are never case-folded."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "FREE", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_model"
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("override", "error_param"),
    [
        pytest.param({"tools": [TOOL]}, "tools", id="tools"),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is this?"},
                            {"type": "image_url", "image_url": {"url": IMAGE_DATA_URL}},
                        ],
                    }
                ]
            },
            "messages",
            id="vision",
        ),
        pytest.param(
            {"response_format": {"type": "json_object"}},
            "response_format",
            id="response_format",
        ),
    ],
)
@respx.mock
def test_free_capability_refusals_make_zero_outbound(
    client: TestClient, override: dict, error_param: str
) -> None:
    """Each capability exclusion refuses locally: 400 and no outbound request."""
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    body = {
        "model": "free",
        "messages": [{"role": "user", "content": "hi"}],
        **override,
    }

    response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "capability_unsatisfiable"
    assert response.json()["error"]["param"] == error_param
    assert route.call_count == 0


@respx.mock
def test_free_streaming_refusal_makes_zero_outbound(
    write_config, set_api_key, tmp_path, monkeypatch
) -> None:
    """A non-streaming candidate is excluded before dispatch (scenario 2's sibling)."""
    yaml_text = (
        _routing_yaml() + "models:\n" + _model_yaml("google/gemini-3.5-flash-lite", streaming=False)
    )
    unary = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    streaming = respx.post(STREAM_URL).mock(return_value=httpx.Response(200, content=b""))
    app = _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text)

    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": "free", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "streaming_unsupported"
    assert response.json()["error"]["param"] == "stream"
    assert unary.call_count == 0
    assert streaming.call_count == 0


@respx.mock
def test_free_unconfigured_priors_is_named_and_makes_zero_outbound(
    write_config, set_api_key, tmp_path, monkeypatch
) -> None:
    """R10: a model without priors is excluded with a reason, not defaulted."""
    # The scoreable model cannot serve tools; the tool-capable model has no priors.
    yaml_text = (
        _routing_yaml()
        + "models:\n"
        + _model_yaml("google/gemini-3.5-flash-lite")
        + _model_yaml(
            "google/gemini-3.5-pro",
            provider_id="gemini-3.5-pro",
            quality=None,
            tools=True,
            tools_provenance="measured",
        )
    )
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    app = _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text)

    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "free",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [TOOL],
            },
        )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "google/gemini-3.5-flash-lite (tools)" in message
    assert "google/gemini-3.5-pro (not_configured_for_routing)" in message
    assert route.call_count == 0


@respx.mock
def test_scenario_4_capacity_exclusion_makes_zero_outbound(
    write_config, set_api_key, tmp_path, monkeypatch
) -> None:
    """Scenario 4: with the quota spent, ``free`` fails locally, naming the model.

    The first request is the one that spends the one-request window; the second
    is the refusal under test. A third-party call count of one proves the
    refusal never reached the provider.
    """
    quota = (
        "    quota:\n"
        '      - resource: "requests"\n'
        "        limit: 1\n"
        "        window_seconds: 60\n"
        "        provenance: measured\n"
    )
    yaml_text = (
        _routing_yaml() + "models:\n" + _model_yaml("google/gemini-3.5-flash-lite", extra=quota)
    )
    route = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    app = _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text)
    body = {"model": "free", "messages": [{"role": "user", "content": "hi"}]}

    with TestClient(app) as test_client:
        first = test_client.post("/v1/chat/completions", json=body)
        second = test_client.post("/v1/chat/completions", json=body)

    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "no_available_capacity"
    assert "google/gemini-3.5-flash-lite (no_quota)" in second.json()["error"]["message"]
    assert route.call_count == 1


@respx.mock
def test_scenario_3_cooling_model_is_skipped_with_one_outbound(
    write_config, set_api_key, tmp_path, monkeypatch
) -> None:
    """Scenario 3: a cooling top candidate is skipped; the free request still sends once."""
    yaml_text = (
        _routing_yaml()
        + "models:\n"
        + _model_yaml(
            "google/gemini-3.5-pro", provider_id="gemini-3.5-pro", quality=95, latency_ms=500
        )
        + _model_yaml("google/gemini-3.5-flash-lite", quality=70, latency_ms=1000)
    )
    pro = respx.post(GENERATE_PRO_URL).mock(return_value=httpx.Response(429, json={}))
    flash = respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    app = _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text)

    with TestClient(app) as test_client:
        # Cool the highest-scoring model with an explicit request.
        cooled = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "google/gemini-3.5-pro",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        selected = test_client.post(
            "/v1/chat/completions",
            json={"model": "free", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert cooled.status_code == 429
    assert selected.status_code == 200
    assert selected.json()["model"] == "google/gemini-3.5-flash-lite"
    assert pro.call_count == 1  # only the cooling request, never retried
    assert flash.call_count == 1  # the free request, dispatched exactly once


# ==============================================================================
# Selection discriminants and the privacy boundary of the decision record
# ==============================================================================


def test_explicit_request_reports_explicit_selection_and_no_score() -> None:
    """``selection`` distinguishes explicit from auto without a special case at the route."""
    cfg = Config(
        models=[_make_model("test/model-a", quality=70)],
        routing=RoutingConfig(weights=RoutingWeights()),
    )
    target, decision = resolve_request(cfg, _make_req(), "test/model-a")

    assert target.selection == "explicit"
    assert target.score is None
    assert decision is None


def test_substituted_request_still_reports_substitution() -> None:
    """M3 substitution is not relabelled as auto selection."""
    model_a = _make_model("test/model-a", quality=70, tools=False, fallback_models=["test/model-b"])
    model_b = _make_model("test/model-b", quality=60, tools=True)
    cfg = Config(
        models=[model_a, model_b],
        routing=RoutingConfig(weights=RoutingWeights()),
    )
    target, decision = resolve_request(cfg, _make_req(tools=True), "test/model-a")

    assert target.selection == "substituted"
    assert target.substituted_from == "test/model-a"
    assert target.model.id == "test/model-b"
    assert decision is None


@respx.mock
def test_routing_decision_record_carries_no_payload_content(
    write_config, set_api_key, tmp_path, monkeypatch
) -> None:
    """T089: the widened allowlist only ever receives ids, scores and reason codes."""
    yaml_text = (
        _routing_yaml()
        + "models:\n"
        + _model_yaml("google/gemini-3.5-flash-lite", tools=True, tools_provenance="measured")
    )
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=GEMINI_RESP))
    app = _app_with_config(write_config, set_api_key, tmp_path, monkeypatch, yaml_text)

    recorder = LogRecorder()
    root = logging.getLogger()
    try:
        with TestClient(app) as test_client:
            root.addHandler(recorder.handler)
            root.setLevel(logging.DEBUG)
            response = test_client.post(
                "/v1/chat/completions",
                json={
                    "model": "free",
                    "messages": [{"role": "user", "content": "SECRET_PROMPT_TEXT"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "SECRET_TOOL_NAME",
                                "description": "SECRET_TOOL_DESCRIPTION",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                },
            )
    finally:
        root.removeHandler(recorder.handler)
        recorder.close()

    assert response.status_code == 200
    decisions = [r for r in recorder.records if r.get("event") == "routing_decision"]
    assert len(decisions) == 1
    record = decisions[0]
    serialised = json.dumps(record)
    assert "SECRET_PROMPT_TEXT" not in serialised
    assert "SECRET_TOOL_NAME" not in serialised
    assert "SECRET_TOOL_DESCRIPTION" not in serialised
    assert "messages" not in record
    assert "tools" not in record
