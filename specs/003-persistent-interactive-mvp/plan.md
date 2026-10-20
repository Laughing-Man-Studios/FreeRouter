# Implementation Plan: M2 Persistent & Interactive MVP

**Specification:** `specs/003-persistent-interactive-mvp/spec.md`
**Planning Issue:** #36

## Technical Approach

M2 adds four capabilities to a working router. The plan follows from three
observations about what M1 already got right, because the cheapest implementation
is the one that does not disturb them.

### The persistence substrate already exists

M0 delivered async SQLite (`aiosqlite`), WAL enforcement with a fail-fast check,
and Alembic migrations applied at startup. `request_logs` exists and is unused.
CONSTITUTION §3.1 already mandates this stack.

**M2 does not choose a database, and adds no driver.** It adds tables and a
persistence layer attached to the existing tracker. Choosing Redis now would
contradict ROADMAP §3.14's instruction to defer that decision until real usage is
known, and would violate the single-implementation constraint.

### One implementation, with persistence attached

ADR-0004's central claim was that *"the interface, not the storage, is the durable
decision."* M2 cashes that in — and in doing so, **supersedes the
two-implementations framing** that plan.md and ADR-0005 originally carried.

If counters batch behind a flush interval, the state they batch **must** be hot in
memory. A tracker that read and wrote SQLite on every reservation would put a
database round-trip on the dispatch path — the exact cost the asymmetry principle
exists to avoid. So the design is:

```text
QuotaTracker  (M1, unchanged window logic, hot in-memory state)
    └── persistence layer  (M2: write-through cooldowns, batched counter flush)
```

**There is no second implementation of the window logic**, so no protocol
extraction is needed to keep two in step, and **no fallback implementation to keep
alive**. If persisted state is unreadable, the same tracker starts with empty
state and continues — the degradation path is a state, not a class.

What must **not** happen: the route handler, dispatcher, key manager or adapters
change to accommodate persistence. If they do, the interface was not as general as
ADR-0004 claimed, and that is a finding worth recording rather than working around.

### Durability is asymmetric, and the write path stays off the dispatch path

The two kinds of state have opposite requirements:

| State | If lost on restart | Write policy |
| :--- | :--- | :--- |
| `cooldown_until`, breaker status | Router re-contacts a provider that just refused it — the exact behaviour §8.3 forbids | **Write-through, immediately** |
| Request/token counters | Router loses a few seconds of accounting; may re-contact a provider it had just exhausted, once | **Batched**, 20 s interval (B2) |
| LRU `last_used` ordinals | Selection order resets; no safety consequence | Batched with counters |

**This asymmetry is the main design decision in M2** (ADR-0005). A uniform
"persist on every change" rule would place a database round-trip on the dispatch
path — the concern that made M1 defer persistence in the first place — to protect
a few seconds of counter accuracy that costs nothing to lose.

Writes are also **coalesced**: a burst of N requests in one interval produces one
write of their sum, not N writes.

### Token estimation separates input from output

M1 reserves `chars / 4` over all message content. That covers input, which is
knowable, and silently assumes output is zero, which it is not.

M2 reserves:

```text
reserved = ceil((estimated_input + assumed_output) * safety_margin)
```

with `assumed_output` **1025** and configurable **globally** (B1), and with the
**safety margin itself configurable** (B3) — it is the multiplier on that
assumption, so an operator tuning one would reasonably expect to tune the other.

**The tracker composes the reservation, not the caller.** The caller estimates
the *input*, because that is the part it can see; the tracker adds the output
assumption it holds, because that is the part that is policy. The tracker
therefore owns the whole reserved figure, and a caller cannot forget the output
half the way it could forget the margin.

**The output assumption is global, not per-model.** It is a *policy* number, not
a measured one, and the measured part — the input estimate — is already
per-model in effect, because it is computed from the request. A global default is
one number to reason about; a per-model value invites the belief that it is
calibrated, and a value that is not calibrated is worse than one that obviously
is not.

