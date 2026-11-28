# M5: Adaptive Router

**Feature ID:** `006-adaptive-router`
**Milestone:** M5 — Adaptive Router (Issue #24, milestone #6)
**Planning Issue:** #39

## Purpose

Let the router replace static priors with **observed** estimates where the router
can honestly measure something: how long a model takes, whether it completes, and
whether the structure of its answer is valid.

M4 made `model = "free"` choose the highest-scoring eligible model, but every
number in that score was an operator's opinion. The shipped configuration says so
in capitals: *"THESE ARE OPINIONS, NOT BENCHMARK RESULTS. Nothing in M4 observes,
learns or measures them."* M5 is the milestone that creates observations, and the
first one that can tell an operator whether a prior was right.

What M5 does **not** do is estimate answer quality from free traffic. Free-tier
traffic is not a benchmark: the router never sees a ground-truth answer, and an
agentic workload changes the task from request to request. Inferring semantic
quality from that would be a guess wearing a measurement's clothes. M5 therefore
adapts **reliability and latency**, keeps the operator's quality prior, and records
structural validity — tool-call parseability, JSON conformance, non-empty
completion — as the only quality-adjacent evidence it can defend.

The roadmap calls for the learning approach to be its own design exercise before
implementation. The decisions that exercise produced are recorded in § *Clarifications
Recorded* (S1–S10), with the alternatives that were rejected.

## Scope

| Area | Roadmap | Batch |
| :--- | :--- | :--- |
| Milestone design, telemetry schema, estimate policy (this document) | M5 objective, separate research topic | 0 |
| Observation model, SQLite schema, recorder, EWMA, retention, restore | M5 deliverables 1–4 | 1 |
| Reliability multiplier, observed latency, decision record, config wiring | M5 deliverables 5–6, 9 | 2 |
| Offline replay harness and policy evaluation report | M5 deliverable 7, research topic | 3 |
| Exit-criteria verification, live observation run, milestone report | M5 deliverables 8–9 | 4 |

**In scope:** per-request operational observation of dispatched requests; a
time-decayed EWMA for latency, completion reliability, tool-call validity and quota
prediction error; persistence of estimates and bounded observation history; a
bounded reliability multiplier in `free` ranking; observed latency replacing the
static latency prior after a sample floor; and an offline replay harness that
evaluates estimator settings without contacting a provider.

**Out of scope, and deliberately:** semantic quality estimation, LLM-based
classification of requests or answers, contextual bandits, machine learning,
per-key reliability, quota-ratio scoring, live shadow traffic, and any change to
explicit-model behaviour. See § *Explicit Non-Goals*.

## Requirements

### 1. Telemetry Is Operational Metadata Only

Every persisted observation **must** be expressible as a row in the operational
schema: provider, model, timestamp, outcome category, HTTP status, latency, token
counts, structural-validity flags and the routing scores already emitted in the
`routing_decision` log record.

The observation **must not** contain prompt text, system instructions, tool
declarations, tool results, image data, completion text, or message counts derived
from content (CONSTITUTION §9.1). This is not a redaction policy applied after the
fact: the recorder's input type has no field capable of carrying payload, so the
guarantee is structural, exactly as `core/logging.py`'s allowlist is structural.

Token counts are permitted because they are already operational metadata in M2's
quota accounting. They are counts, not text.

### 2. What Is Observed, and What Is Attributed to the Model

One observation is recorded **per dispatched request**, after the request reaches a
terminal outcome. Internal retries and key failovers are one request, not several:
the client experienced one outcome, and the model either served it or did not. A
request refused before dispatch produces no observation: no model was tested, and
attributing a local refusal to a provider would corrupt the estimate.

| Outcome | Recorded as | Counts toward reliability? |
| :--- | :--- | :--- |
| Completion returned and structurally valid | `success` | yes, as `1` |
| Provider `5xx` | `provider_error` | yes, as `0` |
| Provider `429` | `rate_limited` | **no** |
| Request/connection timeout | `timeout` | yes, as `0` |
| Transport failure (connect, reset, DNS) | `transport_error` | yes, as `0` |
| Malformed or truncated provider response | `invalid_response` | yes, as `0` |
| Other `4xx` (client/request fault) | `request_error` | **no** |
| Client disconnected mid-stream | `client_aborted` | **no** |

The exclusions are the point:

- **A `429` is capacity, not unreliability.** It is already handled by the cooldown
  and quota machinery, which is the mechanism that protects free-tier quota. Counting
  it again as a reliability failure would double-penalise a model for being popular
  and would make a busy free tier look broken.
- **A `4xx` other than `429` is the request's fault.** The router does not lower a
  model's reliability because a client sent an invalid schema. The one exception
  M3/M4 already recognises — a context-length refusal degrading a model for large
  requests — remains the breaker's job and is not re-scored here.
- **A client abort is not the provider's failure.** The router cannot distinguish a
  user pressing Ctrl-C from a network drop on the client side.

**Structural validity** is evaluated from the normalized response the router already
holds:

- A tool call is valid when it has a non-empty name and its `arguments` parse as
  JSON, or the arguments string is empty (a no-argument call). This applies to unary
  responses and to reassembled streamed calls.
- A `json_object` or `json_schema` response is valid when its text content parses as
  JSON.
- A response with neither text nor a tool call is invalid.
- A stream that ends without a terminal event and without a client abort is
  `invalid_response`.

A structurally invalid response is a reliability failure even though the provider
returned HTTP 200. That is the only sense in which M5 observes "quality": the
objective structure the request asked for, not the semantic worth of the answer.

**The unit is provider/model, not key.** A provider with two keys is one model for
reliability purposes; key-level health is M2's key rotation and M6's administrative
concern. Per-key estimates would multiply the sample requirement and make a key
rotation look like a model change.

### 3. Estimates Use a Time-Decayed EWMA With a Sample Floor

Each metric is an exponentially weighted moving average with a **time-based decay**,
so an estimate reflects recent behaviour and recovers without a separate reset:

```text
alpha          = 1 - 0.5 ** (elapsed_seconds / half_life_seconds)
new_estimate   = alpha * observation + (1 - alpha) * old_estimate
```

- `elapsed_seconds` is measured from the previous update for that metric, clamped to
  `>= 0`. A backwards clock step must not rewind an estimate or produce a negative
  weight.
- A metric with no prior observation is seeded with its first value.
- **No estimate is used until `min_samples` observations exist for that metric.**
  Below the floor the router behaves exactly as M4: the static prior and a neutral
  multiplier. The floor is what prevents one unlucky request from reordering the
  whole deployment.
- The half-life is configurable because the right value is a property of the
  operator's traffic rate. The shipped default is one hour, which is long enough to
  smooth a burst and short enough to recover within a working session.

Reliability is the EWMA of the binary success indicator defined in § 2. Latency is
the EWMA of total request latency in milliseconds, updated only for outcomes where
generation time is meaningful: `success`, `invalid_response` and `timeout`. A `429`
or a fast `5xx` is recorded with its latency for diagnostics but does not make a
model look fast in the ranking. Tool-call validity is the EWMA of the tool-call
validity flag, recorded for diagnostics and for the milestone report. Quota
prediction error is the EWMA of
`abs(actual_tokens - reserved_tokens) / max(actual_tokens, 1)`, recorded for
diagnostics.

### 4. Reliability Is a Bounded Multiplier, Not a Weight or a Filter

The M4 weight set stays exactly as it is: quality `0.60`, tool support `0.30`,
latency `0.10`. Reliability enters **after** the weighted base score:

```text
base_score        = (w_q*q + w_t*t + w_l*l) / (w_q + w_t + w_l)
reliability       = observed success rate, or 1.0 below the sample floor
multiplier        = floor + (1 - floor) * reliability
final_score       = base_score * multiplier
```

- `floor` is configurable in `[0, 1]`, default `0.5`. A model that fails every
  observed request still retains half its base score rather than being removed.
- **Reliability never excludes a candidate.** Hard constraints remain capability
  fit and current capacity (CONSTITUTION §7.1). A failing model that is still
  dispatchable can still win if every alternative is worse; the log shows the
  multiplier that made it expensive.
- **Reliability is not a weight.** Adding it to the weight vector would change the
  meaning of every M4 score and invite re-tuning of the other three. A multiplier
  composes with the existing policy and is one number in the log.
- **The multiplier is bounded below**, so an unlucky burst cannot make a model
  permanently unelectable. Recovery is the same EWMA run forward: successes raise
  the estimate and the multiplier with it.

The score remains in `[0, 1]`, because `base_score` is in `[0, 1]` and the multiplier
is in `[floor, 1]`.

### 5. Observed Latency Replaces the Static Prior After the Floor

Latency scoring keeps M4's absolute-ceiling formula, but the input changes when the
router has enough observations:

```text
latency_component = max(0, 1 - latency_ms / latency_ceiling_ms)
latency_ms        = observed total latency   if latency_samples >= min_samples
                  = operator prior           otherwise
```

- **Total latency is scored; time-to-first-byte is recorded.** The M4 prior is a
  whole-request expectation, and replacing it with TTFB would silently change what
  the number means. TTFB remains in the observation and the milestone report because
  it is the number that predicts how an agentic client feels.
- A timeout is a latency observation. A hanging model must not look fast.
- A `429`, request-fault `4xx`, transport failure or `5xx` records its latency for
  diagnostics but does not update the scored latency estimate: a fast refusal is not
  a fast completion, and letting one in would make a busy or broken model look quick.
- The decision record names the source (`prior` or `observed`) and the sample count,
  so a reader can tell which number produced the score.
- If observations push latency above the ceiling, the component is `0` and the model
  stays eligible, exactly as in M4. Latency is a preference, not a constraint.

### 6. Quality Remains an Operator Prior

`routing.quality` is unchanged: a 0–100 operator judgement, not a measurement. M5
adds no field that writes to it and no process that infers it.

The evidence M5 can defend is structural, and it is already captured by the
reliability multiplier: a model that returns unparseable tool calls, invalid JSON
for a schema, or empty completions will accumulate failures and lose score. A model
that returns valid structure keeps its multiplier near `1.0`.

What M5 explicitly refuses to infer:

- Whether an answer is **correct**.
- Whether one model writes **better prose** than another.
- Whether a model is better at **coding** than at reasoning.
- Any per-task or per-prompt classification, including LLM-based classification.

Those require a ground truth the router does not have. A future milestone may add an
explicit operator or client feedback signal; M5 does not invent one.

### 7. Quota Prediction Accuracy Is Recorded, Not Acted On

For every reconciled request, the router records the reserved token estimate and the
provider-reported actual usage, then updates the quota-error EWMA.

- **M5 does not change the estimator.** Moving reservation estimates would move M2's
  measured quota behaviour inside a telemetry milestone, and the estimator already
  carries a deliberate safety margin (CONSTITUTION §8.2). The recorded error is
  evidence for a later, separately scoped change.
- The error is surfaced in the replay report and the milestone report, so estimator
  drift is visible rather than absorbed.
- A request with no provider-reported usage produces no quota observation. A guess
  at the actual count would make the error metric meaningless.

### 8. Persistence, Retention, and Degradation

Estimates **must** survive a process restart (CONSTITUTION §8.3), and the
observation history must be bounded.

**Two tables, two purposes:**

| Table | Purpose | Bounded by |
| :--- | :--- | :--- |
| `telemetry_aggregates` | One row per provider/model holding the current EWMA values and sample counts. Authoritative for scoring. | Configuration: one row per configured model |
| `telemetry_observations` | One row per dispatched request, including the decision snapshot needed for replay. | `retention_days` and `max_observations` |

The aggregate row is restored directly at startup. Observation rows are **not**
replayed into the aggregates on boot: the aggregate is authoritative, and replaying
a bounded window would silently disagree with the estimate the last process used.
The observation table exists for offline evaluation, not for reconstruction.

**Retention** is enforced at startup and periodically thereafter: delete observations
older than `retention_days`, then delete the oldest rows beyond `max_observations`.
Pruning is a bounded `DELETE`, not a table rebuild.

**Persistence failure must not fail a request.** The in-memory estimate is updated
first and remains authoritative for the running process; the write is queued. If the
write fails, the router logs one warning per degradation transition, keeps adapting
in memory, and continues serving. This is deliberately weaker than quota and cooldown
persistence, which must be written through because they protect provider quota
(CONSTITUTION §8.3). A lost telemetry row costs a little accuracy; a failed request
because an analytics write failed would be the tail wagging the dog.

**Writing must not block the request path.** Observations are handed to a bounded
in-process queue drained by one background task. The queue's capacity is fixed; when
it is full the newest observation is dropped and a counter is incremented, because
blocking a completion to record that a completion happened is worse than losing the
record. The queue is drained with a timeout at shutdown.

### 9. Explainability

Every `free` selection continues to emit exactly one `routing_decision` record, and
the record gains the observed inputs that produced the score:

| Field | Content |
| :--- | :--- |
| `estimates` | `enabled`, `half_life_seconds`, `min_samples`, `reliability_floor` |
| per candidate `observed` | `reliability`, `multiplier`, `latency_ms`, `latency_source`, `samples` |
| per candidate `score` | the final score after the multiplier |
| per candidate `contributions` | the weighted base contributions, before the multiplier |

- The record still carries ids, numbers and reason codes only. It never carries
  prompt content, tool names, schema bodies or completion text.
- The multiplier is **separate from the weights**. Presenting it as a fourth weight
  would misstate the policy and make the contributions fail to sum to the score.
- A reader must be able to reconstruct the final score from the record:
  `score = base_score * multiplier`, where `base_score` is the sum of
  `contributions`.
- When telemetry is disabled or below the sample floor, the record says so
  (`reliability: 1.0`, `latency_source: "prior"`, `samples: 0`), rather than
  omitting the fields and leaving the reader to guess whether M5 is active.

Explicit-model requests do not emit `routing_decision` and are unaffected by
estimates. They are still observed, because their outcomes are evidence about the
model.

### 10. Configuration and Validation

Telemetry is **opt-in by presence**, following the project's existing convention
(`routing`, `fallback_models`):

```yaml
telemetry:
  half_life_seconds: 3600   # time constant for every EWMA
  min_samples: 5            # observations before an estimate is used
  reliability_floor: 0.5    # lowest multiplier a failing model can receive
  retention_days: 30        # observation history kept for replay
  max_observations: 10000   # hard cap on retained rows
```

- With no `telemetry` block, **nothing is recorded and routing is exactly M4**.
  This is the switch that makes the milestone reversible without a code change.
- The shipped `config.yaml` enables telemetry with the defaults above, so the
  milestone's deliverable is the default behaviour rather than an undocumented
  option.
- All values are validated at startup, with the offending key named:
  `half_life_seconds > 0`, `min_samples >= 1`, `reliability_floor` in `[0, 1]`,
  `retention_days >= 1`, `max_observations >= 1`.
- An unknown key inside `telemetry` is refused. Accepting a misspelled
  `reliability_flor` and silently using `0.5` would be the configuration equivalent
  of the silent degradation this project prohibits.
- Telemetry does **not** require `routing` to be configured. Observing explicit
  requests is useful and harmless; the estimates simply have no `free` ranking to
  influence until routing exists.

### 11. Replay Evaluates Policies Without Spending Quota

A deterministic offline harness replays retained observations through candidate
estimator and scoring settings and reports what would have changed. It **must not**
contact a provider.

For each retained `free` observation, the recorder stores the decision snapshot:
candidate model ids, each candidate's normalized components (quality, tool support,
latency), the weights in force, and the selected model. The snapshot contains no
content.

