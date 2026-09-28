"""Database initialisation and WAL enforcement."""

from pathlib import Path

import pytest

from free_router.db.engine import (
    DatabaseInitializationError,
    _enforce_wal,
    init_db_engine,
    journal_mode_of,
)
from free_router.db.schema import models_table, request_logs_table


def test_init_creates_engine_and_tables(tmp_path: Path) -> None:
    """Initialisation creates the file and both M0 tables."""
    db_path = tmp_path / "data" / "router.db"

    engine = init_db_engine(db_path)
    try:
        assert engine.dialect.name == "sqlite"
        with engine.connect() as connection:
            tables = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).scalars()
            )
    finally:
        engine.dispose()

    assert {models_table.name, request_logs_table.name} <= tables


def test_wal_mode_is_enforced(tmp_path: Path) -> None:
    """The database really is in WAL mode after initialisation."""
    db_path = tmp_path / "router.db"

    engine = init_db_engine(db_path)
    engine.dispose()

    assert journal_mode_of(db_path).lower() == "wal"


def test_startup_fails_when_wal_cannot_be_enabled(
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
        init_db_engine(db_path)

    assert journal_mode_of(db_path).lower() == "delete"


def test_enforce_wal_accepts_a_wal_database(tmp_path: Path) -> None:
    """The enforcement check passes for a correctly initialised database."""
    db_path = tmp_path / "router.db"
    engine = init_db_engine(db_path)
    try:
        _enforce_wal(engine)  # must not raise
    finally:
        engine.dispose()


def test_request_logs_has_no_content_columns() -> None:
    """Prompt and completion text must not be storable (spec 9, CONSTITUTION 9.1)."""
    column_names = {column.name for column in request_logs_table.columns}

    assert "prompt" not in column_names
    assert "completion" not in column_names
    assert "content" not in column_names
    assert {"request_id", "model", "http_status", "latency_ms"} <= column_names
