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

### 5. `GET /v1/models`

The endpoint must return the models the router will actually serve, generated from
**configuration/model-registry state**. Dynamic provider discovery is not required
and is not implemented (ROADMAP §3.3).

The response must be OpenAI-compatible in shape: an object with a `data` array of
model objects carrying at least `id`, `object: "model"`, `created` and `owned_by`.

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

This is a deliberate correctness choice over an availability one: a client that
receives a truncated response from one provider followed by unrelated content from
another is worse off than one that receives a clean error it can retry.

### 7. Improved Provider Error Classification

Provider errors must be classified into the router's normalized set with enough
fidelity that a failure is actionable — a quota problem, a credential problem, a
payload problem, or a transient upstream problem must remain distinguishable.

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

## Open Decisions Requiring Sign-Off

These are recorded with a recommendation rather than silently resolved. #36's
acceptance criteria require them *agreed*, and each changes behaviour an operator
would notice.

### D1 — Estimation error is tolerated on tokens only

**Recommendation:** adopt.

Requests are counted exactly; tokens are estimated. The tolerated error is
therefore stated per dimension, not globally. A vague global tolerance ("±20%")
would be unfalsifiable, because the request dimension has no error to absorb.

Reserving input estimate plus a configurable output assumption is recommended over
estimating input alone. Under-reserving output is what breaches a TPM ceiling,
because the output size is the part genuinely unknown pre-dispatch.

### D2 — Reconciliation corrects only what it reserved into

**Recommendation:** adopt.

This is what the M1 code already does deliberately, so agreeing it costs nothing.
The alternative — retroactive rewriting of historical windows — cannot be
implemented honestly for a rolling window that has already been pruned, and a
fixed daily window is a record of what occurred, not a mutable tally.

### D3 — OpenHands as a real workload is likely not reachable in M2

**Recommendation:** reduce the objective, and record it.

ROADMAP Milestone 2 asks that OpenHands be used "as an actual workload as early as
practical", and simultaneously places **tool calling outside the boundary** as
"the next compatibility feature after streaming".

**These two are in tension, and the tension is structural rather than a matter of
effort.** OpenHands is an agentic client: it acts by issuing tool calls to read
files, run commands and edit code. Without tool calling it can converse but cannot
act, which is not the workload the objective describes.

Options:

1. **Accept a reduced objective** — validate streaming and model discovery against
   OpenHands' chat path, and record that agentic use awaits tool calling.
2. **Move tool calling into M2** — enlarges the milestone and displaces work
   already committed.
3. **Defer the OpenHands objective to M3** — where capability-aware routing and tool
   support converge.

**Option 1 is recommended**, because it delivers what M2's deliverables actually
support without renegotiating the milestone. But this is a scoping decision about
the objective rather than an implementation detail, and it is the one item here
most likely to be contested.

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
