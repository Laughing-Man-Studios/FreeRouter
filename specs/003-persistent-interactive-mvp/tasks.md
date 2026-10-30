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

**A correction, 2026-10-26: the layer was built, but the application never used
it.**

Batch 2 delivered `quota/persistence.py`, the migration, and every component's
export and import, and verified them with two-process restart tests. What it did
**not** do was connect them to the running application. `QuotaStateStore` and
`PersistenceCoordinator` were imported by nothing outside their own module,
`app.state.db_engine` was created and then used only to dispose of it, and there
was no restore in lifespan and no flush task. **Every table stayed empty however
much traffic the router served.**

The restart tests passed because `tests/restart_helper.py` constructs the tracker
and the store *by hand* in a subprocess. That proves the layer works; it cannot
prove the router uses it. The gap was found by live traffic during Batch 3's
T052 measurement — the persisted counters that task requires to exist did not
exist — and it is why the wiring tests now start from `create_app`.

The wiring, and the tests that would have caught its absence, are in the
`M2-Batch2-Persistence-Wiring` change:

- The store is built over the lifespan-owned engine and passed to the cooldown
  manager and the breaker, so write-through genuinely writes through.
- `restore()` runs **before the lifespan yields**, so no request can be served
  against an unrestored counter.
- The flush coordinator starts with the application and stops with it, flushing
  once more on the way out.
- `config.max_cooldown_seconds` is now passed to `CooldownManager`. It had been
  declared, validated and silently ignored, so B4's configurable cap was not
  configurable.
- `export_windows` no longer persists a window with no entries. It had been
  writing a row for every configured dimension on every shutdown, whether or not
  a request had been served.

**The lesson is about where the test boundary sat.** A test that assembles its
own collaborators verifies the collaborators. It cannot witness a missing *edge*,
and the edge is where the defect was.

Full suite after the correction: **499 passing**, up from 491.

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

