# Task Breakdown: M1 MVP OpenAI-Compatible Proxy

**Maps to GitHub Issue:** `#6 [M1 Epic] MVP OpenAI-Compatible Proxy`
**Specification:** `specs/002-mvp-openai-proxy/spec.md`
**Target:** ≥80% success rate, measured against the M0 baseline of ~50–60%

**Agent Instruction:**

- Implement **one batch at a time**.
- At the end of each batch, execute the explicit **Verification & Stop** criteria.
- Use `gh` to update the corresponding GitHub Issue status.
- Do not proceed to the next batch until the current batch is verified and human review is complete.

**Task IDs continue from M0** (which used T001–T019). **Task IDs are stable identifiers, not execution order** — the sequence below was revised on 2026-10-08 and Batch 2's tasks are T035–T037 while Batch 5's are T023–T026. Follow the batch numbers, not the ID range.

---

## Batch 1: Compliance Evaluation & Policy Foundation

**Maps to GitHub Issue:** `#51 Provider Compliance Evaluation & Free-Tier Quota Research`

- [ ] **T020: Provider compliance research.** Produce the written CONSTITUTION §6.1 evaluation for Google and Mistral, citing current source documentation with retrieval dates. Determine per provider whether key pooling is permitted, prohibited, or silent-and-therefore-capped at 2 keys. Determine whether mandatory request pacing is required and, if so, its parameters. Record the result as configuration data, not as constants.
- [ ] **T021: Free-tier quota research — REVISED, completed by investigation.** Neither provider publishes authoritative free-tier numbers: Google defers its rate-limit table to a signed-in AI Studio console, and Mistral shows live values only under Admin → Limits. Third-party figures conflict by an order of magnitude. **T020 also established that Google's quota is per-project, not per-key**, so pooling adds no capacity there.
  **Revised outcome:** ship **conservative placeholder quota values marked unverified**, tune them from measured success rate, and do not populate figures that cannot be sourced. Measuring against the live API to obtain precise numbers is rejected for M1 as a poor trade against a ~1 RPS ceiling.
  **Remaining work:** record the placeholder values, their provenance-as-unverified, and the published-figure sources that were checked and found silent.
- [ ] **T022: ADR-0003 and ADR-0004.** Record the key-pool configuration mechanism (ADR-0003) and the in-memory quota state decision with its M2 path (ADR-0004). Also correct ADR-001 §2.4, which still states the superseded 400 ms timeout.

> **Investigation outcome (2026-10-06).** Three findings, now recorded in `spec.md` and ADR-0003:
>
> 1. **Google's quota is per-project, not per-key** — *"Rate limits are applied per project, not per API key."* Two keys in one project share one quota bucket. Key pooling is therefore a credential-rotation facility for Google, **not** a capacity lever, and Batch 2 must not be justified on that basis.
> 2. **Neither provider publishes free-tier limits.** Placeholder values ship marked unverified (T021).
> 3. **Pacing is not required** by either provider's terms, so §6.1's pacing requirement is not triggered and T039 is skipped with the reasoning recorded.
>
> **Consequence for the ≥80% target:** with key pooling downgraded as a reliability lever, **retry on transient faults carries that role**, and Batch 4 is the load-bearing batch. See `spec.md` § *Risk Assessment*.

**Verification & Stop:**

1. Confirm the evaluation names a per-provider verdict with reasoning.
2. Confirm no quota value in the configuration is unsourced.
3. Confirm ADR-001 no longer states the 400 ms timeout.
4. **STOP**, review, then close Issue #51. It is a mandatory gate on closing the M1 milestone.

---

## Batch 2: Mistral Provider Adapter & Identity Registry

**Maps to GitHub Issue:** `#7 Mistral Provider Adapter & Identity Registry`

- [x] **T035: Mistral API verification — complete.** Verified against Mistral's published OpenAPI spec (`docs.mistral.ai/openapi.yaml`, fetched directly) and the error glossary, not from memory. Confirmed: `POST /v1/chat/completions`; `Authorization: Bearer`; required fields `model` + `messages`; roles `system`/`user`/`assistant`/`tool` each `const`-typed; response `choices[0].message.content` with OpenAI-vocabulary `finish_reason`; error envelope `{object, message, type, param, code}`; retryable statuses 429/500/502/503/504.
  **Three findings changed the implementation:**
  1. `system` is a *native* role inside `messages`, so nothing is hoisted out the way Gemini's `systemInstruction` requires — the adapter is near-identity.
  2. **No bad-key-versus-bad-payload heuristic is needed.** Google's requires one because Gemini returns `400 INVALID_ARGUMENT` for a bad key; Mistral documents `401`/`403`. Copying Google's heuristic would be a branch Mistral's taxonomy can never reach.
  3. Free-tier quota was **read from the workspace console** (see `spec.md` §4). Requests-per-second is a dimension the quota model did not represent, and some models permit **less than 1 RPS**.