**The breaker keeps using the input estimate.** A context-length refusal is about
the prompt; a provider refusing 900k *input* tokens says nothing about how long
the reply would have been. The breaker's threshold is therefore compared against
the input estimate, not the reserved total.

Provider-reported usage, where available, supersedes the estimate at reconcile time
— which the code already supports (`_Entry` carries `actual`, and
`reconcile(None)` deliberately stands the estimate down rather than releasing it).
What M2 adds is the plumbing to *have* a provider figure to report:
`NormalizedResponse` gains a usage field, and both adapters populate it from
Gemini's `usageMetadata` and Mistral's `usage`.

### `/v1/models` is a projection of configuration

The response is generated from `config.models` — the same list that already drives
routing. There is no discovery call, no cache to invalidate, and no second source
of truth. Availability is carried as a *separate* field precisely so that the
listing does not change as provider state moves.

**A configured model is always listed, including while in cooldown.** Hiding it
would make a client's model list oscillate with transient provider state.

`created` is the config file's mtime, `owned_by` is the deployment owner, and
`permission`/`default` are emitted (B5).

### Streaming extends the adapter protocol and the route, not the dispatcher

`chat_completion` is currently a request/response call. Streaming adds a sibling
method rather than a `stream: bool` parameter, because the two have genuinely
different contracts: one returns a complete body, the other returns a sequence that
can fail partway. A flag would force every caller to handle the union.

**The dispatcher is not modified** (A2). The streaming path lives in the route
layer, which re-implements model resolution, key eligibility, capacity gating and
the bounded retry budget, and applies fail-fast at the point where the first chunk
has been written to the client. The dispatcher is the most safety-critical
component in the router and the one M1 measured at 100/100; leaving it
byte-for-byte untouched is worth more than avoiding the duplication.

**The duplication is bounded and must stay that way.** The streaming path takes
exactly one `(provider, model)` pair and never varies it, so CONSTITUTION §7.2
holds there too.

## Components Affected

| Component | Change |
| :--- | :--- |
| `db/schema.py` | New tables: quota windows, cooldowns, key state, model status |
| `db/migrations/` | One Alembic revision (0002) adding the above |
| `quota/persistence.py` | **New.** Write-through for cooldowns/status, batched counter flush |
| `quota/estimation.py` | **New.** Input/output estimation with configurable output assumption |
| `quota/tracker.py` | Attach the persistence layer; add `restore()` and the flush hook |
| `quota/cooldown.py` | Write-through persistence on transition |
| `quota/breaker.py` | Persist degraded/unavailable status |
| `keys/manager.py` | Restore LRU ordinals; write `last_used` through the store |
| `api/models_route.py` | **New.** `GET /v1/models` |
| `api/streaming.py` | **New.** SSE writer, provider-chunk → OpenAI-chunk translation, capacity gating |
| `api/routes.py` | Branch on `stream`; unchanged for the unary path |
| `providers/base.py` | `stream_chat_completion` on the adapter protocol |
| `providers/{google,mistral}/adapter.py` | Implement streaming for both |
| `api/main.py` | Restore state before serving; start the flush task |

**Explicitly unchanged:** `core/normalization.py`, the adapter/request translation
logic already proven by M1's 100/100 measurement, and the dispatcher's *logic*.

**`core/dispatch.py` did change, during Batch 2, and the change is recorded rather
than absorbed.** A2's "the dispatcher is not modified" was about *streaming*, and
it was correct there. It did not survive contact with persistence, for a reason
worth stating: **a durability feature cannot report its own failure through a
fire-and-forget task.** The dispatcher makes exactly two state-changing calls —
entering a cooldown and recording a breaker failure — and both are places where
the state must reach the database before the request can be answered. Making
those `async` and awaiting them is the smallest change that lets a write failure
be reported, and a third change adds the gate that refuses a capacity unit whose
limit could not be recorded.

Three call sites gained an `await`; one gate branch was added. The dispatcher's
contract, its decision logic and its measured behaviour are unchanged. The
alternative — scheduling the write as a background task — would have preserved
the letter of the rule and lost the guarantee the rule was protecting.

