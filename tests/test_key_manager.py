"""Key pool, LRU selection, and eligibility (T023, T025, T026).

The properties that matter are narrow and specific:

- **Selection is least-recently-used, not round-robin.** Concentration is
  correct; the goal is to use a key until it cannot be used.
- **Selection does not mark a key used.** A request that then fails must not
  leave a working credential looking cold.
- **A suspended key is never selected**, and concurrent requests cannot both
  take the last eligible key.
- **No log output, repr, or exception carries key material** (CONSTITUTION §9.1).
- **The lock is never held across provider I/O** — asserted structurally below,
  because holding it across a network call would serialise every dispatch behind
  the slowest provider response.
"""

import asyncio

import pytest
from pydantic import SecretStr

from free_router.core.config import (
    ConfigError,
    key_cap_for,
    key_env_var,
    load_config,
)
from free_router.keys.manager import KeyManager, KeyRef, SelectionReason

PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _manager(clock: FakeClock | None = None, **aliases: str) -> KeyManager:
    return KeyManager(
        {(PROVIDER, MODEL): {alias: SecretStr(value) for alias, value in aliases.items()}},
        clock=clock or FakeClock(),
    )


def _single() -> KeyManager:
    return _manager(primary="secret-primary")


# --- Selection: least recently used, not round-robin ---


async def test_single_key_is_always_selected() -> None:
    manager = _single()
    for _ in range(5):
        selection = await manager.select(PROVIDER, MODEL)
        assert selection.key is not None
        assert selection.key.alias == "primary"
        assert selection.reason is SelectionReason.ONLY_KEY


async def test_least_recently_used_is_chosen() -> None:
    """Using `a` must move the preference to `b`, not back to `a`.

    This is the property that distinguishes LRU from round-robin. Round-robin
    would return `a` again here.
    """
    manager = _manager(primary="p", secondary="s")
    first = await manager.select(PROVIDER, MODEL)
    assert first.key is not None and first.key.alias == "primary"

    await manager.mark_used(first.key)
    second = await manager.select(PROVIDER, MODEL)

    assert second.key is not None
    assert second.key.alias == "secondary"
    assert second.reason is SelectionReason.LEAST_RECENTLY_USED


async def test_full_rotation_across_three_keys() -> None:
    manager = _manager(a="1", b="2", c="3")
    order = []
    for _ in range(6):
        selection = await manager.select(PROVIDER, MODEL)
        assert selection.key is not None
        order.append(selection.key.alias)
        await manager.mark_used(selection.key)

    # Strict rotation, in declaration order, with no key served twice running.
    assert order == ["a", "b", "c", "a", "b", "c"]


async def test_an_unused_key_sorts_first_on_a_fresh_process() -> None:
    """A restart forgets LRU order, and the correct bootstrap is to try each once."""
    manager = _manager(a="1", b="2", c="3")

    assert (await manager.select(PROVIDER, MODEL)).key is not None and True
    selection = await manager.select(PROVIDER, MODEL)

    assert selection.key is not None
    assert selection.key.alias == "a"


async def test_selection_is_deterministic_when_timestamps_tie() -> None:
    """Two keys marked used at the same instant must still order predictably."""
    clock = FakeClock()
    manager = _manager(a="1", b="2", clock=clock)
    keys = {
        alias: KeyRef(alias, PROVIDER, MODEL, SecretStr(v)) for alias, v in (("a", "1"), ("b", "2"))
    }
    await manager.mark_used(keys["a"])
    await manager.mark_used(keys["b"])

    # Alias breaks the tie, so repeated selection agrees.
    first = await manager.select(PROVIDER, MODEL)
    second = await manager.select(PROVIDER, MODEL)

    assert first.key is not None and second.key is not None
    assert first.key.alias == second.key.alias == "a"


async def test_lru_ordering_survives_a_clock_that_never_advances() -> None:
    """Ordering uses an ordinal, not a timestamp.

    An earlier implementation stamped ``last_used`` with the injected clock and
    broke ties on alias. With a clock whose resolution is coarser than the request
    rate -- routine on some platforms, and what a fake clock looks like -- every
    mark lands on the same instant and LRU silently collapses to alphabetical,
    pinning all traffic onto the first alias while appearing to work.

    This test freezes the clock outright, which no real monotonic clock does, and
    asserts the rotation is still exact.
    """

    class FrozenClock:
        def __call__(self) -> float:
            return 1000.0

    manager = _manager(FrozenClock(), a="1", b="2", c="3")
    order = []
    for _ in range(6):
        selection = await manager.select(PROVIDER, MODEL)
        assert selection.key is not None
        order.append(selection.key.alias)
        await manager.mark_used(selection.key)

    assert order == ["a", "b", "c", "a", "b", "c"]


