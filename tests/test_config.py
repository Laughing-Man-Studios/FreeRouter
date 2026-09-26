"""Configuration loading tests.

Covers spec Scenario 2 (missing API key) and Scenario 3 (invalid schema), the
environment-over-``config.yaml`` precedence required by ADR-001, and the refusal
to keep secrets in version-controlled configuration.
"""

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from free_router.core.config import CONFIG_PATH_ENV_VAR, ConfigError, load_config

pytestmark = pytest.mark.usefixtures("workdir")

_SERVER_SCRIPT = (
    "import sys, uvicorn; "
    "uvicorn.run('free_router.api.main:app', port=int(sys.argv[1]), log_level='warning')"
)


def _free_port() -> int:
    """Reserve an unused TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_env(config_path: Path, api_key: str | None) -> dict[str, str]:
    """Environment for a server subprocess, isolated from the developer's shell."""
    env = dict(os.environ)
    for name in ("GEMINI_API_KEY", "LOG_LEVEL"):
        env.pop(name, None)
    env[CONFIG_PATH_ENV_VAR] = str(config_path)
    env["PYTHONUNBUFFERED"] = "1"
    if api_key is not None:
        env["GEMINI_API_KEY"] = api_key
    return env


@contextlib.contextmanager
def _server(config_path: Path, api_key: str | None) -> Iterator[tuple[subprocess.Popen[str], int]]:
    """Run the real application in a subprocess, yielding it and its port."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVER_SCRIPT, str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_server_env(config_path, api_key),
        cwd=config_path.parent,
    )
    try:
        yield process, port
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()


def _wait_for_http(port: int, timeout: float = 30.0) -> httpx.Response:
    """Poll an endpoint until the server answers, then return the response."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/docs", timeout=2.0)
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"server did not accept traffic within {timeout}s: {last_error}")


