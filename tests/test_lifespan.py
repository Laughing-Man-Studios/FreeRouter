"""ASGI lifespan: singleton creation, cleanup, and fail-fast behaviour."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from free_router.api.main import create_app
from free_router.core.config import ConfigError
from free_router.db.engine import DatabaseInitializationError


@pytest.fixture
def startup_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[Path], None]:
    """Point the database at a throwaway file and clear lifecycle env vars."""

    def _configure(db_path: Path) -> None:
        monkeypatch.setenv("ROUTER_DB_PATH", str(db_path))

    return _configure


def test_lifespan_attaches_singletons(ready_app: Callable[[], FastAPI]) -> None:
    """Config, database engine, and HTTP client are all present after startup."""
    with TestClient(ready_app()) as client:
        state = client.app.state
        assert state.config.models[0].id == "google/gemini-3.5-flash-lite"
        assert isinstance(state.http_client, httpx.AsyncClient)
        assert state.db_engine is not None


def test_lifespan_closes_http_client_on_shutdown(ready_app: Callable[[], FastAPI]) -> None:
    """The shared client is closed on shutdown rather than leaked."""
    with TestClient(ready_app()) as client:
        http_client: httpx.AsyncClient = client.app.state.http_client

    assert http_client.is_closed


def test_startup_fails_without_api_key(
    startup_env: Callable[[Path], None],
    tmp_path: Path,
    write_config: Callable[..., Path],
) -> None:
    """Scenario 2: a missing GEMINI_API_KEY aborts startup."""
    write_config()
    startup_env(tmp_path / "router.db")

    with pytest.raises(ConfigError), TestClient(create_app()):
        pass


def test_startup_fails_on_invalid_config(
    startup_env: Callable[[Path], None],
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
) -> None:
    """Scenario 3: an invalid config.yaml aborts startup."""
    set_api_key()
    startup_env(tmp_path / "router.db")
    write_config("models: []\n")

    with pytest.raises(ConfigError), TestClient(create_app()):
        pass


def test_startup_fails_when_database_cannot_initialise(
    startup_env: Callable[[Path], None],
    tmp_path: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
) -> None:
    """Scenario 4: a database failure aborts startup before traffic is served."""
    write_config()
    set_api_key()
    # The parent "directory" is a regular file, so the data directory cannot be made.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    startup_env(blocker / "router.db")

    with pytest.raises(DatabaseInitializationError), TestClient(create_app()):
        pass


def test_default_database_path_is_relative_to_cwd(
    workdir: Path,
    write_config: Callable[..., Path],
    set_api_key: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ROUTER_DB_PATH the database lands under ./data."""
    write_config()
    set_api_key()
    monkeypatch.delenv("ROUTER_DB_PATH", raising=False)

    with TestClient(create_app()):
        pass

    assert (workdir / "data" / "router.db").exists()
