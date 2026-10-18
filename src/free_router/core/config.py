"""Configuration loading and startup validation.

Configuration sources, highest precedence first:

1. Explicit initialisation values (used by tests).
2. Process environment variables.
3. An uncommitted local ``.env`` file.
4. ``config.yaml``.

Environment variables therefore strictly override ``config.yaml`` values, as
required by ADR-001. Secrets are read from the environment only: the YAML
source rejects both ``${VAR}`` references and secret-named fields, so a key
can never end up in version-controlled configuration (spec 6, CONSTITUTION 9.2).
"""

from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from free_router.quota.dimensions import QuotaDimension

KEY_ALIAS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
"""Aliases become environment-variable suffixes, so they are restricted.

The alias is the only key identifier permitted in a log line or the database
(CONSTITUTION §9.1). Restricting it to an identifier-shaped string means an
alias cannot smuggle a newline, a quote, or a path separator into a log record.
"""

DEFAULT_KEY_ALIASES = ("primary",)
"""The alias used when a model does not declare any.

One key is the normal case and must cost nothing to express, so a
single-key deployment writes no ``key_aliases`` at all.
"""

_KEY_CAP_DEFAULT = 2
"""Cap applied to a provider with no recorded compliance classification.

Fail-safe. CONSTITUTION §6.1 permits uncapped pooling only where a provider's
terms *explicitly* permit it, so a provider nobody has evaluated gets the
conservative limit rather than the benefit of the doubt.
"""

_KEY_CAPS: Mapping[str, int | None] = {
    "google": 2,
    "mistral": None,
}
"""Per-provider key caps, from the Issue #51 compliance evaluation (ADR-0003 §3).

**This is code rather than configuration deliberately.** CONSTITUTION §2 makes
configuration the source of non-secret *policy*, but this limit is a compliance
constraint. Exposing it in ``config.yaml`` would mean anyone who can edit the
file can raise a constitutional limit, which defeats the point of having one.
What legitimately changes is the *classification* of a provider when its terms
change, and that is a reviewed code change recorded in an ADR.

Google is capped at 2 because its quota is per project, so a second key shares
the first one's bucket and pooling conveys no capacity. Mistral permits multiple
keys per organization as a first-class capability with no prohibition in its
terms, so CONSTITUTION §6.1's cap does not apply to it.
"""


def key_cap_for(provider: str) -> int | None:
    """The key cap for a provider, or ``None`` when pooling is permitted.

    An unrecognised provider returns :data:`_KEY_CAP_DEFAULT` rather than
    ``None``. Mistral's ``None`` is a recorded compliance conclusion; an unknown
    provider's would not be, and the two must not be conflated.
    """
    return _KEY_CAPS.get(provider, _KEY_CAP_DEFAULT)


def key_env_var(provider: str, alias: str) -> str:
    """The environment variable holding the key material for an alias.

    **Derived from the provider and alias, never taken from configuration.**
    Accepting a variable name from ``config.yaml`` would let the file direct the
    router at any environment variable on the host, which is exactly the
    capability ADR-0002 exists to withhold.
    """
    return f"{provider.upper()}_KEY_{alias.upper()}"


DEFAULT_CONFIG_FILENAME = "config.yaml"
"""Filename looked up in the current working directory when no path is given."""

CONFIG_PATH_ENV_VAR = "ROUTER_CONFIG_PATH"
"""Environment variable naming an explicit ``config.yaml`` location."""

DOTENV_FILENAME = ".env"
"""Uncommitted local secrets file, matching ``SettingsConfigDict(env_file=...)``."""

_FORBIDDEN_YAML_KEYS = frozenset(
    {
        "api_key",
        "api_keys",
        "gemini_api_key",
        "keys",
        "password",
        "secret",
        "token",
    }
)
"""Field names that must never appear in ``config.yaml``. Keys are env-only."""

_PLACEHOLDER = "${"
"""Prefix of an environment variable reference such as ``${GEMINI_API_KEY}``."""

_config_path_override: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "free_router_config_path", default=None
)
"""Carries an explicit path from :func:`load_config` into the YAML settings source.

``BaseSettings.__init__`` offers no per-call hook for passing a file path to a
custom source, so the path travels via a context variable. Configuration is
loaded once during startup, before the server accepts traffic.
"""