- [x] **T036: Mistral adapter — complete.** `providers/mistral/adapter.py`, registered in `build_adapter` so it is selectable purely through configuration. Translation is close to identity; there is no finish-reason mapping table and no error-body parsing.
  **Two changes outside the adapter, both deliberate:**
  - `ProviderRateLimitError` added. `spec.md` § *Error Behaviour* already specified `429 → provider_rate_limited`, but the hierarchy had no such type, so M0 mapped every 4xx — including rate limits — to `ProviderValidationError`. A rate limit was therefore reported to clients as `400 invalid_request_error`. Both providers now map `429` correctly.
  - Provider timeout resolution extracted to `providers/timeout.py`, shared by both adapters. One environment variable must have one resolved value; a per-adapter copy would be two sources of truth that drift on the first retune.
- [x] **T037: Translation tests — complete.** `tests/test_mistral_adapter.py` plus integration coverage in `tests/test_integration.py`. Fixture shapes are taken from the OpenAPI spec, including the `422` case, which returns FastAPI's `{detail: [...]}` rather than the documented envelope and so cannot be classified by parsing the body.

**Verification & Stop:**

1. `pytest tests/test_mistral_adapter.py` — 39 tests.
2. `ruff check .`, `ruff format --check .`, `mypy --strict src/` — all clean. Full suite: 226 passing.
3. **Seam confirmed.** `api/routes.py`, `core/normalization.py`, and `core/config.py` are **unmodified**. `test_mistral_model_serves_through_the_unchanged_route` proves a Mistral model serves through the identical ingress route, and `test_both_providers_serve_from_one_running_router` proves both coexist in one process. The Google adapter changed only for the two reasons listed above; neither alters how a Google request is dispatched.
4. **STOP**, review, then close Issue #7.

> **Known gap, deferred to Batch 5 (T024).** `gemini_api_key` remains a **required** configuration field, so a Mistral-only deployment fails at startup. Making provider credentials individually optional belongs with the key-pool configuration work, not here.

---

## Batch 3: Quota & Cooldown

**Maps to GitHub Issue:** `#9 Static Quota Counters & Conservative Cooldown State`

- [ ] **T027: Generic quota dimensions.** Implement the Level 1 dimension representation (requests per window, tokens per window, daily requests) as configuration, so a new dimension requires no tracker change.
- [ ] **T028: Reservation cycle.** Implement estimate → reserve → dispatch → reconcile (CONSTITUTION §8.2) with a configurable safety margin on token estimation. Reconciliation must tolerate providers that report no usage.
- [ ] **T029: Cooldown state.** Implement cooldown entry with per-provider/model configurable duration. A `429` marks the key cooling; it must never be retried immediately (CONSTITUTION §6.1).
- [ ] **T030: Context-overload circuit breaker.** On a hard context-length-exceeded response, mark the model degraded for large-context requests rather than retrying it.

**Verification & Stop:**

1. `pytest tests/test_quota.py`
2. `ruff check .`, `mypy --strict src/`
3. Confirm no reservation over-commits a single window under concurrent load.
4. Confirm a `429` never results in an immediate retry on the same key.
5. Confirm the in-memory limitation is documented in code where a reader would expect persistence.
6. **STOP**, review, then close Issue #9.

---

## Batch 4: Error Handling, Retries & Failover

**Maps to GitHub Issue:** `#10 Error Handling, 429/5xx Retries & Failover Policies`

- [ ] **T031: Capacity exceptions and 503.** Add `NoAvailableCapacityError`, `RetriesExhaustedError`, and `ProviderRateLimitedError` to the M0 status map, preserving the most-specific-first ordering. Make `503` a first-class outcome for exhausted free capacity.
- [ ] **T032: Bounded retry.** Implement retry for transient `5xx` and timeouts within an attempt and elapsed-time budget. Increment and log `attempt` on every dispatch.
- [ ] **T033: Key failover within one model.** On `429` or a retryable fault, move to the next eligible key **for the same model only**. Cross-model failover stays prohibited (CONSTITUTION §7.2).
- [ ] **T034: Non-retryable classification.** Ensure ordinary `4xx` and auth failures are never blindly retried, and that retrying into a rate limit is structurally impossible.

