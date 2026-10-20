# M2: Persistent & Interactive MVP

**Feature ID:** `003-persistent-interactive-mvp`
**Milestone:** M2 — Persistent & Interactive MVP (Issue #11, milestone #3)
**Planning Issue:** #36

## Purpose

Make the M1 proxy practical for **repeated** local use rather than one-shot testing.

M1 shipped a router that measured 100/100 under paced, low-volume conditions well
inside quota. That result says the router is *correct while within limits*. It says
nothing about two situations M1 could not reach:

1. **The router forgets at boot.** Quota counters and cooldowns are in memory
   (ADR-0004), so a restart silently returns the router to believing it has full
   daily capacity. The §8.3 deviation that M1 closed *with* rather than resolved
   lands here.
2. **The router cannot be used interactively.** No `GET /v1/models` for client
   discovery, and no streaming — so a client that streams, or that enumerates
   models, cannot use it at all.

M2 closes the durability gap, adds model discovery and SSE streaming, and makes
token accounting real rather than notional.

## Scope

| Area | Roadmap | Issue |
| :--- | :--- | :--- |
| Local persistent quota/cooldown state | §3.14 | #12 |
| Token estimation before dispatch | §3.5 | #13 |
| `GET /v1/models` | §3.3 | #14 |
| SSE streaming + fail-fast | §3.11 | #15 |
| Improved provider error classification | §3.5, §3.10 | #12, #15 |

**In scope:** the persistence substrate, the restore semantics, the `/v1/models`
contract, the streaming contract including its failure policy, and a manual
integration-test matrix.

**The persistence implementation is already built.** M0 delivered async SQLite via
`aiosqlite`, WAL enforcement with a fail-fast check, and Alembic migrations applied
at startup. `request_logs` exists and is unused. M2's work is the *quota-state*
schema and the tracker behind the existing interface — not a new database.

## Requirements

### 1. Persisted State

Quota counters, cooldown state and provider availability **must** be persisted to
SQLite (CONSTITUTION §8.3), using the single existing local implementation. No
external or hosted persistence service is introduced (ROADMAP §3.14).

The persisted state must be sufficient to reconstruct:

```text
provider
model (provider-native id)
key alias reference        -- never key material
last request time
cooldown_until
request counts per window
estimated and actual token counts per window
error counts
status (healthy / degraded / unavailable)
window timestamps and reset behaviour
```

**Key material is never persisted.** Only the *alias* is stored, so state can be
reconciled against the environment without the database ever holding a secret
(CONSTITUTION §9.2). A database containing an alias but no secret is safe to
inspect, back up and diff.

**Durability is asymmetric, and deliberately so:**

- **Cooldown and availability transitions are written through immediately.**
  Forgetting a cooldown causes the router to re-contact a provider that just
  refused it — precisely the behaviour §8.3 exists to prevent.
- **Counters may be batched behind a bounded flush interval.** Losing the last
  few seconds of request counts is tolerable; paying a database round-trip on
  every request to prevent it is not.

The asymmetry is the design, not an optimisation. It is stated here because a
uniform "persist everything" rule would be simpler to describe and would put a
write on the latency-sensitive dispatch path for no safety gain.

**The counter flush interval is 20 seconds** (B2), and the requests within an
interval are coalesced into a single additive write. A `SIGKILL` therefore loses up
to 20 seconds of counters and nothing else — cooldowns are written through
immediately and survive an unclean shutdown regardless.

**The maximum cooldown is 24 hours, configurable** (B4). It bounds a pathological
lockout — a `cooldown_until` written by a clock that later jumps backwards — while
accommodating the longest legitimate provider hint, a daily cap resetting at
midnight. A backwards clock jump is better *detected* than patched, by comparing
persisted `cooldown_until` against the system clock at restore time; the cap is
the backstop, not the primary mechanism.

### 2. Restore on Restart

On startup the router must reconstruct its quota position from persisted state
**before serving any request**.

- **A cooldown whose `cooldown_until` has not passed must survive the restart.**
  The affected `(provider, model)` must be refused until the cooldown expires.
- **A cooldown whose `cooldown_until` has passed must not be restored.** It is
  expired, and retaining it would refuse capacity that is actually free.
- **A window whose reset instant has passed must not be restored.** For a rolling
  window, usage older than the window length is gone by definition. For a fixed
  daily window, the recorded reset instant is compared against the current time.
- **A degraded or unavailable model status must survive the restart** for as long
  as the breaker criteria that set it still hold.

Restoration must not block startup indefinitely, and a corrupt or unreadable state
row must not prevent the router from starting — it must degrade to in-memory
accounting and record that it did so, rather than crash-looping.

### 3. Token Estimation Before Dispatch

The router must estimate a request's token consumption **before** dispatch and
reserve against that estimate (ROADMAP §3.5, CONSTITUTION §8.2).

- **The request dimension is exact.** One dispatched request reserves one request.
  There is no estimation error on this dimension and none is tolerated.
- **The token dimension is an estimate**, and the design explicitly tolerates
  error on it.

The estimate must cover **both** directions:

- **Input tokens**, estimated from the normalized request's message content.
- **Output tokens**, which are unknown before generation. The router must reserve
  an assumption for them, because under-reserving output is what actually breaches
  a token-per-minute ceiling. The assumption must be explicit and configurable,
  not hidden inside a constant.

Existing M1 behaviour reserves `chars / 4` with a safety margin. M2 extends this to
separate input and output assumptions and states the tolerated error.

**The output assumption is 1025 tokens, configurable globally** (B1). The
**safety margin is configurable** (B3), alongside the assumption it multiplies.

**The reservation is composed by the tracker, not the caller.** The caller
estimates the *input*, because that is the part it can see; the tracker adds the
output assumption it holds, because that is the part that is policy. The tracker
therefore owns the whole reserved figure, and a caller cannot forget the output
half the way it could forget the margin.

**The breaker keeps using the input estimate.** A context-length refusal is about
the *prompt* — a provider refusing 900k input tokens says nothing about how long
the reply would have been — so the breaker's threshold is compared against the
input estimate, not the reserved total.

### 4. Reconciliation

When a provider reports actual usage, the router must settle the reservation
against it (CONSTITUTION §8.2).

- **Reconciliation corrects only the entries the reservation created.** It does
  not retroactively rewrite other windows, because those windows may already have
  expired or been pruned, and a rolling window is a record of what occurred rather
  than a mutable tally.
- **When a provider reports no usage, the estimate stands.** Capacity is not
  released on the assumption it was unused — every dispatched attempt consumes
  quota whether or not it produced output.
- **Reconciliation must be idempotent.** A repeated settle cannot double-count.

This closes the third open question from #36 by observing what the code already
does deliberately, rather than by introducing new behaviour: `_Entry` carries both
an estimate and an actual, and reconciliation writes the actual into the entries
that reservation created.

**The provider's own figure reaches the tracker.** `NormalizedResponse` gains a
usage field, and both adapters populate it from what the provider reports —
Gemini's `usageMetadata`, Mistral's `usage`. Until M1 the normalised response
carried no usage, so every reconciliation received `None` and every reservation's
estimate stood; the plumbing was already there and had nothing to read.

**A provider figure that is implausibly large is not believed.** A cumulative
counter reported as if it were per-request would inflate reconciliation without
bound, so an actual more than an order of magnitude above the reservation is
treated as implausible and the estimate stands. This is a heuristic, and it is
recorded as one: the *authoritative* check on estimator drift is the console
cross-check, which compares accumulated persisted counts against what the
provider actually bills.

**The estimate and the actual must be stored separately in persisted state** (not
collapsed into one effective figure), because the estimator-drift measurement
described in ADR-0006 §6 depends on being able to compare them after a restart.

### 5. `GET /v1/models`

The endpoint must return the models the router will actually serve, generated from
**configuration/model-registry state**. Dynamic provider discovery is not required
and is not implemented (ROADMAP §3.3).

The response must be OpenAI-compatible in shape: an object with a `data` array of
model objects carrying at least `id`, `object: "model"`, `created` and `owned_by`.

**`created` is the configuration file's modification time**, **`owned_by` is the
deployment owner**, and **`permission` and `default` are emitted** (B5).

**A model that is configured but currently unavailable must still be listed**, with
its availability conveyed separately. Hiding it would make a client's model list
change as a side effect of transient provider state, and a client that re-fetches
on that basis would oscillate.

### 6. Streaming

The router must translate provider-native streaming into OpenAI-compatible SSE
where the provider supports it (ROADMAP §3.11).

- **Streaming is negotiated per provider.** A provider that cannot stream must fail
  clearly rather than silently buffering a whole response and pretending to stream.
- **Quota accounting must still settle.** A stream that ends mid-flight must
  reconcile with whatever usage the provider reported, and an aborted stream must
  not leak a reservation.

**Fail-fast once a stream has begun.** Partial content already delivered to a
client **must not** be replayed from another provider (ROADMAP §3.11, and an
explicit non-goal of #36). The connection is terminated and the failure is
recorded. Cross-provider replay after stream start is not attempted at any phase.

**The fail-fast boundary is drawn at delivery, not dispatch.** A stream that fails
before emitting any chunk has delivered nothing and is an ordinary transient
fault, retryable within the existing budget. A stream that has emitted a chunk
cannot be retried. A **zero-chunk stream is not "after stream start"** — this
distinction is what makes the rule precise rather than merely conservative.

**The dispatcher is not modified** (A2). The streaming path lives in the route
layer, which re-implements model resolution, key eligibility, capacity gating and
the bounded retry budget, and applies fail-fast at the point of delivery. The
streaming path takes exactly one `(provider, model)` pair and never varies it.

**Chunk format** (B7): a `role` delta is emitted first, `usage` is included on
the final chunk, and `stream_options: {"include_usage": true}` is honoured.

This is a deliberate correctness choice over an availability one: a client that
receives a truncated response from one provider followed by unrelated content from
another is worse off than one that receives a clean error it can retry.

### 7. Improved Provider Error Classification

Provider errors must be classified into the router's normalized set with enough
fidelity that a failure is actionable — a quota problem, a credential problem, a
payload problem, or a transient upstream problem must remain distinguishable.

**M2 delivers four specific improvements** (A4):

1. **Provider-specific error codes are surfaced to the client**, so a payload
   problem is distinguishable from a credential problem without parsing prose.
2. **A burst `429` is distinguished from a daily-cap exhaustion.** The first is
   transient with a short window; the second means the day's budget is spent and
   no amount of retrying will help. They are identical on the wire.
3. **`5xx` overload is separated from upstream defect.** An overload signal is
   retryable; a provider bug is not, and retrying it burns quota to learn nothing.
4. **The provider's own retryable signal is trusted** where one is supplied.

Ordinary `400`s must not degrade a model. Only a genuine context-length refusal
does.

## Behavioral Rules

1. Persistence must never block the async event loop (CONSTITUTION §8.3).
2. Persisted state must never contain key material or payload text (§9.2, §9.1).
3. No capability gated behind configuration may be *implied* by a default; a
   persisted row must not be able to assert availability the environment cannot
   support.
4. A restore failure degrades to in-memory accounting and is recorded. It never
   prevents startup.
5. Cross-model failover remains prohibited for an explicitly requested model
   (CONSTITUTION §7.2), including on the streaming path.
6. Streaming never replays partial content from another provider.
7. The reconciled actual usage, where known, supersedes the estimate for the
   windows the reservation touched — and only those.
8. Every dispatched attempt consumes quota, including streaming attempts that
   produce nothing.
9. The estimate and the actual are persisted separately, so estimator drift stays
   measurable after a restart.
10. The dispatcher is unchanged by M2. The streaming path re-implements capacity
    gating identically rather than diverging from it.

## Acceptance Scenarios

### Scenario 1: A cooldown survives a restart

```text
Given google/gemini-3.5-flash-lite has been rate limited
And its cooldown has 30 seconds remaining
When the router process is stopped and started again
Then the model must be refused
And no request may be dispatched to it
And the refusal must complete within the remaining cooldown
```

### Scenario 2: An expired cooldown is not restored

```text
Given a cooldown_until timestamp 10 minutes in the past is persisted
When the router starts
Then that cooldown must not be applied
And the model must be eligible for dispatch
```

### Scenario 3: An expired daily window is not restored

```text
Given a daily request counter of 480 was persisted against a 500 RPD limit
And the recorded window reset instant has passed
When the router starts
Then the restored count must not be 480
And the model must be treated as having its full daily allowance
```

### Scenario 4: Counters survive a restart

```text
Given 120 requests were dispatched to a model with a 500 RPD limit
And the counters have been flushed to disk
When the router restarts
Then the restored count must be 120
And 380 requests must remain available
```

### Scenario 5: Reservation covers output tokens

```text
Given a request whose input is estimated at 1000 tokens
And the configured output assumption is 500 tokens
When a reservation is made
Then at least 1500 tokens must be reserved for the token dimension
```

### Scenario 6: Reconciliation supersedes the estimate

```text
Given a reservation estimated at 1500 tokens
And the provider reports 420 actual tokens
When reconciliation runs
Then the window must reflect 420 tokens for that entry
And the remaining 1080 must be available again
```

### Scenario 7: No reported usage leaves the estimate standing

```text
Given a reservation estimated at 1500 tokens
And the provider reports no usage for it
When reconciliation runs
Then the window must still reflect 1500 tokens
And the capacity must not be released
```

### Scenario 8: Model discovery reflects configuration

```text
Given config.yaml declares google/gemini-3.5-flash-lite and mistral/ministral-3b-2512
And the mistral model is currently in cooldown
When GET /v1/models is called
Then both models must appear in data
And the response must carry an OpenAI-compatible model object shape
```

### Scenario 9: Streaming fails fast after the first chunk

```text
Given a streaming request has already emitted one chunk to the client
And the provider then closes the connection
When the router handles the interruption
Then the stream must be terminated
And no request may be dispatched to another provider
And the interrupted reservation must still be reconciled
```

### Scenario 10: A non-streaming provider fails clearly

```text
Given a provider that cannot stream
When a client requests a streamed response
Then the router must return a defined error
And it must not silently buffer and replay the whole response
```

### Scenario 11: No key material or payload is persisted

```text
Given a request and response have been served
And quota state has been flushed
Then the database must contain no key material
And no prompt text or completion text
And the key must be identifiable only by alias
```

### Scenario 12: A corrupt state row degrades rather than crashes

```text
Given a persisted state row is unreadable or corrupt
When the router starts
Then it must start successfully
And it must fall back to in-memory accounting for that unit
And it must record that it did so
```

## Edge Cases

- **Clock skew / backwards jump.** A `cooldown_until` in the future because the
  system clock moved backwards must not become an indefinite lockout. Cooldowns are
  bounded by a configured maximum.
- **A dimension removed from configuration.** Persisted counters for a quota
  dimension that no longer exists must be discarded, not applied to a surviving
  dimension.
- **A model removed from configuration.** Its persisted state must be retained or
  discarded deliberately, and never silently applied to a model that reuses the id.
- **Concurrent restore and first request.** Restoration completes before the
  listener accepts traffic; there is no window in which a request is dispatched
  against an unrestored counter.
- **A stream with zero chunks.** A stream that fails before emitting anything is
  *not* "after stream start" — no content has been delivered, so the request may
  be retried within the existing budget. This distinction is what makes fail-fast
  precise rather than merely conservative.
- **Flush interval vs. a hard shutdown.** A `SIGKILL` may lose up to one flush
  interval of counters. This bound is stated rather than eliminated, because
  eliminating it requires a write per request.
- **A provider that reports cumulative rather than per-request usage.** Must not be
  treated as per-request; a cumulative figure would inflate reconciliation without
  bound.

## Error Behavior

| Condition | Status | Code |
| :--- | :--- | :--- |
| No capacity available after restore | 503 | `no_available_capacity` |
| Provider rate limited | 429 | `provider_rate_limited` |
| Provider timeout after retries | 504 | `provider_timeout` |
| Provider 5xx surviving retries | 502 | `provider_server_error` |
| Credential rejected | 401 | `provider_auth_error` |
| Streaming requested, provider cannot stream | 400 | `streaming_unsupported` |
| Stream interrupted after first chunk | connection terminated | `stream_interrupted` |
| Persistence failure on the request path | 503 | `persistence_unavailable` |

A persistence failure must not be silent. If the router cannot record a
reservation, continuing would permit unbounded quota consumption — the exact
failure the quota system exists to prevent — so the request is refused rather than
served unaccounted.

## Security / Privacy Considerations

- **No payload in the database** (CONSTITUTION §9.1). The schema stores counts and
  timestamps. Prompt text, system instructions, tool declarations and completion
  content are never written.
- **Token counts are not payload.** A count of tokens is operational metadata, not
  content: it says how much was spent, not what was said. The provider's reported
  usage is therefore safe to reconcile against and to persist, and unsafe to
  derive from — the router never reconstructs text from a token count.
- **No key material in the database** (§9.2). Aliases only. The database is
  therefore safe to inspect, and a state dump is safe to paste into an issue.
- **Restore does not trust the database for secrets.** A persisted row can describe
  *which* slot was in cooldown; it can never supply that slot's material.
- **A tampered state file degrades quota protection, not security.** The worst a
  hostile edit achieves is self-limiting — fewer requests dispatched, or more.
  This is recorded because it is the reason no integrity mechanism is proposed: the
  database is local, single-user, and an attacker who can write it already has the
  process's environment.

## Explicit Non-Goals

From #36, carried forward verbatim:

- External or hosted persistence services. Redis is **not** a committed
  requirement and the schema must not assume one.
- Transparent cross-provider replay after a stream has started.
- Tool calling — the next compatibility feature after streaming, but outside M2.
- Capability-aware routing (M3).

Added by this specification:

- **Dynamic provider model discovery.** `/v1/models` is generated from
  configuration.
- **Historical window rewriting.** Reconciliation corrects what it reserved into.
- **Cross-process or multi-node quota coordination.** Single-node, single-user.
- **Exactly-once accounting.** The estimate/actual pair may disagree by one
  in-flight request across an unclean shutdown, and that is accepted.

## Clarifications Recorded

Agreed 2026-10-19 with the operator, before implementation began. Each is
recorded with its reasoning so it does not have to be reconstructed later.

| # | Decision |
| :--- | :--- |
| A1 | The OpenHands objective is **deferred to M3** (was D3) |
| A2 | The **dispatcher stays unary-only**; the streaming path lives in the route layer |
| A3 | **One implementation** — in-memory hot state with a persistence layer attached |
| A4 | Error classification: **all four** improvements |
| B2 | Flush interval: **20 s** |
| B3 | The **safety margin is configurable** |
| B4 | Maximum cooldown: **24 h**, configurable |
| B5 | `/v1/models`: `created` = config file mtime, `owned_by` = deployment owner, `permission`/`default` emitted |
| B6 | `request_logs` population **deferred** |
| B7 | Streaming: role delta emitted, `usage` in the final chunk, `stream_options.include_usage` honoured |
| B1 | The output-token assumption is **1025**, configurable globally — see below |

### A1 — the OpenHands objective is deferred to M3

ROADMAP Milestone 2 asks that OpenHands be used "as an actual workload as early as
practical", while simultaneously placing **tool calling outside the boundary** as
"the next compatibility feature after streaming". Those two are in tension
structurally, not by effort: OpenHands is an agentic client that acts by issuing
tool calls, so streaming and `GET /v1/models` support *conversational* use but not
agentic use.

**Decision: defer the OpenHands objective to M3**, where capability-aware routing
and tool support converge. M2 delivers the capabilities the objective needs —
streaming and model discovery — and does not claim the workload.

**Consequence, recorded deliberately:** M2's exit criterion, "can serve interactive
OpenHands workloads with streaming where supported", is **not met by M2 and is not
claimed to be**. The milestone's exit criteria are restated in `tasks.md` §
*Milestone Exit Criteria* to what M2 actually delivers. The ROADMAP's Milestone 2
section carries the same deferral.

This is a scope decision about the milestone, not an implementation detail, and it
is recorded as a deviation rather than absorbed silently.

### A2 — the dispatcher stays unary-only

**Decision:** the streaming path lives in the route layer. `core/dispatch.py` is
**not modified**.

Fail-fast needs to know whether a chunk has been delivered to the client, and the
dispatcher has no way to express that. Two options were considered:

1. **Route-layer streaming** — the dispatcher keeps its single `(provider, model)`
   signature and its whole unary contract; the route owns the stream and applies
   fail-fast at the point of delivery.
2. **A streaming dispatcher variant** — a delivery callback threaded into the
   dispatcher's retry loop.

**Option 1 is chosen.** The dispatcher is the most safety-critical component in the
router and the one M1 measured at 100/100 under live traffic. Leaving it
byte-for-byte untouched is worth more than avoiding some duplicated
capacity-gating logic in the route.

**The duplication is bounded and must stay that way.** The streaming path in the
route re-implements: model resolution, key eligibility, capacity gating, and the
bounded retry budget. It must re-implement them **identically**, and the
cross-model prohibition of CONSTITUTION §7.2 must hold there too — the streaming
path takes exactly one `(provider, model)` pair and never varies it across
attempts.

### A3 — one implementation with a persistence layer attached

**Decision:** there is one quota tracker. Its hot window state stays in memory and
a persistence layer is attached to it.

This supersedes the two-implementations framing in `plan.md` and ADR-0005, which
was inconsistent with ADR-0005's own reasoning. If counters batch behind a flush
interval, the state they batch **must** be hot in memory; a tracker that read and
wrote SQLite on every reservation would put a database round-trip on the dispatch
path, which is the exact cost the asymmetry principle exists to avoid.

**Consequences:**

- **There is no second implementation of the window logic**, and therefore no need
  to extract a `QuotaTrackerProtocol` to keep two in step. The protocol extraction
  planned in `tasks.md` T043 is **dropped**.
- **The degradation path is not a different class.** If persisted state is
  unreadable, the same tracker starts with empty state and continues. There is no
  fallback implementation to keep alive.
- **Restore is a method on the tracker**, called once at startup before the
  listener accepts traffic.

### A4 — error classification: all four improvements

**Decision:** M2 delivers all four:

1. **Provider-specific error codes are surfaced to the client** — Gemini's
   `INVALID_ARGUMENT`, Mistral's `invalid_api_key`, and so on, so a client can
   distinguish a payload problem from a credential problem without parsing prose.
2. **A burst `429` is distinguished from a daily-cap exhaustion.** The first is
   transient and the window is short; the second means the day's budget is gone
   and no amount of retrying will help. These look identical on the wire.
3. **`5xx` overload is separated from upstream defect.** A `503` with an overload
   signal is retryable; a `500` from a provider bug is not, and retrying it burns
   quota to learn nothing.
4. **The provider's own retryable signal is trusted** where the provider supplies
   one, rather than the router's own guess.

### B2 — flush interval: 20 seconds

**Decision:** 20 s.

A `SIGKILL` loses up to one interval of counters. 20 s bounds that loss to a
small fraction of a 500-per-day budget while keeping the flush cost negligible —
one write per 20 s regardless of request rate, because the interval's requests are
coalesced into a single additive write.

Cooldowns are unaffected: they are written through immediately and survive an
unclean shutdown regardless of this value.

### B3 — the safety margin is configurable

**Decision:** the safety margin becomes configuration, alongside the output
assumption it multiplies.

It is currently a constructor parameter defaulting to `1.1`, unreachable from
`config.yaml`. Making the output assumption configurable while leaving the margin
fixed would be incoherent: the margin is the multiplier on that assumption, and an
operator tuning one would reasonably expect to tune the other.

**The §6.1 key cap is *not* affected by this.** `key_cap_for` stays in code: a
constitutional limit that YAML could raise is not a constitutional limit.

### B4 — maximum cooldown: 24 hours, configurable

**Decision:** `max_cooldown_seconds` defaults to **86400** (24 h).

The cap exists to bound a pathological lockout — a `cooldown_until` written by a
clock that later jumps backwards, or a provider hint far beyond any real reset.
24 h accommodates the longest *legitimate* provider hint (a daily cap resetting at
midnight) while still bounding the pathological case to one day.

**A backwards clock jump is better detected than patched.** A cap treats the
symptom; comparing persisted `cooldown_until` against the system clock at restore
time treats the cause, and is the mechanism that should carry the weight. The cap
is the backstop.

### B5 — `/v1/models` response shape

**Decision:**

- **`created`** is the **modification time of the configuration file** the models
  are derived from. A config-declared model has no creation date, and a stable
  value matters: a client that re-fetches and sees a changed `created` may treat
  the model as updated. The config file's mtime changes only when the operator
  changes it.
- **`owned_by`** is the **deployment owner** — the identity the router is
  operating as — not the provider. The provider is already carried by the model id
  (`google/…`, `mistral/…`), so repeating it in `owned_by` would add no
  information.
- **`permission` and `default` are emitted.** Some clients read them, and
  omitting them produces inconsistent behaviour across client implementations.

### B6 — `request_logs` population is deferred

**Decision:** M2 does not populate `request_logs`. M0 built the table; M1 left it
unused; M2 continues to leave it unused.

**This is a deferral, not a decision that it is unwanted.** A table that exists,
is migrated, and is never written is a loose end, and M2 is the milestone that
makes real persistence possible. It is deferred because populating it is new
observability work that is not in M2's deliverables, and adding it would widen
the milestone. Worth revisiting when a usage report is actually wanted.

### B7 — streaming chunk details

**Decision:**

- **A `role` delta is emitted first**, matching the OpenAI chunk format, so
  clients that assemble deltas into a message see the role before content.
- **`usage` is included on the final chunk**, so a streaming client can account
  for tokens without a second request.
- **`stream_options: {"include_usage": true}` is honoured.** Usage is included
  when the client asks for it.

### B1 — the output-token assumption: 1025, configurable globally

**Still open, pending the operator's decision.** The supporting analysis is below
so the decision can be made on evidence rather than intuition.

**Measured provider-reported usage** (2026-10-19, direct calls, two prompt
shapes):

| Provider | Prompt | Input tokens | Output tokens | Output chars |
| :--- | :--- | :--- | :--- | :--- |
| `gemini-3.5-flash-lite` | single word | 8 | 1 | 4 |
| `gemini-3.5-flash-lite` | short paragraph | 28 | **146** | 827 |
| `ministral-3b-2512` | single word | 11 | 3 | 4 |
| `ministral-3b-2512` | short paragraph | 33 | **178** | 891 |

Three findings that bear on the decision:

1. **The input estimate is sound.** 144 prompt characters produced 28–33 input
   tokens, i.e. 4.4–5.1 characters per token. M1's `chars / 4` therefore
   *over*-estimates input slightly — conservative in the right direction, and no
   correction is needed.
2. **The output assumption is not capacity-critical at current rates.** Gemini's
   ceiling is 250K TPM against a 15 RPM burst limit; even a 1000-token output
   assumption reaches 15K tokens/minute, roughly 6% of the ceiling. Mistral's
   1.3M TPM against 12.5 RPS is looser still. **TPM is not the binding
   constraint**, which M1's spec already recorded.
3. **So the assumption's real job is accounting accuracy, not capacity
   protection.** Because reconciliation supersedes the estimate with the
   provider's own figure, an over-reservation is refunded after the fact. A
   generous default therefore costs almost nothing in capacity while keeping the
   reservation meaningful.

**Consequence of 2 and 3 together:** the exact value is far less critical than it
first appears. A value that comfortably exceeds a typical response (the measured
"short paragraph" is 146–178 tokens) is sufficient, and the cost of choosing
generously is bounded by TPM slack that is 16× the requirement.

**The `max_tokens` complication.** The client *cannot* currently tell the router
how long a response will be: `normalize_chat_completion_request` discards every
field except `model` and `messages`, so `max_tokens` never reaches the router.
The assumption therefore has to be deployment-side configuration — or M2 changes
the normalization contract to honour `max_tokens`, which is a deliberate change
to behaviour M1 established and is **not** in M2's deliverables.

**Options for the operator:**

| Option | Effect |
| :--- | :--- |
| **A.** Configurable global default, no `max_tokens` | Simplest. Recommended unless a workload needs a different value. |
| **B.** Configurable **per model** | Follows the quota model, which is already per-model. More configuration surface for a value that barely matters. |
| **C.** Honour `max_tokens` when present, fall back to the default | Most accurate. Changes the normalization contract M1 established, and widens M2. |
| **D.** Per request, from the client | Not possible without C. |

**Decision: option A, with a default of 1025 tokens, configurable globally.**

1025 is about 6× the measured short-paragraph output, costs ~8% of Gemini's TPM
ceiling at the 15 RPM burst limit, and is refunded on reconciliation. **Option B
— per-model — becomes worth revisiting if a long-form workload appears**, and
that is the reason the value is configuration rather than a constant.

The output assumption is deliberately *not* per-model. It is a policy number
rather than a measured one, and the measured part — the input estimate — is
already per-model in effect, because it is computed from the request. A global
default is one number to reason about; a per-model value invites the belief that
it is calibrated, and a value that is not calibrated is worse than one that
obviously is not.

**The configuration is global, not per-model**, and lives in `config.yaml` under
`quota.output_token_assumption`.

### D1 — Estimation error is tolerated on tokens only

**Agreed.**

Requests are counted exactly; tokens are estimated. The tolerated error is
therefore stated per dimension, not globally. A vague global tolerance ("±20%")
would be unfalsifiable, because the request dimension has no error to absorb.

Reserving input estimate plus a configurable output assumption is agreed over
estimating input alone. Under-reserving output is what breaches a TPM ceiling,
because the output size is the part genuinely unknown pre-dispatch.

### D2 — Reconciliation corrects only what it reserved into

**Agreed.**

This is what the M1 code already does deliberately, so agreeing it costs nothing.
The alternative — retroactive rewriting of historical windows — cannot be
implemented honestly for a rolling window that has already been pruned, and a
fixed daily window is a record of what occurred rather than a mutable tally.

## Recorded Deviations Carried From M1

| Deviation | Status in M2 |
| :--- | :--- |
| CONSTITUTION §8.3 — in-memory quota state (ADR-0004) | **Resolved by this milestone.** Removed from M1's `spec.md` § *Recorded Deviations* only once the restore scenarios pass. |
| ADR-001 §2.4 — stale timeout value | Stands; not a persistence concern. |
| ROADMAP §2 — free-tier quota values are workspace-measured | Stands. M2 adds token accounting, which makes re-measurement more valuable, not less. |
| ROADMAP §4 — success target ≥80% | Stands. M2 does not re-measure the M1 result; it adds durability and interactivity, neither of which the M1 measurement tested. |
| Mistral RPD unknown | **More material in M2.** Persistent counters make an unknown daily ceiling easier to detect empirically — the first place this can be resolved without guessing. |

## Measuring the Success Rate

M2 extends the recording requirement from `specs/002-mvp-openai-proxy/spec.md` §
*Measuring the success rate* rather than replacing it. The same fields apply —
success rate **and request count**, request volume total and per model per day,
provider and model served, and the conditions (tracker active, retry active,
cooldown active, whether the window coincided with console-reported usage).

**Two additions, both forced by what M2 changes.**

### Streamed and unstreamed rates are recorded separately

M1's 100/100 covers **unary requests only**. Streaming holds the connection open
longer, so the same underlying fault rate produces more interruptions — and an
interruption after the first chunk is *not* recoverable by retry (ADR-0007).

**Reporting a single combined figure would hide exactly the regression M2 could
introduce.** The streamed rate is the first place fail-fast becomes visible as a
user-facing error, and averaging it into a number dominated by unary traffic would
conceal it.

### Persistence state is recorded, not just outcomes

M2's central claim is that state survives restart. A success rate cannot show that,
so the following must also be recorded:

- **The pre-restart quota position** — counters, active cooldowns, degraded models.
- **The first request after restart**, and whether it was refused as it should have
  been (Scenario 1) or dispatched as it should not have been.
- **Counter drift against the provider console** after the flush interval, compared
  with M1's baseline of **+1 over 54 dispatches** (ADR-0006 §6).

Drift is the honest measure of whether the estimator is sound. M1's delta was
small enough to attribute to an aborted in-flight request; a delta that grows with
volume means the estimator is wrong and recalibration is owed, not tuning.

### Conditions that must be stated alongside any M2 figure

- Whether the window included **any restart**. A rate measured across a restart is
  not comparable to one measured within a single process lifetime.
- Whether cooldowns were **ever triggered**. A run with no cooldown has not
  exercised the durability path this milestone exists to build.
- Whether the run was **paced**, and at what rate.

## Risk Assessment

### Persistent state could be trusted too much

Once cooldowns survive restarts, an operator may reasonably assume limits are
enforced as strictly as the provider enforces them. They are not: the estimate is
still an estimate, and the console remains the authority. The M1 cross-check — the
console reporting 53 against the router's 54 dispatches — showed the router
counting one *more* than the provider bills. Persistent counters will accumulate
such differences rather than eliminate them.

**This argues for re-running the console cross-check once counters are durable.** It
is cheap, it is the only independent check available, and it is the measurement
that would catch a systematically drifting estimator.

### A flush interval trades accuracy for latency

Every counter that is not written immediately is a counter that can be lost to a
`SIGKILL`. The bound is one flush interval. This is acceptable for counts and
unacceptable for cooldowns, which is why the two are treated differently rather
than uniformly.

### Streaming is where fail-fast will be felt

A stream that dies mid-flight cannot be replayed, so the client sees an error
rather than an answer. Under M1's measured conditions, transient faults were rare
(2 in 104 dispatches, both recovered by retry). **That measurement was for
unary requests only** — streaming multiplies exposure because the connection is
open longer. The success rate for streamed workloads should be measured separately
rather than assumed equal to the unary figure.

### M2 may not improve the success rate at all

Persistence prevents *overshoot after restart*, a failure mode M1 never
exercised. It does not improve handling of a transient fault, which retry already
covers. If M2 is expected to lift the ≥80% figure, that expectation should be
corrected now.