class ConfigError(RuntimeError):
    """A condition that must prevent the process from starting."""


class ModelConfig(BaseModel):
    """A single externally-addressable model and its provider mapping."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    provider_id: str
    cooldown_seconds: float = Field(default=60.0, gt=0)
    """How long to wait after a rate limit before this model may be dispatched again.

    Per provider/model rather than global (spec "Configuration is the source of
    non-secret policy"): a provider whose window rolls over in a minute and one
    whose daily budget resets at midnight Pacific must not share a value.

    This is a **floor**. A provider that states its own retry delay wins when it
    asks for longer, never when it asks for less -- CONSTITUTION §6.1 forbids
    retrying into a rate limit, and shortening the wait is precisely that.
    """
    quota: list[QuotaDimension] = Field(default_factory=list)
    """Level 1 quota limits for this model.

    Deliberately keyed on ``(provider, provider_id)`` rather than on a key
    alias: Google's limits apply per *project*, so two keys in one project share
    one bucket and a per-key counter would report headroom that does not exist.
    See :mod:`free_router.quota.dimensions`.

    Empty means no dimensions are enforced. An unverified limit is omitted
    rather than declared infinite, because an omitted dimension is a visible gap
    while an infinite one would be a false guarantee.
    """
    key_aliases: list[str] = Field(default_factory=lambda: list(DEFAULT_KEY_ALIASES))
    """Named slots for this model's key material. Names slots, never secrets.

    Key material lives in the environment under the variable
    :func:`key_env_var` derives from the provider and alias. The alias is the
    only key identifier permitted in logs or the database (CONSTITUTION §9.1).

    This is a *permitted structural field*, deliberately distinct from the
    secret-named fields in :data:`_FORBIDDEN_YAML_KEYS`. ``key_aliases`` cannot
    carry a secret because it names nothing but the slot the secret occupies.
    """

    @field_validator("key_aliases")
    @classmethod
    def _validate_aliases(cls, value: list[str]) -> list[str]:
        """Require aliases to be well-formed, present, and unique.

        Raises:
            ValueError: On an empty list, a duplicate, or a malformed alias.
        """
        if not value:
            raise ValueError(
                "key_aliases must not be empty. Omit the field entirely for the "
                f"default single key ('{DEFAULT_KEY_ALIASES[0]}')."
            )
        seen: set[str] = set()
        for alias in value:
            if not KEY_ALIAS_PATTERN.match(alias):
                raise ValueError(
                    f"key alias '{alias}' must start with a letter and contain only "
                    "letters, digits and underscores. Aliases become environment "
                    "variable suffixes and appear in log lines, so an alias shaped "
                    "like anything else is refused."
                )
            lowered = alias.lower()
            if lowered in seen:
                raise ValueError(
                    f"duplicate key alias '{alias}'. Aliases must be unique within a "
                    "model, since the alias is the only key identifier in a log."
                )
            seen.add(lowered)
        return value

    @model_validator(mode="after")
    def _validate_cap(self) -> ModelConfig:
        """Enforce the per-provider key cap as a validation failure.

        CONSTITUTION §6.1's limit exists to reduce account-suspension risk. A
        limit honoured by convention fails exactly when it is under pressure, so
        exceeding it stops startup.

        Raises:
            ValueError: If the model declares more aliases than its provider's cap.
        """
        cap = key_cap_for(self.provider)
        if cap is not None and len(self.key_aliases) > cap:
            raise ValueError(
                f"model '{self.id}' declares {len(self.key_aliases)} key aliases but "
                f"provider '{self.provider}' permits at most {cap}. Pooling is capped "
                "under CONSTITUTION 6.1 Conservative Compliance Mode; see ADR-0003."
            )
        return self


class QuotaConfig(BaseModel):
    """Router-wide quota behaviour shared by every provider."""

    model_config = ConfigDict(extra="forbid")

    estimation_safety_margin: float = Field(default=1.1, gt=1.0)
    """Multiplier applied to estimated tokens before reserving (CONSTITUTION §8.2).

    Must exceed 1.0. An estimate below the true figure reserves less capacity
    than the request will consume, which is precisely the overshoot the
    reservation cycle exists to prevent.
    """


class DispatchConfig(BaseModel):
    """Retry budget and failure policy for the dispatch loop.

    Defaults are deliberately small. **Every retry consumes real quota**: the
    request was sent, so it counts against the provider's per-minute and
    per-day ceilings even though it produced nothing. Against measured limits of
    15 RPM and 500 RPD for ``gemini-3.5-flash-lite``, an aggressive retry policy
    spends the day's budget on retries rather than on answers.
    """

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=10)
    """Total dispatches permitted for one request, including the first.

    Two means one retry. Bounded because CONSTITUTION §6.1's Conservative
    Compliance Mode exists to stop traffic looking automated; an unbounded retry
    against a failing provider is precisely that pattern.
    """

    attempt_budget_seconds: float = Field(default=10.0, gt=0)
    """Wall-clock ceiling for all attempts of one request.

    A second bound alongside the attempt count, because the two fail differently.
    An attempt budget alone lets a few very slow attempts run long past any
    sensible client timeout.
    """

    backoff_seconds: float = Field(default=0.2, ge=0)
    """Delay before retrying a *transient fault*.

    Applied only to retryable faults -- a 5xx or a timeout. **Never applied to a
    rate limit**: retrying into one is prohibited outright rather than spaced,
    and the cooldown duration governs when capacity returns. The delay here is a
    courtesy to an upstream that may be recovering, not a compliance mechanism.
    """


class Config(BaseSettings):
    """Validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Unrelated keys must not break startup: a developer's .env routinely
        # holds variables this service does not read. Unknown keys in
        # config.yaml are rejected by YamlConfigSource instead, with a message
        # that names the offending key.
        extra="ignore",
    )

    models: list[ModelConfig] = Field(min_length=1)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def key_slots(self) -> dict[tuple[str, str], list[str]]:
        """Declared aliases for every model, keyed by ``(provider, provider_id)``.

        Keyed on the provider-native model id rather than the external one,
        because that is what the dispatch path resolves to.
        """
        return {
            (entry.provider, entry.provider_id): list(entry.key_aliases) for entry in self.models
        }

    def resolve_key_material(self) -> dict[tuple[str, str], dict[str, SecretStr]]:
        """Resolve every declared alias to its key material from the environment.

        Called at startup so a missing credential aborts the process rather than
        surfacing as a 401 on the first request.

        **Precedence matches the rest of the configuration** (ADR-001): process
        environment first, then an uncommitted ``.env``. ``.env`` is consulted
        here explicitly because the derived names ``<PROVIDER>_KEY_<ALIAS>`` are
        not statically declared pydantic fields, so ``pydantic-settings`` cannot
        load them. Without it a developer's usual local-secret store would
        silently stop working for keys.

        **An environment variable matching the convention but not declared is
        ignored.** Otherwise a stray ``GOOGLE_KEY_SOMETHING`` in a developer's
        shell would silently join the pool, and CONSTITUTION §6.1's cap would be
        unenforceable against the environment it is meant to constrain.

        Returns:
            Key material keyed by ``(provider, provider_id)`` then alias.

        Raises:
            ConfigError: If a declared alias has no corresponding environment
                variable. The message names the **alias and variable only** —
                never a value, which does not exist yet, and never the value of
                any other credential.
        """
        dotenv_values = _read_dotenv_values()
        resolved: dict[tuple[str, str], dict[str, SecretStr]] = {}
        for entry in self.models:
            slots: dict[str, SecretStr] = {}
            for alias in entry.key_aliases:
                variable = key_env_var(entry.provider, alias)
                material = os.environ.get(variable) or dotenv_values.get(variable)
                if not material:
                    raise ConfigError(
                        f"Model '{entry.id}' declares key alias '{alias}', but no "
                        f"environment variable {variable} is set. Set it to the key "
                        "material for this slot. Secrets are read from the environment "
                        "only and must never be placed in config.yaml (CONSTITUTION 9.2)."
                    )
                slots[alias] = SecretStr(material)
            resolved[(entry.provider, entry.provider_id)] = slots
        return resolved

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the sources so environment variables outrank ``config.yaml``."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls),
            file_secret_settings,
        )


