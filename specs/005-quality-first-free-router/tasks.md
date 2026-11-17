# Task Breakdown: M4 Quality-First Free Router

**Feature ID:** `005-quality-first-free-router`
**Issues:** #20 (epic), #38 (planning)
**Companion documents:** `spec.md` (contract), `plan.md` (approach)
**ADRs:** 0010, 0011

Task numbering continues from M3, which ended at **T074**. M4 starts at **T075**.
Numbers are stable identifiers: a dropped task keeps its number and is marked
dropped rather than recycled.

Batch issues are **not** created yet. Batch 0 is covered by #38; Batches 1–6 are
created as sub-issues of #20 when each batch starts, per `docs/SDD_WORKFLOW.md`
§ 27. This document is the plan those issues are cut from, so a batch that changes
shape records the change here rather than only in the issue.

## Ordering Rationale

```text
Batch 1  boundary + estimator        (no behaviour change)
              │
              ├──────────▶ Batch 2  tools        ──▶ OpenHands unblocked
              │                 │
              │                 ├──▶ Batch 3  structured output
              │                 │
              │                 └──▶ Batch 4  vision
              │
              └──────────▶ Batch 5  routing policy + model = "free"
                                   │
                                   ▼
                            Batch 6  verification + milestone report
```

- **Batch 1 has to come first.** Tools, tool results and image parts are all payload
  the current estimator ignores. Pass-through without sizing would dispatch requests
  that the provider then rejects on context, spending a call to learn what the router
  could have known.
- **Batch 2 comes before 3 and 4** because it is the OpenHands unblocker and the
  largest provider-translation risk. If the milestone has to give something up, it
  should give up the smallest thing, and that is cheaper to discover first.
- **Batch 5 is independent of 2–4 in code** and sequenced after them because the
  `tool_support` weight is a dead weight until a model can declare tool support
  truthfully. Building it against a registry where every model scores `0` would ship a
  weight that looks implemented and does nothing.
- **Batch 6 is verification, not implementation.** A failure there is a finding, not a
  quiet fix.

---

## Batch 0: Milestone Design (planning, no code)

**Maps to GitHub Issue:** `#38 Milestone Planning & Scoring Policy Design`

- [x] **T075: Author the M4 design documents — this batch.**

  `specs/005-quality-first-free-router/spec.md`, `plan.md`, `tasks.md`, plus ADR-0010
  (auto-routing: static priors, hard constraints before scoring, concentration) and
  ADR-0011 (capability pass-through and declared serving).

  **The three open questions in #38 are answered with reasons, not assumptions**
  (spec § *Clarifications Recorded*):

  - *How is quality scored at M4?* A static 0–100 operator prior per model (R2). No
    benchmark, no inference from model size, no deferral of `free` to M5.
  - *How is reliability represented?* As a hard eligibility filter, not a score (R3).
    The only health state that exists at M4 is already eligibility: cooling, breaker
    degradation, persistence failure.
  - *Is the virtual id exactly `free`?* Yes, case-sensitive, enabled by the presence of
    a `routing` block (R1).

  **One roadmap inconsistency is corrected rather than copied.** ROADMAP §3.9 lists
  five illustrative weights including `quota` and `reliability`, and in the same
  section makes quota a hard constraint. Both cannot hold. The priority list wins, the
  weight set is three dimensions, and an operator who copies the illustrative block
  gets a startup error naming the refused key and the reason (R3). The roadmap is not
  edited: it already says its values are illustrative and not final.

  **The pass-through deliverables are planned in full, including vision, which has no
  consumer yet.** Dropping it quietly is exactly what the M3 report criticised about
  the M2 hand-off. If it slips, Batch 6 records it as undelivered.

**Verification & Stop:**

1. `markdownlint` clean.
2. Every open question in #38 has a recorded decision with its reasoning and the
   alternatives rejected.
3. Every acceptance criterion in #38 is traceable to a section of `spec.md` and a
   batch in this document.