The harness can therefore evaluate:

- different `half_life_seconds`, `min_samples` and `reliability_floor` values;
- different weight vectors, using the stored components;
- how often the selected model would have changed, and for how long a demoted model
  would have stayed demoted.

It **cannot** evaluate a different latency prior or a different estimator formula,
because those inputs are already baked into the stored components. That limitation
is stated in the report rather than hidden; a question outside it needs a synthetic
harness, not a reinterpretation of observed data.

The harness reads the database read-only, writes nothing, and prints a JSON summary.
It is a development tool, not a production endpoint.

## Behavioral Rules

1. Telemetry records operational metadata only; no field can carry prompt,
   completion, tool or image content (CONSTITUTION §9.1).
2. An observation is recorded once per dispatched request, after its terminal
   outcome; a pre-dispatch refusal records nothing.
3. `429`, request-fault `4xx`, and client aborts do not lower a model's reliability.
4. Estimates use a time-decayed EWMA and are not applied below `min_samples`.
5. Reliability is a bounded multiplier applied after the M4 weighted score; it never
   excludes a candidate.
6. Observed total latency replaces the static prior only after the latency sample
   floor; TTFB is recorded but not scored.
7. The operator quality prior is never written, inferred or replaced.
8. Quota prediction error is recorded and reported, never used to change reservation
   estimates in M5.
