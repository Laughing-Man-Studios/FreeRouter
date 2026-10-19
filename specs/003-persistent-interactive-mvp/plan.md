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
tracker behind an interface that ADR-0004 deliberately built for exactly this
purpose. Choosing Redis now would contradict ROADMAP §3.14's instruction to defer
that decision until real usage is known, and would violate the single-implementation
constraint.

### The quota interface was designed to be swapped

ADR-0004's central claim was that *"the interface, not the storage, is the durable
decision."* `QuotaTracker` already exposes `reserve`, `reconcile`, `available`,
`used` and `dimensions_for`, and its internals are already private (`_Window`,
`_Entry`, `_pending`).

So the implementation is a **new class satisfying the same surface, constructed at
startup instead of `QuotaTracker`**, with the in-memory one retained for tests and
as the degradation path. This is the payoff of having deferred the decision rather
than guessed at it.

What must **not** happen: the route handler, dispatcher, key manager or adapters
change to accommodate persistence. If they do, the interface was not as general as
ADR-0004 claimed, and that is a finding worth recording rather than working around.

### Durability is asymmetric, and the write path stays off the dispatch path

The two kinds of state have opposite requirements:

| State | If lost on restart | Write policy |
| :--- | :--- | :--- |
| `cooldown_until`, breaker status | Router re-contacts a provider that just refused it — the exact behaviour §8.3 forbids | **Write-through, immediately** |
| Request/token counters | Router loses a few seconds of accounting; may re-contact a provider it had just exhausted, once | **Batched**, bounded flush interval |
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

with `assumed_output` configurable per deployment. **The output assumption is
explicit configuration rather than a constant**, because it is a policy choice about
how much headroom to buy, and a hidden constant would make that policy invisible
and unchangeable without a code edit.

Provider-reported usage, where available, supersedes the estimate at reconcile time
— which the code already supports (`_Entry` carries `actual`, and
`reconcile(None)` deliberately stands the estimate down rather than releasing it).

### `/v1/models` is a projection of configuration

The response is generated from `config.models` — the same list that already drives
routing. There is no discovery call, no cache to invalidate, and no second source
of truth. Availability is carried as a *separate* field precisely so that the
listing does not change as provider state moves.

**A configured model is always listed, including while in cooldown.** Hiding it
would make a client's model list oscillate with transient provider state.

### Streaming extends the adapter protocol, not the router

`chat_completion` is currently a request/response call. Streaming adds a sibling
method rather than a `stream: bool` parameter, because the two have genuinely
different contracts: one returns a complete body, the other returns a sequence that
can fail partway. A flag would force every caller to handle the union.

The route layer translates the provider's chunk sequence into SSE. **Fail-fast is
enforced at the point where the first chunk has been written to the client**, not
in the adapter — the adapter cannot know whether anything has been delivered.

## Components Affected

| Component | Change |
| :--- | :--- |
| `db/schema.py` | New tables: quota windows, cooldowns, key state, model status |
| `db/migrations/` | One Alembic revision (0002) adding the above |
| `quota/store.py` | **New.** SQLite-backed `QuotaTracker` satisfying the M1 surface |
| `quota/estimation.py` | **New.** Input/output estimation with configurable output assumption |
| `quota/tracker.py` | Extracted `QuotaTrackerProtocol` so both implementations are checked against one contract |
| `quota/cooldown.py` | Write-through persistence on transition |
| `quota/breaker.py` | Persist degraded/unavailable status |
| `keys/manager.py` | Restore LRU ordinals; write `last_used` through the store |
| `api/models_route.py` | **New.** `GET /v1/models` |
| `api/streaming.py` | **New.** SSE writer, provider-chunk → OpenAI-chunk translation |
| `api/routes.py` | Branch on `stream`; unchanged for the unary path |
| `providers/base.py` | `stream_chat_completion` on the adapter protocol |
| `providers/{google,mistral}/adapter.py` | Implement streaming for both |
| `api/main.py` | Restore state before serving; construct the SQLite tracker |

**Explicitly unchanged:** `core/normalization.py`, `core/config.py` (beyond the new
output-assumption field), `core/dispatch.py` (except streaming dispatch), and the
adapter/request translation logic already proven by M1's 100/100 measurement.

## Interfaces

### The tracker contract

Both implementations must satisfy one protocol, extracted from `QuotaTracker`:

```python
class QuotaTrackerProtocol(Protocol):
    async def restore(self) -> None: ...

    def dimensions_for(self, provider: str, model_id: str) -> tuple[QuotaDimension, ...]: ...

    def available(self, provider: str, model_id: str) -> bool: ...

    async def reserve(
        self, provider: str, model_id: str, estimated_tokens: int
    ) -> Reservation | None: ...

    async def reconcile(self, reservation: Reservation, actual_tokens: int | None) -> None: ...

    def used(self, provider: str, model_id: str, resource: DimensionResource) -> float: ...
```

`reserve`/`reconcile` **keep their signatures**, including `Reservation | None` and
`int | None`. Changing them would ripple into the dispatcher for no benefit, and
ADR-0004's interface is the thing being cashed in here — not revised.

`restore()` is the one addition. It is called once during lifespan, before the
listener accepts traffic, and is absent from the in-memory implementation's
behaviour (a no-op) so the dispatcher does not need to know which it holds.

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

**Fail-fast boundary:** once any chunk has been written to the client, a provider
error terminates the connection. Before that point, an error is an ordinary fault
and may be retried within the existing budget.

## Data / Persistence Changes

New tables, all keyed by `(provider, model_id)` unless noted:

```text
quota_windows
  provider, model_id            (composite key)
  resource, window_seconds      (composite key)
  reset_kind                    rolling | fixed
  reset_at                      fixed windows only
  bucket_used                   JSON map of bucket ordinal -> amount
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
```

Four decisions are worth stating because they are not obvious from the schema:

- **`bucket_used` is a JSON map keyed by bucket ordinal**, mirroring `_Window`'s
  in-memory buckets. A relational row per bucket would multiply rows unboundedly
  on a rolling window and make the flush far more expensive than the state it
  holds.
- **`reset_at` is stored for fixed windows only.** Google's RPD resets at midnight
  Pacific; a rolling window has no such instant. Modelling both as one nullable
  column keeps the dimension model uniform.
- **`suspended` is stored but not restored.** Credential suspension is M1's
  conservative response to an auth failure. A credential that was revoked an hour
  ago may have been fixed since; resurrecting the suspension would refuse a
  working key. Cooldowns are time-bounded and so are restorable; suspensions are
  event-shaped and so are not.
- **`request_logs` is left alone.** It is M0's telemetry table and is not a quota
  source. Reading quota state back from request logs would require reconstructing
  rolling windows from individual rows on every boot — correct but far more
  expensive, and it would make a corrupt log row fatal to startup.

**No payload columns exist in any of these tables**, which is what makes a state
dump safe to paste into an issue.

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

### `bucket_used` as JSON trades queryability for write cost

Per-bucket rows would be queryable but would multiply writes. Since the primary
consumer is "restore and continue", not "query the last hour", the trade favours
the cheap write. Recorded because it would need revisiting if an operator-facing
usage report is ever built.

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
