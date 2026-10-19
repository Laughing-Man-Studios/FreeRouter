"""SQLAlchemy Core table definitions (no ORM).

M0 creates the database shell only. ``models`` mirrors the provider mapping
loaded from ``config.yaml`` so the router has a stable internal identity for
each model, and ``request_logs`` records operational metadata for auditing.

Neither table stores prompt or completion text (spec 9, CONSTITUTION 9.1).
Quota accounting against these tables is deferred to M1/M2.

M2 adds the four quota-state tables that close the CONSTITUTION 8.3 deviation
recorded in ADR-0004. They follow the same privacy rule as the M0 tables:
**no table stores prompt or completion text, and none stores key material.**
``key_state`` refers to a credential by *alias* only, so a state dump is safe
to read, diff and paste into an issue.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Float, Integer, MetaData, String, Table

__all__ = [
    "cooldowns_table",
    "key_state_table",
    "metadata",
    "model_status_table",
    "models_table",
    "quota_windows_table",
    "request_logs_table",
]

metadata = MetaData()

models_table = Table(
    "models",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider", String, nullable=False),
    Column("provider_id", String, nullable=False),
)

request_logs_table = Table(
    "request_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String, nullable=False, index=True),
    Column("created_at", String, nullable=False),
    Column("model", String, nullable=False),
    Column("provider", String, nullable=False),
    # provider_id is a free-form string in the M0 schema: request_logs is a
    # generic audit table, so it must not block when a provider id is absent.
    Column("provider_id", String, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("http_status", Integer, nullable=True),
    Column("error_category", String, nullable=True),
)

quota_windows_table = Table(
    "quota_windows",
    metadata,
    Column("provider", String, nullable=False, primary_key=True),
    Column("model_id", String, nullable=False, primary_key=True),
    Column("resource", String, nullable=False, primary_key=True),
    Column("window_seconds", Float, nullable=False, primary_key=True),
    Column("reset_kind", String, nullable=False),
    Column("reset_at", String, nullable=True),
    Column("entries", JSON, nullable=False),
    Column("flushed_at", String, nullable=False),
)
"""Usage against one quota dimension: a provider/model/resource over a window.

**The primary key is the whole dimension**, including ``window_seconds``. A
dimension's identity includes its window, so a configuration change that alters
a window produces a *different* row rather than silently reinterpreting the old
one. The old row is then simply not matched at restore, which is what
``spec.md`` requires: state for a dimension no longer configured is discarded,
not applied elsewhere.

``reset_kind`` mirrors the dimension's ``WindowReset`` -- ``rolling`` or
``fixed``. ``reset_at`` holds the wall-clock instant at which the current
*fixed* bucket ends, and is null for rolling windows, which have no boundary.
Google's daily allowance resets at midnight Pacific; a fixed window is not the
same thing as a rolling 24 hours, because at the boundary a request admitted
just before may be followed immediately by one just after.

``entries`` holds the window's usage as a JSON list, one object per reservation:

```text
{"at": <wall-clock epoch seconds>, "reserved": <float>, "actual": <float|null>}
```

Three properties of that shape are load-bearing:

**Timestamps are wall-clock, not monotonic.** The in-memory tracker measures
durations on a monotonic clock, which is the right choice for measuring
intervals -- it is immune to an NTP step. But a monotonic value is only
meaningful against the current boot, so persisting one and restoring it after
a reboot would restore nonsense. Wall-clock instants survive a reboot and a
container recreate, and the conversion into the local monotonic domain happens
at the persistence boundary.

**``reserved`` and ``actual`` are stored separately, and both are kept.** The
estimate is what the router reserved; ``actual`` is what the provider reported,
and it stays null until the reservation is reconciled. A window's usage
contributes ``actual`` where known and ``reserved`` otherwise -- exactly
``_Entry.effective``. Keeping both rather than collapsing them into one
effective figure is what makes estimator drift measurable after a restart
(spec Behavioral Rule 9): the accumulated reservation and the accumulated
reported usage can be compared, which is the only independent check available
against a provider's console.

**The list holds reservations, not bucket totals.** A trailing rolling window
is a sum over *ages*, not over fixed buckets, so a per-bucket total cannot
reconstruct it -- the boundary slices of a bucket are inside the window on one
side and outside it on the other. The list is bounded by the dimension's own
limit (a daily window holds at most a day of requests), because a dimension
that has exhausted its allowance refuses further requests rather than recording
them.