4. `spec.md` stateable in one sentence: *a client can ask for the best free model that
   can serve the request, receive a concrete model id, and read in the log exactly why
   that model and not another.*
5. **STOP**, review, then cut the M4 branch and close #38.

---

## Batch 1: Boundary and Estimator

- [ ] **T076: Extend the internal models for the capabilities M3 detects.**

  `core/normalization.py`: `NormalizedRequest` gains `tools`, `tool_choice`,
  `response_format`, and message content that may be a list of parts;
  `NormalizedResponse` gains `tool_calls`; `NormalizedChunk` gains a tool-call delta;
  `FinishReason` gains `"tool_calls"`; `content` becomes optional on both response
  models.

  **Nothing is forwarded in this batch.** The fields exist so the adapters can be
  written one at a time, and so the boundary change is reviewed once rather than three
  times. A model that still declares `assumed` still causes the M3 refusal, so no
  request changes behaviour yet — asserted by the existing M3 suite passing unchanged.

  **`content` becomes `str | None`, not `""`.** OpenAI returns `null` with a tool
  call; an empty string is indistinguishable from an empty answer, and a client that
  branches on truthiness would silently treat a call as a message.

  `extra="forbid"` stays. `temperature` is still stripped, and a test asserts it —
  the boundary's original claim must survive the boundary growing.

- [ ] **T077: Size the payload the router is now willing to forward.**

  `quota/estimation.py`: per-message framing overhead, serialised `tools` /
  `tool_choice` / `response_format` size, and list-shaped `content` (text parts counted
  as text, image parts counted by a configured tokens-per-image assumption).

  **Carried from M3 and now load-bearing.** M3 recorded the bias and deliberately did
  not fix it, to avoid moving M2's measured quota behaviour inside a capability
  milestone. That reason has expired: an agentic turn can carry a whole file in a tool
  result, and `chars / 4` cannot see it.

  **The change is measured, not asserted.** Record the estimate before and after on a
  fixed set of payloads in this task's batch note. Estimates rise, which is the safe
  direction for a token window, but it is still a change to numbers M2 measured.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. The full M1–M3 suite passes unchanged with a `routing` block absent: this batch adds
   capability, not behaviour.
3. A payload carrying `tools` normalizes into a request that still has no forwarded
   tools, proving the boundary can carry what no adapter yet sends.
4. `pytest tests/test_normalization.py tests/test_quota_estimation.py
   tests/test_capabilities_detection.py`
5. **STOP**, review, then update the batch issue.

---

## Batch 2: Tool Pass-Through

- [ ] **T078: Gemini tools, unary, including the second turn.**

  `providers/google/adapter.py`: `tools` → `functionDeclarations`; `tool_choice` →
  `toolConfig.functionCallingConfig`; assistant `tool_calls` → `functionCall` parts;
  `role: "tool"` messages → `functionResponse` parts in the following turn;
  `functionCall` in the response → OpenAI `tool_calls` with `content: null` and
  `finish_reason: "tool_calls"`.

  **Call ids are synthesised**, because Gemini returns none and OpenAI clients echo the
  id back with the result. Deterministic within a response; the router does not promise
  stability across independent requests.

  **The second turn is the acceptance bar.** A single tool call that cannot be followed
  by its result is a demo, not a working loop. The test posts call, result, follow-up.

  The `functionResponse` turn shape (role and part placement) is confirmed against the
  provider's current documentation at implementation time. If it cannot be made to
  work, the finding is recorded and `supports_tools` stays `assumed` — no declaration
  ships on an unverified guess.

- [ ] **T079: Mistral tools, unary, including the second turn.**

  `providers/mistral/adapter.py`: forward `tools`, `tool_choice`, `tool_calls` and
  `role: "tool"` messages; pass `finish_reason: "tool_calls"` through; delete the
  adapters' "unreachable because M0 strips tools at ingress" notes, which this batch
  makes false.

  **This adapter is close to identity and that is the finding, not a shortcut.** The
  work here is proving the OpenAI-shaped fields survive the boundary intact, which is
  also the cheapest place to catch a boundary bug before blaming Google.