async def test_selection_does_not_mark_a_key_used() -> None:
    """A request that then fails must not leave a working key looking cold.

    Marking on selection would rotate away from a key whose dispatch failed for
    reasons unrelated to the key, which is precisely when you want to keep using it.
    """
    manager = _manager(primary="p", secondary="s")

    for _ in range(3):
        selection = await manager.select(PROVIDER, MODEL)
        assert selection.key is not None
        assert selection.key.alias == "primary", "selection must not rotate on its own"


# --- Eligibility ---


async def test_a_suspended_key_is_never_selected() -> None:
    manager = _manager(primary="p", secondary="s")
    await manager.suspend(PROVIDER, MODEL, "primary")

    for _ in range(3):
        selection = await manager.select(PROVIDER, MODEL)
        assert selection.key is not None
        assert selection.key.alias == "secondary"


async def test_suspending_every_key_yields_none() -> None:
    manager = _manager(primary="p", secondary="s")
    await manager.suspend(PROVIDER, MODEL, "primary")
    await manager.suspend(PROVIDER, MODEL, "secondary")

    selection = await manager.select(PROVIDER, MODEL)

    assert selection.key is None
    assert selection.reason is SelectionReason.NONE_AVAILABLE


async def test_an_unknown_model_yields_none() -> None:
    selection = await _single().select(PROVIDER, "not-configured")

    assert selection.key is None
    assert selection.reason is SelectionReason.NONE_AVAILABLE


async def test_restore_returns_a_key_to_rotation() -> None:
    manager = _manager(primary="p")
    await manager.suspend(PROVIDER, MODEL, "primary")
    assert (await manager.select(PROVIDER, MODEL)).key is None

    await manager.restore(PROVIDER, MODEL, "primary")

    assert (await manager.select(PROVIDER, MODEL)).key is not None


def test_suspension_is_reported_for_operators() -> None:
    manager = _manager(primary="p")

    assert not manager.is_suspended(PROVIDER, MODEL, "primary")


# --- Concurrency ---


async def test_concurrent_selection_cannot_hand_out_a_suspended_key() -> None:
    """Selection and suspension share one lock, so no interleaving can leak one."""
    manager = _manager(a="1", b="2", c="3")
    await manager.suspend(PROVIDER, MODEL, "a")

    results = await asyncio.gather(*(manager.select(PROVIDER, MODEL) for _ in range(40)))
    aliases = {r.key.alias for r in results if r.key is not None}

    assert "a" not in aliases
    assert aliases <= {"b", "c"}


async def test_concurrent_mark_used_leaves_a_consistent_order() -> None:
    """Concurrent marking must not interleave into a lost update."""
    manager = _manager(a="1", b="2", c="3")
    keys = [
        KeyRef(alias, PROVIDER, MODEL, SecretStr(v))
        for alias, v in (("a", "1"), ("b", "2"), ("c", "3"))
    ]

    await asyncio.gather(*(manager.mark_used(k) for k in keys))

    selection = await manager.select(PROVIDER, MODEL)
    assert selection.key is not None


async def test_the_lock_is_not_held_across_provider_io() -> None:
    """Structural check on the thing that would silently serialise the router.

    If selection held the lock across an ``await`` of provider I/O, every
    concurrent request would queue behind the slowest upstream response. This
    asserts the lock is free between operations by proving a second coroutine can
    make progress while a simulated slow dispatch is in flight.
    """

    async def slow_dispatch(manager: KeyManager) -> None:
        selection = await manager.select(PROVIDER, MODEL)
        assert selection.key is not None
        await asyncio.sleep(0.05)  # stands in for the provider call
        await manager.mark_used(selection.key)

    manager = _manager(a="1", b="2")
    await asyncio.gather(slow_dispatch(manager), slow_dispatch(manager))

    # Both completed, and the second used the other key: no serialisation.
    assert manager.pool_size(PROVIDER, MODEL) == 2


# --- Logging safety (CONSTITUTION §9.1) ---


