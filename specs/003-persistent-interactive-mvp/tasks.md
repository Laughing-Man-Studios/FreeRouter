# Task Breakdown: M2 Persistent & Interactive MVP

**Maps to GitHub Epic:** `#11 [M2 Epic] Persistent & Interactive MVP`
**Planning Issue:** `#36 Milestone Planning & Persistence Design`
**Specification:** `specs/003-persistent-interactive-mvp/spec.md`
**Plan:** `specs/003-persistent-interactive-mvp/plan.md`

**This file supersedes the planning task list in #36.** Those eight items are design
decisions, all of which are now made and recorded in `spec.md` § *Open Decisions
Requiring Sign-Off* and ADRs 0005–0007. What remains below is implementation work.

**Agent Instruction:**

- Implement **one batch at a time**.
- At the end of each batch, execute the explicit **Verification & Stop** criteria.
- Use `gh` to update the corresponding GitHub Issue status.
- Do not proceed to the next batch until the current batch is verified and human
  review is complete.

**Task IDs continue from M1** (which used T001–T040). Task IDs are stable
identifiers, not execution order — M1's sequence was revised twice and the ranges no
longer track batch numbers. Follow the batch numbers.

---

## Ordering Rationale

The batches are **not** the order the issues were created in, and the reasoning is
worth stating because two of the reorderings were forced by dependencies that were
only visible once the plan existed.

```text
Batch 0  Debug redaction fix (M1 defect)      #66
Batch 1  Persistence schema & migration      #12
Batch 2  Persistence layer & restore          #12
Batch 3  Token estimation & reservation       #13
Batch 4  GET /v1/models                       #14
Batch 5  SSE streaming & fail-fast            #15
Batch 6  Error classification & verification  #12, #15
```

**Batch 0 is a defect fix, not planned work.** It leads because it is small,
independent of everything else, and it repairs the instrument M2's diagnosis work
depends on. It was found while measuring provider-reported usage for the M2
output-token-assumption decision, so it is adjacent to work M2 is about to do.

**Batches 1–2 come first because they close a constitutional deviation.** ADR-0005
closes CONSTITUTION §8.3, and ROADMAP §3.14 places it first among M2's
deliverables. It is also the only M2 work that is *required* rather than additive.

**Batch 3 follows persistence, not precedes it.** An estimator is easy to write and
easy to get subtly wrong; a persisted counter built on a wrong estimator is harder to
correct afterwards because the error is now in durable state. Estimating first means
any recalibration happens in memory where it is cheap, which is also what ADR-0006
§6's console cross-check needs.

**Batch 5 (streaming) is last among features** because it is the largest and the
only one whose design could still change the interfaces above it. It depends on the
adapter protocol gaining a method, which is cheaper to do once persistence has
settled what the adapters are allowed to know.

**Batch 6 exists because error classification is cross-cutting.** It touches both
adapters and touches the dispatcher's use of their results, so folding it into either
adapter's batch would mean reviewing an adapter change and a classification change
together, which is how a behavioural regression hides.

---

## Batch 0: Debug Payload Redaction Fix (M1 defect)

**Maps to GitHub Issue:** `#66 Debug payload redaction mangles token usage fields and misses private_key`

**This is a defect fix, not planned milestone work.** It is M1's T038 code and was
found on 2026-10-19 while measuring provider-reported token usage for the M2
output-token-assumption decision — the usage figures were the first thing the
redaction mangled.

It sits ahead of Batch 1 because it is small, it is independent of every other
task, and leaving a known defect in the debug stream through a milestone that
depends on that stream for diagnosis would be the wrong trade.