9. Telemetry persistence failure degrades the estimate's durability, never the
   request; the request path never waits on a telemetry write.
10. `free` remains sticky in the M4 sense: deterministic ranking, no randomness, no
    rotation, no affinity state. Estimates change the ranking only by changing the
    inputs to that deterministic ranking.
11. Explicit-model requests are observed but never influenced by estimates.
12. Every `free` selection remains explainable from one `routing_decision` record,
    including the multiplier and its sample count.
13. No new path reaches paid capacity, and no telemetry operation contacts a
    provider (CONSTITUTION §2.1).
14. With telemetry absent, behaviour is byte-for-byte M4.

## Acceptance Scenarios

### Scenario 1: A successful completion updates the estimate

```text
Given telemetry is enabled
When a dispatched request completes successfully
Then one observation is recorded with outcome "success"
And the model's latency and reliability aggregates are updated
And no prompt or completion content is present in the row
```

### Scenario 2: Below the sample floor, the ranking is unchanged

```text
Given a model has fewer than min_samples observations
When a request for "free" is ranked
Then its reliability multiplier is 1.0
And its latency component uses the operator prior
And the selected model is the same one M4 would select
```

### Scenario 3: Sustained failures demote a model

```text
Given model A has the higher base score but has failed enough recent requests
And model B has a lower base score and a healthy estimate
When a request for "free" is ranked
Then B's final score exceeds A's
And B is selected
And the record shows A's multiplier below 1.0
```