`core/config.py` gains two fields: `max_cooldown_seconds` (B4) and
`quota_flush_interval_seconds` (B2).

## Interfaces

### The tracker

`QuotaTracker`'s existing surface is preserved exactly (A3). `reserve`/`reconcile`
**keep their signatures**, including `Reservation | None` and `int | None`.
Changing them would ripple into the dispatcher for no benefit, and ADR-0004's
interface is the thing being cashed in here — not revised.

One method is added:

```python
async def restore(self) -> None:
    """Load persisted quota state. Called once at startup, before serving."""
```

It is called during lifespan, before the listener accepts traffic, so no request
can be dispatched against an unrestored counter.

The persistence layer is attached to the tracker rather than replacing it, so the
tracker gains a collaborator rather than a second implementation to keep in step.

### Adapter streaming

```python
async def stream_chat_completion(
    self,
    request: NormalizedRequest,
    key: KeyRef,
    provider_model_id: str,
) -> AsyncIterator[NormalizedChunk]: ...
```

Plus `supports_streaming: bool` as a class attribute, so the route can fail fast
with `streaming_unsupported` rather than buffering.

A provider that cannot stream raises `StreamingUnsupportedError` rather than
returning an empty iterator — an empty stream and an unsupported stream are
different failures and must not share an outcome.

### Streaming route contract

```text
POST /v1/chat/completions  with  {"stream": true}
```

Response: `text/event-stream`, OpenAI-compatible chunk objects, terminated by
`data: [DONE]`.

A `role` delta is emitted first, `usage` is included on the final chunk, and
`stream_options: {"include_usage": true}` is honoured (B7).

**Fail-fast boundary:** once any chunk has been written to the client, a provider
error terminates the connection. Before that point, an error is an ordinary fault
and may be retried within the existing budget. **A zero-chunk stream is not
"after stream start"** — it has delivered nothing, so it is retryable.

## Data / Persistence Changes

New tables, all keyed by `(provider, model_id)` unless noted:

```text
quota_windows
  provider, model_id            (composite key)
  resource, window_seconds      (composite key)
  reset_kind                    rolling | fixed
  reset_at                      fixed windows only
  entries                       JSON list of {at, reserved, actual}
  flushed_at

cooldowns
  provider, model_id            (composite key, primary)
  cooldown_until                nullable — NULL means no cooldown
  reason, retry_after
  updated_at

model_status
  provider, model_id            (composite key, primary)
  status                        healthy | degraded | unavailable
  detail, updated_at

key_state
  provider, alias               (composite key, primary)
  last_used_ordinal
  suspended                     boolean — suspension is in-memory and resets at boot
  updated_at
```

**This shape changed during Batch 1, and the reasons are worth recording** — the
original draft specified `bucket_used` as a JSON map of bucket ordinal to amount,
which would have been wrong in two independent ways.

**A rolling window is a sum over ages, not over buckets.** The in-memory window
keeps a flat list of reservations and computes a rolling window by summing those
within the trailing period; bucketing exists only for a *fixed* window. A
per-bucket total therefore cannot reconstruct a rolling window: the boundary
slices of a bucket fall inside the trailing period on one side and outside it on
the other, and the two halves differ. The persisted unit is the reservation
list, which is exactly what the in-memory window holds.

**Timestamps are wall-clock, not monotonic.** The tracker measures durations on a
monotonic clock, which is right for measuring intervals — it is immune to an NTP
step. But a monotonic value is only meaningful against the current boot, so
persisting one and restoring it after a reboot would restore nonsense. Wall-clock
instants survive a reboot and a container recreate. The conversion into the
local monotonic domain happens at the persistence boundary.

**The estimate and the actual are stored separately.** Each entry keeps
`reserved` alongside `actual`, rather than the two being collapsed into one
effective figure. Collapsing them would make estimator drift unmeasurable after
a restart, and the console cross-check is the only independent check available.
This is `spec.md` Behavioral Rule 9, and the schema is where it is enforced.

**`reset_at` is stored for fixed windows only.** Google's RPD resets at midnight
Pacific; a rolling window has no such instant. Modelling both as one nullable
column keeps the dimension model uniform.

