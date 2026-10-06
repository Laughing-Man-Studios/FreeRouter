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
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_FILENAME = "config.yaml"
"""Filename looked up in the current working directory when no path is given."""

CONFIG_PATH_ENV_VAR = "ROUTER_CONFIG_PATH"
"""Environment variable naming an explicit ``config.yaml`` location."""

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
    gemini_api_key: SecretStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

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


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate configuration, or raise :class:`ConfigError`.

    Args:
        path: Optional explicit configuration file location.

    Raises:
        ConfigError: If the file is missing or unreadable, is not valid YAML,
            contains a secret, contains an unknown key, or fails validation.
    """
    resolved = _resolve_config_path(path)
    token = _config_path_override.set(resolved)
    try:
        return Config()
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    finally:
        _config_path_override.reset(token)


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