- [ ] **T066: Replace the debug redaction rule.** The loose-substring rule
  matched the bare word `token`, which produced two failures at once:
  **over-redaction** of legitimate usage fields (`promptTokenCount`,
  `candidatesTokenCount`, `totalTokenCount`, `max_tokens`, `token_count`) and
  **under-redaction** of real key material (`private_key` was not redacted at
  all).

  Replaced with a **phrase** rule — `apikey`, `authorization`, `credential`,
  `password`, `secret`, `passphrase`, `accesstoken`, `authtoken`, `apitoken`,
  `bearertoken`, `idtoken`, `refreshtoken`, `sessiontoken`, `privatekey`,
  `signingkey`, `encryptionkey`, `clientkey` — with the bare word `token`
  deliberately removed, plus one narrow value-shape rule for **ambiguous terminal
  words**: a key ending in `token`/`tokens`/`key`/`secret`/`password`/
  `credential`/`bearer` is redacted only when its value is a 16+ character
  string. So a JWT-shaped value is caught, a count is not, and `key_alias`
  survives.

  Keys are matched on **word boundaries** (separators *and* camelCase), so
  `promptTokenCount` and `prompt_tokens` are both handled.

  **The module docstring changes too.** It currently claims redaction is
  "name-based rather than pattern-based", which was never quite true and is
  less true now. The new rule consults a value's shape in exactly one narrow
  case, and the docstring has to say so rather than overclaim.

  This is the same class of defect as the `x-goog-api-key` miss fixed in M1
  (T038): a substring rule simultaneously too broad and too narrow. **A rule that
  produces both kinds of failure is not a rule.**

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`, full `pytest`.
2. The acceptance criteria in #66 pass — the legitimate usage fields survive, the
   credential names are redacted, and `Authorization` / `x-goog-api-key` do not
   regress.
3. A test asserts Gemini's `usageMetadata` survives end to end, so the
   regression cannot return silently.
4. **STOP**, review, then update #66.

---

## Batch 1: Persistence Schema & Migration

**Maps to GitHub Issue:** `#12 Single-File Local Persistence Engine`

- [x] **T041: Define the persisted state schema — complete.** Four tables per
  `plan.md` § *Data / Persistence Changes*: `quota_windows`, `cooldowns`,
  `model_status`, `key_state`.

  **`entries` is a JSON list of reservations**, each `{at, reserved, actual}`. The
  original draft specified a JSON map of bucket ordinal to amount, which would
  have been wrong twice over: a rolling window sums by *age*, not by bucket, so
  a per-bucket total cannot reconstruct it; and collapsing the estimate into the
  effective figure would make estimator drift unmeasurable after a restart.
  Timestamps are **wall-clock**, because a monotonic value is only meaningful
  against the current boot and would restore nonsense after a reboot.

  **`reset_at` exists for fixed windows only.** Google's RPD resets at midnight
  Pacific; a rolling window has no such instant. One nullable column keeps the
  dimension model uniform.

  **`key_state` carries `updated_at`.** `suspended` is persisted for inspection,
  and a suspension with no timestamp says only that it happened, not when.

  **No payload columns exist in any table**, and no key-material column either —
  only `alias`. That is what makes a state dump safe to paste into an issue.

- [x] **T042: Alembic revision 0002 — complete.** Adds the four tables. Must apply cleanly to a
  database created by revision 0001 and must be idempotent on re-run, since
  migrations run at every startup.

  **The pre-Alembic adoption path is reconsidered here**, because M0's docstring
  said it had to be: its expected table set was derived from the *current* schema,
  which is only correct while there is exactly one revision. The set is now pinned
  to the initial revision, and the adoption path **stamps at that revision and
  then upgrades** — stamping at head would mark a legacy database as already
  carrying the new tables, and they would never be created.

- [ ] **T043: ~~Extract `QuotaTrackerProtocol`~~ — DROPPED, and the drop is informative.**

  Originally planned as the test of ADR-0004's claim that *"the interface, not the
  storage, is the durable decision"*: extract a protocol so a second, SQLite-backed
  implementation could be checked against it.

  **Decision A3 collapsed the reason for it.** There is one implementation — hot
  in-memory state with a persistence layer attached — so there is no second
  implementation to keep in step, and no protocol to extract. The claim is now
  tested differently: by checking that `reserve`/`reconcile`/`available` keep their
  existing signatures *including* `Reservation | None` and `int | None`, and that
  the dispatcher, key manager and adapters are untouched. If attaching persistence
  turns out to require changing the dispatcher's call into the tracker,
  ADR-0004's claim was wrong, and that is a finding to record rather than work
  around.

  **T043's number is retired, not reused.** A retired task number in a stable
  identifier scheme is worth keeping visible rather than silently recycling.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. Migration applies to a fresh database and to one created by revision 0001.
3. The tracker's public surface is **unchanged** — Batch 1 defines the schema and
   does not touch the tracker at all, so `restore()` and the persistence layer
   are Batch 2's work.
4. A test asserts the new tables contain no column able to hold key material or
   payload text.
5. **A legacy pre-Alembic database gains the new tables** rather than failing to
   upgrade.
6. `pytest tests/test_db.py tests/test_migrations.py tests/test_quota_schema.py
   tests/test_quota_tracker.py`
7. **STOP**, review, then update #12.

