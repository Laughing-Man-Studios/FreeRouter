"""Programmatic Alembic migration runner.

``alembic`` is applied at startup, before the app accepts traffic, as spec 8
requires. The database URL is passed in programmatically rather than read from
``alembic.ini`` so the migration target is the same path the engine opens and
cannot silently drift between the two.

Running from code rather than a subprocess keeps failures inside the existing
fail-fast path: a migration error raises :class:`DatabaseInitializationError`
and aborts startup, which the lifespan already logs and handles.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from free_router.db.engine import DEFAULT_DATABASE_PATH, DatabaseInitializationError

__all__ = ["MIGRATIONS_DIR", "alembic_config", "run_migrations"]

MIGRATIONS_DIR = Path(__file__).parent
"""Directory holding ``env.py`` and the revision scripts."""


def alembic_config(database_path: str | Path | None = None) -> Config:
    """Build an Alembic config pointed at the router's migrations.

    Args:
        database_path: Target database file. Defaults to
            ``data/router.db``.

    Returns:
        A configured :class:`alembic.config.Config`.
    """
    resolved = Path(database_path) if database_path is not None else DEFAULT_DATABASE_PATH
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # A synchronous URL: migrations run over pysqlite while the app uses
    # aiosqlite. Both drivers open the same file.
    config.set_main_option("sqlalchemy.url", f"sqlite:///{resolved}")
    return config


def run_migrations(database_path: str | Path | None = None) -> None:
    """Upgrade the database to the latest revision.

    Safe to call on an already-migrated database; Alembic records applied
    revisions in ``alembic_version``.

    A database written before migrations existed has tables but no
    ``alembic_version`` table, so a plain upgrade would fail trying to create
    tables that are already there. Such a database is *stamped* at head
    instead: its schema is, by construction, the initial revision, since that
    is the only schema the pre-migration code could produce. Genuine upgrades
    from a later revision are unaffected.

    Args:
        database_path: Target database file. Defaults to ``data/router.db``.

    Raises:
        DatabaseInitializationError: If migrations cannot be applied.
    """
    config = alembic_config(database_path)
    try:
        if _needs_adoption(database_path):
            command.stamp(config, "head")
            return
        command.upgrade(config, "head")
    except Exception as exc:
        raise DatabaseInitializationError(f"Database migrations failed: {exc}") from exc


def _needs_adoption(database_path: str | Path | None) -> bool:
    """True when the database has user tables but no Alembic version table.

    That combination means the file predates Alembic and must be stamped
    rather than upgraded.
    """
    resolved = Path(database_path) if database_path is not None else DEFAULT_DATABASE_PATH
    if not resolved.exists():
        return False

    connection = sqlite3.connect(resolved)
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table';")
        }
    except sqlite3.Error:
        return False
    finally:
        connection.close()

    if "alembic_version" in names:
        return False
    # alembic_version aside, only tables the router owns count.
    return bool(names - {"sqlite_sequence"})


def current_revision(database_path: str | Path | None = None) -> str | None:
    """Return the revision currently applied to the database, if any.

    Exposed so tests can assert convergence between a fresh and an existing
    database without shelling out to the Alembic CLI.

    Returns:
        The revision identifier, or ``None`` for an unmigrated database.

    Raises:
        DatabaseInitializationError: If the version cannot be read.
    """
    from sqlalchemy import create_engine

    resolved = Path(database_path) if database_path is not None else DEFAULT_DATABASE_PATH
    engine = create_engine(f"sqlite:///{resolved}", future=True)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:
        raise DatabaseInitializationError(f"Unable to read schema revision: {exc}") from exc
    finally:
        engine.dispose()


def head_revision() -> str | None:
    """Return the newest revision defined in the migration scripts."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()