``flushed_at`` is the wall-clock instant of the last write, so a stale row can
be recognised without reading its contents.
"""

cooldowns_table = Table(
    "cooldowns",
    metadata,
    Column("provider", String, nullable=False, primary_key=True),
    Column("model_id", String, nullable=False, primary_key=True),
    Column("cooldown_until", String, nullable=True),
    Column("reason", String, nullable=True),
    Column("retry_after", Float, nullable=True),
    Column("updated_at", String, nullable=False),
)
"""The current cooldown on a provider/model capacity unit.

``cooldown_until`` is null when the unit is **not** in cooldown -- a null is
meaningful, not missing data, and it distinguishes "no cooldown" from "a
cooldown we failed to record".

**This is the state that must be written through immediately.** A forgotten
cooldown makes the router contact a provider that has just refused it, which
spends quota to learn nothing and is the overshoot CONSTITUTION 8.3 names. A
cooldown is time-bounded, so it is also safe to restore: an expired one is
dropped rather than reinstated.

``retry_after`` keeps the provider's own hint in seconds, so the reason a
cooldown ended is not lost with it.
"""

model_status_table = Table(
    "model_status",
    metadata,
    Column("provider", String, nullable=False, primary_key=True),
    Column("model_id", String, nullable=False, primary_key=True),
    Column("status", String, nullable=False),
    Column("detail", String, nullable=True),
    Column("threshold_tokens", Integer, nullable=True),
    Column("updated_at", String, nullable=False),
)
"""Whether a provider/model is healthy, degraded or unavailable.

``status`` is one of ``healthy``, ``degraded`` or ``unavailable``. ``detail``
carries the reason -- a context-length refusal, for instance, degrades a model
without making it unusable.

Ordinary ``400``s do not degrade a model. Only a genuine context-length
refusal does, because that is the one payload failure that says something
about the model rather than about the request.

A degraded model still serves small requests, so this is not an on/off switch.
``threshold_tokens`` is the size at or above which it is degraded, and it is the
whole meaning of the state: a model shown to fail only on a 900k-token request
must still serve a 4k-token one. Without the column the state cannot be
restored -- a restart would either forget the degradation or, worse, degrade
the model without knowing which requests it applies to.
"""

key_state_table = Table(
    "key_state",
    metadata,
    Column("provider", String, nullable=False, primary_key=True),
    Column("alias", String, nullable=False, primary_key=True),
    Column("last_used_ordinal", Integer, nullable=False),
    Column("suspended", Boolean, nullable=False),
    Column("updated_at", String, nullable=False),
)
"""Per-credential selection state, keyed by **alias**.

**No key material is stored, and no column could hold any.** A credential is
identified by the alias the operator gave it; the material itself comes from
the environment at every start (CONSTITUTION 9.2, ADR-0002). The table
therefore cannot leak a secret, which is what makes it safe to inspect.

``last_used_ordinal`` is an ordinal, not a timestamp, and it is restored.
Ordering LRU by timestamp silently degrades into ordering by arrival -- a
burst of requests within one clock tick would make every key look equally
stale, and the rotation would stop being a rotation. An ordinal is independent
of the clock and of the wall, so it is the right thing to persist.

**Restoring an ordinal means the new process must not reuse it.** The ordinal
counter starts at zero in a fresh process, so a restored ordinal of 500 and a
freshly assigned ordinal of 1 would invert the LRU order and collide. The
counter must be seeded above the highest restored ordinal. That is a restore
concern rather than a schema one, and it is recorded here because the schema is
where the reader looks to understand what the column means.

``suspended`` records that a credential was suspended after an authentication
failure, and is **not restored**. A cooldown is time-bounded and safely
restorable; a suspension is event-shaped and resolved by events the router
cannot observe. A credential revoked an hour ago may have been fixed since,
and resurrecting the suspension would refuse a working key and surface a
confusing 503 instead of the 401 that would diagnose it. The column is kept
for inspection, not for enforcement -- and ``updated_at`` is what makes that
inspection meaningful, since a suspension with no timestamp says only that it
happened, not when.
"""