class YamlConfigSource(PydanticBaseSettingsSource):
    """Read ``config.yaml`` into a settings source, rejecting secrets."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._path = _resolve_config_path()

    @property
    def path(self) -> Path:
        """The file this source reads."""
        return self._path

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Unused: this source supplies a whole mapping, not per-field values."""
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        data = self._read_yaml()
        _reject_secret_fields(data, self._path)
        self._reject_unknown_keys(data)
        return data

    def _read_yaml(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Unable to read configuration file '{self._path}': {exc}") from exc

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"Configuration file '{self._path}' is not valid YAML: {exc}"
            ) from exc

        if data is None:
            raise ConfigError(f"Configuration file '{self._path}' is empty.")
        if not isinstance(data, dict):
            raise ConfigError(
                f"Configuration file '{self._path}' must contain a mapping at the top level, "
                f"found {type(data).__name__}."
            )
        return data

    def _reject_unknown_keys(self, data: dict[str, Any]) -> None:
        """Reject unknown top-level keys so a typo fails fast instead of being ignored."""
        known = set(self.settings_cls.model_fields)
        unknown = sorted(key for key in data if key not in known)
        if unknown:
            expected = ", ".join(sorted(known))
            raise ConfigError(
                f"Configuration file '{self._path}' contains unknown key(s): "
                f"{', '.join(unknown)}. Expected keys: {expected}."
            )


