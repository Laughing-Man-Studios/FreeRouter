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

- [x] **T023: Configuration schema — complete, with one deliberate design decision and one migration.**

  **The §6.1 key cap is code, not configuration.** `key_cap_for(provider)` returns 2 for Google, `None` for Mistral, and **2 for any unrecognised provider**. CONSTITUTION §2 makes configuration the source of non-secret *policy*, but this limit is a compliance constraint: exposing it in `config.yaml` would mean anyone who can edit the file can raise a constitutional limit, which defeats the point of having one. What legitimately changes is a provider's *classification* when its terms change, and that is a reviewed code change recorded in an ADR. The default of 2 for an unknown provider is fail-safe — CONSTITUTION §6.1 permits uncapped pooling only where terms *explicitly* permit it, so nobody gets the benefit of the doubt.

  **`KeyRef` gains `model_id`** beyond `plan.md`'s `(alias, provider, secret)`. A key is pooled per `(provider, model)`, and without the model on the reference `mark_used` had to search every pool to find which one this key belonged to. The field is non-secret.

  **⚠️ BREAKING CHANGE: credentials moved from a config field to derived environment variables.** `gemini_api_key` is gone; `key_aliases` replaces it and material resolves from `<PROVIDER>_KEY_<ALIAS>`. **`GEMINI_API_KEY` must become `GOOGLE_KEY_PRIMARY`, and `MISTRAL_API_KEY` must become `MISTRAL_KEY_PRIMARY`.** This is inherent to ADR-0003 rather than incidental: accepting a variable name from configuration would let `config.yaml` direct the router at any environment variable on the host, which is the capability ADR-0002 exists to withhold.

  **A regression was caught and fixed while doing this.** `resolve_key_material` reads `os.environ` directly, which silently bypassed the `.env` fallback `pydantic-settings` provides — so a developer's usual local-secret store would have stopped working for keys. `.env` is now consulted explicitly, with the process environment still winning (ADR-001 precedence), because the derived names are not declared pydantic fields. `python-dotenv` is now a **declared** dependency rather than a transitive one.

  Carried from #7: `gemini_api_key` was required, so a Mistral-only deployment failed at startup. **Provider credentials are now per model**, and a Mistral-only configuration starts.