**Batch 1 verification gate (2026-10-19):**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/` — clean.
2. Migration applies to a fresh database and to one created by revision 0001.
3. **The tracker is untouched** — Batch 1 adds tables and a revision, and changes
   no runtime behaviour. `restore()` and the persistence layer are Batch 2's.
4. A test asserts **no column in any table** could hold key material or payload
   text, checked across every table rather than only the new ones, and that
   `key_state` identifies a credential by alias only.
5. **The migration and the schema definitions are compared against each other**,
   so the two representations of one schema cannot drift apart. A drift would
   surface on a real query rather than at startup, which is the worst place for
   it.
6. **A legacy pre-Alembic database gains the four new tables** rather than
   failing to upgrade — the case M0's adoption path would have broken on.
7. The pre-existing gap in the fixed window's boundary is **recorded rather than
   silently inherited**: `WindowReset.FIXED` is documented as resetting at a
   wall-clock boundary, but the implementation buckets on the monotonic clock. No
   fixed window is configured today, so nothing misbehaves; Batch 2 must
   reconcile the two, in the window implementation rather than in the persistence
   layer.

**Full suite after Batch 1: 461 passing**, up from 446.

---

## Batch 2: Persistence Layer & Restore

**Maps to GitHub Issue:** `#12 Single-File Local Persistence Engine`

- [x] **T044: `quota/persistence.py` — the persistence layer.** Attached to the
  existing `QuotaTracker`; **the tracker's window logic is unchanged** (A3).

  **Durability is asymmetric** (ADR-0005): cooldowns and status transitions write
  through immediately; counters and LRU ordinals batch behind a **20 s** flush
  interval (B2), coalesced so N requests in one interval produce one additive
  write.

  **There is no second implementation and no fallback class** (A3). The degradation
  path is a state, not a class: an unreadable row means the same tracker starts
  with empty state and continues.

- [x] **T045: `restore()` before the listener serves.** Called in lifespan so no
  request can be dispatched against an unrestored counter. Prunes windows whose
  reset instant has passed; does not restore expired cooldowns; **detects a
  backwards clock jump** by comparing persisted `cooldown_until` against the system
  clock (the primary mechanism — the 24 h cap in B4 is the backstop).

- [x] **T046: Cooldown and breaker write-through.** `cooldown.py` and `breaker.py`
  persist on transition. A failed cooldown write returns `503
  persistence_unavailable` rather than dispatching into a limit the router cannot
  record.

  **Suspension is persisted but not restored** (ADR-0005 §5). A credential revoked
  an hour ago may have been fixed; resurrecting the suspension would refuse a working
  key and produce a confusing 503 instead of the `401` that would diagnose it.

- [x] **T047: LRU ordinal restore.** `key_state` restores selection order. Verified by
  **observed selection order**, not by reading the table — a test that reads the
  table would pass even if selection ignored it.

- [x] **T048: Failure degradation.** An unreadable or corrupt row discards that
  unit's state and continues with in-memory accounting, recording it. A general
  restore failure degrades the tracker and startup **succeeds**. A crash-loop is worse
  than forgetting a cooldown.

**Batch 2 verification gate (2026-10-19):**

1. Full `pytest`, `ruff`, `ruff format --check .`, `mypy --strict src/` — clean.
2. **Scenarios 1–4 and 12 pass against a real process restart.** The restart
   tests run **two separate processes** — one writes the state and exits, the
   second constructs fresh objects and restores from the database. A single
   process that cleared its objects would pass even if the state only ever
   lived in memory, which is the failure this milestone exists to prevent.
3. **The event loop stays responsive** under concurrent reservation — a
   heartbeat coroutine must keep ticking while reservations are made. This is
   the CONSTITUTION §8.3 requirement and the easiest thing to violate silently.
4. **Suspension survives a store round-trip but not a restart**, and the LRU
   ordinal counter is **seeded above the highest restored ordinal** — a fresh
   process starts its counter at zero, so a restored ordinal of 500 and a fresh
   one of 1 would invert the LRU order *and* collide with it.
5. **A failed cooldown write yields `503 persistence_unavailable` and no
   dispatch.**
6. **Full suite after Batch 2: 471 passing**, up from 461.

**Three things changed the design, and are recorded rather than absorbed.**

**The dispatcher is no longer untouched.** The plan said `core/dispatch.py` would
be unchanged "in its entirety". It is not, and the reason is worth stating: a
durability feature cannot report its own failure through a fire-and-forget task,
so the two state-changing calls the dispatcher makes — entering a cooldown and
recording a breaker failure — became `async` and are now awaited. A third change
adds the gate that refuses a unit whose limit could not be recorded. **The
dispatcher's logic, contract and measured behaviour are unchanged**; three call
sites gained an `await` and one gate branch was added.

