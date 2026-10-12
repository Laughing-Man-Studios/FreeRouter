# Task Breakdown: M1 MVP OpenAI-Compatible Proxy

**Maps to GitHub Issue:** `#6 [M1 Epic] MVP OpenAI-Compatible Proxy`
**Specification:** `specs/002-mvp-openai-proxy/spec.md`
**Target:** ≥80% success rate, measured against the M0 baseline of ~50–60%

**Agent Instruction:**

- Implement **one batch at a time**.
- At the end of each batch, execute the explicit **Verification & Stop** criteria.
- Use `gh` to update the corresponding GitHub Issue status.
- Do not proceed to the next batch until the current batch is verified and human review is complete.

**Task IDs continue from M0** (which used T001–T019). **Task IDs are stable identifiers, not execution order** — the sequence has been revised twice, so ID ranges no longer track batch numbers. Batch 2 holds T035–T037, Batch 4 holds T023–T026, and Batch 5 holds T031–T034. Follow the batch numbers, not the ID range.

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

- [x] **T027: Generic quota dimensions — complete.** `quota/dimensions.py` plus a `quota` block on `ModelConfig` and a router-wide `QuotaConfig`. A dimension is `(resource, limit, window_seconds, reset, provenance)`, so adding a provider's limits is a config change rather than a code change.
  **Three decisions, each made against a rejected alternative:**
  1. **Requests-per-second and requests-per-minute are the same resource over different windows.** Making RPS a separate kind would have put a provider-specific branch through the reservation cycle, which is the thing T027 exists to prevent.
  2. **No way to express "unlimited".** `limit` is a required positive number. RPD is unverified for both providers, and an optional limit meaning unlimited would convert *"we do not know this limit"* into *"this limit is infinite"* — the router would sail past a real ceiling while reporting full headroom. An unknown dimension is **omitted**, which is a visible gap rather than a false guarantee.
  3. **`provenance` is required, not defaulted** — `published` / `measured` / `placeholder`. Google's values are placeholders and Mistral's were measured; neither is provider-published, and a defaulted field would let a guess read as an authority.

  **A real bug the tests caught before it shipped.** `limit` was first typed `int`, which cannot express Mistral's `ministral-14b-2512` ceiling of **0.50 requests/second** (nor `labs-leanstral-1-5-1` at 0.63). Rounding 0.5 up to 1 would dispatch at **twice** the permitted rate against a provider that enforces the ceiling per second. `limit` is now a `float`, with a configuration-level regression test.

  **Also rejected during implementation.** A `from_mapping` classmethod on the dimension, written against a stdlib dataclass. Pydantic auto-converted the dataclass, which made the classmethod unreachable and turned `reset` into a mandatory field. The representation is now a pydantic model, matching the rest of the codebase.
- [x] **T028: Reservation cycle — complete.** `quota/tracker.py` implements estimate → reserve → dispatch → reconcile (CONSTITUTION §8.2). Check-and-reserve is atomic under a single `asyncio.Lock` held only across in-memory updates, never across provider I/O. Clock is injected.

  **Five bugs the tests and a manual probe caught, all fixed:**
  1. **The safety margin over-reserved by one token.** `100 * 1.1` is `110.00000000000001` in binary floating point, so `ceil()` yielded 111. Reproducible, but for an arithmetic reason rather than a policy one. The margin is now a `Decimal`, so the configured margin is the applied margin.
  2. **Reconciliation settled only the last window.** A reservation touches one entry *per dimension*, but the pending map held a single entry, so each reserve overwrote the previous one and **token-dimension reconciliations were silently dropped**. Now one identifier maps to all its entries.
  3. **The min-spacing fallback leaked to token dimensions.** A token budget smaller than a request was being deferred rather than refused — waiting cannot make a budget cover a request. Now restricted to request-rate semantics.
  4. **`available()` reported "available" against a fully spent token budget**, because it asked the token dimension whether it had room for *zero* tokens. Now checks the smallest useful dispatch.
  5. **A fractional limit admitted a request early.** The 0.5 RPS spacing is 2s, which is longer than the 1s window; looking back only a window found nothing to wait for by 1.9s. Caught by an out-of-test probe, not the unit tests — the lookback is now the minimum spacing.

  **Fractional request limits are a token-bucket-plus-spacing model, not a smaller window.** `has_room` has two regimes: where the limit can hold a request, it is a token-bucket check; where it cannot — `ministral-14b-2512` at 0.50 RPS — summing would refuse every request forever, so the real constraint is a minimum spacing of `window / limit` seconds.

  **`reserve` returns `None` when exhausted rather than raising.** `NoAvailableCapacityError` is T031's, and Batch 5 attaches cooldown and failover semantics to it; introducing it here would pre-empt that. **This deviates from `plan.md`,** which types the return as `Reservation`.

  **`reconcile` cannot fail a request that already succeeded.** A provider reporting *more* tokens than estimated is recorded as genuine over-spend and `available()` goes False from then on. Unwinding would let the router keep dispatching against a limit it has demonstrably passed.

  Verified empirically, not only by unit test: 200 concurrent racers against a 7 RPS limit admit exactly 7.