def _reject_secret_fields(value: Any, path: Path) -> None:
    """Reject secret-named keys and ``${VAR}`` references anywhere in the document."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_YAML_KEYS:
                raise ConfigError(
                    f"Configuration file '{path}' must not define '{key}'. Secrets are read "
                    "from environment variables only and must never be committed to "
                    "config.yaml (spec 6, CONSTITUTION 9.2)."
                )
            _reject_secret_fields(item, path)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item, path)
    elif isinstance(value, str) and _PLACEHOLDER in value:
        raise ConfigError(
            f"Configuration file '{path}' contains an environment variable reference "
            f"('{value}'). References are not supported: read secrets from environment "
            "variables instead (spec 6)."
        )


def _read_dotenv_values() -> dict[str, str]:
    """Read ``.env`` as a fallback source for derived key variables.

    A missing or unparseable ``.env`` yields an empty mapping rather than an
    error: it is an uncommitted convenience file, and its absence must not stop a
    deployment that supplies everything through the process environment.
    """
    try:
        return {name: value for name, value in dotenv_values(DOTENV_FILENAME).items() if value}
    except OSError:
        return {}


def _resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve the configuration file location.

    Precedence: explicit argument, then ``ROUTER_CONFIG_PATH``, then
    ``./config.yaml``.
    """
    if path is not None:
        return Path(path)
    override = _config_path_override.get()
    if override is not None:
        return override
    from_env = os.environ.get(CONFIG_PATH_ENV_VAR)
    if from_env:
        return Path(from_env)
    return Path(DEFAULT_CONFIG_FILENAME)


def load_config(path: str | Path | None = None, *, resolve_keys: bool = True) -> Config:
    """Load and validate configuration, or raise :class:`ConfigError`.

    Args:
        path: Optional explicit configuration file location.
        resolve_keys: Whether to require a key for every declared alias.
            Startup passes ``True`` so a missing credential aborts the process
            rather than surfacing as a 401 on the first request. Tests that are
            about the YAML schema rather than credentials pass ``False``.

    Raises:
        ConfigError: If the file is missing or unreadable, is not valid YAML,
            contains a secret, contains an unknown key, fails validation, or --
            when ``resolve_keys`` -- a declared alias has no key material.
    """
    resolved = _resolve_config_path(path)
    token = _config_path_override.set(resolved)
    try:
        config = Config()
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    finally:
        _config_path_override.reset(token)

    if resolve_keys:
        config.resolve_key_material()
    return config


def _format_validation_error(exc: ValidationError) -> str:
    """Render a validation failure for a human operator.

    Only the location and the message are reported. Pydantic's ``input`` value
    is deliberately omitted so that a malformed secret can never be echoed into
    logs or a crash message.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        problems.append(f"  - {location}: {error['msg']}")
    detail = "\n".join(problems)
    return f"Invalid router configuration:\n{detail}"
