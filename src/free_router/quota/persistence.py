"""Durable quota state: reading it back, and writing it out.

CONSTITUTION §8.3 requires quota counts, cooldown timestamps and provider
availability to be persisted to SQLite, so that a restart does not immediately
overshoot a limit the router has already reached. ADR-0004 deferred that to M2
and left the interface in place for it; this module is the other half of that
bargain.

**Durability is deliberately asymmetric**, and that is the design rather than an
optimisation (ADR-0005):

- **Cooldowns and model status are written through.** A forgotten cooldown
  makes the router contact a provider that has just refused it, which spends
  quota to learn nothing. That is the overshoot §8.3 names.
- **Counters and key ordinals are batched** behind a flush interval, coalesced so
  a burst of requests in one interval produces one additive write. Losing the
  last few seconds of counting is tolerable; paying a database round-trip per
  request to prevent it is not.

**The hot state stays in memory.** The tracker keeps its windows in memory and
the store keeps a durable copy, which is why the write path can be batched at
all. There is one implementation of the window logic, not two.

Timestamps cross the boundary in **wall-clock** time, because a monotonic value
is only meaningful against the current boot: persisting one and restoring it
after a reboot would restore nonsense. The conversion into the tracker's
monotonic domain happens here, at the boundary, rather than leaking into the
window logic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from free_router.core.timestamps import from_iso, to_iso
from free_router.db.schema import (
    cooldowns_table,
    key_state_table,
    model_status_table,
    quota_windows_table,
)

__all__ = [
    "DEFAULT_FLUSH_INTERVAL_SECONDS",
    "PersistenceCoordinator",
    "QuotaStateStore",
    "RestoreReport",
    "to_iso",
    "from_iso",
]

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_INTERVAL_SECONDS = 20.0
"""Seconds between counter flushes (clarification B2).

A ``SIGKILL`` loses up to one interval of counters. Cooldowns are unaffected --
they are written through.
"""

_WINDOW_KEY = ("provider", "model_id", "resource", "window_seconds")
_COOLDOWN_KEY = ("provider", "model_id")
_STATUS_KEY = ("provider", "model_id")
_KEY_STATE_KEY = ("provider", "alias")


class RestoreReport:
    """What a restore actually recovered.

    Recorded rather than merely logged, because the whole point of the exercise
    is to be able to say afterwards what the router knew at boot.
    """

    def __init__(self) -> None:
        self.windows = 0
        self.window_entries = 0
        self.cooldowns = 0
        self.model_status = 0
        self.key_state = 0
        self.degraded: list[str] = []

    def describe(self) -> str:
        """Render for a log line. Carries no payload and no key material."""
        return (
            f"windows={self.windows} entries={self.window_entries} "
            f"cooldowns={self.cooldowns} status={self.model_status} "
            f"keys={self.key_state} degraded={len(self.degraded)}"
        )


class QuotaStateStore:
    """Reads and writes the four quota-state tables.

    Deliberately dumb: it moves rows and nothing else. What a row *means* is the
    business of the component that owns it -- the tracker, the cooldown manager,
    the breaker and the key manager each know how to export and import their own
    state, and this class never interprets a field.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Build a store over the router's database.

        Args:
            engine: The lifespan-owned async engine. The store never disposes
                it; the application owns its lifetime.
        """
        self._engine = engine

    # --- reading -----------------------------------------------------------

    async def load_windows(self) -> list[dict[str, Any]]:
        """Every persisted window row."""
        return await self._select(quota_windows_table)

    async def load_cooldowns(self) -> list[dict[str, Any]]:
        """Every persisted cooldown row."""
        return await self._select(cooldowns_table)

    async def load_model_status(self) -> list[dict[str, Any]]:
        """Every persisted model-status row."""
        return await self._select(model_status_table)

    async def load_key_state(self) -> list[dict[str, Any]]:
        """Every persisted key-state row."""
        return await self._select(key_state_table)

    # --- writing -----------------------------------------------------------

    async def save_windows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Upsert window rows."""
        await self._upsert(quota_windows_table, rows, _WINDOW_KEY)

    async def save_cooldown(self, row: Mapping[str, Any]) -> None:
        """Upsert one cooldown row."""
        await self._upsert(cooldowns_table, [row], _COOLDOWN_KEY)

    async def save_model_status(self, row: Mapping[str, Any]) -> None:
        """Upsert one model-status row."""
        await self._upsert(model_status_table, [row], _STATUS_KEY)

    async def save_key_state(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Upsert key-state rows."""
        await self._upsert(key_state_table, rows, _KEY_STATE_KEY)

    # --- internals ---------------------------------------------------------

    async def _select(self, table: Any) -> list[dict[str, Any]]:
        async with self._engine.connect() as connection:
            result = await connection.execute(table.select())
            return [dict(row) for row in result.mappings()]

    async def _upsert(
        self,
        table: Any,
        rows: Sequence[Mapping[str, Any]],
        key: Sequence[str],
    ) -> None:
        if not rows:
            return
        async with self._engine.begin() as connection:
            for row in rows:
                statement = sqlite_insert(table).values(**row)
                update = {name: statement.excluded[name] for name in row if name not in key}
                statement = statement.on_conflict_do_update(
                    index_elements=list(key),
                    set_=update,
                )
                await connection.execute(statement)


class PersistenceCoordinator:
    """Restores state at startup and flushes counters on an interval.

    Owns the two rhythms of durability: a restore before the listener serves
    anything, and a periodic flush thereafter.
    """

    def __init__(
        self,
        store: QuotaStateStore,
        snapshot: Callable[[], tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]],
        *,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        """Build a coordinator.

        Args:
            store: The durable store.
            snapshot: Callable returning the current ``(windows, key_state)``
                rows. A callable rather than a pair, so every flush writes the
                state as it is *now* rather than a snapshot taken when the loop
                started.
            flush_interval: Seconds between flushes.
        """
        self._store = store
        self._snapshot = snapshot
        self._flush_interval = flush_interval
        self._task: asyncio.Task[None] | None = None

    @property
    def flush_interval(self) -> float:
        """Seconds between counter flushes."""
        return self._flush_interval

    async def flush(self) -> None:
        """Write the batched state: counters and key ordinals.

        A failure here is logged and swallowed. The counters are a record of
        what has already been spent, and refusing traffic because a background
        write failed would be a worse outcome than losing the record of a few
        seconds of counting.
        """
        try:
            windows, keys = self._snapshot()
            await self._store.save_windows(windows)
            await self._store.save_key_state(keys)
        except Exception as exc:
            logger.warning(
                "quota_state_flush_failed",
                extra={
                    "event": "quota_state_flush_failed",
                    "error_category": type(exc).__name__,
                },
            )

    async def run(self) -> None:
        """Flush on an interval until cancelled.

        The final flush is best-effort: a clean shutdown should not be able to
        lose the last interval of counters, but an unclean one may, and that
        loss is bounded and accepted.
        """
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                await self.flush()
        except asyncio.CancelledError:
            await self.flush()
            raise

    def start(self) -> asyncio.Task[None]:
        """Start the flush loop as a background task."""
        self._task = asyncio.create_task(self.run(), name="quota-state-flush")
        return self._task

    async def stop(self) -> None:
        """Stop the flush loop, flushing once more on the way out."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