- [ ] **T080: Streamed tool calls, both adapters.**

  `api/streaming.py`: `StreamRenderer` emits `delta.tool_calls` and delivers a chunk
  whose only content is a call. Both adapters translate their streamed function-call
  form into that delta.

  **A call that arrives whole and a call that arrives in fragments are both supported.**
  Gemini may deliver a complete `functionCall` in one event; OpenAI-shaped providers
  stream `arguments` fragments. A renderer written for one shape drops the other, and
  drops it silently.

  The terminal chunk carries `finish_reason: "tool_calls"` and the stream still ends
  with `[DONE]`. M2's fail-fast rule is unchanged: once a chunk has been delivered, a
  later failure still terminates rather than replaying.

- [ ] **T081: Measure and declare tool support.**

  A live unary and streamed tool call through each configured model, then
  `supports_tools: {value: true, provenance: measured, source: "<measurement, date>"}`
  in `config.yaml` for the models that passed, and in the commented Mistral block for
  the entry that cannot be measured without a key.

  **The declaration is the point of the batch.** M3's C5 and M4's R9 say a capability
  is declared `true` only when it is servable and verified. A batch that forwards tools
  but leaves the capability `assumed` has delivered code the router will never use;
  a batch that declares it without measuring has delivered a guess with a measurement's
  authority.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. Round-trip per adapter: call, `content: null`, `finish_reason: "tool_calls"`,
   arguments as a JSON string.
3. Second turn per adapter: a `role: "tool"` message produces a normal assistant answer.
4. Streamed call per adapter: `delta.tool_calls` delivered, terminal reason
   `tool_calls`, `[DONE]` present.
5. With the capability still `assumed`, the M3 refusal still fires — pass-through must
   not have made the filter permissive.
6. `make check` and `make test-hermetic` green.
7. **STOP**, review, then update the batch issue.

---

## Batch 3: Structured-Output Pass-Through

- [ ] **T082: Gemini structured output.**

  `response_format: json_schema` → `generationConfig.responseMimeType` plus
  `responseSchema`; `json_object` → the mime type alone.

  **Gemini's `responseSchema` is an OpenAPI 3.0 subset, not full JSON Schema.** A
  client schema using a construct outside it cannot be forwarded faithfully, and the
  choice is between refusing locally and forwarding something that may be rejected.
  Decide in this batch, record the decision, and refuse locally only if forwarding
  would change the constraint.

- [ ] **T083: Mistral structured output, and declare both.**

  Forward `response_format` (near passthrough). Then measure `json_schema` and
  `json_object` against each provider and record `supports_json_schema` as `measured`
  for the pairs that passed.

  **Measured per provider, not per capability.** It is entirely possible for one
  provider to satisfy both forms and the other only `json_object`; the capability
  block is per model and must say what is true for that model.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. A `json_object` request is forwarded and returned unchanged in shape, per adapter.
3. A `json_schema` request is forwarded where supported; where it is not, the refusal
   is local, names the limitation, and makes no outbound call.
4. With the capability `assumed`, M3's refusal still fires.
5. `make check` and `make test-hermetic` green.
6. **STOP**, review, then update the batch issue.

---

## Batch 4: Vision Pass-Through

- [ ] **T084: Gemini image input.**

  `image_url` parts with base64 data → `parts[].inlineData` with the mime type taken
  from the data URL. A plain `http(s)` URL is refused locally (R11).

  **The router does not fetch images.** Server-side fetching of a client-supplied URL
  is an SSRF surface and an egress proxy, and neither is in scope for a request proxy
  that has never made an outbound call the configuration did not declare.

- [ ] **T085: Mistral image input, and declare both.**

  Forward image content parts in the shape Mistral accepts, apply the same remote-URL
  rule, then measure and record `supports_vision` as `measured` for the models that
  passed.

  **This is the batch most likely to slip, and that is stated in advance.** OpenHands
  needs tools, not images; vision is a ROADMAP deliverable with no consumer yet. If it
  slips, Batch 6 records it as undelivered rather than the plan omitting it.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. A base64 image part round-trips per adapter and the completion is returned.
