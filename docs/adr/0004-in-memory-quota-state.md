# ADR-0004: In-Memory Quota and Cooldown State for M1

**Date:** 2026-10-06
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

CONSTITUTION §8.3 is unambiguous:

> "Quota counts, key cooldown timestamps, and provider availability states must be persisted to SQLite. The system must survive unexpected process restarts without losing rate-limit history or immediately overshooting provider limits upon boot."

ROADMAP §3.14 places persistence in **M2**, not M1. M1's deliverables (ROADMAP Milestone 1) list "simple quota counters based on static configured limits" and "conservative cooldown state" without specifying durability.

M0 already built the persistence substrate: SQLite via `aiosqlite`, WAL enforcement with a fail-fast check, and Alembic migrations applied at startup (`specs/001-project-bootstrap`). The `request_logs` table exists and is unused.

So the question is not whether to build persistence — that infrastructure is done — but whether M1 should use it for quota state.

The operator has directed that the Constitution is treated as the definition of done for the MVP, and that M1 accepts in-memory state with the gap documented. This ADR records that decision, its consequences, and the path to closure.

## 2. Decision

Quota counters and cooldown timestamps are held **in memory** for the duration of the M1 milestone. They are not persisted.

Persistence is deferred to **M2**, where ROADMAP §3.14 already places it.

The `QuotaTracker` interface is designed now so that M2 can add persistence **behind it** without changing the key manager, the route handler, or the adapters.

## 3. Reasoning

**Persisting on the request path adds latency and failure modes for little M1 benefit.** CONSTITUTION §8.3 forbids the database blocking the event loop. Every quota operation would become a database round-trip on a latency-sensitive path, with a new class of failure (write contention, lock contention) at exactly the moment the system is trying to protect quota.

**The durability gap is bounded and understood.** The router is a single-node, single-user, containerised service. A restart is rare and operator-initiated. The consequence is that the router forgets daily request counts and cooldown state, and may briefly exceed a limit it had already reached.

**The interface, not the storage, is the durable decision.** Defining `reserve`/`reconcile`/`available` now means M2 swaps an in-memory counter for a SQLite-backed one without touching callers. That is the part that would be expensive to retrofit; the storage choice is not.

**M2 is already committed to it.** ROADMAP §3.14's exit criterion is that the router "survives a restart without immediately losing its quota state". This ADR therefore marks work as *deferred and tracked*, not *undecided*.

## 4. Alternatives Considered

**Persist quota state in M1.** Rejected for now. It pulls M2's central deliverable forward, adding a database round-trip to the dispatch path while the retry and failover logic that actually drives the success-rate target is still being built. Sequencing it after that logic means the persistence layer is written once, against a settled interface and a known load profile.

**Persist only cooldown timestamps.** Considered as a partial measure — cooldowns matter most, since a forgotten cooldown causes an immediate request that is likely to be rate-limited again. Rejected as a half-measure: it introduces the whole write path (migration, repository layer, error handling, event-loop discipline) while retaining the reset-after-restart behaviour for counters. The complexity is largely paid either way, so it is better paid once, completely, in M2.

**No quota tracking at all in M1.** Rejected. Counters are what prevent a burst from exhausting a daily limit, and CONSTITUTION §8.1 makes quota management a proactive protection mechanism rather than analytics.

## 5. Consequences

### Positive

- The dispatch path stays free of database round-trips.
- No new failure mode is introduced on the latency-sensitive path.
- M2's persistence work lands against a settled interface.

### Negative / Trade-offs

- **After a restart, the router forgets daily request counts and cooldown state.** It may exceed a daily limit it had already reached, and may retry a key that was cooling.
- The behaviour is invisible unless documented. It is recorded in `spec.md` § *Recorded Deviations* so it is not rediscovered as an unexplained fault.
- Any test asserting durability must wait for M2.
- **The gap is a known constitutional deviation**, accepted deliberately rather than absorbed.

## 6. Path to Closure

M2 must:

- Add tables for quota counters, cooldown state, and provider availability.
- Implement a SQLite-backed `QuotaTracker` satisfying the same interface, including the non-blocking requirement of CONSTITUTION §8.3.
- Read persisted cooldowns at startup so a restart does not retry a cooling key.
- Remove the recorded deviation from `spec.md` § *Recorded Deviations*.

Until then, this deviation is open.

## 7. Constitutional Compliance Check

| Constitution Directive | Compliance Validation |
| :--- | :--- |
| **8.3 State Durability & Concurrency** | **Deviation, accepted and recorded.** Persistence of quota counts, cooldown timestamps, and provider availability is deferred to M2. Tracked as open work, not as satisfied. |
| **8.3 Concurrency** | **Pass:** No database operation on the dispatch path, so the event loop is never blocked by quota accounting. |
| **8.2 Reservation & Reconciliation Cycle** | **Pass:** The estimate → reserve → dispatch → reconcile cycle is implemented in memory. Only its durability is deferred. |
| **8.1 Resource Protection Imperative** | **Pass:** Counters still prevent bursts from exhausting a daily limit within a process lifetime. |
| **29 Relationship to the Constitution** | **Pass:** The deviation is deliberate, recorded, and tracked rather than worked around implicitly. |