- [x] **T024: Identity registry — assessed, and deliberately NOT built.**

  `ModelConfig` already carries `id` / `provider` / `provider_id`, `resolve_model` maps an external id to a `(provider, provider_id)` pair and raises `UnsupportedModelError` for anything unconfigured, and PR #55 proved two providers work through the unmodified route. **The requirement is already satisfied.** Introducing `core/models.py` as `plan.md` intended would be a refactor with no behavioural gain, and CONSTITUTION §2's "prefer the simplest implementation that satisfies the current milestone" argues against speculative structure.

  What the measured limits *do* change is that **model selection is now a capacity lever** — `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` each carry 500 RPD, so ~1,000/day combined. Making the router *choose* between models is M3 territory (ROADMAP's capability-aware router) and is explicitly out of M1's scope. **Recorded as satisfied, not skipped.**

- [x] **T025: Key manager — complete.** `keys/manager.py`. `KeyRef` carries a loggable alias, a `SecretStr`, and a redacting `__repr__`. Selection is least-recently-used and **does not mark a key used** — a request that then fails must not leave a working credential looking cold, which is exactly when you want to keep using it.

  **A bug the tests caught: LRU ordering silently collapsed to alphabetical.** `last_used` was stamped with the injected clock and ties broke on alias. With any clock coarser than the request rate — routine on some platforms, and exactly what a fake clock looks like — every mark lands on the same instant, so "least recently used" became "alphabetically first" and all traffic pinned to one alias while *appearing* to work. Ordering now uses a **monotonic ordinal**, exact and immune to clock resolution; the clock stamps a diagnostic timestamp only. Pinned by `test_lru_ordering_survives_a_clock_that_never_advances`.

- [x] **T026: Eligibility and concurrency guard — complete, with a correction to the task's own wording.**

  The task says *"exclude cooling and exhausted keys from selection."* **Per-key cooldown would be actively wrong**, given #51: a rate limit on one Google key applies to its sibling too, because both draw on one project bucket. Cooling only one would send the very next request into the same exhausted bucket. Cooldown and quota are therefore **model-level** (`quota/cooldown.py`, `quota/tracker.py`), and the dispatch path composes them before selection. The key manager's eligibility is **suspension only** — the credential itself being unusable, i.e. rejected authentication or revoked. That is the only per-key state, and it is the only state a key carries.

  The `asyncio.Lock` is held **only across in-memory updates**. `test_the_lock_is_not_hold_across_provider_io` proves a second coroutine proceeds during a simulated slow dispatch — holding it across an `await` of provider I/O would serialise every request behind the slowest upstream response.

  Dispatch now selects a credential **before** building the adapter, logs `key_alias` and `selection_reason` (never material), and raises `NoAvailableCapacityError` → `503` when nothing is eligible. **`503` is new**: "no capacity" is a real outcome on free tiers and was previously indistinguishable from a provider fault, which invites exactly the retry the cooldown exists to prevent.

  **Adapters no longer read the environment.** `chat_completion` takes a `KeyRef`, so no variable name reaches an adapter and a test cannot make one by exporting a variable. Both adapter test suites gained a decoy-variable test for this.

  **`body_indicates_context_length_error` was added to both adapters, which closes the Batch 3 wiring gap** — the breaker can now be tripped once dispatch composes it in Batch 5. Mistral's version matches nothing yet, documented, and fails *open*.

**Batch 4 verification gate (2026-10-12):**

1. `pytest tests/test_key_manager.py tests/test_config.py` — 66 passed.
2. `ruff check .`, `ruff format --check .`, `mypy --strict src/` — clean.
3. No key material in any output — verified structurally, not just by grep. `KeyRef.describe()` emits `key_alias=primary`; `KeyRef.__repr__` and `KeySelection.describe()` were each checked against a sentinel secret and none leak. Real selection output: `key_alias=primary selection_reason=least_recently_used`.
4. A suspended key is never selected under load — 200 concurrent selections with `primary` suspended returned only `secondary`, with no spurious exhaustion.
5. The lock is never held across provider I/O — proved **structurally**, by walking the AST for `async with` blocks containing an `await`. Zero. Holding it across a network call would serialise every dispatch behind the slowest provider response.
6. **Full suite: 390 passing**, up from 345 at the start of Batch 4.

## ⚠️ Operator action required: credentials have moved

**`GEMINI_API_KEY` must become `GOOGLE_KEY_PRIMARY`, and `MISTRAL_API_KEY` must become `MISTRAL_KEY_PRIMARY`.**

The shipped `config.yaml` needs no change — a model with no `key_aliases` declares one key, called `primary`. A deployment that has not renamed its variables **will fail at startup** with a message naming the alias and the expected variable. That is deliberate: a missing credential is a startup failure, not a 401 on the first request.

**Verification & Stop:**

1. `pytest tests/test_key_manager.py tests/test_config.py`
2. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
3. Confirm no test or log output contains key material — assert the alias appears and the secret does not.
4. Confirm a cooling key is never selected, and that concurrent requests cannot both take the last eligible key.
5. **STOP**, review, then close Issue #8.

---

## Batch 5: Error Handling, Retries & Failover

**Maps to GitHub Issue:** `#10 Error Handling, 429/5xx Retries & Failover Policies`

- [x] **T031: Capacity exceptions and 503 — complete, and the naming discrepancy settled.**

  `NoAvailableCapacityError` (503) and `ProviderRateLimitError` (429) shipped in Batches 4 and 2; `RetriesExhaustedError` (503) is new here.

  **Naming reconciled.** The three documents disagreed: `spec.md` specifies the code `provider_rate_limited`, this task named the class `ProviderRateLimitedError`, and Batch 2 implemented `ProviderRateLimitError`. **Settled on `ProviderRateLimitError`**, since it is already implemented, tested and in use; the task text is corrected rather than the code renamed. Spec's *code* value is unchanged.

  **`RetriesExhaustedError` is deliberately narrow.** Raised only when **nothing was ever dispatched** — the attempt budget elapsed before a fault. A provider fault that survived its retries is re-raised as *that fault* (5xx → 502, timeout → 504, auth → 401), per spec's error table. Reporting an upstream failure as a capacity problem would send an operator to quota instead of to the provider.

  **`ProviderRateLimitError` now carries `retry_after`**, populated by the adapter from `Retry-After` or Google's `RetryInfo`, so the provider's own answer reaches the cooldown rather than being discarded with the body.

- [x] **T032: Bounded retry — complete.** `core/dispatch.py`, plus a `DispatchConfig` section. Bounded by **both** an attempt count and a wall-clock budget, because they fail differently: the count alone lets a few very slow attempts run past any sensible client timeout.

  **Defaults are deliberately small — `max_attempts: 2`, one retry.** Every retry consumes real quota: the request was sent, so it counts against the 15 RPM and 500 RPD ceilings even though it produced nothing. An aggressive retry policy spends the day's budget on retries rather than answers.

  **The backoff applies to transient faults only, never to a rate limit.** A `5xx` gets `backoff_seconds` as a courtesy to a recovering upstream. A `429` is spaced by the cooldown duration, which is a compliance floor, not a politeness delay.

- [x] **T033: Key failover within one model — complete, and "on `429`" is implemented as a refusal.**

  **A rate limit stops the request rather than failing over.** On a `429` the cooldown marks the whole `(provider, model)` unit and the next iteration's gate refuses to dispatch. Issue #51 established Google's limits are per project, so a second key draws on the same exhausted bucket — failing over would spend another request to learn nothing. CONSTITUTION §6.1 forbids retrying into a rate limit, and **the prohibition is structural rather than a guard someone can forget.**

  **Failover therefore happens for authentication only** — the one genuinely per-key failure, since a rejected credential says nothing about its sibling. The credential is suspended so later requests do not walk into the same wall.

  **Cross-model failover remains impossible, not merely disabled**: the dispatcher's signature takes exactly one `(provider, model)` pair and never varies it across attempts.

- [x] **T034: Non-retryable classification — complete.** A `400` (including `422`) is never retried, verified by a test that offers `max_attempts=5` and counts one provider call. Auth failures move to another credential rather than retrying the same one.

  **Every dispatched attempt consumes quota, including the ones that fail**, so the reservation is reconciled on **every** terminal path and never released on the assumption that a failed call was free. Assuming otherwise would let a run of `5xx` quietly drain a 500-per-day budget.

- [x] **Wiring closed: the pieces M1 built now meet.**

  The dispatcher is the first place the key pool, quota tracker, cooldown manager and circuit breaker interact. Capacity gates run **before** anything is sent, so a request refused for lack of capacity **never reaches the provider and spends no quota**.

  **The context-overload breaker is finally wired.** `ProviderValidationError` now carries `context_length`, set by the adapter where the body is read, and the dispatcher records the failure — **closing Batch 3's known gap.** An ordinary `400` does not degrade the model, and a degraded model still serves small requests.

  `routes.py` is thinner than before: it validates, resolves the model, and hands off. The log context is opened in the route so every dispatcher record still carries the externally-requested `model` (CONSTITUTION §9.1) — an intermediate attempt lost that, and a telemetry test caught it.

**Batch 5 verification gate (2026-10-13):**

1. `pytest tests/test_failover.py tests/test_integration.py` — 71 passed.
2. `ruff check .`, `ruff format --check .`, `mypy --strict src/` — clean.
3. Retry budget exhaustion terminates rather than looping — `max_attempts=4` against a
   persistent `500` produced **exactly 4 calls**, then `ProviderServerError`.
4. A `400` is never retried and a `429` is never retried — **run, not merely asserted**:
   a `400` with `max_attempts=5` produced **1 call**; a `429` with **two keys available**
   produced **1 call**.
5. An explicit model request never fails over to another model — one `(provider, model)`
   pair holds for the loop's whole life, asserted against every call's URL and body.
6. No eligible key yields a normalized `503` without dispatching — **HTTP 503**, code
   `no_available_capacity`, **provider never called**.

**Full suite: 418 passing**, up from 390 at the start of Batch 5.

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

- [x] **T038: Opt-in debug payload logging — complete.** New `core/debug_logging.py`, plus four integration points.

  Enabled by `ROUTER_DEBUG_PAYLOAD_LOG=<path>`. Absent means disabled, and the operational formatter is neither modified nor aware of the module — so CONSTITUTION §9.1's structural guarantee holds unchanged rather than being re-established around an exception.

  **Isolation is the design, not a side effect.** The debug logger is separate from the operational one and sets `propagate = False`. **The destination is a file, not a stream, and that choice was forced by the deployment:** Docker's logging driver merges stdout *and* stderr into one container log, so a stderr debug stream would merge into the operational stream and defeat the entire point. Verified against the running container, not only in tests.

  **Credential-shaped values are redacted before writing**, so enabling the feature cannot disclose a key even when a provider echoes one in an error body. Adapters pass only payloads and never headers — credentials travel in headers — and redaction is the backstop for the case nobody anticipates.

  **A test caught a real bug in that redaction.** The first implementation matched `api_key` and `apikey` as substrings, which **silently missed Google's own header, `x-goog-api-key`** — hyphenated, so neither pattern matched. The most important key in the codebase was the one the guard did not cover. Matching now strips separators before comparing, so `x-goog-api-key`, `api_key` and `apiKey` are recognised as one thing.

  Redaction is deliberately **name-based, not pattern-based.** Guessing at key *shapes* would be fragile in both directions — missing unrecognised formats while mangling legitimate payload content, which is the very thing this stream exists to show in full.

  Recorded: `ROUTER_DEBUG_PAYLOAD_LOG` joins `MANAGED_ENV_VARS` in `tests/conftest.py`, and an autouse fixture resets the module's enabled flag after every test. Both were real leak vectors — a developer with debug logging set in their shell would otherwise have had it apply to the whole suite, and a test that enabled it would have enabled it for every test after.
- [x] **T039: Request pacing — SKIPPED, decision recorded.** Issue #51 found that neither Google's nor Mistral's documented terms impose mandatory request pacing, so CONSTITUTION §6.1's pacing requirement is **not triggered** for M1. No pacing is implemented. This stands as the recorded outcome rather than an open task; revisit only if a provider's terms change or Issue #51 is reopened.

- [x] **T040: Milestone verification — measured 2026-10-18.**

  Run against the containerised router (`free-router`, healthy) with both providers
  configured in `config.yaml` and both credential slots resolving. 100 requests,
  alternating Google and Mistral, three prompt lengths so the token dimension was
  exercised rather than assumed.

  | | |
  |---|---|
  | **Client-observed success rate** | **100/100 = 100.0%** (target ≥80%) |
  | 95% Wilson interval | **[96.3%, 100%]** — clears the target by 16.3 pts |
  | Per provider | `gemini-3.5-flash-lite` 50/50 · `ministral-3b-2512` 50/50 |
  | Latency | p50 762 ms · p95 1366 ms · max 1901 ms |
  | 429s · cooldowns · breaker trips | **none** |

  **Conditions (recorded per `spec.md` § *Measuring the success rate*):**

  - **Request volume: 100 requests on 2026-10-18**, 50 per model.
  - **Against ceilings: 50 of Gemini's 500 RPD = 10.0%**; Mistral peaked at 0.5 of
    12.5 RPS = 4.0%. **Volume did not approach the daily ceiling** — the call-out
    `spec.md` asks for is a clean negative, so it is recorded as one.
  - **Paced at 4 s** between requests (7.5 RPM per provider). Not incidental: Gemini's
    binding limit is 15 RPM, so an unpaced script would have measured the *client's*
    burstiness and reported it as a router defect.
  - **Tracker active** throughout; all reservations reconciled.
  - **Retry active and exercised twice** — Google returned `ProviderTimeoutError` on
    attempt 1 at 02:42:58 and 02:43:59; both recovered on attempt 2. Client-observed
    success is unaffected, but the router made 106 upstream dispatches for 104 client
    requests.
  - **Failover NOT exercised.** `failovers` is null on all 104 dispatches. T033 is
    implemented and unit-tested but **this run provides no live evidence for it** —
    recorded as unmeasured rather than let it read as verified.

  **Honest limits of this result, which matter more than the headline:**

  - **It is not comparable to M0's ~50% baseline and must not be read as a 50→100
    improvement.** The conditions differ in the direction that flatters M1: this run
    was paced and stayed at 10% of the daily ceiling. A like-for-like comparison was
    not attempted, so none is claimed.
  - **100% here does not predict 100% at sustained load.** At the real ceiling the
    binding constraint becomes quota, not router quality. This run establishes the
    router is *correct while within limits*; it says nothing about behaviour at the
    boundary, which is where the ≥80% target will actually be tested in real use.
  - **Retry contributed only 2 points.** With retry disabled the run would still have
    scored 98%. The result reflects headroom, not the retry layer.

  **Console cross-check (independent of the router's own accounting):** AI Studio
  reported **53** RPD used. The router dispatched **54** Google calls — 52 client
  requests plus the 2 timed-out retries. **Delta: +1, the router over-counting.**

  The direction is the safe one: a quota system that reserves more than it consumes
  cannot overshoot the ceiling. The most likely cause is one of the two timed-out
  requests being aborted in flight, so the router recorded a dispatch the provider
  never billed. **Attributed, not proven** — the router cannot distinguish an aborted
  request from a billed one, and closing that gap would require trusting a provider's
  own accounting over our own.

  **Remaining T040 obligations:** Issue #51 confirmed still closed; the CONSTITUTION
  §8.3 deviation (in-memory quota state, ADR-0004) re-confirmed still accurate and
  still outstanding for M2.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`, full `pytest`
2. Confirm operational logs contain no payload and no key material **with debug logging disabled**.
3. Confirm debug output does not appear in the operational stream **when enabled**.
4. Record the measured success rate and the conditions under which it was measured.
5. **STOP**, review, then close Issues #6 and milestone #2 (M1).

**Batch 6 verification gate (2026-10-18):**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/` — clean. Full
   `pytest`: **435 passing**, up from 418.
2. **Debug disabled — run against the live container.** One request carrying the
   canary prompt `ZEBRAFISH-CANARY-9f3a2b` produced four operational records
   (`provider_dispatched`, `provider_responded`, `dispatch_completed`,
   `request_completed`), all telemetry only. Canary **absent**; payload-shaped keys
   (`messages`, `content`, `data`, `normalized`) **absent**; `AIza` /
   `x-goog-api-key` **absent**; **no debug file created**.
3. **Debug enabled — run against the live container.** The same request produced
   **0 canary occurrences in the operational stream** and **4 debug records**
   (`inbound`, `wire_request`, `wire_response`, `client_response`) in the file.
   Asserted as a *difference*, not an absence: the canary was confirmed present in
   the debug file first, so "not in the operational stream" cannot pass vacuously
   if the feature were broken. No credential material in either stream.
4. Success rate recorded above: **100/100**, with its conditions and its limits.
5. Disabled is the restored default — confirmed by deleting the debug file,
   reissuing the request, and observing that **no file was recreated**.

**Also fixed, found while running the gate:** `docker-compose.yml` passed only
`GOOGLE_KEY_PRIMARY`. With a Mistral model configured the container would have
failed at startup — the `:?` guard that reports a missing Google key loudly simply
did not exist for the second provider, because compose was never updated when the
Mistral adapter landed in Batch 2. `MISTRAL_KEY_PRIMARY` added with a conditional
default, so a Google-only deployment still starts without it. `ROUTER_DEBUG_PAYLOAD_LOG`
added alongside it.

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
