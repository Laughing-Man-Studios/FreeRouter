# Implementation Plan: M5 Adaptive Router

**Feature ID:** `006-adaptive-router`
**Companion documents:** `spec.md` (behavioral contract), `tasks.md` (batch breakdown)
**ADRs:** [ADR-0012](../../docs/adr/0012-observed-telemetry-and-adaptive-reliability.md) ·
[ADR-0013](../../docs/adr/0013-offline-replay-for-adaptive-policy-evaluation.md)

## Technical Approach

M5 adds a feedback loop around the M4 ranking without changing what the ranking is
for:

```text
   dispatch ──▶ observation ──▶ EWMA estimates ──▶ multiplier ──▶ free ranking
       │              │                                  │
       │              └──▶ bounded history ──▶ replay ──┘
       │
       └── explicit requests are observed but never re-ranked
```

The loop has three separable pieces, built in this order:

1. **Observe and persist.** Record one metadata-only observation per dispatched
   request, update in-memory estimates, persist aggregates and a bounded history.
   No routing behaviour changes.
2. **Score.** Read estimates in `score_model`, substitute observed latency after the
   sample floor, apply the bounded reliability multiplier, and extend the decision
   record.
3. **Evaluate.** Replay the retained observations through candidate estimator and
   weight settings, offline, and report what would have changed.

The order is deliberate. Batches 1 and 2 can be verified independently: Batch 1 by
inspecting the database after a request, Batch 2 by driving `score_model` with
synthetic estimates. Batch 3 is only meaningful once there is real history to
replay, which is why it follows rather than leads.

### Part 1: the observation is taken where the outcome is known

