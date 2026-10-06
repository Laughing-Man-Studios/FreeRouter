"""Database initialisation and WAL enforcement."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from free_router.db.engine import (
    DatabaseInitializationError,
    _enforce_wal,
    init_db_engine,
    journal_mode_of,
)
from free_router.db.schema import models_table, request_logs_table


async def test_init_creates_engine_and_tables(tmp_path: Path) -> None:
    """Initialisation creates the file and both M0 tables."""
    db_path = tmp_path / "data" / "router.db"

    engine = await init_db_engine(db_path)
    try:
        assert engine.dialect.name == "sqlite"
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table';"
            )
            tables = set(result.scalars())
    finally:
        await engine.dispose()

    assert {models_table.name, request_logs_table.name} <= tables


async def test_engine_uses_the_async_driver(tmp_path: Path) -> None:
    """The engine speaks aiosqlite, not the blocking pysqlite driver.

    CONSTITUTION 3.1 and spec 8 both require an async driver for the MVP, so
    this asserts the driver explicitly rather than trusting the URL.
    """
    engine = await init_db_engine(tmp_path / "router.db")
    try:
        # AsyncEngine delegates to a sync engine, whose dialect reports the
        # base "sqlite" name; the async driver is on the URL.
        assert engine.sync_engine.dialect.driver == "aiosqlite"
        assert "sqlite+aiosqlite://" in str(engine.url)
    finally:
        await engine.dispose()


async def test_wal_mode_is_enforced(tmp_path: Path) -> None:
    """The database really is in WAL mode after initialisation."""
    db_path = tmp_path / "router.db"

    engine = await init_db_engine(db_path)
    await engine.dispose()

    # Read through a plain sqlite3 connection, so this verifies the file itself
    # rather than restating what the engine believes about it.
    assert journal_mode_of(db_path).lower() == "wal"


async def test_startup_fails_when_wal_cannot_be_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4: a database that cannot enter WAL mode aborts startup.

    SQLite does not raise when ``PRAGMA journal_mode=WAL`` cannot be honoured
    (an unwritable data directory, or a filesystem without shared-memory
    support): it keeps the previous journal mode and returns it. The pragma set
    is therefore neutralised here to reproduce that silent failure, which
    ``_enforce_wal`` must catch rather than let the app start degraded.
    """
    db_path = tmp_path / "router.db"
    monkeypatch.setattr(
        "free_router.db.engine.PRAGMAS",
        ("PRAGMA journal_mode=DELETE;", "PRAGMA busy_timeout=5000;"),
    )

    with pytest.raises(DatabaseInitializationError, match="WAL"):
        await init_db_engine(db_path)

    assert journal_mode_of(db_path).lower() == "delete"


async def test_enforce_wal_accepts_a_wal_database(tmp_path: Path) -> None:
    """The enforcement check passes for a correctly initialised database."""
    engine = await init_db_engine(tmp_path / "router.db")
    try:
        async with engine.connect() as connection:
            await _enforce_wal(connection)  # must not raise
    finally:
        await engine.dispose()


async def test_engine_is_usable_from_async_code(tmp_path: Path) -> None:
    """A write and read round trip succeeds without blocking the loop.

    The point of the async driver is that request handlers can touch the
    database, so the conversion is only real if that path actually works.
    """
    engine = await init_db_engine(tmp_path / "router.db")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO models (id, provider, provider_id) VALUES ('google/x', 'google', 'x');"
            )
        async with engine.connect() as connection:
            rows = (
                (await connection.exec_driver_sql("SELECT provider FROM models;")).scalars().all()
            )
    finally:
        await engine.dispose()

    assert rows == ["google"]


async def test_dispose_is_a_coroutine(tmp_path: Path) -> None:
    """AsyncEngine.dispose must be awaited; a bare call would leak the pool."""
    engine: AsyncEngine = await init_db_engine(tmp_path / "router.db")

    await engine.dispose()


def test_request_logs_has_no_content_columns() -> None:
    """Prompt and completion text must not be storable (spec 9, CONSTITUTION 9.1)."""
    column_names = {column.name for column in request_logs_table.columns}

    assert "prompt" not in column_names
    assert "completion" not in column_names
    assert "content" not in column_names
    assert {"request_id", "model", "http_status", "latency_ms"} <= column_names