**Verification & Stop:**

1. `pytest tests/test_failover.py tests/test_integration.py`
2. `ruff check .`, `mypy --strict src/`
3. Confirm retry budget exhaustion terminates rather than looping.
4. Confirm a `400` is never retried and a `429` never retried on the same key.
5. Confirm an explicit model request never fails over to another model.
6. Confirm no eligible key yields a normalized `503` without dispatching.
7. **STOP**, review, then close Issue #10.

---

## Batch 5: Key Pool & Identity Registry

**Maps to GitHub Issue:** `#8 Key Pool Manager & Simple LRU Key Selection`

- [ ] **T023: Configuration schema.** Extend `config.yaml` handling for pooled keys per ADR-0003, enforcing the cap concluded in T020 as a **configuration validation error**, not a convention. Key material remains environment-only; the loader must continue to reject any secret-named field or `${VAR}` reference in `config.yaml` (ADR-0002).
- [ ] **T024: Identity registry.** Introduce `provider/model` identity distinct from the external OpenAI identifier, supporting multiple providers without routing-core changes. Unknown models resolve to `UnsupportedModelError` as in M0.
- [ ] **T025: Key manager.** Implement the pool and deterministic LRU selection over eligible keys, with `KeyRef` carrying a loggable alias and a `SecretStr`. Inject a clock; do not read wall time.
- [ ] **T026: Eligibility and concurrency guard.** Exclude cooling and exhausted keys from selection. Guard selection and state mutation with an `asyncio.Lock` held only across in-memory updates — never across provider I/O. Log `key_alias` and `selection_reason`.

**Verification & Stop:**

1. `pytest tests/test_key_manager.py tests/test_config.py`
2. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
3. Confirm no test or log output contains key material — assert the alias appears and the secret does not.
4. Confirm a cooling key is never selected, and that concurrent requests cannot both take the last eligible key.
5. **STOP**, review, then close Issue #8.

---

## Batch 6: Debug Logging & Milestone Verification

**Maps to GitHub Issue:** `#6 [M1 Epic] MVP OpenAI-Compatible Proxy`

- [ ] **T038: Opt-in debug payload logging.** Implement full request/response logging behind explicit environment configuration, written to an **isolated** stream. When disabled, the existing structural guarantee holds unchanged (CONSTITUTION §9.1). This is a ROADMAP M1 deliverable that M0 skipped and no other issue owned.
- [ ] **T039: Request pacing — SKIPPED, decision recorded.** Issue #51 found that neither Google's nor Mistral's documented terms impose mandatory request pacing, so CONSTITUTION §6.1's pacing requirement is **not triggered** for M1. No pacing is implemented. This stands as the recorded outcome rather than an open task; revisit only if a provider's terms change or Issue #51 is reopened.
- [ ] **T040: Milestone verification.** Re-verify the specification end to end, measure the achieved success rate against the ≥80% target, confirm Issue #51 remains closed, and confirm the recorded CONSTITUTION §8.3 deviation is still accurate.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`, full `pytest`
2. Confirm operational logs contain no payload and no key material **with debug logging disabled**.
3. Confirm debug output does not appear in the operational stream **when enabled**.
4. Record the measured success rate and the conditions under which it was measured.
5. **STOP**, review, then close Issues #6 and milestone #2 (M1).

---

## Ordering Rationale

Failover is built before the second provider because CONSTITUTION §7.2 forbids cross-model failover for an explicitly requested model. A Mistral adapter therefore does not improve reliability for a client asking for a Google model — it only adds capability. The key pool, quota, and failover chain is what moves the success rate, so it ships first.

**Revised after Issue #51.** Because Google's quota is per-project, the key pool does not add capacity there either. Retry (Batch 4) is now the primary reliability mechanism, which strengthens rather than weakens this ordering — the failover chain still ships before the second provider.

Batch 1 precedes everything because its conclusions — the key cap and whether pacing is required — are inputs to the key manager's contract, not documentation written afterwards.

## Non-Goals for M1

Streaming, tool calling, `GET /v1/models`, capability filtering, `model = "free"`, persistence, and the Admin API all remain out of scope. See `spec.md` § *Explicit Non-Goals*.
