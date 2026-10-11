# M1: MVP OpenAI-Compatible Proxy

## Purpose

Turn the M0 prototype into a service that is *reliably usable*.

M0 proved the core abstraction works: an OpenAI-compatible request reaches Google Gemini and comes back normalized. It did so at roughly **50–60% success**, because a single request maps to a single attempt against a single key, and the provider fails often — measured live at `m0.0.0`, with provider latency spanning **482 ms to 5364 ms** and direct calls bypassing all router code failing at the same rate. The failures are upstream behaviour, not a router defect.

M1 makes the router survive that. It adds a key pool, quota awareness with cooldown, retry and failover policy, and a second provider. The success target rises from ROADMAP §4's "~50%" to **≥80%**.

## Scope

- A provider/model identity registry covering more than one provider.
- Multiple API keys per provider/model, drawn from a pool.
- Deterministic LRU key selection among eligible keys.
- Level 1 static quota counters and conservative cooldown state.
- Retry and failover policy: `429` handling, transient `5xx` retry, no blind `4xx` retry.
- A Mistral provider adapter.
- Opt-in debug request/response logging.
- Conditional request pacing, as required by CONSTITUTION §6.1.
- In-process concurrency control around key selection and quota reservation.

## Requirements

### 1. Provider/Model Identity

- The router must maintain an unambiguous internal identity of the form `provider/model`, distinct from the externally visible OpenAI-compatible identifier.
- The registry must support at least two providers, and adding a provider must not require changing the routing core.
- A client-requested model must resolve to exactly one provider and one provider-native model id.
- If no configured model matches, the request must be refused. The router must not substitute a different model.

### 2. Key Pool

