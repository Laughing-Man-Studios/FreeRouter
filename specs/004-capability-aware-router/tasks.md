# Task Breakdown: M3 Capability-Aware Router

**Feature ID:** `004-capability-aware-router`
**Issues:** #16 (epic), #17 (schema), #18 (detection), #19 (filtering), #37 (planning)
**Companion documents:** `spec.md` (contract), `plan.md` (approach)
**ADRs:** 0008, 0009

Task numbering continues from M2, which ended at **T064**. M3 starts at **T065**.
Numbers are stable identifiers: a dropped task keeps its number and is marked
dropped rather than recycled.

## Ordering Rationale

The three implementation batches are a chain, not a list:

```text
Batch 1 (#17) capability schema ──▶ Batch 2 (#18) detection
                │                          │
                └──────────┬───────────────┘
                           ▼
                Batch 3 (#19) filter + substitution + wiring
                           ▼
                Batch 4    exit-criteria verification
```

- **Detection without a schema has nowhere to put its result.** #18 could merge
  first and produce `RequestRequirements` objects nobody consumes, but its tests
  would have no way to assert eligibility, so the batch would test parsing and not
  behaviour.
- **The filter needs both**, because every rule it applies is a comparison between
  a detected requirement and a declared capability.
- **Batch 3 is the only batch that changes request handling.** Until it lands, the
  router behaves exactly as it does at the M2 head. That keeps the behaviour change
  in one reviewable place.

One dependency is deliberately **not** in this milestone: adapter pass-through for
tools, vision and structured output. `spec.md` § C5 records why, and Batch 4's
verification states the consequence rather than pretending the gap closed.

---

## Batch 0: Milestone Design (planning, no code)

**Maps to GitHub Issue:** `#37 Milestone Planning & Capability Model Design`

- [ ] **T065: Author the M3 design documents — this batch.**

  `specs/004-capability-aware-router/spec.md`, `plan.md`, `tasks.md`, plus
  ADR-0008 (capability metadata and fail-closed eligibility) and ADR-0009 (opt-in
  substitution and §7.2).

  **Six decisions are recorded rather than assumed** (spec § *Clarifications
  Recorded*): substitution is opt-in per model and off by default (C1); missing
  capability data is disqualifying (C2); the schema lives on `ModelConfig` and no
  `ModelRegistry` class is introduced (C3), which is a deviation from #17's task
  text; verification is recorded per capability via `provenance` + `source` (C4);
  M3 delivers detection but not forwarding, so tool/vision/schema requests are
  refused (C5); `streaming_unsupported` keeps its own code (C6).

  **C5 is a deviation from the M2 record and is recorded as one.** `specs/003`
  § A1 deferred the OpenHands objective to "M3, where capability-aware routing and
  tool support converge". M3 delivers the first half only. The M3 exit criterion is
  still met; the wider objective is not, and `tasks.md` § *Milestone Exit Criteria*
  says so before any code is written.

  **The `ModelRegistry` wording in #17 is not implemented literally.** No such class
  exists — the registry is `config.models` — and §2.3 argues against inventing one
  for three lookups. Recorded in spec C3 and in the #17 batch, so the issue text and
  the implementation difference is visible rather than discovered later.

**Verification & Stop:**

1. `markdownlint` clean.
2. Every open question in #37 has a recorded decision with its reasoning and the
   alternatives that were rejected.
3. `spec.md` stateable in one sentence: *a request is never dispatched to a model
   whose declared capabilities cannot satisfy it, and never silently repaired to
   make it fit.*
4. **STOP**, review, then the M3 branch is cut and #37 closes.

---

## Batch 1: Capability Schema & Registry Expansion

**Maps to GitHub Issue:** `#17 Model Capability Metadata Schema & Registry Expansion`

