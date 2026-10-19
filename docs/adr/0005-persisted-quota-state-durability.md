# ADR-0005: Persisted Quota State with Asymmetric Durability

**Date:** 2026-10-19
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)
**Closes:** ADR-0004 §6 (Path to Closure), CONSTITUTION §8.3 deviation

## 1. Context

CONSTITUTION §8.3:

> "Quota counts, key cooldown timestamps, and provider availability states must be
> persisted to SQLite. The system must survive unexpected process restarts without
> losing rate-limit history or immediately overshooting provider limits upon boot.
> To support concurrent agent workloads (e.g., OpenHands), the database layer must
> not block the async event loop."

ADR-0004 deferred this from M1 to M2, giving three reasons: a database round-trip
on the latency-sensitive dispatch path, a new failure mode at the moment the system
is trying to protect quota, and a preference for writing the persistence layer once
against a settled interface.

Two of those reasons still hold in M2. The dispatch path is still latency-sensitive,
and `QuotaTracker`'s interface is now settled — which is exactly what ADR-0004 was
buying.

§8.3 names two kinds of state with **different** failure consequences:

- Losing a **cooldown** means the router contacts a provider that just refused it.
  That is the behaviour the clause exists to prevent.
- Losing a **request count** means the router may dispatch a few requests against
  capacity it had already nearly exhausted — once, after a crash, not in a loop.

The clause's own language distinguishes them: "without losing rate-limit history
**or** immediately overshooting provider limits upon boot." The second is the
urgent one.

## 2. Decision

Persist to **SQLite through the existing async `aiosqlite` stack** (M0's; mandated
by CONSTITUTION §3.1), behind the `QuotaTracker` interface ADR-0004 preserved.

**Durability is asymmetric:**

| State | Policy | Flush |
| :--- | :--- | :--- |
| `cooldown_until`, breaker status | Write-through | Immediate, per transition |
| Request/token counters | Batched | Bounded interval, coalesced |
| LRU ordinals | Batched | With counters |

A burst of N requests within one interval produces **one** write of their sum.
Credential suspension is persisted but **not restored** — see §5.

## 3. Reasoning

**The uniform alternative is worse, not simpler.** "Persist on every change" reads as
one rule and needs one code path. It puts a database write on the dispatch path for
every request, which is the specific cost ADR-0004 cited when deferring. It buys
protection against losing a few seconds of counter accuracy — and counter accuracy
is exactly the property that survives a crash *least* badly, because the worst case
is a small number of extra requests against a limit the operator can see.

Cooldowns invert this. A forgotten cooldown causes an immediate request that is
*likely* to be refused again, and the refusal spends quota to learn nothing. That is
the specific overshoot §8.3 names.

**Coalescing makes batching near-free.** Because window usage is a bucketed
accumulator rather than an event log, an interval's N requests are one additive
write. The batching cost is one write per interval, not one per request — so the
asymmetry costs almost nothing and protects the state that actually needs it.

**The interface is being cashed in, not revised.** ADR-0004's claim was that *"the
interface, not the storage, is the durable decision."* M2 adds exactly one method,
`restore()`, called once at startup. `reserve`/`reconcile`/`available` keep their
signatures, including `Reservation | None` and `int | None`. If implementing this
required changing the dispatcher, ADR-0004's claim would have been wrong — and that
is worth recording as a finding rather than quietly working around.

**Restore must not be able to prevent startup.** A crash-loop is a worse failure
than forgetting a cooldown: the router serves nothing at all. So an unreadable or
corrupt row degrades that unit to in-memory accounting and records it. §8.3 requires
surviving restarts, not refusing to start.

## 4. Alternatives Considered

**Persist counters write-through too.** Rejected. Adds a database round-trip per
request to protect a few seconds of accounting, reintroducing the exact cost
ADR-0004 accepted. Also fails the §8.3 non-blocking requirement in spirit: not
blocking is not the same as being free.

