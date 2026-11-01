"""Helper for the process-restart tests.

Run as a subprocess so a "restart" is a real one: the first invocation writes
state and exits, the second constructs fresh objects and restores from the
database. Nothing survives in memory between them, which is the whole point.

Usage::

    python restart_helper.py write <db-path>
    python restart_helper.py read  <db-path>
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from free_router.db.engine import init_db_engine
from free_router.keys.manager import KeyManager
from free_router.quota.cooldown import CooldownManager
from free_router.quota.dimensions import DimensionResource, QuotaDimension, WindowReset
from free_router.quota.persistence import QuotaStateStore
from free_router.quota.tracker import QuotaTracker

PROVIDER = "google"
MODEL = "gemini-3.5-flash-lite"
DIMENSION = QuotaDimension(
    resource=DimensionResource.REQUESTS,
    limit=500.0,
    window_seconds=86400.0,
    reset=WindowReset.FIXED,
    provenance="measured",
)


async def _build(db_path: str) -> tuple[QuotaTracker, CooldownManager, QuotaStateStore, KeyManager]:
    engine = await init_db_engine(Path(db_path))
    store = QuotaStateStore(engine)
    tracker = QuotaTracker({(PROVIDER, MODEL): (DIMENSION,)})
    cooldown = CooldownManager(store=store)
    keys = KeyManager({(PROVIDER, MODEL): {"primary": "key-material"}})
    return tracker, cooldown, store, keys


async def _write(db_path: str) -> None:
    tracker, cooldown, store, keys = await _build(db_path)

    await cooldown.enter(PROVIDER, MODEL, reason="rate_limited")
    reservation = await tracker.reserve(PROVIDER, MODEL, 100)
    assert reservation is not None
    await tracker.reconcile(reservation, None)
    await keys.mark_used(
        type(
            "K",
            (),
            {
                "pool_key": (PROVIDER, MODEL),
                "alias": "primary",
                "secret": None,
            },
        )()
    )

    await store.save_windows(tracker.export_windows())
    await store.save_key_state(keys.export_key_state())


async def _read(db_path: str) -> dict[str, object]:
    tracker, cooldown, store, keys = await _build(db_path)

    window_rows = await store.load_windows()
    cooldown_rows = await store.load_cooldowns()
    key_rows = await store.load_key_state()

    entries = tracker.import_windows(window_rows)
    restored_cooldowns = cooldown.import_cooldowns(cooldown_rows)
    await keys.import_key_state(key_rows)

    return {
        "entries": entries,
        "cooldowns": restored_cooldowns,
        "cooling": cooldown.is_cooling(PROVIDER, MODEL),
        "used_requests": tracker.used(PROVIDER, MODEL, DimensionResource.REQUESTS),
        "available": tracker.available(PROVIDER, MODEL),
        "window_rows": len(window_rows),
        "cooldown_rows": len(cooldown_rows),
    }


def main() -> int:
    mode, db_path = sys.argv[1], sys.argv[2]
    if mode == "write":
        asyncio.run(_write(db_path))
        return 0
    if mode == "read":
        print(json.dumps(asyncio.run(_read(db_path))))
        return 0
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