- [x] **T066: Define the capability schema.**

  `CapabilityValue[T]` (`value`, `provenance`, `source`) and `Capabilities`
  (`context_window`, `supports_streaming`, `supports_tools`, `supports_vision`,
  `supports_json_schema`) in `core/config.py`, attached to `ModelConfig` as a
  **required** field.

  Generic rather than a union type so `context_window` stays an `int` under
  `mypy --strict`; a `int | bool` field would need a cast at every comparison.

  Validators, all at load time:

  - `source` required for `published` / `measured`, refused for `assumed`.
  - `context_window` a positive integer; zero is refused as a missing value
    wearing a number.
  - unknown capability keys refused (`extra="forbid"`), because a misspelled
    capability silently ignored is the same defect class as a stripped field.

  **`provenance: assumed` is not decoration.** It is the only way to record a
  guess *without* it becoming a guarantee: the filter treats assumed as unknown.
  Without it, an operator's belief and a verified fact are indistinguishable in
  config, and the milestone's central claim rests on beliefs.

- [x] **T067: Populate `config.yaml` with verified capability metadata.**

  Every declared model gains a full capability block. Each `published` value names
  its source and the date it was read; `measured` values name the measurement.

  **Mistral stays commented out, and its block goes inside the comment.** Declaring
  the Mistral model entry makes `MISTRAL_KEY_PRIMARY` mandatory and a Google-only
  operator can no longer start the router — the existing comment records this, and
  M3 must not change the deployment contract as a side effect of adding metadata.
  The commented block carries capability values so enabling Mistral remains a
  copy-paste rather than a research task.

- [x] **T068: Capability and fallback queries on the configuration objects.**

  `Config.model_by_id(model_id) -> ModelConfig | None`, and cross-model validation
  for `fallback_models`: unknown ids, self-reference, duplicates, and a fallback
  that itself declares fallbacks are all refused at startup.

  **No `ModelRegistry` class.** #17 says "expose capability query methods on
  `ModelRegistry`"; the word assumes a class that does not exist. Recorded as a
  deviation in spec C3: a wrapper whose entire content is three lookups over an
  in-memory validated list is §2.3 work, and the filter is already a pure function
  over `config.models`.

  Single-hop is enforced by refusing a fallback that declares its own, rather than
  by silently truncating the chain — a config that says something the router does
  not do is worse than one that refuses to load.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. Every named validation rule has a test that asserts the *message names the
   model and field*, since that message is the operator's only diagnostic.
3. A test parses the shipped `config.yaml` and asserts every model carries all five
   capabilities — the shipped file must not be the one place the rule is unmet.
4. `pytest tests/test_config.py tests/test_capabilities_schema.py`
5. **STOP**, review, then update #17.

**Batch 1 verification gate (2026-11-04).** All four checks pass under `make check`
(ruff, format, mypy strict, pytest, markdownlint), with no provider credential set.
Beyond the original stop conditions, the review added: duplicate `model.id` is
refused (the id is the routing key, so two lookups must not disagree), capability
values are strict (a boolean cannot become a token count), error locations name the
model id rather than its list index, the shipped numbers are pinned to their
sources, and the commented Mistral entry is loaded by a test so it cannot rot.

---

## Batch 2: Local Request Capability & Size Detection

**Maps to GitHub Issue:** `#18 Incoming Request Capability & Context Size Parser`

- [x] **T069: Detect hard requirements from the payload.**

  `core/capabilities.py`: `RequestRequirements`, `detect_requirements(payload,
  assumed_output)`.

  Reads `tools`, `tool_choice`, `response_format`, message content parts,
  `stream`, `max_tokens` / `max_completion_tokens`. Table-driven tests per
  `plan.md` § *Detection*.

  **Detection runs on the raw payload, before `normalize_chat_completion_request`,
  and a test asserts that ordering.** Normalization honours four fields and drops
  the rest; a detector placed after it would inspect a request that has already
  been repaired. This is the same technique M2 used for `stream`, one step earlier
  in the pipeline.

  Two shape decisions worth stating:

  - **`tools: []` is not a requirement.** OpenAI SDKs send an empty array when no
    tools are used; treating it as "needs tools" would refuse ordinary chat.
  - **`tool_choice: "none"` with a non-empty `tools` is still a requirement.** What
    matters is that the client asked for tools to be forwarded, and M3 forwards
    none.

