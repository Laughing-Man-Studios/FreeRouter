"""Alembic migration behaviour.

The property that matters is convergence: a database built fresh and one
upgraded from an older revision must end up at the same schema. That is what
``metadata.create_all`` cannot guarantee, since it only ever adds.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from free_router.db.engine import DatabaseInitializationError, init_db_engine
from free_router.migrations import (
    alembic_config,
    current_revision,
    head_revision,
    run_migrations,
)


def test_migrations_bring_a_fresh_database_to_head(tmp_path: Path) -> None:
    """A new database is created at the latest revision."""
    db_path = tmp_path / "router.db"

    run_migrations(db_path)

    assert current_revision(db_path) == head_revision()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Running twice is a no-op, not an error."""
    db_path = tmp_path / "router.db"

    run_migrations(db_path)
    run_migrations(db_path)

    assert current_revision(db_path) == head_revision()


def test_migrations_create_both_tables(tmp_path: Path) -> None:
    """The initial revision creates models and request_logs."""
    db_path = tmp_path / "router.db"
    run_migrations(db_path)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {"models", "request_logs"} <= tables


def test_unmigrated_database_reports_no_revision(tmp_path: Path) -> None:
    """A database with no alembic_version reports None, not an error."""
    from sqlalchemy import text

    db_path = tmp_path / "router.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE placeholder (id INTEGER);"))
    finally:
        engine.dispose()

    assert current_revision(db_path) is None


def test_fresh_and_existing_database_converge(tmp_path: Path) -> None:
    """The property create_all could not provide: both paths agree.

    This is the case that matters for a Docker volume surviving an upgrade: a
    database that already exists is upgraded in place to the same revision and
    schema a fresh one gets.

    The pre-existing database is seeded with a ``create_all``-era table, which
    is what a volume written before migrations existed would actually contain.
    """
    from sqlalchemy import text

    fresh = tmp_path / "fresh.db"
    existing = tmp_path / "existing.db"

    legacy_models = (
        "CREATE TABLE models ("
        "id VARCHAR NOT NULL, "
        "provider VARCHAR NOT NULL, "
        "provider_id VARCHAR NOT NULL, "
        "PRIMARY KEY (id))"
    )
    legacy_request_logs = (
        "CREATE TABLE request_logs ("
        "id INTEGER NOT NULL PRIMARY KEY, "
        "request_id VARCHAR NOT NULL, "
        "created_at VARCHAR NOT NULL, "
        "model VARCHAR NOT NULL, "
        "provider VARCHAR NOT NULL, "
        "provider_id VARCHAR, "
        "latency_ms FLOAT, "
        "http_status INTEGER, "
        "error_category VARCHAR)"
    )
    legacy_index = "CREATE INDEX ix_request_logs_request_id ON request_logs (request_id)"

    engine = create_engine(f"sqlite:///{existing}", future=True)
    try:
        with engine.begin() as connection:
            for statement in (legacy_models, legacy_request_logs, legacy_index):
                connection.execute(text(statement))
    finally:
        engine.dispose()

    run_migrations(fresh)
    run_migrations(existing)

    assert current_revision(fresh) == current_revision(existing)
    assert current_revision(existing) == head_revision()

    fresh_engine = create_engine(f"sqlite:///{fresh}", future=True)
    existing_engine = create_engine(f"sqlite:///{existing}", future=True)
    try:
        for table in ("models", "request_logs"):
            fresh_columns = {c["name"] for c in inspect(fresh_engine).get_columns(table)}
            existing_columns = {c["name"] for c in inspect(existing_engine).get_columns(table)}
            assert fresh_columns == existing_columns, table
    finally:
        fresh_engine.dispose()
        existing_engine.dispose()


def test_alembic_config_targets_the_requested_database(tmp_path: Path) -> None:
    """The URL is set programmatically, so the CLI placeholder is never used."""
    config = alembic_config(tmp_path / "somewhere.db")

    assert config.get_main_option("sqlalchemy.url") == f"sqlite:///{tmp_path / 'somewhere.db'}"


def test_migration_failure_raises_database_initialization_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken migration aborts startup instead of serving a wrong schema."""

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("migration exploded")

    monkeypatch.setattr("free_router.migrations.command.upgrade", _explode)

    with pytest.raises(DatabaseInitializationError, match="migrations failed"):
        run_migrations(tmp_path / "router.db")


async def test_init_db_engine_applies_migrations(tmp_path: Path) -> None:
    """Engine initialisation migrates first, so tables exist afterwards."""
    db_path = tmp_path / "router.db"

    engine = await init_db_engine(db_path)
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table';"
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert {"models", "request_logs"} <= tables
    assert current_revision(db_path) == head_revision()
