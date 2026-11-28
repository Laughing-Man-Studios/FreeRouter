# Task Breakdown: M5 Adaptive Router

**Feature ID:** `006-adaptive-router`
**Issues:** #24 (epic), #39 (planning/design), #25 (telemetry collector), #26 (reliability feedback loop)
**Companion documents:** `spec.md` (contract), `plan.md` (approach)
**ADRs:** 0012, 0013

Task numbering continues from M4, which ended at **T091**. M5 starts at **T092**.
Numbers are stable identifiers: a dropped task keeps its number and is marked
dropped rather than recycled.

Batch issues are **not** created yet. Batch 0 is covered by #39; Batches 1–4 are
created as sub-issues of #24 when each batch starts, per `docs/SDD_WORKFLOW.md`
§ 27. This document is the plan those issues are cut from, so a batch that changes
shape records the change here rather than only in the issue.

## Ordering Rationale

```text
Batch 1  observe + persist          (no behaviour change)
              │
              ▼
Batch 2  estimates in scoring       (free ranking changes)
              │
              ▼
Batch 3  replay + defaults          (no behaviour change)
              │
              ▼
Batch 4  verification + report
```

- **Batch 1 comes first** because scoring has nothing to read until observations
  exist and persist. It also proves the privacy claim before any ranking depends on
  the data.
- **Batch 2 comes second** because it is the milestone's behavioural change. It is
  kept small: one lookup, one multiplier, one latency source switch, one log
  extension.
- **Batch 3 follows Batch 2** because replay is only meaningful against real
  observations. It can still run before Batch 2 ships by using Batch 1's data, but
  its report is about the multiplier defaults, so it is sequenced after them.
- **Batch 4 is verification, not implementation.** A failure there is a finding, not
  a quiet fix.

---

## Batch 0: Milestone Design (planning, no code)

**Maps to GitHub Issue:** `#39 Milestone Planning & Adaptive Telemetry Design`

- [x] **T092: Author the M5 design documents — this batch.**

  `specs/006-adaptive-router/spec.md`, `plan.md`, `tasks.md`, plus ADR-0012
  (observed telemetry and adaptive reliability) and ADR-0013 (offline replay for
  adaptive policy evaluation).

  **The design exercise the roadmap asked for is recorded with reasons, not
  assumptions** (spec § *Clarifications Recorded*):

  - *Rules, statistical scoring, bandits, or ML?* A time-decayed EWMA with a sample
    floor (S1). Transparent, one formula, recoverable, and testable without a
    training set.
  - *How does reliability enter the M4 score?* A bounded multiplier after the
    weighted score, not a weight and not a filter (S2).
  - *What counts as a failure?* Provider-attributable completion outcomes only;
    `429`, request-fault `4xx` and client aborts are excluded (S3).
  - *What can "dynamic quality" mean?* Structural validity only; the operator
    quality prior is untouched (S5).
  - *How is the estimator evaluated without spending quota?* Offline replay of
    metadata-only observations (S9).

  **The roadmap's separate research topic is answered as a boundary, not a
  promise.** M5 does not commit to a learning technique beyond the estimator it
  implements, and it does not infer semantic quality. The alternatives are recorded
  with why they were rejected.

  **The hard $0 constraint and explainability are preserved by construction.** No
  new path contacts a provider; replay reads a local file. Every `free` decision
  still emits one content-free record, now naming the multiplier and sample count.

**Verification & Stop:**

1. `markdownlint` clean.
2. Every open question in #39 has a recorded decision with its reasoning and the
   alternatives rejected.
3. Every acceptance criterion in #39 is traceable to a section of `spec.md` and a
   batch in this document.
4. `spec.md` stateable in one sentence: *the router learns how reliable and how fast
   each free model actually is, uses that evidence to adjust `free` selection, and
   can show the evidence in the log without ever storing a prompt.*
5. **STOP**, review, then merge the design batch and start Batch 1.

---

## Batch 1: Observe and Persist

**Maps to GitHub Issue:** `#25 Latency, Error Rate & Tool Call Telemetry Collector`

- [ ] **T093: Add the `telemetry` configuration block and validation.**

  `core/config.py`: `TelemetryConfig` with `half_life_seconds`, `min_samples`,
  `reliability_floor`, `retention_days`, `max_observations`; `Config.telemetry`
  optional. Presence enables collection; absence means exact M4 behaviour (S8).

  Validation names the offending key: non-positive half-life, `min_samples < 1`,
  floor outside `[0, 1]`, non-positive retention or row cap, unknown key. Add the
  block to the shipped `config.yaml` with the documented defaults.

  **A test asserts that no `telemetry` block leaves M4 scores unchanged**, so the
  switch is proven rather than asserted.

- [ ] **T094: Create the telemetry tables and migration.**

  `db/schema.py`: `telemetry_aggregates` and `telemetry_observations` as specified
  in `plan.md` § *Data / Persistence Changes*. Migration `0004_telemetry.py` creates
  both tables and their indexes; the downgrade drops only them.

  **No column can hold payload.** The observation table stores outcome, status,
  latency, validity flags, token counts and the decision snapshot; the decision
  snapshot stores model ids, components and weights. A schema test enumerates the
  columns and asserts that none is a text body for prompt, completion, tools or
  images.