- A provider/model must be able to hold more than one API key, identified by a **key alias** that is safe to log and carries no secret material.
- Key material must come from the environment only, never from `config.yaml` and never from source control (CONSTITUTION §9.2, ADR-0002).
- The mechanism by which multiple keys are configured is recorded in ADR-0003. It must not weaken ADR-0002's guarantee that a secret cannot reach `config.yaml`.
- The number of keys a provider accepts must be capped at whatever the compliance evaluation (Issue #51) concludes, and the cap must be **enforced by configuration validation**, not by convention.
- Key aliases must appear in operational logs in place of key material (CONSTITUTION §9.1).

> **Finding (Issue #51): key pooling is a credential-rotation mechanism, not a capacity
> mechanism, for Google.** Google's rate-limits documentation states that *"Rate limits are applied
> per project, not per API key"*, and Google staff confirm that all keys within a project share one
> quota pool. Two keys in one Google project are therefore two credentials against a **single** quota
> bucket, not two buckets. Independent quota on Google requires separate **projects**, which is a
> larger change than a key pool and is out of scope for M1.
>
> Consequences for this specification:
>
> - Key pooling is still required, for credential rotation, for Mistral (whose limits may be
>   per-key — unconfirmed), and as the foundation for per-project quotas later.
> - **It must not be treated as the primary reliability lever for Google.** Requirement 5 (retry on
>   transient faults) carries that role instead. See *Risk Assessment*.
> - Any claim that a second key doubles available capacity is incorrect for Google and must not
>   appear in configuration, logs, or documentation.

### 3. Key Selection (LRU)

- Selection must be deterministic: among eligible keys, the router must select the one least recently used.
- A key in cooldown, or one whose quota is exhausted, is **not eligible** and must never be selected.
- Selection must not distribute requests evenly. Concentration on one key is correct; the goal is to use a key until it cannot be used (ROADMAP §2.5, CONSTITUTION §2.5).
- Every selection must record its reason in the operational log.

### 4. Level 1 Quota & Cooldown

- The quota model must use a generic internal representation so that additional dimensions can be added later without rewriting the routing engine (ROADMAP §3.5).
- **The dimensions are per-minute token rate, per-second request rate, and token budget.** Per-second request rate is included because Mistral enforces it as a distinct ceiling that a client can exceed without ever breaching a per-minute limit — see the finding below.
- Level 1 dimensions are static configuration values. **For Google these values are deliberately conservative placeholders, not published figures** — see the finding below. They must be tuned from measured success rate rather than asserted as authoritative.
- Where a provider publishes authoritative limits, the value must match that documentation. Where it does not, the configuration must be treated as a tunable estimate.
- Every dispatch must follow the reservation cycle: estimate → reserve → dispatch → reconcile (CONSTITUTION §8.2).
- Token estimation must include a configurable safety margin so provider-side context rejections are avoided.
- On a hard provider rejection indicating context-length overflow, the router must mark that model as degraded for large-context requests (CONSTITUTION §8.2 circuit breaker).
- A `429` must place the affected key into cooldown. Cooldown duration must be configurable per provider/model rather than globally hard-coded.
- **Quota and cooldown state is in-memory for M1.** This is a recorded deviation from CONSTITUTION §8.3 — see *Recorded Deviations*.

> **Finding (Issue #51): provider free-tier limits are not published.**
>
> - **Google** defers the numeric rate-limit table to *"View your active rate limits in AI Studio"*,
>   behind sign-in. There is **no authoritative published RPM/TPM/RPD for
>   `gemini-3.5-flash-lite`**. Third-party figures conflict (5–15 RPM, ~250k TPM, 1,000–1,500 RPD
>   depending on model and vintage).
> - **Mistral** publishes no free-tier figures either. Its console lists them per model, and those
>   values were **read directly from the workspace console** (Issue #7) — see below.
>
> ROADMAP §2 requires quota values be "verified against current provider documentation rather than
> hard-coded assumptions." That cannot be satisfied for either provider from published sources.
>
> **Mistral's values are measured, not published.** Read from the free-tier workspace console
> (2026-10-08), they are authoritative *for that workspace* and are recorded as such — a genuine
> improvement on placeholders, but not a claim of provider authority. They can differ per account,
> region, or plan and can change without notice.
>
> | Model | TPM | RPS |
> | --- | --- | --- |
> | `ministral-3b-2512` | 1,300,000 | 12.50 |
> | `ministral-8b-2512` | 625,000 | 3.13 |
> | `ministral-14b-2512` | 937,500 | **0.50** |
> | `labs-leanstral-1-5-1` | 5,000,000 | 0.63 |
> | `mistral-large-2512` | 250,000 | 1.00 |
> | `mistral-medium-latest` | 20,000 | 1.00 |
> | `mistral-small-2603` | 20,000 | 1.00 |
> | `mistral-large-4` | 20,000 | 0.83 |
>
> **Google's values remain conservative placeholders.** The two providers are therefore in different
> evidentiary positions, and the configuration must record which is which rather than presenting them
> uniformly.
>
> The requirement this most directly qualifies is ROADMAP §2's verification clause. It is recorded
> here rather than quietly unmet.
>
> **Finding (Issue #7): Mistral's quota is expressed per *second*, not per minute — and some models
> permit less than one request per second.**
>
> `ministral-14b-2512` allows **0.50 RPS** and `labs-leanstral-1-5-1` **0.63 RPS**. A client issuing
> one request per second overshoots those ceilings by 2× and 1.6× respectively and is rate-limited
> immediately.
>
> The quota model here was written around per-minute dimensions with a daily reset — Google's shape.
> **Requests-per-second is a dimension it does not currently represent.** The tracker must handle it,
> or cooldown arithmetic that counts only per-minute will let these models through at double their
> real ceiling.
>
> This is emphatically **not** grounds for building the pacing module T039 skipped. Pacing is still
> not required by Mistral's terms; this is the opposite obligation — stay *under* a limit the router
> can compute, rather than throttling to a policy it was never given.
>
> **No daily (requests-per-day) limit is shown** for Mistral. The console displays only TPM and RPS.
> Absence from that table is not evidence that no daily ceiling exists, so Mistral's daily budget
> remains **unverified** and is not modelled as unlimited.

### 5. Retry & Failover

**This section carries the primary reliability load for M1.** Key pooling cannot: Google's quota is per-project, so a second key adds no capacity (§2). Retry against transient faults is the main mechanism by which the success rate rises from M0's ~50–60% toward ≥80%.

- A `429` or transient `5xx` occurring **before any bytes are written to the client** must permit failover to another eligible key for the **same** model.
- Cross-model failover is **prohibited** for an explicitly requested model (CONSTITUTION §7.2).
- An ordinary `4xx` (malformed request, context overflow, auth failure) must **not** be blindly retried across keys. Repeating an identical invalid request cannot succeed and wastes quota.
- Retries must operate within a configured attempt budget, bounded by both attempt count and elapsed time.
- If no eligible key remains, the router must fail synchronously with a normalized error. It must not queue or wait (ROADMAP §3.10).
- Retrying into a rate limit is **prohibited**. Pacing governs how fast requests are issued; failover recovers a *failure*. Papering over a `429` by immediate retry would defeat CONSTITUTION §6.1.

### 6. Mistral Adapter

- A Mistral adapter must implement the same adapter interface as the Google adapter and be selectable purely through configuration.
- It must perform bi-directional payload translation and normalize provider errors into the router's exception hierarchy.
- Adding it must not require modification of the routing core, the ingress, or the Google adapter.

### 7. Debug Payload Logging

- Full request/response payload logging must be available as an **explicit opt-in** via environment configuration.
- When enabled it must be **isolated** from the production log stream (CONSTITUTION §9.1).
- When disabled — the default — the existing guarantee is absolute: prompt text, system instructions, tool declarations, and completion contents must never appear in operational logs.

### 8. Request Pacing (Conditional)

- If Issue #51 concludes that a provider's terms require pacing, the router must enforce a minimum interval between dispatches to that provider.
- Pacing parameters are configuration, derived from the compliance evaluation, not hard-coded.

### 9. Concurrency

- Key selection and quota reservation must be safe under concurrent requests within the single Uvicorn process.
- The database layer must not block the event loop (CONSTITUTION §8.3).

## Behavioral Rules

- **Strict $0.** The router must never automatically fall back to paid capacity. When all eligible free capacity is exhausted, the request fails rather than incurring a charge (CONSTITUTION §1.1).
- **Explicit model selection is honoured.** Failover is confined to other keys for that identical model.
- **Strict refusal over silent degradation.** A request that cannot be satisfied must be rejected with a clear error. Silent stripping, truncation, or capability downgrade is prohibited (CONSTITUTION §7.1).
- **Privacy by default.** Payload exclusion from logs and databases is structural, not conventional.
- **Configuration is the source of non-secret policy.** Quota limits, cooldown durations, pacing intervals, and retry budgets are configuration values, not constants in the routing engine.

## Acceptance Scenarios

### Scenario 1: Least-recently-used selection

- **Given** Key A was last used at 10:00 and Key B at 10:05
- **And** both are eligible for the requested provider/model
- **When** a request arrives
- **Then** Key A must be selected
- **And** the operational log must record the alias of Key A and the selection reason

### Scenario 2: A cooling key is never selected

- **Given** Key A is in cooldown following a `429`
- **And** Key B is eligible
- **When** a request arrives
- **Then** Key B must be selected
- **And** Key A must not be dispatched to

### Scenario 3: Rate limit triggers cooldown and failover

- **Given** a request is dispatched on Key A
- **And** the provider responds `429`
- **When** the adapter processes the response
- **Then** Key A must be placed into cooldown
- **And** the router must attempt the same request on another eligible key for the same model
- **And** the client must receive the eventual successful response, or a normalized error if no key remains

### Scenario 4: Transient server error retries within budget

- **Given** the provider responds `500`
- **And** the retry budget has not been exhausted
- **When** the adapter processes the response
- **Then** the request must be retried on an eligible key
- **And** the attempt number must be recorded in the operational log

### Scenario 5: Malformed request is not retried

- **Given** the provider responds `400` for a malformed payload
- **When** the adapter processes the response
- **Then** the router must not retry the identical payload on another key
- **And** the client must receive a `400` with an OpenAI-compatible error body

### Scenario 6: No eligible key remains

- **Given** every key for the requested provider/model is in cooldown or exhausted
- **When** a request arrives
- **Then** the router must not dispatch the request
- **And** it must return a normalized error immediately, without waiting or queueing

### Scenario 7: Cross-model failover is prohibited

- **Given** a client explicitly requests `google/gemini-3.5-flash-lite`
- **And** no key for that model is eligible
- **And** another model is configured and eligible
- **When** a request arrives
- **Then** the router must not dispatch to the other model
- **And** it must return a normalized error

### Scenario 8: Context overflow marks the model degraded

- **Given** the provider returns a hard context-length-exceeded error
- **When** the adapter processes the response
- **Then** that model must be marked temporarily degraded for large-context requests
- **And** the router must not immediately dispatch a similar request to it

### Scenario 9: Pacing is enforced when required

- **Given** the compliance evaluation requires a minimum interval between dispatches
- **And** two requests arrive within that interval
- **When** both are processed
- **Then** the second must be delayed until the interval has elapsed

### Scenario 10: Debug logging is opt-in and isolated

- **Given** debug payload logging is not enabled
- **When** a request containing prompt text is served
- **Then** no prompt or completion text may appear in the operational log stream
- **Given** debug payload logging is enabled
- **Then** payloads must appear only in the isolated debug stream, not in operational logs

### Scenario 11: Key alias never exposes key material

- **Given** a request is served using a pooled key
- **When** the operational log records the dispatch
- **Then** it must record the key alias
- **And** it must not contain the key itself

### Scenario 12: Second provider requires no core changes

- **Given** a Mistral model is configured
- **When** a request is made for it
- **Then** it must be served through the Mistral adapter
- **And** no change to the ingress, routing core, or Google adapter may be required

## Edge Cases

- **Zero eligible keys on first use.** Every key cooling at startup must fail fast rather than dispatch blindly.
- **All providers exhausted.** Must return a normalized error and must never attempt paid capacity.
- **Quota counters after a restart.** In-memory counters reset to zero on restart, so the router may briefly exceed a daily limit it had already reached. This is the known cost of the recorded deviation.
- **Cooldown longer than the process lifetime.** A persisted cooldown from a previous run is not read in M1; the key is treated as fresh.
- **Concurrent requests racing for the last eligible key.** Must not dispatch two requests to a key that has just entered cooldown.
- **A model whose provider has no keys configured at all.** Must be treated as unavailable, not as a configuration error at request time.
- **Provider returning a body that cannot be decoded.** Must map to an upstream fault, not an unhandled exception.

## Error Behavior

All failures continue to return the OpenAI-compatible error envelope established in M0:

```json
{
  "error": {
    "message": "Description of the error",
    "type": "invalid_request_error",
    "param": null,
    "code": "specific_error_code"
  }
}
```

| Condition | Status | Code |
| --- | --- | --- |
| No eligible key or provider capacity | 503 | `no_available_capacity` |
| All keys cooling, retry budget exhausted | 503 | `retries_exhausted` |
| Provider `429` with no alternative key | 429 | `provider_rate_limited` |
| Provider `5xx` after retries | 502 | `provider_server_error` |
| Provider `400` malformed | 400 | `provider_validation_error` |
| Provider auth failure | 401 | `provider_auth_error` |
| Provider timeout after retries | 504 | `provider_timeout` |
| Unknown model | 400 | `unsupported_model` |
| Pacing wait exceeded maximum | 503 | `pacing_exceeded` |

`503` is new in M1: "no eligible capacity" is a real, expected outcome when free tiers are exhausted, and previously it was indistinguishable from a provider fault.

## Security / Privacy Considerations

- **No secrets in configuration.** Key material is environment-only. The key-pool mechanism must not permit a key into `config.yaml` (ADR-0002, ADR-0003).
- **No secrets in logs.** Only key aliases. A log consumer must never be able to reconstruct a key.
- **Payload exclusion remains absolute** for operational logs and databases (CONSTITUTION §9.1). Debug payload logging is opt-in and isolated.
- **No prompts or completions in `request_logs`.** The M0 schema has no such columns and none may be added.
- **Compliance Mode is a security control, not paperwork.** The key cap and pacing requirement exist to reduce the risk of account suspension. They must be enforced in code, not merely documented.

> **Prerequisite, not code: Mistral's free tier trains on prompts unless told not to.**
>
> Mistral's help centre states that *"Customers with pay-as-you-go enabled are opted out of training
> by default"* and that *"Users in **Free mode** may opt out of data training for Mistral Studio and
> related API services"* via **Admin → Privacy → Anonymous improvement data**. The opt-out is
> something a free-mode user *performs*; it is not a default.
>
> This project is strictly $0 (CONSTITUTION §1), so the paid tier's default opt-out is not
> available. Sending prompts to the Mistral adapter therefore **requires** that toggle to be off,
> or CONSTITUTION §9.1 is violated at the provider rather than at the router. The router not logging
> prompts does not compensate for the provider training on them.
>
> **This is a workspace console setting, not a property of the provider, and nothing in this
> repository can enforce or verify it.** A new workspace defaults back to opted-in. It is recorded
> here as a documented manual prerequisite rather than implemented, because Mistral exposes no API to
> query the setting — without this note the toggle is one workspace-clone away from being silently
> lost. Confirmed off for the development workspace on 2026-10-08.

## Explicit Non-Goals

- Automatic paid-provider fallback (strict $0).
- Capability-aware or quality-based model selection (M3/M4).
- Router-controlled `model = "free"` selection (M4).
- Persistent quota and cooldown state across restarts (M2) — see *Recorded Deviations*.
- Streaming / SSE (M2).
- Tool calling (M3).
- `GET /v1/models` (M2).
- Administrative API or UI (M6).
- Provider health scoring or adaptive reliability (M5).
- Migrating the Google adapter to Gemini's newer Interactions API.

## Recorded Deviations

### CONSTITUTION §8.3 — quota state persistence

CONSTITUTION §8.3 requires that quota counts, cooldown timestamps, and provider availability state be persisted to SQLite and survive restarts.

**M1 keeps them in memory.** ROADMAP places persistence in M2. The consequence is real and must not be understated: after a restart, the router forgets daily request counts and cooldown state, so it can briefly exceed a limit it had already reached, and a key that was cooling may be retried immediately.

This deviation is accepted deliberately, with the Constitution treated as the definition of done: it is recorded here and tracked as M2 work rather than being discovered later as unexplained behaviour.

### ADR-001 §2.4 — stale timeout value

ADR-001 §2.4 still records a 400 ms `asyncio.timeout` for the provider envelope. The M0 specification was amended to a 5 s configurable budget after live measurement showed provider headers alone routinely exceed 400 ms. ADR-001 is corrected as part of this milestone's planning so that a future reader does not reintroduce the defect.

### ROADMAP §2 — verification of provider quota values

ROADMAP §2 requires that exact provider quota values be "verified against current provider documentation rather than hard-coded assumptions."

This **cannot be satisfied for either provider**, as Issue #51 established: Google defers its rate-limit table to a signed-in console, and Mistral publishes no free-tier figures at all. M1 ships conservative placeholders marked unverified and tunes from measurement.

The deviation is recorded rather than papered over with third-party figures that conflict.

### ROADMAP §4 — success target

The "~50% successful requests" MVP figure was set when a single key and no retry existed. It is superseded by the **≥80%** target in this specification. ROADMAP §4 is updated to match.

## Risk Assessment

### The ≥80% target may not be reachable by the assumed means

M1 was planned expecting key pooling to contribute materially to the success rate. **It does not, for Google** — Google's quota is per-project, so additional keys share one bucket. Retry on transient faults is therefore the load-bearing mechanism, and it is bounded by the provider's actual failure modes.

If the measured rate lands materially below 80%, the honest response is to record the measurement and its conditions rather than to keep retrying more aggressively — CONSTITUTION §6.1's pacing requirement exists precisely to prevent unbounded retries from looking like abuse. A shortfall is information for M2's planning, not a target to be met by defeating the compliance mode.

### Quota constants start unverified

Both providers hide free-tier limits behind a console. M1 ships conservative placeholders. The consequence is that counters may under- or over-estimate true capacity, and a tuned value may be wrong for a different account or region. This is accepted for M1 and is a known quality gap, not an oversight.

### Key pooling may be inert for Google

Two keys in one Google project provide credential redundancy but no additional capacity. The M0 deployment target of "~1 key per provider" is therefore unchanged in practice, and the two-key scenario is exercised mainly against Mistral.

## Definition of Done

Per `docs/SDD_WORKFLOW.md` §21, and with CONSTITUTION as the governing standard:

- [ ] All acceptance scenarios implemented and verified
- [ ] Relevant unit and integration tests pass
- [ ] Issue #51 closed — compliance evaluation and quota research complete
- [ ] ADR-0003 and ADR-0004 recorded
- [ ] Measured success rate recorded against the ≥80% target, with conditions stated
- [ ] Quota configuration values marked as unverified placeholders, not presented as published figures
- [ ] No claim appears anywhere that a second Google key adds quota capacity
- [ ] No secrets in source, configuration, or logs
- [ ] No out-of-scope functionality introduced
- [ ] The recorded §8.3 deviation is still accurate, or has been closed out
