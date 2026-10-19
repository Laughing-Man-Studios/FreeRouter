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
Batch 1  Persistence schema & migration      #12
Batch 2  SQLite-backed quota store            #12
Batch 3  Token estimation & reservation       #13
Batch 4  GET /v1/models                       #14
Batch 5  SSE streaming & fail-fast            #15
Batch 6  Error classification & verification  #12, #15
```

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

## Batch 1: Persistence Schema & Migration

**Maps to GitHub Issue:** `#12 Single-File Local Persistence Engine`

- [ ] **T041: Define the persisted state schema** (ROADMAP §3.14). Four tables per
  `plan.md` § *Data / Persistence Changes*: `quota_windows`, `cooldowns`,
  `model_status`, `key_state`.

  **`bucket_used` is a JSON map keyed by bucket ordinal**, mirroring `_Window`'s
  in-memory buckets. A row per bucket would multiply writes unboundedly on a rolling
  window and make the flush more expensive than the state it holds.

  **`reset_at` exists for fixed windows only.** Google's RPD resets at midnight
  Pacific; a rolling window has no such instant. One nullable column keeps the
  dimension model uniform.

  **No payload columns exist in any table**, and no key-material column either —
  only `alias`. That is what makes a state dump safe to paste into an issue.

- [ ] **T042: Alembic revision 0002.** Adds the four tables. Must apply cleanly to a
  database created by revision 0001 and must be idempotent on re-run, since
  migrations run at every startup.

- [ ] **T043: Extract `QuotaTrackerProtocol`** from `QuotaTracker` (`plan.md`
  § *Interfaces*), so both implementations are checked against one contract.

  **This is the test of ADR-0004's central claim.** `reserve` and `reconcile` keep
  their existing signatures *including* `Reservation | None` and `int | None`. The
  only addition is `restore()`. If extracting the protocol requires changing the
  dispatcher, ADR-0004's "the interface, not the storage, is the durable decision"
  was wrong — record that as a finding rather than working around it.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. Migration applies to a fresh database and to one created by revision 0001.
3. `QuotaTracker` satisfies `QuotaTrackerProtocol` — asserted, not assumed.
4. A test asserts the new tables contain no column able to hold key material or
   payload text.
5. `pytest tests/test_db.py tests/test_migrations.py tests/test_quota_tracker.py`
6. **STOP**, review, then update #12.

---

## Batch 2: SQLite-Backed Quota Store

**Maps to GitHub Issue:** `#12 Single-File Local Persistence Engine`

- [ ] **T044: `quota/store.py` — SQLite-backed tracker.** Satisfies
  `QuotaTrackerProtocol`, constructed at startup in place of the in-memory tracker.

  **Durability is asymmetric** (ADR-0005): cooldowns and status transitions write
  through immediately; counters and LRU ordinals batch behind a bounded flush
  interval, coalesced so N requests in one interval produce one additive write.

  **The in-memory tracker stays** — as the test double and as the degradation path
  when restore fails.

- [ ] **T045: `restore()` before the listener serves.** Called in lifespan so no
  request can be dispatched against an unrestored counter. Prunes windows whose
  reset instant has passed; does not restore expired cooldowns.

- [ ] **T046: Cooldown and breaker write-through.** `cooldown.py` and `breaker.py`
  persist on transition. A failed cooldown write returns `503
  persistence_unavailable` rather than dispatching into a limit the router cannot
  record.

  **Suspension is persisted but not restored** (ADR-0005 §5). A credential revoked
  an hour ago may have been fixed; resurrecting the suspension would refuse a working
  key and produce a confusing 503 instead of the `401` that would diagnose it.

- [ ] **T047: LRU ordinal restore.** `key_state` restores selection order. Verified by
  **observed selection order**, not by reading the table — a test that reads the
  table would pass even if selection ignored it.

- [ ] **T048: Failure degradation.** An unreadable or corrupt row discards that
  unit's state and falls back to in-memory accounting, recording it. A general
  restore failure degrades the tracker and startup **succeeds**. A crash-loop is worse
  than forgetting a cooldown.

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

  **The output assumption is explicit configuration**, not a constant (ADR-0006).
  M1's estimate silently assumed zero output tokens, which is what actually breaches
  a TPM ceiling.

- [ ] **T050: Wire provider-reported usage.** Both adapters return actual usage where
  the provider reports it; `reconcile` receives `None` where it does not.

  **Provider-reported usage is authoritative where present.** If either provider
  reports cumulative rather than per-request usage, that must be handled explicitly
  — a cumulative figure would inflate reconciliation without bound.

- [ ] **T051: Reconciliation rules.** Confirm and test that reconciliation supersedes
  the estimate for the entries the reservation created, leaves the estimate standing
  on `None`, and is idempotent (ADR-0006). These are existing behaviours; this task
  makes them specified and tested rather than incidental.

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

- [ ] **T057: Fail-fast at the first delivered chunk** (ADR-0007). Enforced in the
  route layer, because the adapter cannot know whether anything reached the client.

  **Zero chunks is not "after stream start"** — nothing was delivered, so it retries
  within the existing budget. This distinction is what makes the rule precise rather
  than merely conservative.

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

- [ ] **T060: Improved provider error classification.** Distinguish quota,
  credential, payload and transient upstream failures precisely enough to be
  actionable. Ordinary `400`s must not degrade a model; only a genuine
  context-length refusal does.

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

- [ ] **T065: Revisit OpenHands.** #36 requires OpenHands brought in "as early as
  practical". See `spec.md` § *Open Decisions Requiring Sign-Off* **D3**: streaming
  and `/v1/models` support conversational use, but **agentic use requires tool
  calling, which is outside M2's boundary**. Validate what is reachable, and record
  precisely what is not.

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

**Read literally, the second clause is not fully reachable in M2.** "Interactive
OpenHands workloads" implies agentic use, which requires tool calling — explicitly
outside this milestone's boundary (see D3). **This needs resolving before the
milestone is declared met**, not at close-out.

What M2 can demonstrate: survival across restart with cooldown and counter state
intact (Batches 1–2), and streaming plus model discovery serving an interactive
client (Batches 4–5).

---

## Carried Into M3, Not Deferred By Choice

| Item | Source | Note |
| :--- | :--- | :--- |
| Tool calling | #36 non-goal | Next compatibility feature after streaming. OpenHands agentic use depends on it (D3). |
| Capability-aware routing | M3 | — |
| Cross-file deployment contract enforcement | M1 Batch 6 | `config.yaml` ↔ `docker-compose.yml` ↔ `ci.yml` is checked only by the CI smoke test. Warnings, not enforcement. |
| Mistral RPD measurement | ADR-0006 §7 | Persistent counters make this answerable empirically for the first time. |
| Cross-provider stream resume | ADR-0007 §4 | No resumable offset on either free tier. Recorded as unavailable. |