### Scenario 4: A 429 is capacity, not unreliability

```text
Given a model returns 429
When the observation is recorded
Then its outcome is "rate_limited"
And its reliability estimate does not change
And the existing cooldown is entered as before
```

### Scenario 5: A 5xx is unreliability

```text
Given a model returns 500
When the observation is recorded
Then its outcome is "provider_error"
And its reliability estimate moves toward 0
And it is not excluded, only down-weighted
```

### Scenario 6: Recovery follows successes

```text
Given a model's reliability estimate has fallen
When it then completes a sustained run of successful requests
Then its estimate rises toward 1.0
And its multiplier rises with it
And no manual reset is required
```

### Scenario 7: Observed latency replaces the prior

```text
Given a model has at least min_samples latency observations
When a request for "free" is ranked
Then its latency component uses the observed total latency
And the record says latency_source "observed"
And before the floor it says "prior"
```

### Scenario 8: An invalid tool call is a structural failure

```text
Given a request carries tools
And the provider returns a tool call whose arguments do not parse as JSON
Then the observation outcome is "invalid_response"
And the reliability estimate moves toward 0
```

### Scenario 9: An explicit model ignores estimates

```text
Given a model has a poor reliability estimate
When the client requests that model explicitly
Then it is used exactly as in M1–M4
And no routing_decision record is emitted
And the request is still observed
```