**A third migration was needed.** `model_status` had no column for the breaker's
threshold, and the threshold is the whole meaning of the degraded state: a model
shown to fail only on a 900k-token request must still serve a 4k-token one.
Without it, a restart would either forget the degradation or degrade the model
without knowing which requests it applies to. `detail` could have carried it as
text, but a value that has to be parsed back out of a string is a value that
will eventually be parsed wrongly. Revision `0003_model_status_threshold` adds
the column.

**A real bug was found and fixed during the batch.** The wall-clock conversion
for a fixed window's reset instant *subtracted* the offset where it had to add
it, producing a boundary far in the past. Every fixed window then looked
already-reset, and its restored usage was silently discarded — the failure
looked like "the counters did not survive", which is exactly what the test was
written to catch.

**One documentation claim was corrected rather than preserved.**
`WindowReset.FIXED` promised a reset "at a wall-clock boundary, e.g. midnight
Pacific for Google's RPD", but the implementation buckets on the monotonic clock,
so a fixed window resets at an arbitrary offset from boot. The persisted
boundary is now the boundary the code actually honours, expressed in wall-clock
terms, and the docstring says so. No fixed window is configured, so nothing
misbehaves today.

**Verification & Stop:**

1. Full `pytest`, `ruff`, `mypy --strict`.
2. **Scenarios 1–4 and 12 pass against a real process restart** — a real `stop`,
   a real `start`, not a simulated store swap.
3. An event-loop responsiveness test passes under concurrent reservation
   (CONSTITUTION §8.3: the database must not block the loop). A heartbeat coroutine
   must keep ticking during writes.
4. Suspension survives a store round-trip but **not** a restart.
5. A failed cooldown write yields `503 persistence_unavailable` and **no dispatch**.
6. **STOP**, review, then update #12.

> **On completing this batch:** the §8.3 deviation is discharged. Remove it from
> `specs/002-mvp-openai-proxy/spec.md` § *Recorded Deviations* **and record the
> removal** — the change is deliberate, not a silent edit. Close the deviation in
> ADR-0004 §6 by reference.

---

## Batch 3: Token Estimation & Pre-Dispatch Reservation

**Maps to GitHub Issue:** `#13 Token Estimation & Pre-Dispatch Quota Reservation System`

