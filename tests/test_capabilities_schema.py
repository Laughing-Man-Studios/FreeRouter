"""Model capability metadata schema and configuration validation tests (T066-T068).

Verifies spec Requirements 1, 2, 7, and ADR-0008 / ADR-0009:
- Generic CapabilityValue[T] typing and YAML round-trip under mypy --strict.
- Provenance constraints: published and measured require source, assumed refuses source.
- Assumed provenance treated as unusable (not verified).
- Fail-closed startup validation: missing block, unknown keys, invalid context window.
- Fallback relationships: default empty, unknown IDs, self-reference, duplicates,
  and single-hop chain prohibition.
- Model lookups via Config.model_by_id with no ModelRegistry class (Clarification C3).
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from free_router.core.config import (
    Capabilities,
    CapabilityValue,
    ConfigError,
    load_config,
)

pytestmark = pytest.mark.usefixtures("workdir")

_VALID_CAPABILITIES_YAML = """\
    capabilities:
      context_window:
        value: 131072
        provenance: published
        source: "provider model documentation, retrieved 2026-11-03"
      supports_streaming:
        value: true
        provenance: measured
        source: "M2 measurement, 20/20 streamed, 2026-10-30"
      supports_tools:
        value: false
        provenance: published
        source: "provider model documentation, retrieved 2026-11-03"
      supports_vision:
        value: false
        provenance: published
        source: "provider model documentation, retrieved 2026-11-03"
      supports_json_schema:
        value: false
        provenance: published
        source: "provider model documentation, retrieved 2026-11-03"
    fallback_models: []
"""


def _model_yaml(
    model_id: str = "google/gemini-3.5-flash-lite",
    provider: str = "google",
    provider_id: str = "gemini-3.5-flash-lite",
    capabilities_body: str = _VALID_CAPABILITIES_YAML,
    extra: str = "",
) -> str:
    return (
        f"models:\n"
        f'  - id: "{model_id}"\n'
        f'    provider: "{provider}"\n'
        f'    provider_id: "{provider_id}"\n'
        f"{capabilities_body}"
        f"{extra}"
    )


# --- 1. Schema typing & YAML round-trip ---


def test_capabilities_round_trip_with_types_intact(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
) -> None:
    """Every configured model round-trips a complete capabilities block with types intact."""
    set_api_key()
    write_config(_model_yaml())

    config = load_config(resolve_keys=False)
    assert len(config.models) == 1
    model = config.models[0]

    assert isinstance(model.capabilities, Capabilities)
    cw: int = model.capabilities.context_window.value
    assert cw == 131072
    assert model.capabilities.context_window.provenance == "published"
    assert model.capabilities.context_window.source == (
        "provider model documentation, retrieved 2026-11-03"
    )
    assert model.capabilities.context_window.is_usable is True

    streaming: bool = model.capabilities.supports_streaming.value
    assert streaming is True
    assert model.capabilities.supports_streaming.provenance == "measured"
    assert model.capabilities.supports_streaming.is_usable is True

    assert model.capabilities.supports_tools.value is False
    assert model.capabilities.supports_vision.value is False
    assert model.capabilities.supports_json_schema.value is False
    assert model.fallback_models == []


# --- 2. Provenance and source rules (ADR-0008) ---


def test_provenance_published_requires_source() -> None:
    """A published capability without a source fails validation."""
    with pytest.raises(ValueError, match="source is required when provenance is 'published'"):
        CapabilityValue[int](value=1000, provenance="published", source=None)

    with pytest.raises(ValueError, match="source is required when provenance is 'published'"):
        CapabilityValue[int](value=1000, provenance="published", source="   ")


def test_provenance_measured_requires_source() -> None:
    """A measured capability without a source fails validation."""
    with pytest.raises(ValueError, match="source is required when provenance is 'measured'"):
        CapabilityValue[bool](value=True, provenance="measured", source=None)


def test_provenance_assumed_refuses_source() -> None:
    """An assumed capability with a source fails validation: guesses cannot claim verification."""
    with pytest.raises(
        ValueError, match="source must not be provided when provenance is 'assumed'"
    ):
        CapabilityValue[bool](value=True, provenance="assumed", source="some docs")


def test_provenance_assumed_is_unusable() -> None:
    """An assumed capability is treated as unknown/unverified (is_usable is False)."""
    cap = CapabilityValue[bool](value=True, provenance="assumed", source=None)
    assert cap.is_usable is False

    published_cap = CapabilityValue[bool](value=True, provenance="published", source="docs")
    assert published_cap.is_usable is True

    measured_cap = CapabilityValue[bool](value=True, provenance="measured", source="test")
    assert measured_cap.is_usable is True


def test_invalid_provenance_rejected(
    write_config: Callable[..., Path],
) -> None:
    """An unknown provenance string fails validation, naming the model and field."""
    yaml_content = _model_yaml(
        capabilities_body="""\
    capabilities:
      context_window:
        value: 131072
        provenance: guessed
        source: "guess"
      supports_streaming:
        value: true
        provenance: measured
        source: "m"
      supports_tools:
        value: false
        provenance: published
        source: "s"
      supports_vision:
        value: false
        provenance: published
        source: "s"
      supports_json_schema:
        value: false
        provenance: published
        source: "s"
