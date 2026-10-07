# Implementation Plan: M1 MVP OpenAI-Compatible Proxy

## Technical Approach

M1 adds a **key-and-quota layer between the route handler and the provider adapter**, and leaves both ends of that seam untouched.

```text
Route handler
    ↓  unchanged from M0
Key manager  ── selects an eligible key (LRU) under an in-process guard
    ↓
Quota tracker ── reserve before dispatch, reconcile after
    ↓  NEW SEAM
ProviderAdapter protocol (unchanged)
    ├── GoogleAdapter   (exists)
    └── MistralAdapter  (new)
```

The load-bearing decision is that **the adapter interface does not change**. M0's adapter already takes the provider-native model id per call and returns a `NormalizedResponse`. In M1 the adapter additionally needs the *key* to authenticate with, so the protocol gains one parameter. Nothing else about it moves, and the Mistral adapter is added without touching the ingress, the routing core, or the Google adapter.

### Why the seam sits here

CONSTITUTION §4 requires architectural isolation, and ROADMAP §11 identifies key selection and quota accounting as concerns that must stay separable from request compatibility and provider translation. Placing the key/quota layer between the route and the adapter is the narrowest seam that achieves that. It also means the M1 work does not disturb the M0 normalization boundary, which is the most heavily tested part of the system.

### Configuration is the policy surface

Every value that could reasonably differ per provider or per model — quota limits, cooldown durations, retry budgets, pacing intervals, and the key cap — is a **configuration value**. The routing engine contains no provider-specific constants. This is required by ROADMAP §2 ("Exact provider quota values should be treated as configuration data and verified against current provider documentation rather than hard-coded assumptions") and keeps adding a provider to a configuration change rather than a code change.

### In-memory state, deliberately

Quota counters and cooldown timestamps are **in-memory** for M1 (recorded deviation, see spec § *Recorded Deviations*). This keeps the dispatch path free of database round-trips, which matters because CONSTITUTION §8.3 forbids the database blocking the event loop. The M0 database infrastructure is retained — the `request_logs` table exists and is unused — so M2 can add persistence without reworking the seam.

The cost is real and is recorded: after a restart the router forgets daily counts and cooldowns.

### Retry is failure recovery, not rate-limit laundering

A `429` is a signal to *stop* issuing to that key, not a transient blip to retry immediately. Retrying into a rate limit would defeat CONSTITUTION §6.1's Conservative Compliance Mode, which exists to make traffic look human. The implementation therefore distinguishes:

- **`429`** → mark the key cooling; move to a *different* key; if none, stop and return.
- **Transient `5xx` / timeout** → retry within budget; these are faults, not signals about rate.
- **`400` / `401`** → no retry. The request cannot succeed unchanged.

## Components Affected

```text
src/free_router/
  ├── api/
  │   └── routes.py            # dispatch now resolves a key before calling the adapter
  ├── core/
  │   ├── config.py            # key-pool schema + quota/pacing/retry configuration
  │   ├── exceptions.py        # new: capacity/rate-limit exceptions
  │   └── models.py            # NEW: provider/model identity registry types
  ├── keys/
  │   ├── manager.py           # NEW: pool, LRU selection, eligibility, in-process guard
  │   └── state.py             # NEW: per-key usage and cooldown state (in-memory)
  ├── quota/
  │   ├── tracker.py           # NEW: Level 1 counters, estimate/reserve/reconcile
  │   └── dimensions.py        # NEW: generic dimension representation
  ├── providers/
  │   ├── base.py              # protocol gains a key parameter
  │   ├── google/adapter.py    # authenticate with a pooled key
  │   └── mistral/
  │       └── adapter.py       # NEW
  └── pacing.py                # NEW: conditional minimum-interval enforcement (Issue #51)
```

`core/models.py` and `keys/`, `quota/` are new packages. Each exists because a distinct concern needs a narrow interface; none is speculative — every one is exercised by this milestone.

## Interfaces

### Key identity

```python
@dataclass(frozen=True)
class KeyRef:
    """A single credential slot. Carries an alias, never key material."""

    alias: str  # safe to log
    provider: str
    secret: SecretStr  # never logged, never serialised
```

The alias is the **only** identifier that may reach a log line or the database.

### Key manager

```python
class KeyManager:
    def select(self, provider: str, model_id: str) -> KeyRef | None: ...
    def mark_used(self, key: KeyRef) -> None: ...
    def enter_cooldown(self, key: KeyRef, duration: float, reason: str) -> None: ...
    def is_eligible(self, key: KeyRef) -> bool: ...
```

`select` returns `None` rather than raising when nothing is eligible; the route handler translates that into a normalized capacity error. Selection and `mark_used` must be atomic with respect to each other, guarded by an `asyncio.Lock` held only for the duration of the in-memory state update — never across an `await` of provider I/O.

### Quota tracker

```python
class QuotaTracker:
    def reserve(self, provider: str, model_id: str, estimated_tokens: int) -> Reservation: ...
    def reconcile(self, reservation: Reservation, actual_tokens: int | None) -> None: ...
    def available(self, provider: str, model_id: str) -> bool: ...
```

Dimensions are declared as configuration, e.g. requests-per-window, tokens-per-window, daily requests. Adding a dimension must not require changing the tracker.