def test_loads_minimal_config_yaml(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """The shipped configuration parses into the expected model mapping."""
    write_config()
    key = set_api_key()

    config = load_config()

    assert config.gemini_api_key.get_secret_value() == key
    assert len(config.models) == 1
    model = config.models[0]
    assert model.id == "google/gemini-3.5-flash-lite"
    assert model.provider == "google"
    assert model.provider_id == "gemini-3.5-flash-lite"


def test_env_var_overrides_yaml_value(
    write_config: Callable[..., Path],
    minimal_config: str,
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
):
    """ADR-001: environment variables strictly override config.yaml values."""
    write_config(f"{minimal_config}log_level: DEBUG\n")
    set_api_key()
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    assert load_config().log_level == "WARNING"


def test_yaml_value_used_when_env_absent(
    write_config: Callable[..., Path],
    minimal_config: str,
    set_api_key: Callable[..., str],
):
    """A non-secret value present only in config.yaml is honoured."""
    write_config(f"{minimal_config}log_level: DEBUG\n")
    set_api_key()

    assert load_config().log_level == "DEBUG"


def test_dotenv_value_used_when_env_absent(
    workdir: Path,
    write_config: Callable[..., Path],
):
    """A local .env supplies the key, matching the .env.example workflow."""
    write_config()
    (workdir / ".env").write_text("GEMINI_API_KEY=key-from-dotenv\n", encoding="utf-8")

    assert load_config().gemini_api_key.get_secret_value() == "key-from-dotenv"


def test_unrelated_dotenv_keys_are_ignored(
    workdir: Path,
    write_config: Callable[..., Path],
):
    """An unrelated variable in .env must not break startup."""
    write_config()
    (workdir / ".env").write_text("GEMINI_API_KEY=k\nUNRELATED_VARIABLE=hello\n", encoding="utf-8")

    assert load_config().gemini_api_key.get_secret_value() == "k"


def test_missing_api_key_fails_fast(write_config: Callable[..., Path]):
    """Scenario 2: the router refuses to start without GEMINI_API_KEY."""
    write_config()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "gemini_api_key" in message
    assert "Field required" in message
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_invalid_schema_fails_fast(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """Scenario 3: a missing required field is reported by name."""
    write_config('models:\n  - id: "google/gemini-3.5-flash-lite"\n    provider: "google"\n')
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "models.0.provider_id" in str(excinfo.value)


def test_invalid_schema_wrong_type_fails_fast(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """Scenario 3: a wrongly typed value is rejected."""
    write_config('models: "not-a-list"\n')
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "models" in str(excinfo.value)


def test_nested_unknown_field_rejected(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """A typo inside a model entry is rejected rather than ignored."""
    write_config(
        'models:\n  - id: "a"\n    provider: "google"\n    provider_id: "b"\n    typo: 1\n'
    )
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "typo" in str(excinfo.value)


def test_unknown_top_level_key_rejected(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """A typo at the top level is reported alongside the expected keys."""
    write_config('modelz:\n  - id: "a"\n')
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "modelz" in message
    assert "models" in message


def test_yaml_placeholder_rejected(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """config.yaml may not reference a secret via ${VAR} (spec 6)."""
    write_config("key: ${GEMINI_API_KEY}\n")
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_literal_secret_in_yaml_rejected(write_config: Callable[..., Path]):
    """A key stored directly in config.yaml is refused (CONSTITUTION 9.2)."""
    write_config(
        'models:\n  - id: "a"\n    provider: "google"\n    provider_id: "b"\n'
        "gemini_api_key: sk-leaked\n"
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "gemini_api_key" in message
    assert "environment variables" in message


def test_missing_config_file_fails_fast():
    """A missing configuration file is an error, never a silent default."""
    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "config.yaml" in str(excinfo.value)


def test_empty_config_file_fails_fast(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    write_config("")
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "empty" in str(excinfo.value)


def test_malformed_yaml_fails_fast(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    write_config("models: [oops\n")
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "not valid YAML" in str(excinfo.value)


def test_non_mapping_config_rejected(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    write_config("- just\n- a\n- list\n")
    set_api_key()

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    assert "mapping" in str(excinfo.value)


def test_config_path_env_var_respected(
    workdir: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
):
    """ROUTER_CONFIG_PATH selects the configuration file."""
    write_config()
    set_api_key()
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(workdir / "config.yaml"))

    assert load_config().models[0].id == "google/gemini-3.5-flash-lite"


def test_explicit_path_overrides_env_var(
    workdir: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """An explicit argument outranks ROUTER_CONFIG_PATH."""
    write_config()
    elsewhere = tmp_path / "other.yaml"
    elsewhere.write_text(
        'models:\n  - id: "google/other"\n    provider: "google"\n    provider_id: "other"\n',
        encoding="utf-8",
    )
    set_api_key()
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(workdir / "config.yaml"))

    assert load_config(elsewhere).models[0].id == "google/other"
    assert load_config().models[0].id == "google/gemini-3.5-flash-lite"


def test_secret_absent_from_repr(
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
):
    """The key must not surface if the configuration object is ever logged."""
    write_config()
    set_api_key("super-secret-value")

    config = load_config()

    assert "super-secret-value" not in repr(config)
    assert "super-secret-value" not in str(config)
    assert "super-secret-value" not in config.model_dump_json()


def test_process_exits_nonzero_without_api_key(write_config: Callable[..., Path]):
    """Scenario 2 end to end: the process dies before accepting traffic."""
    config_path = write_config()

    with _server(config_path, api_key=None) as (process, _port):
        # A router that starts anyway would keep running, so a timeout here is
        # itself the failure signal: the process must exit before serving.
        stdout, _ = process.communicate(timeout=20)

    assert process.returncode != 0
    assert '"startup_failed"' in stdout
    assert "gemini_api_key" in stdout


def test_process_starts_and_echoes_request_id(write_config: Callable[..., Path]):
    """The happy path: the app serves traffic and logs request metadata."""
    config_path = write_config()

    with _server(config_path, api_key="dummy-key-for-startup-test") as (_process, port):
        assert _wait_for_http(port).status_code == 200

        follow_up = httpx.get(
            f"http://127.0.0.1:{port}/docs",
            headers={"X-Request-ID": "req-under-test"},
            timeout=5.0,
        )

    assert follow_up.headers["X-Request-ID"] == "req-under-test"
