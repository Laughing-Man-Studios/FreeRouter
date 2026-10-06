"""Alembic migration behaviour.

The property that matters is convergence: a database built fresh and one
upgraded from an older revision must end up at the same schema. That is what
``metadata.create_all`` cannot guarantee, since it only ever adds.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

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


def test_initial_schema_tables_derives_from_the_schema() -> None:
    """The adoption path's expected set follows the schema, not a literal.

    If a table is added to the initial migration, the adoption path must accept
    it too, or a legitimate pre-Alembic database would stop being adopted.
    """
    from free_router.db.schema import metadata
    from free_router.migrations import INITIAL_SCHEMA_TABLES

    assert INITIAL_SCHEMA_TABLES == frozenset(t.name for t in metadata.sorted_tables)


LEGACY_SCHEMA = (
    "CREATE TABLE models (id VARCHAR NOT NULL, provider VARCHAR NOT NULL, "
    "provider_id VARCHAR NOT NULL, PRIMARY KEY (id))",
    "CREATE TABLE request_logs (id INTEGER NOT NULL PRIMARY KEY, "
    "request_id VARCHAR NOT NULL, created_at VARCHAR NOT NULL, model VARCHAR NOT NULL, "
    "provider VARCHAR NOT NULL, provider_id VARCHAR, latency_ms FLOAT, "
    "http_status INTEGER, error_category VARCHAR)",
    "CREATE INDEX ix_request_logs_request_id ON request_logs (request_id)",
)


def _seed(path: Path, *statements: str) -> Path:
    """Create a SQLite file containing the given DDL statements."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()
    return path


def _table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table';")
        }
    finally:
        connection.close()


def test_adoption_stamps_an_exact_legacy_database(tmp_path: Path) -> None:
    """A pre-Alembic database at the initial schema is stamped, not upgraded."""
    db_path = _seed(tmp_path / "router.db", *LEGACY_SCHEMA)

    run_migrations(db_path)

    assert current_revision(db_path) == head_revision()
    assert {"models", "request_logs"} <= _table_names(db_path)


def test_adoption_is_skipped_for_an_unrelated_database(tmp_path: Path) -> None:
    """A database with none of the router's tables must not be stamped.

    Regression test. The adoption path once accepted any database with a table
    and no ``alembic_version``, so pointing ``ROUTER_DB_PATH`` at an unrelated
    SQLite file declared it migrated while ``models`` and ``request_logs`` were
    never created. Nothing in M0 queries them, so that would not have surfaced
    until the first feature that did.
    """
    db_path = _seed(tmp_path / "other.db", "CREATE TABLE unrelated (x INTEGER)")

    run_migrations(db_path)

    # Falls through to upgrade, which creates the missing tables.
    assert current_revision(db_path) == head_revision()
    assert {"models", "request_logs"} <= _table_names(db_path)
    # The pre-existing table is left alone rather than destroyed.
    assert "unrelated" in _table_names(db_path)


def test_partial_schema_fails_loudly_rather_than_being_stamped(tmp_path: Path) -> None:
    """A half-correct schema must fail fast, not be declared migrated.

    With only ``models`` present, neither stamping nor upgrading is correct:
    stamping would leave ``request_logs`` missing while claiming head, and
    upgrading collides on the existing table. Failing at startup with a clear
    error is the safe outcome -- the operator deletes the file and starts over.
    """
    db_path = _seed(tmp_path / "partial.db", LEGACY_SCHEMA[0])

    with pytest.raises(DatabaseInitializationError, match="migrations failed"):
        run_migrations(db_path)

    # Critically, it is NOT recorded as migrated.
    assert current_revision(db_path) is None


def test_adoption_tolerates_unrelated_extra_tables(tmp_path: Path) -> None:
    """A database holding the router's schema plus an unrelated table still adopts.

    Migrations only ever touch tables they own, so an extra table neither
    blocks stamping nor changes the outcome. Requiring an exact table match
    would needlessly fail a developer's database that also holds something else.
    """
    db_path = _seed(
        tmp_path / "router.db", *LEGACY_SCHEMA, "CREATE TABLE some_unrelated_tool (x INTEGER)"
    )

    run_migrations(db_path)

    assert current_revision(db_path) == head_revision()
    names = _table_names(db_path)
    assert {"models", "request_logs"} <= names
    assert "some_unrelated_tool" in names, "an unrelated table must not be dropped"


def test_already_migrated_database_is_not_stamped_again(tmp_path: Path) -> None:
    """A migrated database upgrades normally and is left idempotent."""
    db_path = tmp_path / "router.db"
    run_migrations(db_path)
    run_migrations(db_path)

    assert current_revision(db_path) == head_revision()


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