- [x] **T029: Cooldown state — complete.** `quota/cooldown.py`, plus `cooldown_seconds` on `ModelConfig`.
  **Cooldown is keyed on `(provider, model)`, not on a key.** Cooling one Google key would move the same request to a sibling key drawing on the same exhausted project bucket. Google's own quota identifier — `GenerateRequestsPerMinutePerProjectPerModel`, dimensions `{location: 'global', model: ...}` — confirms the granularity. **This deviates from `plan.md`,** whose `KeyManager` interface has `enter_cooldown(key, ...)`.
  **The wait is the maximum of the provider hint and the configured floor, never the minimum.** Google's quota errors carry `google.rpc.RetryInfo` with `retryDelay` such as `"16s"`, and the standard `Retry-After` header is parsed in both documented forms. A hint of 5s against a 60s floor yields 60s: shortening the wait is exactly the retry-into-a-rate-limit that CONSTITUTION §6.1 prohibits.
  **No exponential backoff, by decision.** Backing off and retrying the same bucket is retrying into a rate limit with extra steps.
  **Expiry is evaluated lazily on read**, so there is no timer task to be a second source of truth or to cancel on shutdown.
  **`enter_until` exists because a duration is the wrong shape for the dominant failure.** With a ~20 RPD free tier, a daily budget exhausted at 23:50 cannot be retried in sixty seconds.

  **Research finding recorded in `spec.md` § *Risk Assessment* — this one is large.** Google's free tier is now **~20 requests per day per model**, reduced from 50 RPD in February 2026 and confirmed by Google staff: *"The main goal of the free tier is to try and give people a sense of what the model can do for early testing. It was not designed to be a long term solution to build things on top of."* The 1,000–1,500 RPD figures this project had been carrying are **stale by two orders of magnitude**. At 20 RPD the **≥80% success target is unreachable by any router improvement** — the ceiling is the provider's. Three options are recorded for the operator; none is applied silently.

  Also recorded: Google names the metric and limit in the error body, so the provider's authoritative numbers are readable at the moment of refusal. That is an M2 opportunity, not M1 work.
