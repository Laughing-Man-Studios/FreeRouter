"""Alembic environment for the router's embedded SQLite database.

Migrations run against a **synchronous** pysqlite connection, while the
application itself uses ``aiosqlite`` (CONSTITUTION 3.1). That is the
sync-to-async handoff described in ``plan.md``: migrations are a startup-only
concern that must complete before the app accepts traffic, so running them
without an async migration toolchain is simpler and has no runtime cost. The
database file is shared between both drivers, so both see the same schema.

The URL is passed in programmatically by :func:`free_router.db.migrations.run_migrations`
rather than read from ``alembic.ini``, so the migration target is the exact
same path the engine will open and cannot drift between the two.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from free_router.db.schema import metadata

# Alembic's config object, typed as Any because its stubs are untyped.
config = context.config

target_metadata = metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live synchronous connection."""
    section = config.get_section(config.config_ini_section, {})
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            # SQLite cannot ALTER most things in place, so migrations are
            # rendered as batch operations that recreate the table.
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