- [ ] **T095: Implement the EWMA and the estimate state.**

  `core/telemetry.py`: `Outcome`, `ObservedEstimate`, `EstimateState`, `decay()`.
  Four metrics per model — latency, completion success, tool-call validity, quota
  prediction error — each with its own value and sample count. Missing data is
  `None`, not zero. Only `success`, `invalid_response` and `timeout` update the
  scored latency metric; every outcome records its latency for diagnostics.

  The floor is per metric. `decay()` clamps negative elapsed time to zero and seeds
  the first observation directly. Unit tests cover half-life, long gaps, first
  value, and the floor boundary.

- [ ] **T096: Implement the recorder, the queue and the writer.**

  `core/telemetry.py`: `TelemetryRecorder.observe()` updates in-memory state
  synchronously and enqueues; `estimates_for()` returns a frozen value;
  `start()`/`close()` manage one background writer. The queue is bounded and never
  blocks the request path; a full queue drops the newest observation and counts it.

  `telemetry/persistence.py`: one transaction per observation inserts the row and
  upserts the aggregate. A failed write logs once per transition and leaves the
  in-memory estimate authoritative. A test injects a failing writer and asserts the
  caller is unaffected.

- [ ] **T097: Restore estimates at startup and enforce retention.**

  Load `telemetry_aggregates` before traffic is served; skip a corrupt row with a
  warning that names provider/model. Do not replay observations into estimates.

  Retention runs at startup and periodically: delete observations older than
  `retention_days`, then the oldest rows beyond `max_observations`. Aggregates are
  never pruned. Tests cover age, count, and the no-op case.

- [ ] **T098: Classify outcomes and structural validity.**

  `classify_failure(exc) -> Outcome` maps the existing exception types to
  `provider_error`, `rate_limited`, `timeout`, `transport_error`,
  `request_error` and `client_aborted`, using the table in `spec.md` § 2.
  `structural_validity(request, response)` checks tool-call names and JSON
  arguments, JSON content for `json_object`/`json_schema`, empty completions, and
  the streaming terminal event.

  The streaming renderer accumulates tool-call argument fragments so a reassembled
  call is judged, not a partial one. Tests cover each outcome and each validity
  case, including the Gemini whole-call-in-one-delta shape.

- [ ] **T099: Wire observation into both dispatch paths.**

  `core/dispatch.py`: optional recorder; start a monotonic timer at entry and record
  one terminal observation in a `finally`. Internal retries and key failovers are
  one request, not several observations.

  `api/stream_dispatch.py` and `api/routes.py`: record a pre-delivery failure from
  the exception; record the terminal stream outcome after the iterator finishes or
  raises; classify a client disconnect as `client_aborted`. Ensure exactly one
  observation per request on each path.

  **Verification:** a unary request and a streaming request against the same
  synthetic provider produce the same outcome category; a retried-then-successful
  request produces one `success`, not a failure plus a success.

**Batch 1 stop condition:** telemetry enabled, requests served unchanged, and the
database contains the expected rows with no payload. `make check` green.

---

## Batch 2: Estimates in Scoring

**Maps to GitHub Issue:** `#26 Dynamic Reliability & Quota Prediction Update Feedback Loop`

- [ ] **T100: Read estimates in `score_model`.**

  `core/routing.py`: `score_model(model, policy, estimates=None)`. `None` produces
  the M4 score exactly. `ModelScore` gains `base`, `multiplier` and `observed`;
  `total` becomes the final score. `resolve_request`/`select_model` take an optional
  `estimate_lookup` callable so routing does not import persistence.

  **A parity test asserts `estimates=None` is byte-for-byte the M4 score** for the
  existing M4 fixture set.

- [ ] **T101: Apply the reliability multiplier and the latency source switch.**

  Compute `multiplier = floor + (1 - floor) * success_rate` when the outcome floor
  is met, else `1.0`. Use observed total latency when the latency floor is met, else
  the operator prior. Clamp the final score to `[0, 1]`; never exclude on estimate.

  Tests: zero success rate yields `floor`, not exclusion; recovery raises the
  multiplier; observed latency above the ceiling scores `0` and stays eligible.

- [ ] **T102: Extend the decision record and the log allowlist.**

  `RoutingDecision` gains `estimates` (enabled, half-life, min samples, floor) and
  per-candidate `observed` (reliability, multiplier, latency, latency source,
  samples). `core/logging.py` allowlists exactly the new field names.

  The record must satisfy `score == sum(contributions) * multiplier` and contain no
  content. A test parses the JSON log line and asserts both.

- [ ] **T103: Wire the recorder into the route and prove explicit parity.**

  `api/routes.py` constructs the recorder during startup, passes the estimate lookup
  to `resolve_request`, and keeps the recorder on `app.state` for shutdown. Explicit
  requests still bypass scoring and emit no `routing_decision`.

  **Verification:** the full existing explicit-model suite passes with telemetry
  enabled and non-neutral estimates present.