### Scenario 10: A persistence failure does not fail the request

```text
Given the telemetry write fails
When a request completes
Then the client receives its normal response
And the in-memory estimate is updated
And one warning records the degraded persistence
```

### Scenario 11: Estimates survive a restart

```text
Given a model has aggregates recorded
When the process restarts
Then its estimates and sample counts are restored
And the first post-restart ranking uses them
```

### Scenario 12: Retention bounds the history

```text
Given observations older than retention_days
Or more rows than max_observations
When retention runs
Then the oldest rows are deleted
And the aggregate rows are retained
```

### Scenario 13: Replay spends no quota

```text
Given retained observations
When the replay harness runs with candidate settings
Then it reads the database read-only
And makes no outbound request
And reports the selections that would have changed
```

### Scenario 14: The decision is explainable

```text
Given a request for "free" with telemetry enabled
When the request is ranked
Then one routing_decision record names the multiplier and sample count per candidate
And the score equals the sum of contributions times the multiplier
And it contains no message content
```

### Scenario 15: Telemetry absent is M4

```text
Given config.yaml has no telemetry block
When a request for "free" is served
Then no observation is written
And the score is exactly the M4 score
```

## Edge Cases

- **A model is removed from configuration.** Its aggregate and observations remain
  until retention removes them but are never read for ranking; a model that returns
  later starts from its old estimate, which is a deliberate choice: the provider's
  behaviour did not reset because the operator edited a file.
- **A new model is added.** It has no observations, so it is scored with its priors
  and a neutral multiplier until the floor is reached.
- **Every model is failing.** Each retains at least `floor` of its base score; the
  best-ranked failing model still serves. Removing every candidate would turn a
  degraded provider into a router outage.
- **One configured model.** The multiplier changes its score but not the selection;
  there is nothing else to choose.
