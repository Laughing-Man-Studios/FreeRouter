"""SQLite engine initialisation and WAL enforcement.

The router runs on an embedded SQLite database with no external service
(CONSTITUTION 5.1). Write-Ahead Logging is mandatory, not an optimisation: the
single Uvicorn process writes request logs while serving traffic, and without
WAL those writes block readers.

Initialisation is a standalone function so it can be tested directly, and so
the lifespan stays a thin orchestrator. WAL enforcement failure raises
:class:`DatabaseInitializationError` rather than degrading quietly -- the
application must refuse to start in that case (spec Scenario 4).

Note that SQLAlchemy's ``aiosqlite`` dialect is deliberately not used here. M0
runs a fixed set of statements once at startup and never queries from the
event loop, so a plain connection is sufficient and keeps the dependency
surface minimal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

__all__ = [
    "DEFAULT_DATABASE_PATH",
    "PRAGMAS",
    "DatabaseInitializationError",
    "init_db_engine",
]

DEFAULT_DATABASE_PATH = Path("data/router.db")
"""Database location relative to the working directory when none is given."""

PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA synchronous=NORMAL;",
)
"""Pragmas applied on initialisation, in order."""


class DatabaseInitializationError(RuntimeError):
    """The database could not be initialised, so the app must not start."""


def init_db_engine(database_path: str | Path | None = None) -> Engine:
    """Create the database, apply pragmas, and return a ready engine.

    The schema is created with ``metadata.create_all`` rather than an Alembic
    revision for M0; the migration toolchain lands with the same batch that
    needs versioned schema changes.

    Args:
        database_path: Database file location. Defaults to
            ``data/router.db``. The parent directory is created if absent.

    Returns:
        A synchronous SQLAlchemy ``Engine`` with WAL confirmed active.

    Raises:
        DatabaseInitializationError: If the file cannot be created, a pragma
            fails, or WAL mode is not active afterwards.
    """
    from free_router.db.schema import metadata

    path = Path(database_path) if database_path is not None else DEFAULT_DATABASE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseInitializationError(
            f"Unable to create database directory '{path.parent}': {exc}"
        ) from exc

    engine = _create_engine(path)
    try:
        _apply_pragmas(engine)
        _enforce_wal(engine)
        metadata.create_all(engine)
    except DatabaseInitializationError:
        engine.dispose()
        raise
    except Exception as exc:
        engine.dispose()
        raise DatabaseInitializationError(f"Unable to initialise database '{path}': {exc}") from exc
    return engine


def _create_engine(path: Path) -> Engine:
    try:
        return create_engine(
            f"sqlite+pysqlite:///{path}",
            future=True,
            connect_args={"check_same_thread": False},
        )
    except Exception as exc:
        raise DatabaseInitializationError(f"Unable to open database '{path}': {exc}") from exc


def _apply_pragmas(engine: Engine) -> None:
    """Run each pragma, raising on the first one that fails.

    ``journal_mode`` is applied first and its result is verified separately by
    :func:`_enforce_wal`; SQLite returns the resulting mode rather than raising
    when WAL is unavailable, so a successful call is not sufficient proof.
    """
    with engine.connect() as connection:
        for statement in PRAGMAS:
            try:
                connection.exec_driver_sql(statement)
            except Exception as exc:
                raise DatabaseInitializationError(
                    f"Failed to apply '{statement.strip()}': {exc}"
                ) from exc
        connection.commit()


def _enforce_wal(engine: Engine) -> None:
    """Verify WAL mode is actually active; raise if it is not (spec Scenario 4)."""
    with engine.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode;").scalar()
    if str(mode).lower() != "wal":
        raise DatabaseInitializationError(
            f"WAL mode is required but the database reports journal_mode='{mode}'. "
            "The application cannot start without Write-Ahead Logging "
            "(spec 8, CONSTITUTION 5.1)."
        )


def journal_mode_of(database_path: str | Path) -> str:
    """Return the live ``journal_mode`` for an existing database file.

    Exposed so tests can assert on the real database rather than trusting the
    engine's return value.

    Raises:
        DatabaseInitializationError: If the mode cannot be read.
    """
    try:
        connection = sqlite3.connect(Path(database_path))
        try:
            cursor = connection.execute("PRAGMA journal_mode;")
            row = cursor.fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseInitializationError(f"Unable to read journal mode: {exc}") from exc
    if row is None:
        raise DatabaseInitializationError("PRAGMA journal_mode returned no result.")
    return str(row[0])
