"""The shipped `config.yaml`, validated.

Every other configuration test in this suite writes its own `config.yaml` through
the ``write_config`` fixture. That is correct for testing the schema, and it has a
blind spot: **nothing loaded the file real deployments use.** Batch 6 added the
Mistral model to the shipped config to enable a two-provider measurement, which
made `MISTRAL_KEY_PRIMARY` mandatory. All 435 tests passed, because every one of
them wrote its own config, and the failure surfaced only when CI's smoke test
started a container with `GOOGLE_KEY_PRIMARY` alone and it aborted at startup.

These tests load the real file so that class of change is caught by `pytest`.

**What this cannot cover.** The unit suite has no way to know which credentials a
container will be *given* -- that lives in `.github/workflows/ci.yml` and
`docker-compose.yml`, and reading those here would make a test assert on workflow
syntax rather than on the router. The cross-file contract is therefore still
checked only by the CI smoke test, which is the right place for it: it starts the
actual image with the actual environment. The warnings in `config.yaml` and
`ci.yml` say so where the next person will actually be editing.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import MANAGED_ENV_VARS

from free_router.core.config import ConfigError, key_env_var, load_config

SHIPPED_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
"""The repository's default configuration, as baked into the image by the Dockerfile."""

_MODEL_KEYS = {"id", "provider", "provider_id", "key_aliases", "quota"}
"""Fields a model entry may declare. Anything else is a mistake or a pasted secret."""


def _shipped(*, resolve_keys: bool = False) -> Any:
    return load_config(SHIPPED_CONFIG, resolve_keys=resolve_keys)


def test_shipped_config_exists_and_is_valid() -> None:
    """The default configuration parses and satisfies the schema.

    ``resolve_keys=False`` so this asserts the file itself rather than the
    caller's environment: whether a credential is present is a deployment
    question, but whether the document is well-formed is a repository question.
    """
    assert SHIPPED_CONFIG.is_file(), f"expected the shipped config at {SHIPPED_CONFIG}"

    config = _shipped()

    assert config.models, "the shipped config declares no models"
    for model in config.models:
        assert model.provider and model.provider_id, f"{model.id} is incompletely declared"


def test_shipped_config_declares_no_secret() -> None:
    """No credential-shaped field appears in the default configuration (9.2).

    Checked against the *parsed* document rather than the raw text. A substring
    scan over the file is worse than useless here: this config carries comments
    that legitimately mention "tokens" and "secrets", so a text scan either
    cries wolf or has to special-case prose. What actually matters is the
    structure -- a field the schema does not recognise, which is how a pasted key
    would arrive.
    """
    document = yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))

    assert isinstance(document, dict), "the shipped config is not a mapping"
    unexpected_top = set(document) - {"models"}
    assert not unexpected_top, (
        f"the shipped config declares unexpected top-level keys {sorted(unexpected_top)}."
    )

    for entry in document["models"]:
        unexpected = set(entry) - _MODEL_KEYS
        assert not unexpected, (
            f"model {entry.get('id')!r} declares unexpected fields {sorted(unexpected)}. "
            f"Known fields are {sorted(_MODEL_KEYS)}; anything else is a mistake or a secret."
        )


def test_shipped_config_requires_only_managed_credentials() -> None:
    """Every credential slot the shipped config needs is one the suite manages.

    This keeps `MANAGED_ENV_VARS` honest. If a model for an unlisted provider were
    added here, its variable would not be cleared between tests, so a developer's
    real shell could leak into an unrelated result. Failing here names the variable
    to add.
    """
    required = {
        key_env_var(model.provider, alias)
        for model in _shipped().models
        for alias in model.key_aliases
    }

    unmanaged = required - set(MANAGED_ENV_VARS)
    assert not unmanaged, (
        f"the shipped config requires {sorted(unmanaged)}, which tests/conftest.py does not "
        f"clear. Add them to MANAGED_ENV_VARS."
    )


def test_shipped_config_resolves_with_its_declared_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given each declared slot, the shipped config resolves without error.

    Closes the loop on the startup path CI exercises: this is the same
    ``resolve_key_material`` call that aborts a real boot, driven from the real
    file rather than a fixture.
    """
    config = _shipped()

    for model in config.models:
        for alias in model.key_aliases:
            monkeypatch.setenv(key_env_var(model.provider, alias), "placeholder-not-a-real-key")

    resolved = config.resolve_key_material()

    assert resolved, "the shipped config resolved no credential material"
    values = [secret for slots in resolved.values() for secret in slots.values()]
    assert values, "no slot produced key material"
    assert all(v.get_secret_value() == "placeholder-not-a-real-key" for v in values)


def test_a_google_only_deployment_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default deployment contract: one credential, and that must be enough.

    Guards the property Batch 6 nearly broke. Every credential a model declares
    is mandatory and a missing one aborts startup, so adding a model to the
    shipped config silently makes a second credential required for *every* user.
    That is a legitimate change to make -- but deliberately, with the deployment
    files updated, not as a side effect of a measurement.
    """
    for name in MANAGED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_KEY_PRIMARY", "placeholder-not-a-real-key")

    config = load_config(SHIPPED_CONFIG)  # resolve_keys=True, as startup does

    assert {model.provider for model in config.models} == {"google"}, (
        "the shipped config now requires providers beyond Google. Legitimate -- but "
        "docker-compose.yml and ci.yml must pass those credentials too, or the "
        "container aborts at startup."
    )


def test_missing_shipped_config_reports_the_path() -> None:
    """An absent config is a clear error naming the file looked for."""
    with pytest.raises(ConfigError, match="definitely-not-here.yaml"):
        load_config(SHIPPED_CONFIG.parent / "definitely-not-here.yaml", resolve_keys=False)
