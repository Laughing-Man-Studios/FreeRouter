"""Provider timeout configuration.

The budget is resolved from the environment at import, in
:mod:`free_router.providers.timeout`. These tests re-import that module rather
than patching the constant, because resolution happens at import time and a test
that patches the attribute would not exercise that at all.

The tests target the timeout module rather than an adapter because resolution is
shared: it was extracted out of the Google adapter when the Mistral adapter
needed the same value, so that one environment variable has one source of truth
instead of two that drift.
"""

import importlib
from collections.abc import Iterator

import pytest

from free_router.providers import timeout as adapter_module
from free_router.providers.google import adapter as google_adapter
from free_router.providers.mistral import adapter as mistral_adapter


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Ensure the override is absent unless a test sets it."""
    monkeypatch.delenv("ROUTER_PROVIDER_TIMEOUT", raising=False)
    yield monkeypatch


def test_default_budget_fits_observed_provider_latency() -> None:
    """The default must exceed real provider header latency.

    Fifteen live calls to gemini-3.5-flash-lite measured p50 743 ms, p90 2332 ms,
    and 2893 ms maximum. The default is deliberately above that maximum: a
    budget the provider cannot meet turns working traffic into 504s, which is
    exactly the defect the original 0.4 s window caused.
    """
    assert adapter_module.DEFAULT_PROVIDER_TIMEOUT_SECONDS == 5.0
    assert adapter_module.PROVIDER_TIMEOUT_SECONDS >= 2.9


def test_default_exceeds_the_original_budget_by_a_wide_margin() -> None:
    """Records why the value changed, so it is not quietly reverted."""
    # The original spec value, retained here as the regression this prevents.
    original_spec_value = 0.4
    assert adapter_module.DEFAULT_PROVIDER_TIMEOUT_SECONDS > original_spec_value * 5, (
        "the default should have ample headroom over the original 400 ms window"
    )


def test_no_override_yields_the_default(clean_env: pytest.MonkeyPatch) -> None:
    """With no environment variable, the documented default applies."""
    reloaded = importlib.reload(adapter_module)
    try:
        assert reloaded.PROVIDER_TIMEOUT_SECONDS == reloaded.DEFAULT_PROVIDER_TIMEOUT_SECONDS
    finally:
        clean_env.delenv("ROUTER_PROVIDER_TIMEOUT", raising=False)
        importlib.reload(adapter_module)


def test_override_is_honoured(clean_env: pytest.MonkeyPatch) -> None:
    """M1 needs per-attempt budgets, so the value must be settable."""
    clean_env.setenv("ROUTER_PROVIDER_TIMEOUT", "1.5")
    reloaded = importlib.reload(adapter_module)
    try:
        assert reloaded.PROVIDER_TIMEOUT_SECONDS == 1.5
    finally:
        clean_env.delenv("ROUTER_PROVIDER_TIMEOUT", raising=False)
        importlib.reload(adapter_module)


@pytest.mark.parametrize("bad", ["abc", "", "1.2.3", "-1", "0", "-0.5"])
def test_invalid_override_fails_loudly(clean_env: pytest.MonkeyPatch, bad: str) -> None:
    """A nonsensical budget must not be silently accepted.

    Falling back to the default would hide a typo in a deployment script; zero
    or negative would time out every request immediately.
    """
    clean_env.setenv("ROUTER_PROVIDER_TIMEOUT", bad)
    try:
        with pytest.raises(ValueError, match="ROUTER_PROVIDER_TIMEOUT"):
            importlib.reload(adapter_module)
    finally:
        clean_env.delenv("ROUTER_PROVIDER_TIMEOUT", raising=False)
        importlib.reload(adapter_module)


def test_module_exposes_both_names() -> None:
    """Both the default and the effective value are part of the public surface.

    Tests and diagnostics need the default to assert against; runtime code uses
    the effective value.
    """
    assert hasattr(adapter_module, "DEFAULT_PROVIDER_TIMEOUT_SECONDS")
    assert hasattr(adapter_module, "PROVIDER_TIMEOUT_SECONDS")


def test_both_adapters_share_one_resolved_value() -> None:
    """Adapters must not each resolve the budget independently.

    A per-adapter copy would be two sources of truth for one environment
    variable, and the first retune of the default from measurement would leave
    one provider on the old value.
    """
    assert google_adapter.PROVIDER_TIMEOUT_SECONDS == adapter_module.PROVIDER_TIMEOUT_SECONDS
    assert mistral_adapter.PROVIDER_TIMEOUT_SECONDS == adapter_module.PROVIDER_TIMEOUT_SECONDS