- [x] **T049: `quota/estimation.py`.** Separate `estimated_input` from
  `assumed_output`. Reserved = `ceil((input + output) * safety_margin)`, margin
  applied inside the tracker so a caller cannot forget it (M1's existing property).

  **Both the output assumption and the safety margin become configuration**
  (ADR-0006, B3). M1's estimate silently assumed zero output tokens, which is what
  actually breaches a TPM ceiling, and the margin was a constructor parameter
  unreachable from `config.yaml`.

  **The output assumption is 1025, configurable globally** (B1, agreed
  2026-10-20). It is recorded in `spec.md` § *Clarifications Recorded* B1 with
  the supporting measurement: a short-paragraph response measured 146–178 output
  tokens, and TPM is ~16× slack at the measured burst limits, so a generous
  default costs little.

  **The tracker composes the reservation, not the caller.** The caller estimates
  the *input*; the tracker adds the output assumption it holds. A caller can no
  more forget the output half than it could forget the margin.

  **The breaker keeps using the input estimate.** A context-length refusal is
  about the prompt, so the breaker's threshold is compared against the input
  estimate, not the reserved total.

  The §6.1 key cap is unaffected and stays in code.

- [x] **T050: Wire provider-reported usage.** `NormalizedResponse` gains a usage
  field; both adapters populate it from what the provider reports — Gemini's
  `usageMetadata`, Mistral's `usage`. `reconcile` receives `None` where the
  provider reports nothing.

  **Until M1 the normalised response carried no usage**, so every reconciliation
  received `None` and every reservation's estimate stood. The plumbing was
  already there and had nothing to read.

  **Provider-reported usage is authoritative where present.** A cumulative figure
  reported as if it were per-request would inflate reconciliation without bound,
  so an actual more than an order of magnitude above the reservation is treated
  as implausible and the estimate stands. This is a heuristic, and it is recorded
  as one: the *authoritative* check on estimator drift is the console cross-check
  in T052.

- [x] **T051: Reconciliation rules.** Confirm and test that reconciliation supersedes
  the estimate for the entries the reservation created, leaves the estimate standing
  on `None`, and is idempotent (ADR-0006). These are existing behaviours; this task
  makes them specified and tested rather than incidental.

  **The estimate and the actual are persisted separately**, not collapsed into one
  effective figure, so the drift measurement in T052 survives a restart.

- [x] **T052: Record the tolerated error — complete (2026-10-27).** Update `spec.md` with the measured
  residual against the provider console per ADR-0006 §6.

  Re-run against live Google Gemini (`gemini-3.5-flash-lite`) traffic and persisted counters
  in SQLite (`data/router.db`).

  5 live unary requests (`ALPHA`, `BRAVO`, `CHARLIE`, `DELTA`, `ECHO`) were dispatched
  and persisted through `quota_windows`.
  - **Requests**: 5 router dispatches vs 5 on the AI Studio console dashboard
    (hourly bucket) -> **Delta: 0 (exact match)**.
  - **Tokens**: Input estimated at 6 tokens/req, reserved at 1135 tokens/req
    (1025 output assumption, 1.1 safety margin). Reconciled upon response receipt
    to provider-reported `usageMetadata` (8, 10, 10, 9, 9 tokens total, summing
    to **46 tokens** across all 5 requests; 35 input, 11 output). AI Studio console
    hourly metric shows exact alignment (~35 input, ~11 output, 5 requests).
  - **Estimator error & residual**: Raw prompt estimate had -1 token error per request
    (6 estimated vs 7 actual input tokens), completely absorbed by the 1025 output
    assumption and safety margin, and fully reconciled to 0 residual drift in persistent
    storage upon completion. Recorded in `spec.md` § *Clarifications Recorded* D1.

**Batch 3 verification gate (2026-10-20):**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`, full `pytest` —
   clean. **491 passing**, up from 471.
2. **Scenarios 5–7 pass**, including that `actual_tokens=None` leaves the estimate
   standing rather than releasing capacity.
3. A test asserts the reservation covers input **plus** the configured output
   assumption — the regression M1's estimator would have permitted.
4. **A provider-reported figure supersedes the estimate**, and an implausible
   one is not believed.
5. **The request dimension is exact** — a provider's token count must not
   inflate it.
6. **T052 is outstanding**: the console cross-check needs live traffic and the
   console figure, so it is recorded as open rather than claimed.

**One real bug was caught by the existing suite, and it was a serious one.**

Reconciliation was writing the provider's **token** count into every entry of the
reservation, including the **request** entry. One request reporting 7 tokens
therefore consumed **seven request-slots instead of one**, and an RPM budget
would have been exhausted roughly seven times faster than the traffic warranted.

**The request dimension is exact** — one dispatched attempt is one request,
whatever the response contained, and a provider's token count says nothing about
how many requests were made. Reconciliation now corrects only the token
dimension.

This is worth dwelling on for a moment. The bug was introduced by *this* batch,
it was caught by a test written for a *different* feature, and it would have
degraded throughput on every request in production. It is also the kind of error
that a test asserting "the reported figure is recorded" would have passed, because
the figure *was* recorded — in the wrong dimension.

**The estimator was consolidated.** `estimate_tokens` lived in the tracker and
duplicated what `quota/estimation.py` now owns. The tracker no longer has an
estimator of its own; there is one implementation, and the two directions —
input, which is measured, and output, which is a policy — are kept apart.

**T052 remains open.** The console cross-check compares accumulated persisted
counts against what the provider actually bills, which needs live traffic and a
figure read from the provider's console. It is the authoritative check on
estimator drift, and this batch has made it *possible* rather than performed it.

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

**A second blocker, 2026-10-26: the read timeout was a constant.**

T052 could not be performed because the router refused every live request. The
cause was not the provider. `HTTP_CLIENT_TIMEOUT` in `api/main.py` fixed the
response **read** timeout at 10 seconds, as a module constant with no
configuration path, so a provider answering in 15–50 s produced a `504` on every
request and no setting could change it.

Two timeouts already existed and only one had been measured. The provider window
(`ROUTER_PROVIDER_TIMEOUT`, 5 s) bounds connection and response **headers**, and
was measured at p50 743 ms and p90 2332 ms. `read` bounds the response **body**,
which spans generation — unbounded in principle — and the 10 s figure was an
inherited httpx default with no measurement behind it for that phase. Conflating
the two is the same mistake the 400 ms routing window made.

`attempt_budget_seconds` did not cover the gap: the deadline is checked only
*before* each attempt, never during one, so a single slow attempt runs straight
past it.

`provider_read_timeout_seconds` is now configuration, defaulting to 120 s. That
default is derived from the workload rather than chosen to feel generous: the
router reserves against a 1025-token output assumption, and at the 15–30
tokens/second a loaded free tier delivers, a full response takes 34–68 s. The
longest generation measured during this investigation was 66.7 s, and it
succeeded once the bound allowed it. `connect`, `pool` and `write` stay
constants: they describe the router's own local connection behaviour, and only
`read` varies by orders of magnitude.
Streaming (Batch 5) supersedes this value rather than extending it — an
incremental read wants a per-chunk bound, not a per-response one.

The same change fixed a third instance of the silent-ignore class:
`quota_flush_interval_seconds` was declared and validated but never passed to
`PersistenceCoordinator`, so B2's 20 s default was the only value that could
take effect.

Full suite after the correction: **509 passing**, up from 499.

---

## Batch 4: `GET /v1/models`

**Maps to GitHub Issue:** `#14 GET /v1/models Model Discovery Endpoint`

- [x] **T053: `api/models_route.py`.** OpenAI-compatible shape from
  `config.models` — the same list that drives routing. No discovery call, no cache
  to invalidate, no second source of truth.

  **`created` is the config file's mtime, `owned_by` is the deployment owner, and
  `permission`/`default` are emitted** (B5). The config mtime changes only when
  the operator changes it, so a client re-fetching does not see a spurious
  "model updated".

- [x] **T054: Availability as a separate field.** **A configured model is always
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

**Batch 4 verification gate (2026-10-28):**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`, full `pytest` —
   clean. **521 passing**, up from 509.
2. **Scenario 8 passes**, and the cooldown is produced by a real `429` dispatched
   through the router rather than by inserting a record. A test that built its own
   `CooldownManager` would pass even if nothing were wired — the failure mode the
   Batch 2 correction was about.
3. The shape matches the OpenAI model object, including `object: "model"`, which
   is required by spec §5 and easy to omit. Ids come from `config.models` in
   declaration order.
4. **No second source of truth.** `GET /v1/models` moves no quota counter, proven
   by comparing `export_windows()` either side of a call.
5. `created` is the config file's mtime and is **stable across requests**. The test
   backdates the file, because a file written moments earlier has an mtime
   indistinguishable from "now" and would let a `time.time()` bug pass.

**A deviation, recorded rather than papered over.** Verification criterion 4 reads
"every listed id is dispatchable **or** explicitly marked unavailable". The
`available` field reflects **cooldown only**. Quota exhaustion and key depletion do
not appear in it, so a model whose daily budget is spent is listed as
`available: true` and would be refused on dispatch.

That gap is deliberate. Those are per-request capacity decisions the dispatcher
makes against a *specific* token estimate, not a property of the model; projecting
them would mean inventing an estimate and would make the listing change on nearly
every request — the oscillation `available` exists to prevent. The stricter reading
of criterion 4 is therefore **not** met, and it is recorded here rather than
satisfied by a field that would be wrong in the other direction.

**Not implemented:** `GET /v1/models/{id}`. Spec §5 defines the listing only, and an
unspecified endpoint is surface area nothing tests. A test pins the 404 so it is
obvious the omission was a decision.

---

## Batch 5: SSE Streaming & Fail-Fast

**Maps to GitHub Issue:** `#15 OpenAI-Compatible SSE Streaming Translation & Fail-Fast Mechanics`

- [x] **T055: `stream_chat_completion` on the adapter protocol,** as a sibling of
  `chat_completion` rather than a `stream: bool` parameter (ADR-0007). The two have
  different contracts: one returns a complete body, the other a sequence that can
  fail partway, and a flag would force every caller to handle the union.

  Plus `supports_streaming: bool`. A provider that cannot stream raises
  `StreamingUnsupportedError` — **an empty iterator and an unsupported stream are
  different failures and must not share an outcome.**

- [x] **T056: `api/streaming.py`.** Translate provider chunks to OpenAI-compatible
  SSE, terminated by `data: [DONE]`, for both Gemini and Mistral.

  **A `role` delta is emitted first, `usage` is included on the final chunk, and
  `stream_options: {"include_usage": true}` is honoured** (B7).

- [x] **T057: Fail-fast at the first delivered chunk** (ADR-0007). Enforced in the
  route layer, because the adapter cannot know whether anything reached the client.

  **Zero chunks is not "after stream start"** — nothing was delivered, so it retries
  within the existing budget. This distinction is what makes the rule precise rather
  than merely conservative.

  **The dispatcher is not touched** (A2). The streaming path in the route
  re-implements model resolution, key eligibility, capacity gating and the bounded
  retry budget, and must re-implement them **identically** — including taking
  exactly one `(provider, model)` pair, so CONSTITUTION §7.2 holds there too.

- [x] **T058: Reservation lifecycle under streaming.** An interrupted stream
  reconciles with whatever usage was reported; **no reservation leaks**. Streaming
  holds a reservation far longer than a unary request, so a leak here is materially
  worse than M1's case.

- [x] **T059: Route branch.** `routes.py` branches on `stream`. **The unary path is
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

**Batch 5 verification gate (2026-10-28):**

1. Full `pytest`, `ruff`, `mypy --strict`, `markdownlint` — clean. **568 passing**,
   up from 521.
2. **Scenario 9 passes.** A Google stream delivers one chunk and then sends a
   malformed event. The response ends there, and the mistral endpoint — declared in
   the same configuration and therefore reachable — is **never called**. The test
   asserts on the call count, every call's URL, and the request body, so a silent
   substitution of provider or payload would fail it.
3. **A zero-chunk interruption retries.** A `200` containing only `data: [DONE]`
   is retried inside the existing budget and the second attempt's content reaches
   the client. An empty stream that never recovers is a `502`, not a `200` with no
   body.
4. **An interrupted stream leaves no pending reservation.** `_pending` is empty
   after both the interrupted and the completed paths, and the interrupted stream
   settles against whatever usage arrived before the fault.
5. A provider that cannot stream is refused with `400 streaming_unsupported`
   rather than served an empty iterator. **See the deviation below**: the refusal
   is raised by the route from `supports_streaming`, because both shipped adapters
   can stream and there is nothing to exercise the adapter-level raise against.
6. `CONSTITUTION` §7.2 intact: the streaming path binds one `(provider, model)`
   pair before the loop and never varies it. Fail-fast is **structural** — the
   route consumes the first chunk before a `StreamingResponse` exists, so there is
   no code path that could retry after delivery.

**Design notes, recorded because they depart from the written plan.**

- **`api/stream_dispatch.py` is a second new module.** `plan.md` names only
  `api/streaming.py`. Rendering (chunks in, SSE out) and orchestration (capacity,
  retry, reservation) are different jobs with different test stories, and keeping
  them together would have made one 700-line module whose two halves are never
  read together.
- **`NormalizedChunk` was added to `core/normalization.py`.** `plan.md` lists that
  module as explicitly unchanged. The claim is about the request/response
  translation logic, which is untouched; the new type is additive and colocates
  the streaming sibling with `NormalizedResponse` and `TokenUsage`. It is recorded
  here rather than relocated to a module of its own.
- **The protocol signature is `(request, provider_model_id, key)`.** `plan.md`
  writes `(request, key, provider_model_id)`. That order differs from the
  existing `chat_completion` sibling for no reason, and two sibling methods with
  transposed arguments is a bug waiting to be written.
- **`stream_options.include_usage` is read as a *provider* preference, not a
  client output filter.** B7 says both that usage is on the final chunk and that
  the flag is honoured. The only reading that satisfies both is: usage is always
  emitted when known, and the flag is forwarded so Mistral — which reports usage
  only when asked — actually has a number to report. Gemini ignores it.
- **A mid-stream timeout is classified as `ProviderTimeoutError`, not
  `ProviderServerError`.** The provider accepted the request and then went silent,
  which is a 504-class fault. It matters only before delivery; after delivery both
  end the stream identically.

**A deviation, recorded rather than papered over.** Criterion 5 asks that a
"non-streaming adapter raises rather than returning an empty iterator". The
protocol requires that behaviour and documents it, but neither shipped adapter is
non-streaming, so no adapter-level raise exists to test. What is enforced instead
is the outcome the criterion is protecting: the route reads `supports_streaming`
**before dispatch** and raises `400 streaming_unsupported`, so an unsupported
stream is never confused with a stream that produced nothing. The adapter-level
raise is specified but unexercised.

**Not changed:** `core/dispatch.py` and the unary path in `api/routes.py` beyond
the branch itself. A2's premise is that the measured M1 path is worth more than
avoiding duplication; a test posts a non-streaming request and pins the JSON
envelope it returned before this batch.

---

## Batch 6: Error Classification, Measurement & Milestone Verification

**Maps to GitHub Issues:** `#79` (created 2026-10-29; previously mapped to `#12`
and `#15`, both closed before this batch started, which left the milestone's
remaining work with no issue of its own)

- [x] **T060: Improved provider error classification** (A4). All four:

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

  **Implemented 2026-10-29** on `M2-Batch6-ErrorClassification`. Verification gate
  and the decisions the spec left open are recorded below.

- [ ] **T061: Manual integration-test matrix** (#36 deliverable). Success, `429`,
  `4xx`, `5xx`, timeout, **mid-stream interruption**, and **post-restart behaviour**,
  per provider. Recorded with conditions, not as pass/fail assertions alone.

- [x] **T062: Re-measure the success rate, streamed and unstreamed separately.**
  M1's 100/100 covers unary requests only. Streaming holds connections open longer,
  so the same fault rate produces more interruptions — and interruption after the
  first chunk is **not** recoverable by retry. Reusing M1's figure for streamed
  traffic would be unfounded.

  Record per `spec.md` § *Measuring the success rate*, including request volume and
  whether the window coincided with console-reported usage.

  **Measured 2026-10-30, 17:19–17:24 UTC**, against the live Google free tier
  through the router on `127.0.0.1:8111` (shipped model, console-sourced quota
  dimensions declared: 15 RPM, 250k TPM, 500 RPD).

  | Mode | Volume | Successes | Rate | Median latency | Max |
  | :--- | ---: | ---: | ---: | ---: | ---: |
  | Unary | 20 | 19 | **95%** | 0.93 s | 3.99 s |
  | Streamed | 20 | 20 | **100%** | 0.85 s | 9.33 s |

  Zero streams were interrupted: every one ended with `data: [DONE]`, carried
  real provider usage on the final chunk, and arrived in 4 chunks. Requests were
  sequential with a 4.5 s minimum gap, so the window sits at ~13 RPM — under the
  measured 15 RPM ceiling by design, not by luck.

  One *attempt* did hit the 5 s window on the streaming leg, and it is worth
  recording separately from the request-level rate: the router logged
  `provider_timeout` at exactly 5.0 s, retried, and the retry succeeded in 4.3 s,
  so the request still counted as a success. Attempt-level timeouts are not
  request-level failures, and collapsing the two would have made this look like
  100% first-attempt reliability when it was not.

  **The single unary failure is the interesting result, and it is not a provider
  fault.** It returned `504 provider_timeout` at 10.21 s after *two* attempts
  each exceeded the 5 s provider window (`ROUTER_PROVIDER_TIMEOUT`), at which
  point the 10 s `attempt_budget_seconds` refused a third. Two consecutive
  latency spikes, not two failures.

  **What that reveals: both paths hit the same 5 s window, but they pay for it
  differently — which is the opposite of what this task predicted.** T062 assumed
  streaming would be *less* reliable because it holds connections open longer.
  The measurement says the reverse. A stream's headers arrive before generation
  starts, so the 5 s window covers only the handshake and the 120 s read bound
  then spans generation; a unary `generateContent` sends no headers until
  generation *finishes*, so its 5 s window is effectively a five-second cap on
  total generation. A slow handshake costs a stream one cheap retry; a slow
  generation costs a unary request the entire attempt, and two in a row cost it
  the request. That asymmetry between the two paths was not designed — it follows
  from what each provider endpoint does with its headers.

  Two consequences, stated rather than acted on in this batch because both are
  behaviour changes beyond a measurement task:

  1. `attempt_budget_seconds` (10 s) and the provider window (5 s) mean a unary
     request whose first two attempts each take over 5 s cannot succeed, however
     patient the client is. The budget is doing what it says; the 5 s window is
     the surprising bound.
  2. A longer unary generation than 5 s is unservable **unary** while the same
     generation streams fine. Today's prompts completed in 0.6–3.0 s, so it did
     not bite; earlier measurement recorded generations of 14.7 s, 26.1 s,
     49.7 s and 66.7 s on larger prompts, which would all have failed unary. That
     inconsistency is recorded for the milestone report and is **not** fixed here.

  **The console comparison is not done in this entry.** T063 owns it: whether
  this window's console-reported usage matches the router's counters is a
  separate reading, and claiming the success rate "coincided with console usage"
  without reading the console would be an unfounded claim.

- [ ] **T063: Re-run the console cross-check against durable counters** (ADR-0006
  §6), confirming drift has not accumulated.

- [x] **T064: Confirm the §8.3 deviation stays closed** and remove the M1 entry only
  once Scenarios 1–4 pass against a real restart.

  **Re-confirmed 2026-10-30.** The restart scenarios were re-run after Batches 4
  and 5 rather than trusted from their 2026-10-19 pass, because both batches
  changed the request path and a persistence regression would not announce
  itself:

  ```text
  uv run pytest tests/test_quota_persistence.py tests/test_app_persistence_wiring.py
  18 passed
  ```

  The tests that matter here run **two separate Python processes** through
  `tests/restart_helper.py`: one writes state and exits, the second constructs
  fresh objects and restores from the database. A single process clearing its own
  objects would pass even if the state only ever lived in memory, which is the
  failure §8.3 exists to prevent. Covered: a cooldown survives, counters survive,
  an expired cooldown is not resurrected, a corrupt row degrades rather than
  crashing startup, suspension is written but not restored, the LRU ordinal is
  seeded above the restored value, and the event loop keeps ticking under
  concurrent reservation.

  **The M1 entry stays as it is** — closed, with the removal recorded in
  `specs/002-mvp-openai-proxy/spec.md` § *Recorded Deviations*. T064 asks to
  confirm the closure holds, not to re-edit it; removing the record of a closed
  deviation a second time would erase the evidence that it was ever open.

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

### T060 verification gate (2026-10-29)

1. **The provider's own code is a second field, not a replacement.**
   `error.provider_code` carries `INVALID_ARGUMENT`, `invalid_api_key`,
   `RESOURCE_EXHAUSTED`, `INTERNAL`; `error.code` keeps the router's vocabulary.
   The key is **omitted when the provider supplied no code**, so an ordinary
   error envelope stays byte-identical to OpenAI's four keys. A client that
   validates the envelope sees no change; one that wants the extra detail asks
   for it.
2. **A retry hint is read wherever the provider puts it.** `Retry-After` first,
   then Gemini's `google.rpc.RetryInfo.retryDelay`. Gemini often sends only the
   latter, and discarding it would leave the cooldown guessing.
3. **Retryability is decided in the adapter and honoured, not re-derived, in the
   dispatcher.** `RouterBaseError.retryable` defaults to `False`;
   `ProviderServerError` and `ProviderTimeoutError` set it `True`; the adapter
   overrides it per response. `502`/`503`/`504` and Google's
   `UNAVAILABLE`/`DEADLINE_EXCEEDED`/`RESOURCE_EXHAUSTED` are retryable; `500`
   and `INTERNAL` are not. A `500` now raises on the first attempt.
4. **A daily cap is called on evidence, and otherwise called a burst.** Two
   signals: an exhausted daily `requests` window in the router's own tracker
   (`QuotaTracker.daily_requests_exhausted`), or a provider hint of an hour or
   more. With neither, the answer is "burst", because a wrong "daily" tells a
   client to give up on a day it could still use. A model with no daily dimension
   configured never answers "daily" from the tracker — an unverified limit stays
   unenforced, per the dimensions module.
5. **A `400` still does not degrade a model.** Pinned by a test that sends two
   consecutive `400`s and asserts both were dispatched, with the second only
   possible if no breaker entry was recorded.
6. Both paths share one verdict: `is_daily_cap` is a free function in
   `core/dispatch.py` called by the dispatcher and by `api/stream_dispatch.py`,
   and a test asserts the streaming module references the same object rather than
   a copy. The route-level tests cover `500`-not-retried over both unary and SSE.

**Decisions the spec left open, recorded because they are contract choices.**

- **`provider_code` rather than overloading `error.code`.** A4 item 1 asks for the
  provider's code to be surfaced; it does not say where. Overloading `code` would
  silently break any client switching on `provider_rate_limited`, and the router's
  vocabulary would stop being stable across providers. Additive is the only
  option that satisfies both.
- **A new code for the daily cap: `provider_daily_quota_exhausted`, still `429`.**
  The spec's *Error Behavior* table has one rate-limit row. A4 item 2 requires the
  two cases be distinguishable, and they are only distinguishable to a client if
  the code differs — a `provider_code` would be identical, because the wire is.
  `ProviderDailyQuotaExhaustedError` subclasses `ProviderRateLimitError`, so every
  existing handler and every `isinstance` check still treats it as a rate limit,
  and the table's row remains true for the burst case. It is an addition to the
  table, not a correction of it.
- **`_DAILY_CAP_HINT_SECONDS = 3600`.** Burst windows on both providers are
  per-second and per-minute, so a hint of an hour or more cannot be describing
  one. Lower would misreport a slow burst as a daily cap.
- **The daily cap does not lengthen the cooldown.** The cooldown still applies the
  configured floor and the provider hint. Cooling *until the reset* would be the
  more literal reading of "retrying cannot help", but the reset boundary is not
  modelled yet (see `WindowReset.FIXED`: no fixed window is configured, and the
  boundary is the monotonic epoch rather than midnight Pacific). Guessing a
  duration there would be worse than the 60 s floor, and the client now has the
  code that says not to retry regardless. Recorded as an open item, not silently
  done.

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
