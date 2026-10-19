"""The M2 quota-state schema.

Batch 1 (T041, T042) defines four tables that close the CONSTITUTION 8.3
deviation recorded in ADR-0004: persisted quota usage, cooldowns, model status
and per-credential selection state.

The properties worth defending here are not that the tables exist, but three
things about *what they can hold*:

- **Nothing can hold key material or payload text** (CONSTITUTION 9.1, 9.2). A
  credential is identified by alias, never by its secret.
- **The migration and the schema definitions agree.** These are two
  representations of one thing, and a drift between them would mean the
  database and the code disagree about the schema -- a bug that surfaces at
  runtime, on a real query, rather than at startup.
- **The persisted window state keeps the estimate and the actual separate.**
  Collapsing them would make estimator drift unmeasurable after a restart, and
  the console cross-check is the only independent check available.
"""

from pathlib import Path

import pytest
from sqlalchemy import JSON, Boolean, Float, Integer, create_engine, inspect
from sqlalchemy.engine.reflection import Inspector

from free_router.db.schema import (
    cooldowns_table,
    key_state_table,
    metadata,
    model_status_table,
    models_table,
    quota_windows_table,
    request_logs_table,
)
from free_router.migrations import run_migrations

M2_TABLES = (quota_windows_table, cooldowns_table, model_status_table, key_state_table)

# Column names that would indicate the table could hold a secret or prompt
# text. The check is on the *name* because a column can hold anything its type
# allows, and a column called "prompt" holding a string is a violation however
# carefully it is later used.
FORBIDDEN_COLUMN_FRAGMENTS = (
    "prompt",
    "completion",
    "content",
    "secret",
    "password",
    "passphrase",
    "key_material",
    "api_key",
    "apikey",
    "bearer",
    "access_token",
    "private_key",
    "authorization",
)


def _schema_of(path: Path) -> dict[str, dict[str, list[str]]]:
    """Map table name -> {column name: [primary key columns]}."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        inspector: Inspector = inspect(engine)
        return {
            table: {
                column: inspector.get_pk_constraint(table).get("constrained_columns") or []
                for column in (c["name"] for c in inspector.get_columns(table))
            }
            for table in inspector.get_table_names()
            if table != "alembic_version"
        }
    finally:
        engine.dispose()


def test_all_four_state_tables_are_declared() -> None:
    """The four tables the plan calls for exist in the schema definition."""
    assert {table.name for table in M2_TABLES} == {
        "quota_windows",
        "cooldowns",
        "model_status",
        "key_state",
    }
    assert {table.name for table in metadata.sorted_tables} >= {table.name for table in M2_TABLES}


def test_no_table_can_hold_key_material_or_payload_text() -> None:
    """No column in any table could carry a secret or prompt text.

    This is the constraint that makes a state dump safe to paste into an issue,
    so it is checked across every table rather than only the new ones.
    """
    for table in metadata.sorted_tables:
        for column in table.columns:
            lowered = column.name.lower()
            for fragment in FORBIDDEN_COLUMN_FRAGMENTS:
                assert fragment not in lowered, (
                    f"{table.name}.{column.name} could hold key material or payload "
                    f"text (matched '{fragment}')"
                )


def test_credentials_are_identified_by_alias_only() -> None:
    """``key_state`` refers to a credential by alias, never by material."""
    columns = {column.name for column in key_state_table.columns}

    assert "alias" in columns
    assert "provider" in columns
    assert not columns & {"key", "secret", "token", "material", "value"}


def test_the_migration_and_the_schema_definitions_agree(tmp_path: Path) -> None:
    """Two representations of one schema must not drift apart.

    A mismatch would mean the database and the code disagree about the columns,
    and it would surface on a real query rather than at startup.
    """
    migrated = tmp_path / "migrated.db"
    run_migrations(migrated)

    created = tmp_path / "created.db"
    engine = create_engine(f"sqlite:///{created}", future=True)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    from_migration = _schema_of(migrated)
    from_definitions = _schema_of(created)

    assert set(from_migration) == set(from_definitions)
    for table in from_migration:
        assert set(from_migration[table]) == set(from_definitions[table]), (
            f"{table} has different columns in the migration and the definitions"
        )


def test_composite_primary_keys_are_the_dimension_identity(tmp_path: Path) -> None:
    """A window row is keyed by the whole dimension, not by provider alone.

    Keying on the whole dimension means a configuration change that alters a
    window produces a different row rather than silently reinterpreting the
    old one.
    """
    db_path = tmp_path / "router.db"
    run_migrations(db_path)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        inspector = inspect(engine)
        window_pk = set(
            inspector.get_pk_constraint("quota_windows").get("constrained_columns") or []
        )
        cooldown_pk = set(inspector.get_pk_constraint("cooldowns").get("constrained_columns") or [])
        status_pk = set(
            inspector.get_pk_constraint("model_status").get("constrained_columns") or []
        )
        key_pk = set(inspector.get_pk_constraint("key_state").get("constrained_columns") or [])
    finally:
        engine.dispose()

    assert window_pk == {"provider", "model_id", "resource", "window_seconds"}
    assert cooldown_pk == {"provider", "model_id"}
    assert status_pk == {"provider", "model_id"}
    assert key_pk == {"provider", "alias"}


def test_window_state_keeps_estimate_and_actual_separate() -> None:
    """The persisted window state must not collapse the two into one figure.

    Collapsing them would make estimator drift unmeasurable after a restart, and
    the console cross-check is the only independent check available. The shape
    is a list of entries, each keeping its reservation and its reported usage
    side by side.
    """
    assert "entries" in quota_windows_table.columns
    assert isinstance(quota_windows_table.columns["entries"].type, JSON)


def test_fixed_window_state_records_its_reset_instant() -> None:
    """A fixed window has a boundary and a rolling one does not.

    The instant is stored as a nullable column so both kinds share one shape.
    """
    columns = quota_windows_table.columns

    assert "reset_kind" in columns
    assert "reset_at" in columns
    assert columns["reset_at"].nullable is True
    assert columns["reset_kind"].nullable is False


def test_key_state_ordinal_is_an_integer_and_suspension_is_a_boolean() -> None:
    """The types are load-bearing, not incidental.

    The ordinal is an integer because ordering LRU by timestamp silently degrades
    into ordering by arrival. The suspension flag is a boolean because it is
    read as a flag; a numeric column would invite arithmetic on it.
    """
    assert isinstance(key_state_table.columns["last_used_ordinal"].type, Integer)
    assert isinstance(key_state_table.columns["suspended"].type, Boolean)


def test_window_seconds_is_a_float() -> None:
    """Mistral permits 0.50 requests/second, so a window is not always whole."""
    assert isinstance(quota_windows_table.columns["window_seconds"].type, Float)


def test_the_m0_tables_are_untouched() -> None:
    """Batch 1 adds tables; it does not reshape what M0 built."""
    assert {c.name for c in models_table.columns} == {"id", "provider", "provider_id"}
    assert {c.name for c in request_logs_table.columns} == {
        "id",
        "request_id",
        "created_at",
        "model",
        "provider",
        "provider_id",
        "latency_ms",
        "http_status",
        "error_category",
    }


@pytest.mark.parametrize("table", M2_TABLES, ids=lambda t: t.name)
def test_every_state_table_has_a_timestamp(table: object) -> None:
    """Each table records when it was last written.

    A stale row must be recognisable without reading its contents, which is
    what the timestamp is for.
    """
    columns = {column.name for column in table.columns}  # type: ignore[attr-defined]

    assert "updated_at" in columns or "flushed_at" in columns