- [x] **T070: Size the request against the window.**

  `input_tokens` from `quota.estimation.estimate_input_tokens` — the same estimator
  the reservation cycle uses — plus `reserved_output` from the client's `max_tokens`
  when supplied, else `quota.output_token_assumption`.

  **Reusing the reservation estimator is deliberate.** A second estimator for the
  same question would drift from the first, and the first is already conservative
  against both providers (measured 4.4–5.1 chars/token). The known bias — the
  estimator ignores per-message overhead and list-shaped content — is recorded in
  `plan.md` § *Risks* and is not fixed here: changing the shared estimator would
  move M2's measured quota behaviour inside a capability milestone.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. The table in `plan.md` § *Detection* is a parametrised test, not prose.
3. A payload carrying `tools` is detected **and** still normalizes to a request
   with no tools, proving detection precedes the strip.
4. `pytest tests/test_capabilities_detection.py tests/test_quota_estimation.py`
5. **STOP**, review, then update #18.

**Batch 2 verification gate (2026-11-08).** All checks pass under `make check`
(ruff, format, mypy strict, pytest, markdownlint) and `make test-hermetic`, with
no provider credential set. 669 passed, up from 629. Pure deterministic detector
implemented in `core/capabilities.py`, verified with table-driven tests in
`tests/test_capabilities_detection.py`, and ordering relative to normalization asserted.
Review hardening: `tool_choice: "auto"`/`"none"` and non-function dicts are not
requirements; malformed, boolean, zero or negative token limits fall back to
`assumed_output`; and `assumed_output` is required so the configured quota policy
cannot be bypassed.

---

## Batch 3: Hard-Requirement Filtering, Substitution & Wiring

**Maps to GitHub Issue:** `#19 Hard Requirements Filtering Pipeline`

- [ ] **T071: The eligibility predicate.**

  `mismatch(model, requirements) -> Mismatch | None` — the single place the rules
  live: provenance handling (`assumed` ≡ unknown), capability flags, and
  `input + reserved_output <= context_window`.

  Context comparison is `<=`: a request that exactly fills the window fits. An
  off-by-one that refuses the largest legal request is a worse failure than
  forwarding it to a provider that will apply its own, authoritative boundary.

  Code selection: `streaming_unsupported` when streaming is the unsatisfied
  requirement (C6), `context_window_exceeded` for size, `capability_unsatisfiable`
  otherwise.

- [ ] **T072: `resolve_target` — substitution, and the rules that stop it.**

  Step 3 of the §7.1 pipeline: honour the requested model, else walk
  `fallback_models` in declared order and take the first eligible one. Single hop.

  **Substitution is triggered by capability or context mismatch only.** A rate
  limit or cooldown on the requested model keeps its existing outcome and never
  reaches a fallback — §7.2 confines failover on a rate limit to other credentials
  for the identical model. The test for this is a cooldown on a model that declares
  fallbacks, asserting the fallback was never selected.

  M3 does not rank fallbacks. Step 4 is M4, and "first eligible" is a declared
  order rather than a score.

- [ ] **T073: Wire both routes, and report the model that served the request.**

  `api/routes.py` (unary) and the streaming path both call
  `resolve_target`; the effective model id flows into `to_openai_completion` and
  `StreamingRenderer` so the response `model` is the one actually used.

  Two new exceptions, `CapabilityUnsatisfiableError` and
  `ContextWindowExceededError`, both `ValidationError` subclasses so the existing
  handler produces the M2-shaped `400` envelope.

  **The streaming route gets the same filter call, not a copy.** M2 duplicated
  capacity gating between the two routes and needed a parity test to keep the copies
  in step; M3 puts the policy in one function called by both, so there is nothing to
  keep in step.

  Substitution is logged at `INFO` with the requested model, the satisfying model and
  the requirement that forced it. The log line carries ids and requirement names,
  never payload content (§9.1).

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. Every refusal test asserts **zero outbound requests** via `respx`, not merely a
   `400` response — a refusal that still contacts a provider has spent quota to
   learn nothing.