- **A timeout.** It is both a reliability failure and a latency observation. A
  hanging model must not look fast.
- **A streamed response reassembled from fragments.** Structural validity is judged
  on the reassembled call, not on the first fragment, which is incomplete by design.
- **A provider returns HTTP 200 with no content and no call.** `invalid_response`,
  unless the request shape explicitly permits an empty completion.
- **A clock step backwards.** Elapsed time is clamped to zero; the estimate is not
  rewound.
- **A very short half-life.** Estimates become noisy; that is the operator's choice
  and the replay harness is how they can see it before shipping it.
- **`min_samples` larger than any traffic the deployment will ever produce.**
  Telemetry is effectively observational only; valid, and visible because the
  record always reports the sample count.
- **The telemetry queue is full.** The newest observation is dropped and counted;
  the request is unaffected.
- **A database restored from backup.** Aggregates older than their observations are
  used as-is; the timestamp is in the row so staleness is inspectable.

## Error Behavior

No new client-facing error codes. Telemetry is not part of the request contract:

| Situation | Effect |
| :--- | :--- |
| Telemetry write fails | request succeeds; warning logged; in-memory estimate continues |
| Telemetry queue full | observation dropped and counted; request succeeds |
| Aggregate row unreadable at startup | estimate starts neutral; warning names the row |
| Retention delete fails | warning; old rows remain until the next attempt |
| Replay database is missing | harness exits non-zero with a clear message; no router effect |

A telemetry problem must never change a response body, status code, selected model
mid-request, or the availability of the service.

## Security / Privacy Considerations

- The observation schema has no payload fields. Tool validity is a boolean; a tool
  name is not stored. Structured-output validity is a boolean; the JSON body is not
  stored.
- The decision snapshot stores model ids, normalized score components and weights.
  It does not store the request that produced them.
- Aggregates and observations are operational state, like quota windows and
  cooldowns; a database dump remains safe to inspect.
- Replay reads a local SQLite file and makes no network request. It cannot spend
  quota, and it is not exposed over HTTP.
- Estimates reveal provider performance, not user traffic. A model id and a latency
  are already in the standard logs.
- Debug payload logging remains opt-in and unchanged. M5 does not widen it.

## Explicit Non-Goals

- **Semantic quality estimation.** No correctness scoring, no LLM-as-judge, no
  preference model, no per-task quality dimension.
- **Contextual bandits, reinforcement learning, or training.** The roadmap lists
  them as candidates for a future design exercise, not as M5 requirements. M5 uses
  a transparent statistical estimate.
- **Per-key reliability.** The unit is provider/model; key health stays with M2's
  rotation and M6's administration.
- **Quota-ratio scoring.** Still an anti-pattern (M4 R3); estimates do not reintroduce
  it.
- **Changing the token estimator or reservation margin.** Recorded, not acted on.
- **Live shadow or canary traffic.** Evaluation is offline replay.
- **A telemetry API, dashboard, or UI.** M7.
- **Routing-policy management.** Configuration only; M6.
- **Multi-hop fallback chains.** Still deferred.
- **Paid capacity of any kind.** Prohibited by CONSTITUTION §2.1.
- **Storing prompt, completion, tool or image content.** Prohibited by CONSTITUTION
  §9.1.
- **An affinity table or session state.** M4's stickiness remains a consequence of
  deterministic ranking.

## Clarifications Recorded

Agreed 2026-11-28 with the operator, before implementation began. These close the
open questions in #39 and the design choices the roadmap deferred to this exercise.

| # | Decision |
| :--- | :--- |
| S1 | Estimates are time-decayed EWMAs with a configurable half-life and a sample floor |
| S2 | Reliability is a bounded multiplier after the weighted score, not a weight or a filter |
| S3 | Only provider-attributable completion outcomes count; `429`, request-fault `4xx` and client aborts do not |
| S4 | Observed total latency replaces the static prior after the floor; TTFB is recorded, not scored |
| S5 | Quality remains an operator prior; only structural validity is observed |
| S6 | Quota prediction accuracy is recorded and reported, never used to change reservations in M5 |
| S7 | Aggregates persist in SQLite; bounded observations persist for replay; write failure degrades, never fails a request |
| S8 | Telemetry is enabled by the presence of a `telemetry` block; absent means exact M4 behaviour |
| S9 | Adaptive policies are evaluated by offline replay of metadata-only observations, never by live shadow traffic |
| S10 | The estimate unit is provider/model; per-key estimates are deferred |