**`key_state` carries `updated_at`.** The original draft had none, and it was
added during Batch 1. `suspended` is persisted for *inspection*, and a
suspension with no timestamp says only that it happened, not when — which is
almost the whole value of keeping the column at all.

**`suspended` is stored but not restored.** Credential suspension is M1's
conservative response to an auth failure. A credential that was revoked an hour
ago may have been fixed since; resurrecting the suspension would refuse a
working key. Cooldowns are time-bounded and so are restorable; suspensions are
event-shaped and so are not.

**`request_logs` is left alone.** It is M0's telemetry table and is not a quota
source. Reading quota state back from request logs would require reconstructing
rolling windows from individual rows on every boot — correct but far more
expensive, and it would make a corrupt log row fatal to startup.

**No payload columns exist in any of these tables**, which is what makes a state
dump safe to paste into an issue.

### A second landmine: the pre-Alembic adoption path

M0 derived the adoption path's expected table set from the *current* schema, and
its own docstring said this was only correct "while there is exactly one
revision", and that "when a second revision lands, the adoption path must be
reconsidered". Batch 1 is that moment.

Keeping the derivation would have required a pre-Alembic database to contain the
M2 tables as well, so it would never have been recognised as adoptable, and the
upgrade would have tried to create tables that already existed. The set is now
pinned to the initial revision, and **the adoption path stamps at that revision
and then upgrades** — stamping at head would mark a legacy database as already
carrying the later tables, and they would never be created.

The revision's own text is the authority, and a test asserts the pinned set
against what that revision actually creates, so the two cannot drift.

### A pre-existing gap: the fixed window's boundary

`WindowReset.FIXED` is documented as resetting "at a wall-clock boundary, e.g.
midnight Pacific for Google's RPD", but the implementation buckets on the
monotonic clock, so a fixed window actually resets at a machine-boot-aligned
boundary instead.

**No fixed window is currently configured** — the shipped configuration declares
only rolling dimensions — so nothing is misbehaving today. But `reset_at` in the
persisted state records the *documented* wall-clock boundary, and that is not
what the in-memory window computes. Batch 2 must reconcile the two, and the
reconciliation belongs in the window implementation rather than in the
persistence layer, which should not decide what a bucket means.

## External Integrations

None. Both providers already integrate; M2 adds no dependency, no SDK, and no
network service. The only new library surface is the existing `aiosqlite`.

## Error Handling

| Condition | Behaviour |
| :--- | :--- |
| Write-through of a cooldown fails | `503 persistence_unavailable`. Continuing would dispatch into a limit the router cannot record. |
| Counter flush fails | Log and continue. A lost counter batch is bounded and non-fatal; refusing traffic because a background flush failed would be worse. |
| Restore row is unreadable | Discard that unit's state, start on in-memory accounting, record it. |
| Restore raises generally | Degrade the whole tracker to in-memory, record it, serve traffic. |
| Adapter lacks streaming support | `400 streaming_unsupported`, before any dispatch. |
| Stream fails before first chunk | Ordinary fault; retry within budget. |
| Stream fails after first chunk | Terminate connection. No replay. Reconcile the reservation. |

**The asymmetry between the first two rows is deliberate.** A failed cooldown
write means the router cannot honour a limit it has already been told about, so it
must stop. A failed *counter batch* write means accounting is briefly behind, which
is the condition the flush interval already accepts.

## Testing Strategy

Unit tests are the primary gate, consistent with §19 and §10.1.

### Persistence

- Restore reconstructs counters, cooldowns and status; `reset_at` expiry in all
  three states (not passed, passed, exactly now).
- A cooldown with time remaining survives; one without does not.
- Suspension is written but not restored.
- State for a dimension or model no longer in configuration is discarded, not
  applied elsewhere.
- Corrupt row → in-memory fallback, startup succeeds.
- `key_state` restores LRU ordering, verified by **selection order**, not by
  reading the table.

**No blocking** (§8.3)