There are two dispatch paths — unary (`core/dispatch.py`) and streaming
(`api/stream_dispatch.py`) — and they are separate by design (M2's A2). M5 does not
merge them; it gives each path one call into the same recorder.

**Unary.** `Dispatcher.dispatch` already has the terminal outcome in one place: the
successful `DispatchOutcome`, or the exception that leaves the loop. A timer starts
at entry and one observation is recorded in a `finally` block. Internal retries and
key failovers are **not** separate observations: the client experienced one request,
and the model either served it or did not. The attempt count stays in the existing
logs.

**Streaming.** `start_stream` knows the pre-delivery failures (the request never
reached the client) and returns a `PreparedStream` whose consumption settles the
reservation. The route consumes the iterator, so the terminal observation is
recorded there, once, after the iterator finishes or raises:

- a clean terminal chunk → `success` or a structural failure;
- an exception before the first chunk → classified from the exception, as unary;
- the client disconnecting before the terminal chunk → `client_aborted`, not a
  provider failure.

To keep the classification in one place, `core/telemetry.py` exposes
`classify_failure(exc) -> Outcome` and `structural_validity(request, response)`;
both paths call them. The recorder is injected into the dispatcher at startup, the
same way the quota tracker and cooldown manager are, so no global state is added.

**Why not record inside the adapters.** An adapter knows one HTTP call, not the
request outcome; it cannot see retries, capacity gates or client aborts, and putting
telemetry there would make every adapter responsible for a cross-cutting concern.

### Part 2: estimates are pure arithmetic over persisted state

The EWMA is a small, pure function, which is what makes it testable without a
database or a clock:

```python
# telemetry/estimates.py


def decay(previous: float, observation: float, elapsed: float, half_life: float) -> float:
    alpha = 1.0 - 0.5 ** (max(elapsed, 0.0) / half_life)
    return alpha * observation + (1.0 - alpha) * previous
```

The recorder holds one `EstimateState` per `(provider, model_id)` in a dictionary.
Each state carries the EWMA value and sample count for four metrics: total latency,
completion success, tool-call validity and quota prediction error. A missing metric
is `None`, not zero, because zero is a meaningful latency and a meaningful success
rate; conflating "no data" with "bad data" is the defect this milestone exists to
avoid.

**Sample counts are per metric, not per model.** A model may have fifty latency
observations and two tool-call observations; the latency floor can be met while the
tool floor is not. The decision record reports each count so the asymmetry is
visible.

**Timestamps.** The observation carries a wall-clock UTC timestamp, because it is
persisted and replayed. The elapsed time for the EWMA is computed from the previous
observation's timestamp in the same wall-clock domain, clamped at zero. The request
path's latency measurement uses `time.perf_counter()`, which is monotonic; the two
clocks serve different purposes and are not mixed.

### Part 3: scoring reads estimates through one optional lookup

`score_model` stays a pure function. It gains one optional argument:

```python
def score_model(
    model: ModelConfig,
    policy: RoutingConfig,
    estimates: ObservedEstimate | None = None,
) -> ModelScore | None: ...
```

`ObservedEstimate` is a frozen value object built by the recorder. `None` means
telemetry is disabled or the model has never been observed; both produce exactly the
M4 score. `resolve_request` and `select_model` take an optional
`estimate_lookup: Callable[[str, str], ObservedEstimate | None]`, which the route
binds to the recorder. This keeps `core/routing.py` free of persistence imports and
makes the scoring tests a table of inputs and expected scores.

The multiplier is applied to the **total**, not to each contribution:

```text
base = sum(weight_i * component_i) / sum(weights)
final = base * (floor + (1 - floor) * reliability)
```

Applying it per contribution would make the logged contributions fail to sum to the
score and would let an operator mistake the multiplier for a fourth weight.
`ModelScore` therefore grows `base`, `multiplier` and `observed` fields while
`total` becomes the final score.

### Part 4: latency substitution is a source switch, not a second formula

The latency component keeps M4's absolute ceiling. The only change is which
`latency_ms` feeds it:

```python
latency_ms = estimate.latency_ms if estimate.has_enough_latency else model.routing.latency_ms
source = "observed" if estimate.has_enough_latency else "prior"
```

The decision record carries `latency_source` and `latency_samples` beside the value,
so an operator can tell whether a surprising score came from the provider or from
the configuration. A model whose observed latency exceeds the ceiling scores `0` and
stays eligible, exactly as in M4.

### Part 5: persistence is asynchronous, bounded and non-fatal

The request path does one thing synchronously: update the in-memory estimate and put
an observation on a bounded `asyncio.Queue`. A single background task drains the
queue:

```text
observe() ──▶ update EstimateState (sync, µs)
           └─▶ queue.put_nowait(observation)
                     │
                     ▼
              writer task ──▶ one transaction:
                               INSERT observation
                               UPSERT aggregate
                               periodic retention DELETE
```

- The queue is bounded (`1024`). `put_nowait` never blocks; a full queue drops the
  newest observation and increments a counter. The counter is reported in the
  milestone report and logged at `WARNING` on the first drop of a run.
- Each drain writes the observation and the aggregate in one transaction, so the two
  cannot disagree about the same attempt.
- A failed write is caught, logged once per transition, and does not stop the
  writer. The in-memory estimate remains authoritative until the next successful
  write; the next write carries the current aggregate, so a transient failure
  self-heals rather than requiring a replay.
- `close()` drains with a timeout (the existing shutdown budget) and then cancels the
  task. An observation lost at shutdown is a finding, not a crash.

**Restore.** At startup, before traffic is served, the recorder loads
`telemetry_aggregates` into memory. It does not load observation rows. A corrupt
aggregate row is skipped with a warning; that model starts neutral and the rest of
the deployment is unaffected. This is intentionally more forgiving than quota
restore, because an estimate is not a safety invariant.

### Part 6: the decision snapshot is what makes replay possible

The observation row stores the decision snapshot for `free` requests:

```json
{
  "weights": {"quality": 0.6, "tool_support": 0.3, "latency": 0.1},
  "candidates": [
    {"model": "google/gemini-3.5-flash-lite",
     "components": {"quality": 0.75, "tool_support": 1.0, "latency": 0.76}}
  ],
  "selected": "google/gemini-3.5-flash-lite"
}
```

It stores **components**, not payload: the normalized inputs to the M4 score. Replay
can therefore recompute a base score under different weights, apply a different
multiplier, and re-rank the same candidate set. It cannot recompute a different
latency prior or a different EWMA formula, because those are already folded into the
stored components; the replay report states that boundary.

Explicit-model observations store no decision snapshot. They are still observations
of the model's behaviour, but there was no ranking to replay.

### Part 7: replay is a development tool, not a service

`telemetry/replay.py` exposes a pure function and a `python -m` entry point:

```python
def replay(
    observations: Iterable[Observation],
    *,
    half_life_seconds: float,
    min_samples: int,
    reliability_floor: float,
    weights: RoutingWeights,
) -> ReplayReport: ...
```

It sorts observations by timestamp, maintains the same EWMA state machine as the
recorder, and at each `free` observation recomputes the multiplier and ranking for
the stored candidate set. It reports:

- final estimates per model and the multiplier range over the window;
- selections that would have changed, with the model that would have won;
- the longest continuous period a model would have stayed demoted;
- recovery time after the last observed failure burst;
- quota-error and tool-validity summaries;
- the settings used and the stated limitation.

The entry point opens the SQLite file read-only, accepts the estimator settings as
flags, and prints JSON. It makes no HTTP call. A missing or empty database exits
non-zero with a message that names the path; it does not silently report an empty
success.

The shipped defaults are evaluated in Batch 3 against the observations collected in
Batch 2's live verification. If the defaults look wrong, Batch 3 changes the
configuration, not the formula, and records the evidence.

### Part 8: the explicit path stays untouched

Estimates are only read inside `score_model`, which is only called for `free`.
Explicit requests keep M1–M4 behaviour exactly: same model, same failover, same
errors, no `routing_decision`. They are observed because their outcomes are evidence
about the model, not because they are ranked.

The strongest regression test is the existing suite with telemetry enabled: every
explicit-model test must pass unchanged, and the M4 `free` tests must pass with
neutral estimates.

## Components Affected

| Component | Change |
| :--- | :--- |
| `core/config.py` | `TelemetryConfig` with defaults and validation; `Config.telemetry` |
| `core/telemetry.py` | **New.** `Observation`, `Outcome`, `ObservedEstimate`, `EstimateState`, `decay`, `classify_failure`, `structural_validity`, `TelemetryRecorder` |
| `telemetry/persistence.py` | **New.** Aggregate upsert, observation insert, retention, restore |
| `telemetry/replay.py` | **New.** Pure replay and `python -m` entry point |
| `db/schema.py` | `telemetry_aggregates`, `telemetry_observations` |
| `migrations/versions/0004_telemetry.py` | **New.** Creates both tables and indexes |
| `core/routing.py` | `score_model` accepts `ObservedEstimate`; `ModelScore` gains `base`/`multiplier`/`observed`; decision record gains `estimates` and per-candidate `observed` |
| `core/dispatch.py` | Optional recorder; timer and terminal observation around the retry loop |
| `api/stream_dispatch.py` | Pre-delivery failure observations; terminal observation hook for streamed requests |
| `api/routes.py` | Constructs the recorder, binds the estimate lookup, records stream outcomes, logs the extended decision |
| `api/streaming.py` | Reassembles tool-call arguments and exposes structural validity to the recorder |
| `core/logging.py` | Allowlist additions for the decision record's new fields |
| `core/normalization.py` | **Unchanged** |
| `quota/`, `keys/` | **Unchanged** |
| `config.yaml` | `telemetry` block with shipped defaults |

## Interfaces

```python
# core/telemetry.py


class Outcome(StrEnum):
    SUCCESS = "success"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    REQUEST_ERROR = "request_error"
    CLIENT_ABORTED = "client_aborted"


@dataclass(frozen=True, slots=True)
class ObservedEstimate:
    latency_ms: float | None
    success_rate: float | None
    tool_success_rate: float | None
    quota_error_ratio: float | None
    latency_samples: int
    outcome_samples: int
    tool_samples: int
    quota_samples: int

    @property
    def has_reliability(self) -> bool: ...
    @property
    def has_latency(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Observation:
    provider: str
    model_id: str
    recorded_at: datetime
    outcome: Outcome
    latency_ms: float
    ttfb_ms: float | None
    http_status: int | None
    tool_requested: bool
    tool_valid: bool | None
    structured_requested: bool
    structured_valid: bool | None
    reserved_tokens: int | None
    actual_tokens: int | None
    decision: DecisionSnapshot | None


class TelemetryRecorder:
    def observe(self, observation: Observation) -> None: ...
    def estimates_for(self, provider: str, model_id: str) -> ObservedEstimate | None: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
```

```python
# core/routing.py (additions)


class ModelScore(BaseModel):
    total: float  # final score after the multiplier
    base: float  # M4 weighted score
    multiplier: float  # reliability multiplier, 1.0 when neutral
    contributions: dict[str, float]
    components: dict[str, float]
    observed: ObservedEstimate | None


def score_model(
    model: ModelConfig,
    policy: RoutingConfig,
    estimates: ObservedEstimate | None = None,
) -> ModelScore | None: ...
```

```python
# core/config.py (addition)


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    half_life_seconds: float = Field(default=3600.0, gt=0)
    min_samples: int = Field(default=5, ge=1)
    reliability_floor: float = Field(default=0.5, ge=0, le=1)
    retention_days: int = Field(default=30, ge=1)
    max_observations: int = Field(default=10_000, ge=1)
```

## Data / Persistence Changes

Two new tables in migration `0004`:

```text
telemetry_aggregates
  provider TEXT, model_id TEXT          PRIMARY KEY (provider, model_id)
  latency_ms REAL, ttfb_ms REAL
  success_rate REAL, tool_success_rate REAL, quota_error_ratio REAL
  latency_samples INT, outcome_samples INT, tool_samples INT, quota_samples INT
  updated_at TEXT NOT NULL

telemetry_observations
  id INTEGER PRIMARY KEY AUTOINCREMENT
  created_at TEXT NOT NULL              -- UTC ISO 8601
  provider TEXT NOT NULL, model_id TEXT NOT NULL
  outcome TEXT NOT NULL
  http_status INT, latency_ms REAL, ttfb_ms REAL
  tool_requested BOOLEAN NOT NULL, tool_valid BOOLEAN
  structured_requested BOOLEAN NOT NULL, structured_valid BOOLEAN
  reserved_tokens INT, actual_tokens INT
  decision JSON
  INDEX (created_at), INDEX (provider, model_id, created_at)
```

No payload column exists, so the privacy guarantee is structural. The aggregate row
is one per configured model; the observation table is bounded by retention. The
migration is additive and has a downgrade that drops only the two new tables.

## External Integrations

**None.** M5 adds no provider, no discovery call, no network client, and no external
service. Replay reads a local file. The dependency surface is unchanged.

## Error Handling

No new client-facing error codes. The failure policy is:

| Failure | Handling |
| :--- | :--- |
| Queue full | drop newest, increment counter, first drop logs `WARNING` |
| Aggregate/observation write fails | log once per transition, keep in-memory estimate, retry on the next observation |
| Corrupt aggregate at startup | skip the row, warn with provider/model, start that model neutral |
| Retention delete fails | warn, continue; retry on the next interval |
| Replay database missing/empty | exit non-zero with the path in the message |
| Replay row malformed | skip the row, count it, report the count |

A telemetry failure must not alter a response status, body or selected model. This is
tested by injecting a failing writer and asserting the response is unchanged.

## Testing Strategy

### Batch 1 — observation and persistence

- `decay()` is table-driven: half-life, elapsed time, clamping at zero, first value,
  and a long gap that makes the new observation dominant.
- The sample floor is per metric: latency can be ready while reliability is not.
- An observation round-trips through SQLite and restores the same estimates.
- Retention deletes by age and by count, and never touches aggregates.
- A failing writer leaves the in-memory estimate correct and the response untouched.
- A full queue drops without blocking; the drop is counted.
- Structural validity: valid and invalid tool arguments, valid and invalid JSON
  content, empty completion, streamed call reassembly.

### Batch 2 — scoring and wiring

- `score_model` with `estimates=None` produces exactly the M4 score for the same
  configuration, asserted against the existing M4 expectations.
- The multiplier is bounded by `floor` and `1.0`; a success rate of `0` yields
  `floor`, not exclusion.
- Observed latency substitutes only after the floor; the record names the source.
- A higher-base model with a poor estimate loses to a lower-base healthy model.
- Explicit requests are unchanged with estimates present: same model, no
  `routing_decision`.
- One `routing_decision` per `free` request; `score == sum(contributions) *
  multiplier`; no content in the payload.
- Unary and streaming paths produce the same observation outcome for the same
  synthetic response, including a client abort.

### Batch 3 — replay

- Replay of a hand-built observation sequence reproduces the recorder's estimates
  exactly.
- A floor change alters the reported selections; a half-life change alters recovery
  time.
- Replay makes no HTTP call: the test installs a transport that fails on use.
- The report states its limitation and includes the settings used.
- The shipped defaults are evaluated and the result is recorded in `tasks.md`.

### Batch 4 — verification

- Scenario-by-scenario tests from `spec.md` § *Acceptance Scenarios*, named after the
  scenario.
- Live run: real requests through the router with telemetry enabled; a real `429`
  observed and confirmed not to lower reliability; aggregates inspected in SQLite;
  a restart confirms restore.
- Deterministic fake-provider run for the failure and recovery paths, so the
  verification does not depend on a provider cooperating.
- `make check` and `make test-hermetic` green; markdownlint clean.

## Risks / Tradeoffs

### Free-tier traffic is a small sample

A personal router may make a few dozen requests a day. With `min_samples: 5` and a
one-hour half-life, estimates can be dominated by a single burst. The floor prevents
the worst of it, and the half-life is configurable, but the honest statement is that
M5's estimates are useful at the project's traffic scale only for gross differences:
a model that times out repeatedly versus one that completes. Fine-grained ranking
remains the operator's priors. The milestone report must say this rather than imply
more precision than the data supports.

### Structural validity is not quality

A model can return valid JSON that is wrong. M5 will not notice. This is a deliberate
boundary (S5), and it is the main reason the word "quality" stays attached to the
operator prior. A future feedback signal is the honest way to go further.

### Persistence is weaker than quota persistence

A crash can lose queued observations and the last aggregate update. Estimates then
regress to the last successful write. This is acceptable because an estimate is not a
safety invariant, but it means the estimates are approximate across restarts. The
alternative — a synchronous write on the request path — trades a real latency cost
for a small accuracy gain, which is the wrong trade for this milestone.

### The multiplier can make a good model lose for a while

A burst of provider faults can demote a model that is normally best, and the EWMA
recovers slowly by design. The floor bounds the damage and the log shows the reason,
but the operator may see a lower-quality model win for a window. The replay harness
exists so that window can be measured before the defaults are trusted.

### Replay cannot answer every policy question

Stored components freeze the M4 inputs. Replay can vary weights, the multiplier and
the estimator settings, but not the latency prior or the scoring formula. A question
outside that boundary needs a synthetic harness. Saying so is part of the tool's
contract, not a caveat added later.

### Streaming observation is the riskiest wiring

The streaming path is a separate dispatch implementation, and the terminal outcome is
known only after the client consumes the iterator. A missed terminal record or a
double record is the most likely defect in the milestone. It gets a dedicated
unary-versus-streaming parity test and a client-abort test, and Batch 4's live run
uses streaming because that is the workload the project exists for.

## Architectural Decisions

- **ADR-0012** — observed telemetry is metadata-only, EWMA-based, persisted as
  aggregates plus bounded observations, and enters scoring as a bounded multiplier.
- **ADR-0013** — adaptive policies are evaluated by offline replay of recorded
  decisions; no shadow traffic, no bandits, no quality inference.

Decisions S4 (latency source), S5 (quality boundary), S6 (quota error recorded only),
S8 (presence switch) and S10 (provider/model unit) are recorded in `spec.md`; they
are contract placements with one defensible answer, not architecture with alternatives
worth re-litigating.

## Sequencing

| Batch | Delivers | Changes request handling? |
| :--- | :--- | :--- |
| 0 | These documents and ADR-0012/0013 | no |
| 1 | Observation model, schema, recorder, EWMA, retention, restore | no (records only) |
| 2 | Estimates in scoring, observed latency, multiplier, decision record | yes — `free` ranking uses estimates |
| 3 | Replay harness and default-policy evaluation | no |
| 4 | Exit-criteria verification, live run, milestone report | no |

Batch 1 must precede 2 because scoring has nothing to read without it. Batch 2 must
precede 3 because replay is only meaningful against real observations. Batch 3
precedes 4 because its findings may change the shipped defaults that Batch 4
verifies.

No batch changes the token estimator, the quota tracker, the cooldown manager or the
breaker. If a batch needs to, the design is wrong and that is a finding to record.