3. A remote `http(s)` image part is refused locally with no outbound request.
4. With the capability `assumed`, M3's refusal still fires.
5. `make check` and `make test-hermetic` green.
6. **STOP**, review, then update the batch issue.

---

## Batch 5: Routing Policy and `model = "free"`

- [ ] **T086: The routing configuration schema.**

  `core/config.py`: `RoutingWeights` (quality, tool_support, latency; `extra="forbid"`;
  total > 0), `RoutingConfig` (`weights`, `latency_ceiling_ms`), and
  `ModelConfig.routing: RoutingPrior | None` (`quality` 0–100, `latency_ms` > 0,
  both required together).

  **`quota` and `reliability` are refused by name, with the reason.** Not accepted and
  ignored: a weight that cannot be computed is the configuration equivalent of a
  stripped `tools` field (R3).

  **Priors are optional per model; the policy is not optional if present.** A model
  without priors is excluded from `free` with a stated reason and a startup warning,
  and stays explicitly selectable. Routing configured with no scoreable model is a
  startup failure, because `free` could then never be served (R10).

- [ ] **T087: Scoring.**

  `core/routing.py`: `score_model`, `rank_candidates`, `ModelScore`,
  `RankedCandidate`, `Exclusion`. Pure functions over configuration and the detected
  requirements; no I/O, no state.

  - `latency_component = max(0, 1 - latency_ms / ceiling)`; above the ceiling scores
    zero and stays eligible.
  - `tool_component` treats an `assumed` capability as `0`, consistent with M3.
  - Ties break on configuration order, so the same config and request always select
    the same model.
  - Weights are normalised by their total, so they may be fractions, percentages or
    points.

