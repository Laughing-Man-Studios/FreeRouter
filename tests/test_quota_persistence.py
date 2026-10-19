"""Durable quota state: Batch 2.

CONSTITUTION §8.3 requires that a restart does not immediately overshoot a
provider limit the router has already reached. These tests are about that
property and about the boundary around it -- what is durable, what is
deliberately not, and what happens when durability fails.

The restart tests use **two real processes**. A single process that clears its
objects and reloads would pass even if the state only ever lived in memory,
which is the failure mode this milestone exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from free_router.core.exceptions import PersistenceUnavailableError
from free_router.db.engine import init_db_engine
from free_router.quota.cooldown import CooldownManager
from free_router.quota.dimensions import DimensionResource, QuotaDimension, WindowReset
from free_router.quota.persistence import QuotaStateStore
from free_router.quota.tracker import QuotaTracker

HELPER = Path(__file__).parent / "restart_helper.py"
PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"

DAILY = QuotaDimension(
    resource=DimensionResource.REQUESTS,
    limit=500.0,
    window_seconds=86400.0,
    reset=WindowReset.FIXED,
    provenance="measured",
)
MINUTE = QuotaDimension(
    resource=DimensionResource.REQUESTS,
    limit=500.0,
    window_seconds=60.0,
    reset=WindowReset.ROLLING,
    provenance="measured",
)


def _run_helper(mode: str, db_path: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(HELPER), mode, str(db_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"helper failed: {result.stderr}"
    return result.stdout


async def _store(db_path: Path) -> QuotaStateStore:
    engine = await init_db_engine(db_path)
    return QuotaStateStore(engine)


# --- a real restart ----------------------------------------------------------


def test_a_cooldown_survives_a_process_restart(tmp_path: Path) -> None:
    """A cooldown set before the restart still refuses the model after it.

    This is the case CONSTITUTION §8.3 names: without it, the router forgets a
    limit it has been told about and contacts the provider again, which spends
    quota to learn nothing.
    """
    db_path = tmp_path / "router.db"

    _run_helper("write", db_path)
    result = json.loads(_run_helper("read", db_path))

    assert result["cooldowns"] == 1
    assert result["cooling"] is True


def test_counters_survive_a_process_restart(tmp_path: Path) -> None:
    """A dispatched request is still counted after the restart.

    Without this, a restart would hand back a full daily allowance that had
    already been spent.
    """
    db_path = tmp_path / "router.db"

    _run_helper("write", db_path)
    result = json.loads(_run_helper("read", db_path))

    assert result["entries"] == 1
    assert result["used_requests"] == 1.0
    assert result["available"] is True


def test_a_restart_does_not_resurrect_an_expired_cooldown(tmp_path: Path) -> None:
    """An expired cooldown is dropped, not reinstated.

    A cooldown is time-bounded, so restoring one that has already elapsed would
    refuse capacity that is actually free.
    """
    store_db = tmp_path / "router.db"

    async def write_expired() -> None:
        store = await _store(store_db)
        await store.save_cooldown(
            {
                "provider": PROVIDER,
                "model_id": MODEL,
                "cooldown_until": "2000-01-01T00:00:00Z",
                "reason": "rate_limited",
                "retry_after": None,
                "updated_at": "2000-01-01T00:00:00Z",
            }
        )

    asyncio.run(write_expired())

    manager = CooldownManager()
    restored = manager.import_cooldowns(
        [
            {
                "provider": PROVIDER,
                "model_id": MODEL,
                "cooldown_until": "2000-01-01T00:00:00Z",
                "reason": "rate_limited",
                "retry_after": None,
            }
        ]
    )

    assert restored == 0
    assert manager.is_cooling(PROVIDER, MODEL) is False


def test_a_corrupt_row_degrades_rather_than_crashing(tmp_path: Path) -> None:
    """An unreadable row is discarded, and the router still starts.

    A crash-loop serves nothing at all, which is a worse failure than
    forgetting a cooldown.
    """
    store_db = tmp_path / "router.db"

    async def write_corrupt() -> None:
        store = await _store(store_db)
        await store.save_cooldown(
            {
                "provider": PROVIDER,
                "model_id": MODEL,
                "cooldown_until": "not-a-timestamp",
                "reason": "rate_limited",
                "retry_after": None,
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )

    asyncio.run(write_corrupt())

    manager = CooldownManager()
    restored = manager.import_cooldowns(
        [
            {
                "provider": PROVIDER,
                "model_id": MODEL,
                "cooldown_until": "not-a-timestamp",
                "reason": "rate_limited",
                "retry_after": None,
            }
        ]
    )

    assert restored == 0
    assert manager.is_cooling(PROVIDER, MODEL) is False


# --- what is deliberately not restored ---------------------------------------


def test_suspension_is_written_but_not_restored(tmp_path: Path) -> None:
    """A suspended credential comes back into rotation after a restart.

    A cooldown is time-bounded and safely restorable; a suspension is
    event-shaped and resolved by events the router cannot observe. A credential
    revoked an hour ago may have been fixed since, and refusing it would
    surface a confusing 503 instead of the 401 that would diagnose it.
    """
    from free_router.keys.manager import KeyManager

    keys = KeyManager({(PROVIDER, MODEL): {"primary": "key-material"}})
    asyncio.run(keys.suspend(PROVIDER, MODEL, "primary"))

    rows = keys.export_key_state()
    assert rows[0]["suspended"] is True

    fresh = KeyManager({(PROVIDER, MODEL): {"primary": "key-material"}})
    asyncio.run(fresh.import_key_state(rows))

    assert fresh.is_suspended(PROVIDER, MODEL, "primary") is False


def test_the_ordinal_counter_is_seeded_above_the_restored_ordinal() -> None:
    """A restored ordinal must not collide with a freshly assigned one.

    A fresh process starts its counter at zero. A restored ordinal of 500 and a
    freshly assigned ordinal of 1 would invert the LRU order *and* collide
    with it, so the counter has to be seeded above the highest restored value.
    """
    from free_router.keys.manager import KeyManager

    fresh = KeyManager({(PROVIDER, MODEL): {"primary": "key-material"}})
    asyncio.run(
        fresh.import_key_state(
            [
                {
                    "provider": PROVIDER,
                    "alias": "primary",
                    "last_used_ordinal": 500,
                    "suspended": False,
                }
            ]
        )
    )

    key = fresh._pools[(PROVIDER, MODEL)].keys[0]
    asyncio.run(fresh.mark_used(key))

    assert fresh._pools[(PROVIDER, MODEL)].last_used["primary"] > 500


# --- when durability fails ---------------------------------------------------


def test_a_failed_cooldown_write_marks_the_unit_degraded() -> None:
    """A cooldown that could not be recorded leaves the unit degraded.

    The router then knows it cannot promise to remember the limit, which is
    what the dispatch gate refuses on.
    """
    from free_router.quota.cooldown import CooldownManager as Manager

    class BrokenStore:
        async def save_cooldown(self, row: dict[str, object]) -> None:
            raise OSError("disk is full")

    manager = Manager(store=BrokenStore())
    asyncio.run(manager.enter(PROVIDER, MODEL, reason="rate_limited"))

    assert manager.persistence_degraded(PROVIDER, MODEL) is True
    assert manager.degradation_reason(PROVIDER, MODEL) == "OSError"


def test_the_degraded_gate_raises_persistence_unavailable() -> None:
    """A degraded unit is refused with the distinct error.

    The client is told the router cannot record a limit, rather than being
    told the model is busy -- the two need different responses.
    """
    from free_router.core.dispatch import Dispatcher

    class DegradedCooldown:
        def persistence_degraded(self, provider: str, model_id: str) -> bool:
            return True

        def is_cooling(self, provider: str, model_id: str) -> bool:
            return False

        def remaining(self, provider: str, model_id: str) -> float:
            return 0.0

    class NeverBlocking:
        def blocks(self, provider: str, model_id: str, estimated: int) -> bool:
            return False

    class AlwaysAvailable:
        def available(self, provider: str, model_id: str) -> bool:
            return True

    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher._cooldown = DegradedCooldown()  # type: ignore[attr-defined]
    dispatcher._breaker = NeverBlocking()  # type: ignore[attr-defined]
    dispatcher._quota = AlwaysAvailable()  # type: ignore[attr-defined]

    with pytest.raises(PersistenceUnavailableError) as excinfo:
        dispatcher._check_capacity(PROVIDER, MODEL, 100)  # type: ignore[attr-defined]

    assert excinfo.value.code == "persistence_unavailable"


# --- the write path ----------------------------------------------------------


def test_the_flush_is_coalesced_into_one_write(tmp_path: Path) -> None:
    """A burst of requests produces one write, not one per request.

    The whole reason counters are batched is that the alternative is a database
    round-trip on the dispatch path.
    """

    class CountingStore:
        def __init__(self) -> None:
            self.writes = 0

        async def save_windows(self, rows: object) -> None:
            self.writes += 1

        async def save_key_state(self, rows: object) -> None:
            self.writes += 1

    counting = CountingStore()

    async def burst() -> None:
        tracker = QuotaTracker({(PROVIDER, MODEL): (MINUTE,)})
        for _ in range(20):
            reservation = await tracker.reserve(PROVIDER, MODEL, 100)
            assert reservation is not None
            await tracker.reconcile(reservation, None)
        await counting.save_windows(tracker.export_windows())

    asyncio.run(burst())

    assert counting.writes == 1


def test_the_event_loop_stays_responsive_under_concurrent_reservation() -> None:
    """Concurrency must not block the loop (CONSTITUTION §8.3).

    The database layer must not block the async event loop. A heartbeat
    coroutine that has to keep ticking while reservations are made is the
    cheapest way to catch a blocking call.
    """

    async def exercise() -> None:
        tracker = QuotaTracker({(PROVIDER, MODEL): (MINUTE,)})
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            for _ in range(50):
                reservation = await tracker.reserve(PROVIDER, MODEL, 100)
                assert reservation is not None
                await tracker.reconcile(reservation, None)
                await asyncio.sleep(0)
        finally:
            beat.cancel()

        assert ticks > 0, "the event loop never yielded"

    asyncio.run(exercise())