"""
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "models.0.capabilities.context_window.provenance" in message


# --- 3. Startup validation fails fast (fail-closed, C2) ---


def test_missing_capabilities_block_fails_startup(
    write_config: Callable[..., Path],
) -> None:
    """A model without a capabilities block fails startup naming the model and field."""
    yaml_content = (
        'models:\n  - id: "google/gemini-3.5-flash-lite"\n    provider: "google"\n'
        '    provider_id: "gemini-3.5-flash-lite"\n'
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "models.0.capabilities" in message
    assert "Field required" in message


def test_published_without_source_fails_startup(
    write_config: Callable[..., Path],
) -> None:
    """A published capability with no source fails startup naming the model and field."""
    yaml_content = _model_yaml(
        capabilities_body="""\
    capabilities:
      context_window:
        value: 131072
        provenance: published
      supports_streaming:
        value: true
        provenance: measured
        source: "m"
      supports_tools:
        value: false
        provenance: published
        source: "s"
      supports_vision:
        value: false
        provenance: published
        source: "s"
      supports_json_schema:
        value: false
        provenance: published
        source: "s"
"""
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "models.0.capabilities.context_window" in message
    assert "source is required" in message


def test_non_positive_context_window_fails_startup(
    write_config: Callable[..., Path],
) -> None:
    """A context_window of zero or negative fails startup naming the model and field."""
    for bad_window in (0, -100):
        yaml_content = _model_yaml(
            capabilities_body=f"""\
    capabilities:
      context_window:
        value: {bad_window}
        provenance: published
        source: "docs"
      supports_streaming:
        value: true
        provenance: measured
        source: "m"
      supports_tools:
        value: false
        provenance: published
        source: "s"
      supports_vision:
        value: false
        provenance: published
        source: "s"
      supports_json_schema:
        value: false
        provenance: published
        source: "s"
"""
        )
        write_config(yaml_content)

        with pytest.raises(ConfigError) as excinfo:
            load_config(resolve_keys=False)

        message = str(excinfo.value)
        assert "models.0.capabilities.context_window" in message
        assert "context_window must be a positive integer" in message


def test_unknown_capability_key_fails_startup(
    write_config: Callable[..., Path],
) -> None:
    """An unknown capability key is refused (extra='forbid') naming the unknown key."""
    yaml_content = _model_yaml(
        capabilities_body="""\
    capabilities:
      context_window:
        value: 131072
        provenance: published
        source: "docs"
      supports_streaming:
        value: true
        provenance: measured
        source: "m"
      supports_tools:
        value: false
        provenance: published
        source: "s"
      supports_vision:
        value: false
        provenance: published
        source: "s"
      supports_json_schema:
        value: false
        provenance: published
        source: "s"
      supports_audio:
        value: true
        provenance: published
        source: "audio-docs"