**Batch 2 stop condition:** `free` selection responds to observed reliability and
latency; explicit selection does not; every decision is explainable. `make check`
green.

---

## Batch 3: Replay and Defaults

- [ ] **T104: Implement the pure replay function.**

  `telemetry/replay.py`: `replay(observations, settings) -> ReplayReport`. Sort by
  timestamp, run the same EWMA state machine as the recorder, and for each `free`
  decision snapshot recompute base scores under the supplied weights, apply the
  multiplier, and re-rank.

  The report includes final estimates, multiplier ranges, selections that changed,
  longest demotion, recovery time, quota-error and tool-validity summaries, the
  settings used, and the stated limitation. Tests use a hand-built sequence and
  assert the estimates match the recorder exactly.

- [ ] **T105: Add the `python -m` entry point.**

  `python -m free_router.telemetry.replay --db data/router.db --half-life 3600
  --min-samples 5 --floor 0.5 [--weights quality=0.6,tool_support=0.3,latency=0.1]`
  prints JSON. Opens SQLite read-only, writes nothing, makes no HTTP call. A missing
  or empty database exits non-zero with the path named; malformed rows are skipped
  and counted.

  **A test installs an HTTP transport that fails on use**, proving the harness
  cannot spend quota.

- [ ] **T106: Evaluate the shipped defaults and record the result.**

  Run replay over the observations collected in Batch 1/2's live use and over the
  deterministic fake-provider sequence. Compare the shipped defaults against at
  least two alternatives for half-life, floor and weights. Record the numbers and
  the chosen defaults in this document and in the milestone report.

  If a default changes, it changes in `config.yaml` and the reason is recorded. The
  formula does not change to fit the data.

**Batch 3 stop condition:** replay reproduces the live estimates, the defaults are
justified with numbers, and no provider call is possible from the harness. `make
check` green.

---

## Batch 4: Verification and Milestone Report

- [ ] **T107: Build the exit-criteria matrix and run the deterministic paths.**

  Every `spec.md` acceptance scenario gets a named test. The deterministic
  fake-provider harness drives: success, `5xx`, `429`, timeout, invalid tool call,
  invalid JSON, empty completion, client abort, recovery, restart restore, retention,
  and persistence failure.

  Record each row as **observed** with the command and result, not as "expected".
  A scenario that cannot be observed is recorded as not observed, with the reason.

- [ ] **T108: Run the live verification.**

  With telemetry enabled and real credentials, drive unary and streaming requests
  through `free`. Confirm from SQLite that observations and aggregates exist, that a
  real `429` was recorded as `rate_limited` and did **not** move reliability, and
  that a restart restored the aggregates. Capture the `routing_decision` records and
  the replay report.

  **No injected faults in the live run.** Provider faults that occur are recorded as
  they happen; the deterministic harness is where failures are injected. This is the
  M4 Batch 6 precedent.

- [ ] **T109: Write the milestone report and update the exit criteria.**

  Update this file: check the tasks, add the run matrix, move the exit-criteria table
  from *Planned* to *Met* only where evidence exists, and add carried items. State
  the three claims separately if an integration PR is involved: work merged to `M5`,
  `M5` merged to `main`, milestone closed.

  The report must state the small-sample limitation plainly: at this project's
  traffic volume, M5's estimates are useful for gross reliability and latency
  differences, not fine-grained quality ranking.

- [ ] **T110: Close out and carry forward.**

  Open the `M5 → main` integration PR after the batch PRs land, verify
  `compare/main...M5` reports `ahead_by: 0`, then close the epic and milestone.
  Carried items are recorded here, not dropped: per-key reliability, semantic
  quality, estimator correction from the quota-error metric, multi-hop chains, and
  any observation the live run could not produce.

**Batch 4 stop condition:** every exit criterion is met with evidence or recorded as
unmet, `make check` and `make test-hermetic` are green, and the milestone report is
in this file.

---

## Exit Criteria

| Criterion | Source | Status |
| :--- | :--- | :--- |
| Provider/model latency is recorded, with TTFB and total | ROADMAP M5 | Planned |
| Observed success/failure rates are recorded and update estimates | ROADMAP M5 | Planned |
| Tool-call success/failure is recorded where tools are used | ROADMAP M5 | Planned |
| Quota prediction accuracy is recorded | ROADMAP M5 | Planned |
| Reliability/availability estimates update over time and recover | ROADMAP M5 | Planned |
| Dynamic quality is explored and its boundary documented | ROADMAP M5 | Planned |
| Adaptive routing policies are evaluated without spending quota | ROADMAP M5 | Planned |
| Hard $0 constraint is preserved | CONSTITUTION §2.1 | Planned |
| Major routing decisions remain explainable | CONSTITUTION §9, ROADMAP §3.16 | Planned |
| Explicit-model behaviour is unchanged | CONSTITUTION §7.2 | Planned |

## Carried Items

Recorded as they are found; the list is intentionally empty until Batch 4.

- (none yet)