- [ ] **T049: `quota/estimation.py`.** Separate `estimated_input` from
  `assumed_output`. Reserved = `ceil((input + output) * safety_margin)`, margin
  applied inside the tracker so a caller cannot forget it (M1's existing property).

  **Both the output assumption and the safety margin become configuration**
  (ADR-0006, B3). M1's estimate silently assumed zero output tokens, which is what
  actually breaches a TPM ceiling, and the margin was a constructor parameter
  unreachable from `config.yaml`.

  **The output assumption's value is still open** (B1). It is recorded in
  `spec.md` § *Clarifications Recorded* B1 with the supporting measurement:
  a short-paragraph response measured 146–178 output tokens, and TPM is ~16×
  slack at the measured burst limits, so a generous default costs little. **This
  task does not start until B1 is answered.**

  The §6.1 key cap is unaffected and stays in code.

- [ ] **T050: Wire provider-reported usage.** Both adapters return actual usage where
  the provider reports it; `reconcile` receives `None` where it does not.

  **Provider-reported usage is authoritative where present.** If either provider
  reports cumulative rather than per-request usage, that must be handled explicitly
  — a cumulative figure would inflate reconciliation without bound.

- [ ] **T051: Reconciliation rules.** Confirm and test that reconciliation supersedes
  the estimate for the entries the reservation created, leaves the estimate standing
  on `None`, and is idempotent (ADR-0006). These are existing behaviours; this task
  makes them specified and tested rather than incidental.

  **The estimate and the actual are persisted separately**, not collapsed into one
  effective figure, so the drift measurement in T052 survives a restart.

- [ ] **T052: Record the tolerated error.** Update `spec.md` with the measured
  residual against the provider console per ADR-0006 §6.

**Verification & Stop:**

1. Full `pytest`, `ruff`, `mypy --strict`.
2. **Scenarios 5–7 pass**, including that `actual_tokens=None` leaves the estimate
   standing rather than releasing capacity.
3. A test asserts the reservation covers input **plus** the configured output
   assumption — the regression M1's estimator would have permitted.
4. **Re-run the console cross-check** against persisted counters (ADR-0006 §6). M1
   found +1 over 54 dispatches; a growing delta means recalibrate rather than tune.
   Record the figure and its conditions.
5. **STOP**, review, then update #13.

---

## Batch 4: `GET /v1/models`

**Maps to GitHub Issue:** `#14 GET /v1/models Model Discovery Endpoint`

- [ ] **T053: `api/models_route.py`.** OpenAI-compatible shape from
  `config.models` — the same list that drives routing. No discovery call, no cache
  to invalidate, no second source of truth.

  **`created` is the config file's mtime, `owned_by` is the deployment owner, and
  `permission`/`default` are emitted** (B5). The config mtime changes only when
  the operator changes it, so a client re-fetching does not see a spurious
  "model updated".

- [ ] **T054: Availability as a separate field.** **A configured model is always
  listed, including while in cooldown.** Hiding it would make a client's model list
  oscillate with transient provider state, and a client re-fetching on that basis
  would flip-flop between requests.

**Verification & Stop:**

1. Full `pytest`, `ruff`, `mypy --strict`.
2. **Scenario 8 passes** — a cooling model is still listed.
3. The response shape matches the OpenAI model object; ids come from configuration.
4. No new source of truth: every listed id is dispatchable or explicitly marked
   unavailable.
5. **STOP**, review, then update #14.

---

## Batch 5: SSE Streaming & Fail-Fast

**Maps to GitHub Issue:** `#15 OpenAI-Compatible SSE Streaming Translation & Fail-Fast Mechanics`

- [ ] **T055: `stream_chat_completion` on the adapter protocol,** as a sibling of
  `chat_completion` rather than a `stream: bool` parameter (ADR-0007). The two have
  different contracts: one returns a complete body, the other a sequence that can
  fail partway, and a flag would force every caller to handle the union.

  Plus `supports_streaming: bool`. A provider that cannot stream raises
  `StreamingUnsupportedError` — **an empty iterator and an unsupported stream are
  different failures and must not share an outcome.**

- [ ] **T056: `api/streaming.py`.** Translate provider chunks to OpenAI-compatible
  SSE, terminated by `data: [DONE]`, for both Gemini and Mistral.

  **A `role` delta is emitted first, `usage` is included on the final chunk, and
  `stream_options: {"include_usage": true}` is honoured** (B7).

- [ ] **T057: Fail-fast at the first delivered chunk** (ADR-0007). Enforced in the
  route layer, because the adapter cannot know whether anything reached the client.

  **Zero chunks is not "after stream start"** — nothing was delivered, so it retries
  within the existing budget. This distinction is what makes the rule precise rather
  than merely conservative.

  **The dispatcher is not touched** (A2). The streaming path in the route
  re-implements model resolution, key eligibility, capacity gating and the bounded
  retry budget, and must re-implement them **identically** — including taking
  exactly one `(provider, model)` pair, so CONSTITUTION §7.2 holds there too.

- [ ] **T058: Reservation lifecycle under streaming.** An interrupted stream
  reconciles with whatever usage was reported; **no reservation leaks**. Streaming
  holds a reservation far longer than a unary request, so a leak here is materially
  worse than M1's case.

- [ ] **T059: Route branch.** `routes.py` branches on `stream`. **The unary path is
  unchanged** — it is the path M1 measured at 100/100, and a streaming change that
  alters it would invalidate that measurement.

**Verification & Stop:**

1. Full `pytest`, `ruff`, `mypy --strict`.
2. **Scenario 9 passes**: an interruption after the first chunk terminates the stream
   and **never dispatches to a second provider** — asserted against every call's URL
   and body, as M1's cross-model test did.
3. A zero-chunk interruption **does** retry within budget.
4. An interrupted stream leaves no pending reservation.
5. A non-streaming adapter raises rather than returning an empty iterator.
6. `CONSTITUTION` §7.2 intact: the model never varies after delivery begins.
7. **STOP**, review, then update #15.

---

## Batch 6: Error Classification, Measurement & Milestone Verification

**Maps to GitHub Issues:** `#12`, `#15`

- [ ] **T060: Improved provider error classification** (A4). All four:

  1. **Provider-specific error codes surfaced to the client** — Gemini's
     `INVALID_ARGUMENT`, Mistral's `invalid_api_key`, so a client distinguishes a
     payload problem from a credential problem without parsing prose.
  2. **Burst `429` distinguished from daily-cap exhaustion.** Identical on the
     wire, but the first is transient with a short window and the second means the
     day's budget is spent and retrying cannot help.
  3. **`5xx` overload separated from upstream defect.** An overload signal is
     retryable; a provider bug is not, and retrying it burns quota to learn
     nothing.
  4. **The provider's own retryable signal trusted** where supplied, rather than
     the router's own guess.

  Ordinary `400`s must not degrade a model; only a genuine context-length refusal
  does.

- [ ] **T061: Manual integration-test matrix** (#36 deliverable). Success, `429`,
  `4xx`, `5xx`, timeout, **mid-stream interruption**, and **post-restart behaviour**,
  per provider. Recorded with conditions, not as pass/fail assertions alone.

- [ ] **T062: Re-measure the success rate, streamed and unstreamed separately.**
  M1's 100/100 covers unary requests only. Streaming holds connections open longer,
  so the same fault rate produces more interruptions — and interruption after the
  first chunk is **not** recoverable by retry. Reusing M1's figure for streamed
  traffic would be unfounded.

  Record per `spec.md` § *Measuring the success rate*, including request volume and
  whether the window coincided with console-reported usage.

- [ ] **T063: Re-run the console cross-check against durable counters** (ADR-0006
  §6), confirming drift has not accumulated.

- [ ] **T064: Confirm the §8.3 deviation stays closed** and remove the M1 entry only
  once Scenarios 1–4 pass against a real restart.

- [ ] **T065: ~~Revisit OpenHands~~ — DEFERRED to M3 (A1).** The OpenHands objective
  is moved to M3, where capability-aware routing and tool support converge. M2
  delivers the capabilities the objective needs — streaming and model discovery —
  and does not claim the workload. Recorded in `spec.md` § *Clarifications
  Recorded* A1 and in the ROADMAP's Milestone 2 section.

  **M2's exit criterion is restated accordingly.** The ROADMAP's wording — "can
  serve interactive OpenHands workloads with streaming where supported" — is not
  met by M2 and is not claimed to be. What M2 demonstrates: survival across
  restart with cooldown and counter state intact (Batches 1–2), and streaming plus
  model discovery serving an interactive client (Batches 4–5).

**Verification & Stop:**

1. Full `pytest`, `ruff`, `ruff format --check .`, `mypy --strict src/`.
2. Manual matrix executed and **recorded with its conditions**.
3. Streamed and unstreamed success rates recorded separately, each with volume.
4. Console cross-check recorded.
5. **Confirm the §8.3 deviation remains closed.**
6. **STOP**, review, then close #12, #13, #14, #15, and milestone #3 with epic #11.

---

## Milestone Exit Criteria

Per ROADMAP Milestone 2: *"The router survives a restart without immediately losing
its quota state and can serve interactive OpenHands workloads with streaming where
supported."*

**The second clause is deferred to M3** by decision A1, and M2 does not claim it.
The clause was unreachable in M2 regardless of effort: "interactive OpenHands
workloads" implies agentic use, which requires tool calling — explicitly outside
this milestone's boundary.

What M2 demonstrates:

| Criterion | Batch |
| :--- | :--- |
| Survives a restart without losing quota state | 1–2 |
| Streaming supported where the provider supports it | 5 |
| Model discovery available | 4 |
| Token accounting reconciles against provider-reported usage | 3 |

---

## Carried Into M3, Not Deferred By Choice

| Item | Source | Note |
| :--- | :--- | :--- |
| **OpenHands as a real workload** | A1 | **Deferred to M3 by decision.** Tool calling converges with capability-aware routing there. |
| Tool calling | #36 non-goal | Next compatibility feature after streaming. OpenHands agentic use depends on it. |
| Capability-aware routing | M3 | — |
| Cross-file deployment contract enforcement | M1 Batch 6 | `config.yaml` ↔ `docker-compose.yml` ↔ `ci.yml` is checked only by the CI smoke test. Warnings, not enforcement. |
| Mistral RPD measurement | ADR-0006 §7 | Persistent counters make this answerable empirically for the first time. |
| Cross-provider stream resume | ADR-0007 §4 | No resumable offset on either free tier. Recorded as unavailable. |
| `request_logs` population | B6 | Deferred. A migrated table that is never written is a loose end, and M2 is the milestone that makes real persistence possible. |
| The §8.3 deviation | ADR-0005 | Closed in Batch 2, verified against a real restart, then removed from M1's `spec.md` as a recorded change. |