"""
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "supports_audio" in message


# --- 4. Fallback models validation (ADR-0009, spec §5, §7) ---


def test_fallback_self_reference_refused(
    write_config: Callable[..., Path],
) -> None:
    """A model declaring itself in fallback_models fails startup."""
    yaml_content = _model_yaml(
        model_id="google/gemini-3.5-flash-lite",
        capabilities_body=_VALID_CAPABILITIES_YAML.replace(
            "fallback_models: []",
            'fallback_models: ["google/gemini-3.5-flash-lite"]',
        ),
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "google/gemini-3.5-flash-lite" in message
    assert "self-referential fallback_models entry" in message


def test_fallback_duplicates_refused(
    write_config: Callable[..., Path],
) -> None:
    """Duplicate fallback_models entries fail startup."""
    yaml_content = (
        _model_yaml(
            model_id="google/gemini-3.5-flash-lite",
            capabilities_body=_VALID_CAPABILITIES_YAML.replace(
                "fallback_models: []",
                'fallback_models: ["google/gemini-pro", "google/gemini-pro"]',
            ),
        )
        + '  - id: "google/gemini-pro"\n    provider: "google"\n'
        '    provider_id: "gemini-pro"\n' + _VALID_CAPABILITIES_YAML
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "duplicate fallback_models entry 'google/gemini-pro'" in message


def test_fallback_unknown_model_refused(
    write_config: Callable[..., Path],
) -> None:
    """A fallback_models entry naming an unconfigured model fails startup."""
    yaml_content = _model_yaml(
        model_id="google/gemini-3.5-flash-lite",
        capabilities_body=_VALID_CAPABILITIES_YAML.replace(
            "fallback_models: []",
            'fallback_models: ["google/gemini-not-configured"]',
        ),
    )
    write_config(yaml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert "model 'google/gemini-3.5-flash-lite' declares unknown fallback_models entry" in message
    assert "google/gemini-not-configured" in message


def test_fallback_chain_refused(
    write_config: Callable[..., Path],
) -> None:
    """A fallback that itself declares fallbacks fails startup (single-hop enforcement)."""
    fb_b = 'fallback_models: ["google/model-b"]'
    fb_c = 'fallback_models: ["google/model-c"]'
    model_a = (
        'models:\n  - id: "google/model-a"\n    provider: "google"\n    provider_id: "a"\n'
        + _VALID_CAPABILITIES_YAML.replace("fallback_models: []", fb_b)
    )
    model_b = (
        '  - id: "google/model-b"\n    provider: "google"\n    provider_id: "b"\n'
        + _VALID_CAPABILITIES_YAML.replace("fallback_models: []", fb_c)
    )
    model_c = (
        '  - id: "google/model-c"\n    provider: "google"\n    provider_id: "c"\n'
        + _VALID_CAPABILITIES_YAML
    )
    write_config(model_a + model_b + model_c)

    with pytest.raises(ConfigError) as excinfo:
        load_config(resolve_keys=False)

    message = str(excinfo.value)
    assert (
        "model 'google/model-a' declares fallback 'google/model-b' which itself declares" in message
    )
    assert "Fallback chains are refused" in message


def test_valid_single_hop_fallback_succeeds(
    write_config: Callable[..., Path],
) -> None:
    """A valid single-hop fallback configuration loads successfully."""
    fb_b = 'fallback_models: ["google/model-b"]'
    model_a = (
        'models:\n  - id: "google/model-a"\n    provider: "google"\n    provider_id: "a"\n'
        + _VALID_CAPABILITIES_YAML.replace("fallback_models: []", fb_b)
    )
    model_b = (
        '  - id: "google/model-b"\n    provider: "google"\n    provider_id: "b"\n'
        + _VALID_CAPABILITIES_YAML
    )
    write_config(model_a + model_b)

    config = load_config(resolve_keys=False)
    assert config.models[0].fallback_models == ["google/model-b"]
    assert config.models[1].fallback_models == []


# --- 5. Lookups and no ModelRegistry class (Clarification C3) ---


def test_config_model_by_id_lookup(
    write_config: Callable[..., Path],
) -> None:
    """Config.model_by_id returns the matching ModelConfig or None."""
    write_config(_model_yaml())

    config = load_config(resolve_keys=False)
    found = config.model_by_id("google/gemini-3.5-flash-lite")
    assert found is not None
    assert found.id == "google/gemini-3.5-flash-lite"

    assert config.model_by_id("non-existent") is None


def test_no_model_registry_class_in_src() -> None:
    """Verify no ModelRegistry class was introduced (Clarification C3, CONSTITUTION §2.3)."""
    import free_router.core.config as config_module

    assert not hasattr(config_module, "ModelRegistry")