- [ ] **T088: Selection and the `free` entry point.**

  `resolve_request(config, requirements, requested_model, *, capacity_probe)` is the
  one function both routes call. Explicit ids delegate to M3's `resolve_target`,
  unchanged; `free` sweeps every model through the capability filter, then the
  capacity probe, then scores the survivors.

  **The capacity probe becomes a sweep, and it is cheap.** `Dispatcher._check_capacity`
  is in-memory by design, so probing every candidate is dictionary lookups. When every
  capable candidate is out of capacity, the first probe error in configuration order is
  raised, preserving the M2 status code (M3's rule, reused).

  `Target.selection` distinguishes `explicit`, `substituted` and `auto`. Inferring
  "auto" from `requested == "free"` at the call site would put the special case in two
  handlers, which is the duplication M3 removed.

- [ ] **T089: Explainability and discovery.**

  One `INFO` `routing_decision` record per auto selection: selected model, total score,
  per-dimension contributions, the weights in force, every candidate with its score,
  and every exclusion with its reason. `core/logging.py`'s `_ALLOWED_EXTRA_FIELDS`
  gains exactly the fields that record needs — ids, numbers and reason codes.

  **The allowlist is the privacy boundary, not a formality.** Adding `"candidates"`
  widens what a caller *can* log; the record must therefore only ever put ids, scores
  and reason codes in it, and a test asserts the emitted payload carries no message
  content.

  `GET /v1/models` lists `free` when routing is configured (R8), with `available`
  reporting configuration readiness rather than capacity, because the endpoint carries
  no request shape to size.

- [ ] **T090: Ship the policy and the priors.**

  `config.yaml` gains a `routing` block and a `routing` block per model. Priors are
  chosen deliberately and recorded with the reasoning, because they are opinions and
  the file is where an operator will disagree with them.

  **The shipped priors are not benchmark results and the comment must say so.** A
  future reader who assumes `quality: 70` was measured will not know to revisit it.

**Verification & Stop:**

1. `ruff check .`, `ruff format --check .`, `mypy --strict src/`
2. Scenarios 1–9 and 16 of `spec.md` have tests named after them.
3. Every exclusion path asserts **zero outbound requests** via `respx`.
4. The unary and streaming routes select the same model for the same config and request.
5. `routing_decision` is emitted once per auto request and never for an explicit one.
6. The full M1–M3 suite passes unchanged apart from the shipped `config.yaml` gaining a
   `routing` block — explicit selection must be untouched by construction.
7. `make check` and `make test-hermetic` green.
8. **STOP**, review, then update the batch issue.

---

## Batch 6: Exit-Criteria Verification and Milestone Report

- [ ] **T091: Prove the exit criterion, run OpenHands, and report the gap.**

  Verify against a running router, not only in tests:

  1. `model = "free"` selects the highest-scoring eligible model, and the response
     `model` names it.
  2. The `routing_decision` record explains the choice: candidates, scores, exclusions.
  3. A tools request through each provider returns `tool_calls` and survives the second
     turn.
  4. A streamed tool call delivers `delta.tool_calls` and terminates with
     `finish_reason: "tool_calls"`.
  5. The top-ranked model is used repeatedly while its capacity remains, then the
     next-ranked takes over.
  6. With routing absent, `free` is `unsupported_model` and explicit selection is
     unchanged.

  **Plus the objective this milestone carries from M2: an agentic OpenHands run
  completes through the router** — tool calls forwarded, results returned, a multi-turn
  loop that terminates on its own terms (ROADMAP M4, *OpenHands objective*).

  Every row is recorded as **observed** or **injected**, with its conditions, per
  T061/T074. The OpenHands row is the one most likely to be unavailable in a given
  environment, and if it is, it is recorded as **not run** with the reason — not
  omitted and not replaced by a synthetic multi-turn test.

  **The report must keep three claims separate:** the `free` exit criterion, the
  pass-through deliverables, and the OpenHands objective. M3's report exists because
  the first was briefly at risk of implying the third.

**Verification & Stop:**

1. `make check` green; `make test-hermetic` green with provider credentials unset.
2. Every numbered item above is recorded with its observed output and label.
3. `spec.md` § *Explicit Non-Goals* reviewed against what was built, with any
   discrepancy recorded rather than the spec adjusted.
4. **STOP** for review, then the milestone integration PR.

---

## Milestone Exit Criteria

ROADMAP Milestone 4: *"The router can choose a free model without the client having to
name the provider/model explicitly, while keeping the decision understandable from
structured logs."*

| Criterion | Status after Batch 0 |
| :--- | :--- |
| `model = "free"` selects without the client naming a model | Planned — Batch 5 |
| The decision is understandable from structured logs | Planned — Batch 5 (T089) |
| Configurable routing weights, validated at startup | Planned — Batch 5 (T086) |
| Quota availability as a hard constraint, not a score | Planned — Batch 5 (T088) |
| Concentration on the best eligible model, no distribution | Planned — Batch 5 (T087, R6) |
| Tool support strongly weighted; latency deliberately lowest | Planned — defaults 0.30 / 0.10 (R3) |
| Tool, vision and structured-output pass-through | Planned — Batches 2–4 |
| Capability declared only once servable and measured | Planned — T081, T083, T085 (R9) |

**Not claimed by this document:**

| Objective | Status |
| :--- | :--- |
| OpenHands usable as an actual workload (deferred from M2, then M3) | **Unverified.** Batch 6 runs it or records that it could not. Nothing in a planning document closes it. |

## Carried Into M5

1. **Observed reliability.** Success/failure rates, provider health scoring and the
   `reliability` weight that R3 refuses today. M5 begins with the separate design
   exercise the roadmap requires.
2. **Observed latency and quality.** Replacing the static priors with accumulated
   measurement, keeping the prior as the fallback when observation is thin.
3. **Quota-ratio awareness.** Proactive selection based on remaining capacity, if a
   design can do it without distributing traffic — the constraint R3 records.
4. **Per-task quality dimensions**, if a task signal ever exists that is not an LLM
   classifier (prohibited by ROADMAP §3.7 and outside this project's direction).
5. **Multi-hop fallback chains**, still deferred and still unrequested.
6. **Routing policy management** through the Admin API — M6, not earlier.