- [x] **T030: Context-overload circuit breaker — complete.** `quota/breaker.py`.
  **Degradation is size-conditional, not total.** A model that rejects a 900k-token prompt can still serve a 200-token one, and §8.2's purpose is to protect remaining quota, not to disable the model. `blocks()` returns False below the learned threshold.
  **The threshold is learned from the failure, never from a configured context window.** Neither provider publishes per-model context lengths (#51) and they change. It only ever **tightens**: a later, smaller failure lowers it.
  **The threshold is in *our* estimated tokens, not the provider's.** Google's message carries real counts — *"The input token count (5911388) exceeds the maximum number of tokens allowed (1048576)"* — but comparing those against a chars/4 estimate would compare two tokenizers. The provider figure is recorded for diagnostics and explicitly never used in a comparison.
  **The default is deliberately short (300s).** Google's context refusals are **intermittent** — the same model and project have been observed serving larger requests seconds after a 32,768-token refusal, which Google was investigating. A long breaker would turn a transient backend limit into an outage.
  **An unrecognised `400` does not trip the breaker.** Failing closed would degrade a working model on any malformed request. Mistral's wording has **not** been observed and is deliberately not guessed; an unmatched body behaves as in M0.

  **Known gap — this is not yet wired into dispatch.** Nothing calls `is_context_length_error` or `record_failure` yet: the adapters raise a generic `ProviderValidationError` on `400`, and the dispatch assembly is Batch 5 (#8) plus T034 (#10). The state machine and classifier are complete and tested, but **until that wiring lands the breaker cannot fire.** Recorded here rather than left to be discovered.

**Batch 3 verification gate (2026-10-11):**

1. `pytest tests/test_quota.py` — 25 passed.
2. `ruff check .`, `ruff format --check .`, `mypy --strict src/` — clean.
3. No over-commit under concurrent load — **verified empirically, not only by test.** 500 racers against 7 RPS admit exactly 7; against 3 RPS + 500 TPM admit exactly 3; against 1000 TPM admit 9 (each reserving 111 with the safety margin).
4. A `429` never results in an immediate retry — the dispatch gate `is_cooling` returns True, and a provider hint *shorter* than the configured floor loses to the floor.
5. The in-memory limitation is documented in code where a reader would expect persistence — `ADR-0004` referenced in the module docstring of all three stateful modules (`tracker`, `cooldown`, `breaker`).
6. **STOP**, review, then close Issue #9.

**Full suite: 345 passing**, up from 226 at the start of Batch 3.

**Verification & Stop:**

1. `pytest tests/test_quota.py`
2. `ruff check .`, `mypy --strict src/`
3. Confirm no reservation over-commits a single window under concurrent load.
4. Confirm a `429` never results in an immediate retry on the same key.
5. Confirm the in-memory limitation is documented in code where a reader would expect persistence.
6. **STOP**, review, then close Issue #9.

---

## Batch 4: Key Pool & Identity Registry

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

## Batch 5: Error Handling, Retries & Failover

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

**Revised 2026-10-11: Batches 4 and 5 were swapped.** The key pool now runs before retry and failover.

The original order was unimplementable. T033 — *"on `429` or a retryable fault, move to the next eligible key"* — requires a pool of eligible keys to move **to**, and that pool did not exist until the following batch. Failover was specified against a component that had not been built yet.

The corrected sequence:

```text
Batch 1  Compliance evaluation & policy foundation   ✅ #51 closed
Batch 2  Mistral adapter & identity registry         ✅ #7 closed
Batch 3  Quota & cooldown                            #9
Batch 4  Key pool & identity registry                #8
Batch 5  Error handling, retries & failover          #10
Batch 6  Debug logging & milestone verification      #6
```

Each batch now depends only on what precedes it. Batch 3 supplies the cooldown state that Batch 4's eligibility filter reads; Batch 4 supplies the eligible keys that Batch 5's failover moves between.

**Why the Mistral adapter moved ahead of the key pool (Issue #51).** Google's rate limits are **per-project, not per-API-key**: all keys in a project share one quota bucket. Two keys on one project therefore cannot fail independently, and so cannot exercise the paths the key pool exists to test. A genuinely independent provider can.

**What still carries the success rate.** Because Google's quota is per-project, the key pool adds **credential redundancy, not capacity**, and retry (Batch 5) is the primary reliability mechanism against the ≥80% target. Batch 1 precedes everything because its conclusions — the per-provider key cap and whether pacing is required — are inputs to the key manager's contract, not documentation written afterwards.

CONSTITUTION §7.2 continues to forbid cross-model failover for an explicitly requested model, so the Mistral adapter adds capability and independent-provider testing, not automatic failover for a client that named a Google model.

## Non-Goals for M1

Streaming, tool calling, `GET /v1/models`, capability filtering, `model = "free"`, persistence, and the Admin API all remain out of scope. See `spec.md` § *Explicit Non-Goals*.
