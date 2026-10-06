"""SQLite engine initialisation and WAL enforcement.

The router runs on an embedded SQLite database with no external service
(CONSTITUTION 5.1). Write-Ahead Logging is mandatory, not an optimisation: the
single Uvicorn process writes request logs while serving traffic, and without
WAL those writes block readers.

Initialisation is a standalone function so it can be tested directly, and so
the lifespan stays a thin orchestrator. WAL enforcement failure raises
:class:`DatabaseInitializationError` rather than degrading quietly -- the
application must refuse to start in that case (spec Scenario 4).

The engine is built on ``aiosqlite``, as CONSTITUTION 3.1 and spec section 8
require for the MVP: a synchronous driver would block the event loop on any
request-path read or write, stalling every other in-flight request.

SQLAlchemy's async layer needs ``greenlet``, which is why the dependency is
declared as ``sqlalchemy[asyncio]`` rather than plain ``sqlalchemy``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

__all__ = [
    "DEFAULT_DATABASE_PATH",
    "PRAGMAS",
    "DatabaseInitializationError",
    "init_db_engine",
    "journal_mode_of",
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


async def init_db_engine(database_path: str | Path | None = None) -> AsyncEngine:
    """Create the database, apply pragmas, and return a ready engine.

    The schema is created with ``metadata.create_all`` rather than an Alembic
    revision. That is sufficient while the project has a single revision, and
    ``alembic`` is not a declared dependency. Note that ``create_all`` only
    ever adds tables and columns: it never alters or drops them, so a container
    starting against an older volume can silently disagree with the code.
    Versioned migrations return as T017, when a second revision exists or the
    Docker image ships against a persistent volume.

    Args:
        database_path: Database file location. Defaults to
            ``data/router.db``. The parent directory is created if absent.

    Returns:
        An ``AsyncEngine`` with WAL confirmed active. The caller owns disposal;
        ``AsyncEngine.dispose`` is a coroutine.

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
        async with engine.connect() as connection:
            await _apply_pragmas(connection)
            await _enforce_wal(connection)
            await connection.run_sync(metadata.create_all)
            await connection.commit()
    except DatabaseInitializationError:
        await engine.dispose()
        raise
    except Exception as exc:
        await engine.dispose()
        raise DatabaseInitializationError(f"Unable to initialise database '{path}': {exc}") from exc
    return engine


def _create_engine(path: Path) -> AsyncEngine:
    try:
        # No check_same_thread: aiosqlite is async-native and SQLite serialises
        # writes at the file level under WAL, so the flag has nothing to guard.
        return create_async_engine(f"sqlite+aiosqlite:///{path}", future=True)
    except Exception as exc:
        raise DatabaseInitializationError(f"Unable to open database '{path}': {exc}") from exc


async def _apply_pragmas(connection: AsyncConnection) -> None:
    """Run each pragma, raising on the first one that fails.

    ``journal_mode`` is applied first and its result is verified separately by
    :func:`_enforce_wal`; SQLite returns the resulting mode rather than raising
    when WAL is unavailable, so a successful call is not sufficient proof.
    """

    def _apply(connection: Connection) -> None:
        for statement in PRAGMAS:
            try:
                connection.exec_driver_sql(statement)
            except Exception as exc:
                raise DatabaseInitializationError(
                    f"Failed to apply '{statement.strip()}': {exc}"
                ) from exc

    await connection.run_sync(_apply)


async def _enforce_wal(connection: AsyncConnection) -> None:
    """Verify WAL mode is actually active; raise if it is not (spec Scenario 4).

    The mode is re-read from the live database rather than taken from the
    pragma's return value, because SQLite does not raise when WAL cannot be
    enabled: it keeps the previous journal mode. This check is what turns that
    silent degradation into a startup failure.
    """
    mode: Any = await connection.run_sync(
        lambda sync: sync.exec_driver_sql("PRAGMA journal_mode;").scalar()
    )
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