### Adapter protocol (revised)

```python
class ProviderAdapter(Protocol):
    async def chat_completion(
        self,
        request: NormalizedRequest,
        provider_model_id: str,
        key: KeyRef,
    ) -> NormalizedResponse: ...
```

The single added parameter is the credential. The response type is unchanged, so the normalization boundary is preserved.

### New exceptions

| Exception | Status | Meaning |
| --- | --- | --- |
| `NoAvailableCapacityError` | 503 | No eligible key or all free capacity exhausted |
| `RetriesExhaustedError` | 503 | Retry budget spent without success |
| `ProviderRateLimitedError` | 429 | `429` and no alternative key |

These are added to the `PROVIDER_ERROR_STATUS_MAP` established in M0, with the same most-specific-first ordering rule that caused the earlier 400→502 bug.

## Data / Persistence Changes

**No schema change in M1.** Quota and cooldown state is in-memory; the existing `models` and `request_logs` tables are unchanged.

`request_logs` gains no columns. It continues to hold operational metadata only, with no prompt or completion columns (CONSTITUTION §9.1).

If M2 later persists quota state, it does so in new tables behind the existing `QuotaTracker` interface, which is why that interface is defined now.

## External Integrations

- **Google Gemini** — `generateContent`, authenticated per-dispatch with a pooled key.
- **Mistral** — a new adapter. Its endpoint, auth scheme, and error taxonomy must be verified against current Mistral documentation during implementation, not recalled. In particular: whether an invalid key returns `401` or something else. Google's behaviour taught us not to assume.
- **Both** — free-tier quota values come from Issue #51's research.

## Error Handling

The M0 error envelope is retained. Two additions matter:

- **`503` becomes a first-class outcome.** Exhausting free capacity is an expected state, not a fault. Previously indistinguishable from a provider error.
- **Failover is bounded and observable.** Every attempt increments `attempt` in the log; a failover that exhausts its budget returns a normalized error rather than retrying indefinitely.

Failover never crosses models for an explicitly requested model (CONSTITUTION §7.2).

## Testing Strategy

The M0 suite is `respx`-based with no provider access. M1 extends the same approach and adds the fake-provider behaviours SDD_WORKFLOW §19 calls for at this stage.

| Area | Approach |
| --- | --- |
| LRU selection | Pure unit tests with a fake clock; deterministic, no I/O |
| Cooldown eligibility | Unit tests; assert a cooling key is never selected |
| Quota estimate/reserve/reconcile | Unit tests; assert no over-reservation under concurrency |
| Concurrency | Async tests racing N requests against a small key pool |
| Retry/failover | `respx` returning 429/500/timeouts in sequence |
| Mistral translation | `respx` against recorded fixture shapes |
| Pacing | Fake clock; assert intervals without real sleeping |
| Privacy | Assert no key material and no payload reach logs |

**Determinism rule:** no test may depend on wall-clock timing or real network access. LRU and cooldown use an injected clock; retry budgets use an injected attempt counter.

Manual verification against live providers is reserved for translation correctness and authentication, and is expected to be unreliable given the measured ~50% provider availability — which is itself a reason to prefer mocks.

## Risks / Tradeoffs

### Retry multiplies quota consumption

Retrying on failure increases request volume against a free tier that is already the bottleneck. Mitigations: a hard attempt budget, no retry into `429`, and pacing where §6.1 requires it. Residual risk accepted.

### The ≥80% target is a hypothesis, not a forecast

M0 measured ~50–60% with no retry. That 80% is reachable only if failures are recoverable and a second key exists. It is written as a target precisely so that measuring below it changes what M2 prioritises. If it lands at 65%, that is information, not failure.

### In-memory state resets on restart

Recorded deviation. A restart can overshoot a daily limit. M2 addresses it.

### Key pooling compliance

If Issue #51 concludes a provider prohibits pooling, the cap is 2 keys and the design must be written against the cap. Pacing may become mandatory. Both change the key manager's contract, which is why #51 blocks #8.

### LRU concentration can exhaust one key

LRU by definition concentrates. That is correct (CONSTITUTION §2.5 forbids even distribution as an anti-pattern), but it means quota exhaustion happens in bursts. The cooldown and counter logic must handle that without flapping.

## Architectural Decisions

1. **The adapter interface gains a key parameter and nothing else.** Keeps the normalization boundary intact and makes Mistral a configuration change.
2. **Key/quota layer sits between the route and the adapter.** The narrowest seam that satisfies CONSTITUTION §4 and ROADMAP §11.
3. **All provider-specific values are configuration.** Quota limits, cooldowns, pacing, budgets, and the key cap are never constants in the routing engine.
4. **State is in-memory for M1**, with the deviation recorded rather than absorbed. `QuotaTracker` is defined now so M2 can persist behind it.
5. **Retry recovers failures, never rate limits.** A direct consequence of §6.1 Conservative Compliance Mode.
6. **Failover stays within one model.** CONSTITUTION §7.2, and a consequence of not yet having capability filtering (M3).
7. **ADR-0003 defines the key-pool configuration mechanism.** ADR-0002 explicitly requires a deliberate, reviewed mechanism; the existing loader actively rejects the shape the ROADMAP shows.