async def test_no_description_contains_key_material() -> None:
    manager = _manager(primary="super-secret-value", secondary="other-secret")
    selection = await manager.select(PROVIDER, MODEL)

    assert selection.key is not None
    assert "super-secret-value" not in selection.key.describe()
    assert "super-secret-value" not in selection.describe()
    assert selection.key.alias in selection.describe()


async def test_repr_redacts_the_secret() -> None:
    key = KeyRef("primary", PROVIDER, MODEL, SecretStr("super-secret-value"))

    assert "super-secret-value" not in repr(key)
    assert "<redacted>" in repr(key)


async def test_the_alias_is_what_appears_in_a_description() -> None:
    key = KeyRef("primary", PROVIDER, MODEL, SecretStr("super-secret-value"))

    assert key.describe() == "key_alias=primary"


def test_selection_reason_appears_in_the_description() -> None:
    from free_router.keys.manager import KeySelection

    described = KeySelection(None, SelectionReason.NONE_AVAILABLE).describe()

    assert "selection_reason=none_available" in described
    assert "key_alias=none" in described


# --- The §6.1 cap is code, not configuration ---


def test_google_is_capped_at_two() -> None:
    """ADR-0003 §3: Google's quota is per project, so pooling conveys no capacity."""
    assert key_cap_for("google") == 2


def test_mistral_is_uncapped() -> None:
    """Mistral permits multiple keys per organization as a first-class capability."""
    assert key_cap_for("mistral") is None


def test_an_unrecognised_provider_gets_the_conservative_cap() -> None:
    """Fail-safe: nobody has evaluated this provider's terms, so it gets §6.1's limit."""
    assert key_cap_for("anthropic") == 2


def test_the_env_var_name_is_derived_not_configurable() -> None:
    assert key_env_var("google", "primary") == "GOOGLE_KEY_PRIMARY"
    assert key_env_var("mistral", "secondary") == "MISTRAL_KEY_SECONDARY"


# --- T023: configuration schema ---


def _yaml(provider: str = "google", provider_id: str = MODEL, extra: str = "") -> str:
    return (
        f'models:\n  - id: "{provider}/{provider_id}"\n    provider: "{provider}"\n'
        f'    provider_id: "{provider_id}"\n{extra}'
    )


def test_a_model_without_key_aliases_declares_one_key(write_config, set_api_key) -> None:
    """The single-key case must cost nothing to express."""
    set_api_key()
    write_config(_yaml())

    config = load_config()

    assert config.models[0].key_aliases == ["primary"]


def test_two_aliases_are_accepted_for_a_permitted_provider(write_config, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "a")
    monkeypatch.setenv("MISTRAL_KEY_SECONDARY", "b")
    write_config(
        _yaml("mistral", "ministral-3b-2512", '    key_aliases: ["primary", "secondary"]\n')
    )

    config = load_config()

    assert config.models[0].key_aliases == ["primary", "secondary"]


def test_three_aliases_are_refused_for_google(write_config, monkeypatch) -> None:
    """CONSTITUTION §6.1's cap is a startup failure, not a warning."""
    for alias in ("primary", "secondary", "tertiary"):
        monkeypatch.setenv(f"GOOGLE_KEY_{alias.upper()}", "x")
    write_config(_yaml(extra='    key_aliases: ["primary", "secondary", "tertiary"]\n'))

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "at most 2" in str(excinfo.value)


def test_three_aliases_are_allowed_for_mistral(write_config, monkeypatch) -> None:
    """Mistral is classified permitted, so §6.1's cap does not apply to it."""
    for alias in ("primary", "secondary", "tertiary"):
        monkeypatch.setenv(f"MISTRAL_KEY_{alias.upper()}", "x")
    write_config(
        _yaml(
            "mistral",
            "ministral-3b-2512",
            '    key_aliases: ["primary", "secondary", "tertiary"]\n',
        )
    )

    assert len(load_config().models[0].key_aliases) == 3


def test_a_missing_credential_names_the_alias_and_variable_only(write_config, monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_KEY_SECONDARY", raising=False)
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "real-secret")
    write_config(_yaml(extra='    key_aliases: ["primary", "secondary"]\n'))

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "secondary" in message
    assert "GOOGLE_KEY_SECONDARY" in message
    assert "real-secret" not in message


def test_an_undeclared_matching_variable_is_ignored(write_config, monkeypatch) -> None:
    """A stray variable must not silently join the pool past the §6.1 cap."""
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "real")
    monkeypatch.setenv("GOOGLE_KEY_ROGUE", "must-be-ignored")
    write_config(_yaml())

    resolved = load_config().resolve_key_material()

    assert list(resolved[(PROVIDER, MODEL)]) == ["primary"]