- A concurrency test drives `reserve` from many tasks and asserts the event loop
  stays responsive — a heartbeat coroutine that must tick during database writes.
  This is the constitutional requirement and the easiest thing to violate silently.

### Estimation

- Input estimate is non-zero and monotonic in content length.
- Reservation covers input + configured output assumption.
- Reconciliation supersedes the estimate; `None` leaves it standing.
- Reconciliation is idempotent under repeated calls.

### `/v1/models`

- Shape matches the OpenAI model object; ids come from configuration.
- A cooling model is **still listed**.

#### Streaming

- Chunk translation for both adapters, including the terminating `[DONE]`.
- Fail after first chunk terminates and **never dispatches to a second provider**.
- Fail before first chunk *does* retry within budget — the distinction that makes
  fail-fast precise.
- An interrupted stream reconciles; no reservation leaks.
- A non-streaming adapter raises rather than returning an empty iterator.

### Privacy and manual verification

- A test asserting no key material and no payload text reaches any persisted table,
  across a full request lifecycle.
- Manual matrix — success, 429, 4xx, 5xx, timeout, mid-stream interruption, and
  post-restart behaviour, recorded per `spec.md`.

## Risks / Tradeoffs

### The flush interval is a real accuracy loss

A `SIGKILL` loses up to one interval of counters. Accepted rather than eliminated:
eliminating it requires a write per request, which is the latency the interval
exists to avoid. Stated as a bound so it can be reasoned about, not discovered.

### Persistent counters accumulate drift

M1's console cross-check found the router counting one more dispatch than the
provider billed. Persisted counters make such differences accumulate in a file that
looks authoritative. **Re-run the cross-check once counters are durable** — it is
the only independent check available and the cheapest way to catch a drifting
estimator.

### The estimator is a heuristic and will be wrong on some workloads

`chars / 4` is calibrated for English prose. Code, JSON and non-Latin scripts
diverge, sometimes substantially. The safety margin absorbs ordinary error; the
mitigation for systematic error is that provider-reported usage corrects it at
reconcile time, and that persistent counters make the divergence visible.

### Streaming multiplies exposure

M1 measured 2 transient faults in 104 unary dispatches. A stream holds the
connection open longer, so the same fault rate produces more interruptions — and
interruption after the first chunk is not recoverable by retry. **Streamed success
rate should be measured separately**, not assumed equal to the unary figure.

### Streaming interacts with the quota reservation lifecycle

A long stream holds a reservation far longer than a unary request, so in-flight
capacity is held longer. A single-user router will not notice; the design should
not become the reason it would.

### `entries` as JSON trades queryability for write cost

A row per reservation would be queryable but would multiply writes and rows. Since
the primary consumer is "restore and continue", not "query the last hour", the
trade favours the cheaper shape. Recorded because it would need revisiting if an
operator-facing usage report is ever built.

The list is bounded in practice by the dimension's own limit: a window that has
exhausted its allowance refuses further requests rather than recording them, so
a daily window holds at most a day of requests.

### M2 may not move the success rate

Persistence prevents overshoot after restart — a failure M1 never exercised. It
does not improve transient-fault handling, which retry already covers. **Any
expectation that M2 lifts ≥80% should be corrected now**, before it becomes a
disappointment attributed to the implementation.

## Architectural Decisions

| ADR | Decision | Why it needs recording |
| :--- | :--- | :--- |
| [ADR-0005](../../docs/adr/0005-persisted-quota-state-durability.md) | SQLite persistence with asymmetric durability — cooldowns write-through, counters batched | The write policy *is* the design. Reverses M1's stated reason for deferring, so the reasoning must survive |
| [ADR-0006](../../docs/adr/0006-token-estimation-and-reconciliation.md) | Separate input/output estimation; explicit output assumption; reconcile-what-was-reserved | Estimation error is being accepted deliberately, and an accepted error needs a number attached or it becomes unbounded |
| [ADR-0007](../../docs/adr/0007-streaming-fail-fast.md) | Fail-fast after the first delivered chunk; no cross-provider replay | A client-visible correctness/availability trade. Likely to be questioned, and the ROADMAP only states it in one line |