**Persist only cooldowns (ADR-0004's rejected half-measure).** Rejected *again*, for
ADR-0004's original reason: it introduces the whole write path — migration,
repository layer, error handling, event-loop discipline — while leaving counters
resetting after restart. The complexity is largely paid either way, so it is paid
once and completely.

**One write path for all state, with a single policy.** Rejected as §3. It is the
simplest to *describe* and the most expensive to *run*, because one policy must be
safe for the state that needs the most protection, and that state needs it on every
transition.

**Redis or another external store.** Rejected. ROADMAP §3.14 explicitly defers the
choice until real usage patterns are known, and requires exactly one local
implementation. #36 lists external persistence as a non-goal. Introducing it would
also add a network dependency to a single-user local proxy.

**Snapshot the whole tracker on shutdown only.** Rejected. A clean shutdown cannot
be assumed — containers are killed, laptops sleep, and §8.3 says *unexpected*
restarts. Counter flushing is therefore interval-driven with a best-effort final
flush on clean shutdown, not dependent on it.

**Reconstruct quota state from the existing `request_logs` table.** Rejected. It
would be correct, but requires rebuilding rolling windows from individual rows on
every boot, makes a corrupt log row fatal to startup, and conflates telemetry with
accounting. `request_logs` is left untouched as what it is: telemetry.

## 5. Consequences

### Positive

- **The §8.3 deviation closes.** Cooldowns and availability survive restart, which
  is the overshoot case the clause names.
- **No write on the dispatch path for counters** — the M1 latency profile survives.
- **No new external dependency.** One implementation, as ROADMAP §3.14 requires.
- **The tracker interface is exercised against a second implementation**, which
  tests ADR-0004's claim rather than merely asserting it.

### Negative / Trade-offs

- **A `SIGKILL` loses up to one flush interval of counters.** Bounded and stated,
  not eliminated. Cooldowns are unaffected.
- **Persistence failure becomes a request-path failure.** A failed cooldown
  write-through returns `503 persistence_unavailable` rather than dispatching into
  a limit the router cannot record. This is a new failure mode, and it is
  deliberate: continuing would permit unbounded quota consumption, which is the
  exact condition the quota system exists to prevent.
- **State can drift from reality.** Persisted counters accumulate any difference
  between what the router counts and what the provider bills. M1's cross-check
  already found the router counting one *more* dispatch than billed, so the drift
  is real and observable.
- **Restore adds a startup step that can fail.** Mitigated by degrading rather than
  refusing to start.
- **Corrupt state is now a persistent condition.** A crash mid-write can leave a
  bad row that survives every subsequent boot, degrading silently to in-memory
  accounting until noticed.

### Suspension is written but not restored

Credential suspension is M1's response to an authentication failure. It is
persisted for inspection, but **not** reinstated on boot: a credential revoked an
hour ago may have been fixed since, and resurrecting the suspension would refuse a
working key and produce a confusing 503 instead of the `401` that would diagnose it.

The distinction is that a **cooldown is time-bounded and therefore safely
restorable; a suspension is event-shaped and resolved by the passage of events the
router cannot observe.** The router has no way to learn that a key was reissued.

## 6. Constitutional Compliance Check

| Constitution Directive | Compliance Validation |
| :--- | :--- |
| **8.3 State Durability & Concurrency** | **Pass.** Counts, cooldown timestamps and availability states are persisted to SQLite and restored before traffic is served. Closes the ADR-0004 deviation. |
| **8.3 Concurrency** | **Pass.** Async `aiosqlite` throughout, with a test asserting event-loop responsiveness under concurrent reservation. |
| **8.2 Reservation & Reconciliation Cycle** | **Pass.** The cycle is unchanged; only its durability is now real. |
| **8.1 Resource Protection Imperative** | **Pass.** Protection now survives restart rather than only process lifetime. |
| **9.1 Privacy by Default** | **Pass.** No payload columns exist in the schema; §9.2 is met because only key *aliases* are stored. |
| **29 Relationship to the Constitution** | **Pass.** The deviation is closed deliberately, verified by scenario, and removed from M1's recorded deviations only once the restore scenarios pass. |

## 7. Verification

This ADR is discharged when the following are demonstrated, not merely implemented:

1. `spec.md` Scenarios 1–4 and 12 pass against a real restart of a real process,
   not a simulated store swap.
2. An event-loop responsiveness test passes under concurrent reservation.
3. The §8.3 entry is removed from `specs/002-mvp-openai-proxy/spec.md` § *Recorded
   Deviations* only after 1 and 2, and that removal is recorded as a change rather
   than a silent edit.