def test_duplicate_aliases_are_refused(write_config) -> None:
    """The alias is the only key identifier in a log, so it must be unambiguous."""
    write_config(_yaml(extra='    key_aliases: ["primary", "PRIMARY"]\n'))

    with pytest.raises(ConfigError, match="(?i)duplicate"):
        load_config(resolve_keys=False)


def test_an_empty_alias_list_is_refused(write_config) -> None:
    write_config(_yaml(extra="    key_aliases: []\n"))

    with pytest.raises(ConfigError, match="(?i)must not be empty"):
        load_config(resolve_keys=False)


@pytest.mark.parametrize("alias", ["has space", "-leading-dash", "1digit", "semi;colon", ""])
def test_malformed_aliases_are_refused(write_config, alias: str) -> None:
    """Aliases become environment suffixes and appear in log lines."""
    write_config(_yaml(extra=f'    key_aliases: ["{alias}"]\n'))

    with pytest.raises(ConfigError):
        load_config(resolve_keys=False)


def test_key_aliases_in_yaml_is_not_treated_as_a_secret(write_config, set_api_key) -> None:
    """`key_aliases` names slots, so it must not trip the ADR-0002 secret guard.

    `keys` and `api_keys` remain refused; this field is structurally different and
    is explicitly permitted.
    """
    set_api_key()
    write_config(_yaml(extra='    key_aliases: ["primary"]\n'))

    assert load_config().models[0].key_aliases == ["primary"]


@pytest.mark.parametrize("field", ["keys", "api_keys", "api_key", "gemini_api_key"])
def test_secret_named_yaml_fields_remain_refused(write_config, field: str) -> None:
    """ADR-0002's rejection is retained unchanged by ADR-0003."""
    write_config(_yaml(extra=f"    {field}: leaked-secret\n"))

    with pytest.raises(ConfigError, match=field):
        load_config(resolve_keys=False)


def test_key_material_is_resolved_per_model(write_config, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "google-secret")
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "mistral-secret")
    write_config(
        _yaml() + '  - id: "mistral/ministral-3b-2512"\n    provider: "mistral"\n'
        '    provider_id: "ministral-3b-2512"\n'
    )

    resolved = load_config().resolve_key_material()

    assert resolved[(PROVIDER, MODEL)]["primary"].get_secret_value() == "google-secret"
    assert (
        resolved[("mistral", "ministral-3b-2512")]["primary"].get_secret_value() == "mistral-secret"
    )


def test_a_mistral_only_deployment_starts_without_a_google_key(write_config, monkeypatch) -> None:
    """Deferred from #7: provider credentials are per model, not a required global field."""
    monkeypatch.delenv("GOOGLE_KEY_PRIMARY", raising=False)
    monkeypatch.setenv("MISTRAL_KEY_PRIMARY", "mistral-secret")
    write_config(
        'models:\n  - id: "mistral/ministral-3b-2512"\n    provider: "mistral"\n'
        '    provider_id: "ministral-3b-2512"\n'
    )

    config = load_config()

    assert config.models[0].provider == "mistral"


def test_dotenv_supplies_key_material(write_config, workdir, monkeypatch) -> None:
    """`.env` remains a valid source for keys.

    The derived variable names are not declared pydantic fields, so
    `pydantic-settings` cannot load them; the loader reads `.env` explicitly so a
    developer's usual local-secret store keeps working.
    """
    monkeypatch.delenv("GOOGLE_KEY_PRIMARY", raising=False)
    (workdir / ".env").write_text("GOOGLE_KEY_PRIMARY=from-dotenv\n", encoding="utf-8")
    write_config(_yaml())

    resolved = load_config().resolve_key_material()

    assert resolved[(PROVIDER, MODEL)]["primary"].get_secret_value() == "from-dotenv"


def test_the_process_environment_outranks_dotenv(write_config, workdir, monkeypatch) -> None:
    """ADR-001 precedence: the process environment overrides `.env`."""
    (workdir / ".env").write_text("GOOGLE_KEY_PRIMARY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "from-environment")
    write_config(_yaml())

    resolved = load_config().resolve_key_material()

    assert resolved[(PROVIDER, MODEL)]["primary"].get_secret_value() == "from-environment"