### S1 — EWMA, because the alternative is either memoryless or a model

A rolling window is easy to explain but has a cliff: a single old failure leaves the
window and the estimate jumps. A Bayesian model is principled but introduces priors,
credible intervals and tuning that M5 cannot justify from a few dozen observations.
A time-decayed EWMA sits between them: one formula, one half-life, and a recovery
curve that is the same formula run forward.

The sample floor is separate from the EWMA on purpose. Smoothing alone does not stop
the first observation from being 100% of the estimate; the floor does. It also gives
the operator a single number — *how much evidence before the router acts* — that maps
directly to their traffic rate.

### S2 — a multiplier keeps the M4 policy intact

M4 decision R3 refused `reliability` as a weight because there was no signal. There
is a signal now, but the reason to keep it out of the weight vector has changed: a
fourth weight would renormalise the other three and make every M4 score
incomparable. A multiplier leaves the base ranking exactly as the operator configured
it and applies one bounded, explainable adjustment.

A hard filter was rejected because cooldown and the breaker already remove models
that cannot be contacted. A model returning structurally invalid answers is still
serving requests; refusing it outright could leave no capable candidate, which is a
worse outcome than a down-weighted one.

### S3 — 429 is not a reliability signal

The router already treats `429` as capacity: it enters cooldown, it is excluded from
selection, and it recovers on the provider's own schedule. Counting it again as a
reliability failure would make a model that is simply busy look broken, and would
punish exactly the free tiers the project depends on. The distinction also keeps the
two mechanisms independently testable: quota protects spend, reliability describes
service.

### S4 — total latency, because the prior means total

The M4 prior is a whole-request expectation. Replacing it with TTFB would make a
model's score jump for a reason the operator never configured. TTFB is still recorded
because it is the number an agentic client experiences, and a future milestone may
choose to score it. M5 records both and scores the one that matches the existing
contract.

### S5 — structural validity is the only honest quality signal

The router sees requests and responses, not answers and ground truth. It can tell
whether a tool call parses and whether a schema-conforming response conforms. It
cannot tell whether the answer is right. Reporting a structural failure as a
reliability failure is defensible; calling it "quality" would overstate what was
measured, so the spec keeps the word "quality" for the operator prior.

The rejected alternatives — LLM-as-judge, benchmark probes, user feedback — each
introduce a cost or a dependency the roadmap does not mandate: quota spend, a second
model in the routing path, or a client API that does not exist yet.

### S6 — observe the estimator, do not move it

The reservation estimator is load-bearing for quota safety (CONSTITUTION §8.2) and
was measured in M2 and M4. Changing it inside M5 would make a telemetry milestone
also a quota milestone, and a regression would be attributed to the wrong change.
Recording the error creates the evidence for a later decision without spending that
decision now.

### S7 — aggregates for scoring, observations for replay

The aggregate row is what scoring needs and what must survive a restart. The
observation row is what replay needs and what must be bounded. Storing only
aggregates would make replay impossible; storing only observations would make every
ranking depend on a scan and would lose precision when retention prunes. Two tables,
each with one job, is the simplest shape that satisfies both requirements.

Persistence is deliberately weaker than quota persistence. Quota and cooldown writes
protect the operator's provider accounts; telemetry writes protect a ranking input.
The failure modes are not symmetric, so the handling is not either.

### S8 — presence is the switch

Every opt-in in this project is presence-based: `routing` enables `free`,
`fallback_models` enables substitution, a capability block enables a declaration.
A `telemetry.enabled` boolean would create a second way to say "off" and a state
where the block exists but does nothing. Absence is also the exact M4 behaviour,
which makes the milestone reversible and the regression test trivial.

### S9 — replay, not shadow traffic

Live shadow traffic would spend the quota the project exists to protect and would
show the operator selections they did not ask for. Replay uses observations the
router already recorded, costs nothing, and is reproducible. Its limitation —
components are stored as they were computed — is stated rather than hidden. A
question replay cannot answer needs a synthetic experiment, which is a separate
tool and a separate decision.

### S10 — provider/model is the unit

A key is a credential, not a model. Per-key estimates would split a small sample
across keys, make a rotation look like a behaviour change, and duplicate M2's
key-health machinery. The provider/model unit matches the routing candidate and the
cooldown unit, which is what the score actually needs.