3. Scenarios 1–12 of `spec.md` each have a test, named after the scenario.
4. The full M2 suite passes unchanged with capability blocks in `config.yaml` —
   ordinary chat requests must be untouched by the filter.
5. `pytest tests/test_capabilities_filter.py tests/test_routes.py
   tests/test_streaming.py`
6. **STOP**, review, then update #19.

---

## Batch 4: Exit-Criteria Verification & Milestone Report

- [ ] **T074: Prove the exit criterion, and report the gap.**

  Verify against a running router, not only in tests:

  1. An in-context request dispatches exactly as at the M2 head.
  2. An oversized request is refused locally with `context_window_exceeded` and the
     provider's console shows **no** corresponding request.
  3. A tools request is refused with `capability_unsatisfiable`, and the provider
     console again shows nothing.
  4. A model with `fallback_models` serves an oversized request from the fallback,
     the response `model` names the fallback, and the log records the substitution.
  5. A cooling model with fallbacks does not substitute.
  6. Startup fails, naming model and field, for a config missing a capability or
     carrying an unknown key.

  **The report must state two things separately**, because they are separate:
  the M3 exit criterion is met, and the OpenHands objective carried from M2 is
  **not** — tool calling is detected and refused, not served. Reporting the first
  as if it implied the second is the failure mode this milestone is most exposed to.

**Verification & Stop:**

1. `make check` green; suite passes with no provider credential set.
2. Each numbered item above recorded with its observed output, and with whether it
   was **observed** or **injected** — the distinction M2's T061 matrix established
   and which must not be quietly dropped.
3. `spec.md` § *Explicit Non-Goals* reviewed against what was built.
4. **STOP**, review, then the milestone integration PR.

---

## Milestone Exit Criteria

ROADMAP Milestone 3: *"A request is never deliberately sent to a model that the
local registry already knows cannot satisfy its hard requirements."*

| Criterion | Status after M3 |
| :--- | :--- |
| No dispatch to a model known incapable | **Met** — the filter runs before key selection and before dispatch |
| Hard requirements detected locally, no classifier | **Met** — `detect_requirements`, rules only |
| Clear error when nothing fits, no silent truncation | **Met** — four codes, no truncation path exists |
| Configurable fallback relationships | **Met** — opt-in `fallback_models`, single hop |
| Capability metadata schema, extensible | **Met** — closed schema, additive extension documented |
| Large-context candidate selection | **Met via opt-in fallback**; not automatic, by decision C1 |

**Not met, and not claimed:**

| Objective | Status |
| :--- | :--- |
| OpenHands usable as an actual workload (deferred from M2 by spec 003 § A1) | **Not met.** M3 detects tool calls and refuses them; no adapter forwards `tools`. |

The M2 record deferred OpenHands to M3 "where capability-aware routing and tool
support converge". The convergence did not happen: #17–#19 scope awareness, and no
issue in M3 scopes adapter pass-through. That is visible here, in `spec.md` § C5 and
in the milestone report — not absorbed into a success claim.

## Carried Into M4

1. **Tool, vision and structured-output pass-through.** Google `functionDeclarations`,
   Mistral `tools`, streaming `tool_calls` deltas and the `tool_calls` finish reason.
   The largest piece of work this milestone identified and did not do.
2. **Ranking (step 4 of the §7.1 pipeline)** and `model = "free"` (CONSTITUTION §7.3).
   M3 stops at step 3 by design.
3. **Estimator fidelity**: per-message overhead, and list-shaped content for
   multimodal requests. Carried to avoid moving M2's measured quota behaviour.
4. **Provider-discovered capability metadata**, as a second source behind the same
   provenance fields.
5. **Multi-hop fallback chains**, if a real case appears. Single hop is a policy,
   not a limitation to be relaxed on the way past.
