# M3: Capability-Aware Router

**Feature ID:** `004-capability-aware-router`
**Milestone:** M3 — Capability-Aware Router (Issue #16, milestone #4)
**Planning Issue:** #37

## Purpose

Move the router from *"send this exact model the request the client sent"* to
*"refuse, or substitute, when the model cannot actually serve the request."*

M1 and M2 both treat the requested model id as the only thing that matters. The
router checks that the id is configured, that capacity is available, and then
dispatches. It never asks whether the model **can** accept what it is being sent.
Three consequences follow, and all three are live defects rather than theoretical
ones:

1. **An oversized request is sent to be rejected.** Nothing estimates the request
   against the model's context window before dispatch. The provider returns its own
   400, after the router has spent a request against a free-tier quota and told the
   client a provider-shaped error about a decision the router could have made
   locally.
2. **A feature request is silently stripped.** The ingress boundary honours only
   `model`, `messages`, `stream` and `stream_options.include_usage`
   (`core/normalization.py`). A request carrying `tools` is *silently repaired* into
   a request without them, and the client receives a plausible answer that ignored
   its instructions. CONSTITUTION §7.1 prohibits exactly this.
3. **The client cannot tell why.** Both failures above arrive as either a provider
   error or a success, never as "this router cannot serve this request with the
   configured models, and here is what would be needed."

M3 makes the router aware of what a request *requires* and what a model *can do*,
and makes it refuse anything it cannot serve rather than serving it wrongly.

## Scope

| Area | Roadmap | Issue |
| :--- | :--- | :--- |
| Capability metadata schema and registry expansion | §3.7 | #17 |
| Local request capability and size detection | §3.7 | #18 |
| Hard-requirement filtering and selection flow | §3.7 | #19 |
| Opt-in fallback model relationships | §3.6 | #19 |
| Milestone design (this document) | §3.6, §3.7 | #37 |

**In scope:** the capability schema, the local detection of hard requirements, the
filtering pipeline up to and including explicit-model eligibility and opt-in
substitution, and the error contract for requests no configured model can satisfy.

**Out of scope, and deliberately:** forwarding of the features the filter checks
for. M3 can *detect* that a request needs tools, vision or structured output, but
neither adapter forwards those fields today, so **no model is declared able to
serve them** and such requests are refused with a clear `400` (C5). This is the
honest reading of the milestone: awareness without capability would make the
filter a lie, and a filter that passes a tools request to an adapter that strips
tools is the silent degradation §7.1 forbids.

## Requirements

### 1. Capability Metadata

Every configured model **must** declare its capabilities, and every declared
capability **must** carry a provenance recording how it was established.

The M3 capability set, per ROADMAP §3.7:

```text
context_window          maximum tokens the model accepts for one request
supports_streaming      SSE streaming
supports_tools          function / tool calling
supports_vision         image or other multimodal input
supports_json_schema    structured output constrained by a schema
```

Each capability is an object rather than a bare value:

```yaml
capabilities:
  context_window:
    value: 1000000
    provenance: published
    source: "provider model documentation, retrieved <date>"
  supports_streaming:
    value: true
    provenance: measured
    source: "M2 streaming measurement, 20/20 requests streamed"
```

**Provenance is one of `published`, `measured`, `assumed`, and the distinction is
load bearing:**

- `published` — stated by the provider in documentation. The `source` names the
  document and the date it was read, because a page moves.
- `measured` — verified by this project against the live provider. The `source`
  names the measurement.
- `assumed` — **not verified, and therefore not usable.** An assumed capability is
  recorded for the operator's benefit and treated as *unknown* by the filter. It
  can never satisfy a hard requirement.

The third case is the one that makes "verified" checkable. Without it, a guessed
value and a checked value are indistinguishable in config, and the milestone's
claim — *the router never sends a request to a model it knows cannot serve it* —
would rest on guesses.

**The capability schema is closed, and unknown keys are refused.** Extensibility
means adding a field to the schema and a rule to the filter, not accepting
arbitrary keys: a misspelled capability silently ignored is the same class of
defect as a silently stripped `tools` field.

### 2. Context Window and Request Size

The router **must** estimate a request's size before dispatch and compare it
against the candidate model's `context_window` (ROADMAP §3.6, CONSTITUTION §7.1).

- The estimate reuses `quota.estimation.estimate_input_tokens` (`chars / 4`), which
  is the estimator already used for reservations and which slightly *over*-estimates
  against both measured providers. Reusing it keeps one estimator rather than
  introducing a second, subtly different one for the same question.
- The fit test is `input_estimate + reserved_output <= context_window`, where
  `reserved_output` is the client's `max_tokens` when supplied and
  `quota.output_token_assumption` (1025) otherwise. A request that leaves no room
  for a response does not fit, even if its prompt alone would.
- The estimate is **conservative on purpose**. It can refuse a request that would
  have fitted with a shorter reply. Under-estimating and letting the provider
  refuse is the alternative, and it spends quota to learn something the router
  could have known.
- `context_window` is the provider's stated limit for **one request**, and the
  provenance records which limit that is when a provider states input and total
  limits separately. Providers are not uniform here, and normalizing to a single
  number without saying so would hide the difference.

### 3. Local Request Capability Detection

The router **must** derive hard requirements from the incoming payload locally,
with **no LLM-based classifier and no outbound call** (ROADMAP §3.7, M3
deliverables).

Detection is a pure function over the decoded request body, evaluated **before**
normalization, because normalization is exactly the step that discards the fields
being inspected.

```text
requires_tools          payload.tools is present and non-empty,
                        or payload.tool_choice requests one
requires_vision         any message content part of type image_url / input_image
requires_json_schema    payload.response_format is json_object or json_schema
requires_streaming      payload.stream is true
input_tokens_estimate   estimate_input_tokens(messages), the reservation estimator
reserved_output         payload.max_tokens or max_completion_tokens,
                        else quota.output_token_assumption
```

**Reading a field is not honouring it.** The ingress boundary continues to honour
only `model`, `messages`, `stream` and `stream_options.include_usage`. Detection
reads `tools`, `response_format` and message content parts without forwarding
them, which is safe **only because** a request requiring a feature the router
cannot serve is refused (§6) rather than forwarded with the feature removed. The
pairing is the contract: *detect and refuse* is permitted; *detect and strip* is
prohibited by §7.1.

`json_object` and `json_schema` both map to `supports_json_schema`. That is a
deliberate coarsening — `json_object` is more widely supported than a strict
schema — and it is conservative in the safe direction: it can refuse a request
that would have worked, never accept one that would not.

### 4. Hard-Requirement Filtering

Selection **must** follow CONSTITUTION §7.1's order, and no step may be reordered
or skipped:

```text
Request
  ↓
1. Capability filtering   (tools, vision, json schema, streaming, context fit)
  ↓
2. Quota & cooldown       (exclude models with no usable free capacity)
  ↓
3. Selection              (explicit model, or opt-in substitution -- §5)
  ↓
4. Ranking                (M4; M3 declares a single candidate)
```

M3 implements steps 1–3 and stops. Ranking is out of scope because M4 owns quality
scoring, and selecting "the best" model with no scoring would be step 4 in name
only.

The filter is a pure function of `(config, requirements)` returning the eligible
models in configuration order. Purity matters for more than testability: the same
function must answer both "is the requested model eligible?" and "is any fallback
eligible?", so the two answers cannot diverge.

**Eligibility is per model, not per provider**, and a model with any unsatisfied
hard requirement is removed regardless of how much quota it has. A model with
plenty of capacity that cannot accept the request is not a candidate at any
score.

### 5. Model Substitution (opt-in)

CONSTITUTION §7.2 honours the exact requested model and prohibits cross-model
fallback for an explicit request. ROADMAP §3.6 contemplates routing an oversized
request to a larger-context model "when routing policy allows model substitution".
Those are reconcilable only if substitution is a policy the operator **declares**,
not a behaviour the router assumes.

**Decision (C1): substitution is opt-in per model and off by default.**

```yaml
- id: "google/gemini-3.5-flash-lite"
  fallback_models:            # absent or empty = no substitution, ever
    - "google/gemini-3.5-pro"
```

Rules, all enforced:

1. **Absent or empty `fallback_models` means the requested model is the only
   candidate.** If it cannot serve the request, the request is refused (§6). This
   is the default for every model, including the shipped configuration.
2. **Substitution is triggered by a capability or context mismatch only.** A rate
   limit, cooldown or exhausted quota on the requested model is **not** a
   substitution trigger: §7.2 confines failover on a rate limit to other
   credentials for the *identical* model, and that behaviour is unchanged.
3. **A fallback must itself be eligible.** Every fallback is run through the same
   filter as the requested model; the first eligible one in declared order wins.
   M3 does not rank fallbacks (that is step 4, M4).
4. **Substitution is single-hop.** A fallback's own `fallback_models` are not
   followed. Chains make the actual model hard to predict from configuration and
   hard to explain in a log; one hop is enough for the large-context case M3 is
   solving.
5. **The response says which model served it.** `model` in the response body is
   the model actually used, never the requested one. A substitution the client
   cannot see is a silent substitution, which §7.1 prohibits regardless of intent.
6. **Every substitution is logged** at `INFO` with the requested model, the
   satisfied-by model, and the requirement that forced it.

The last rule is the one that costs something: a client that pins a model id and
reads `model` back will see a different value. That is the honest outcome, and it
is why substitution is opt-in rather than default.

### 6. Failure Behaviour

When no eligible model exists, the request **must** be refused with `400 Bad
Request` (CONSTITUTION §7.1, "strict refusal over silent degradation") and an
OpenAI-shaped error naming what failed.

The error must distinguish, at minimum:

| Situation | `code` | `param` |
| :--- | :--- | :--- |
| A required capability no model supports | `capability_unsatisfiable` | `tools` / `response_format` |
| The context needed exceeds every eligible model | `context_window_exceeded` | `messages` |
| The requested model streams, but cannot | `streaming_unsupported` | `stream` |
| The requested model is not configured | `unsupported_model` | `model` |

`streaming_unsupported` is retained rather than folded into
`capability_unsatisfiable` (C4): it is an existing M2 contract that a client can
already match on, and silently changing the code for a streaming request would
break callers to fix a taxonomy.

The error message names the numbers and the models considered, so a failure is
actionable without reading the router's source:

```text
No configured model can accept this request: it requires about 132,400 input
tokens plus 1,025 reserved output tokens, and the largest eligible context
window is 131,072. Considered: google/gemini-3.5-flash-lite.
```

**What must never happen instead:** truncating the messages, dropping the tools,
weakening `response_format`, substituting without an opt-in, or returning a
provider's error for a decision the router made.

### 7. Configuration Validation

Capability configuration is validated at **startup**, not at first use. A router
that starts with unusable capability data has deferred the failure to a request,
where it looks like a routing bug rather than a configuration one.

Validation must refuse:

- a model with **no** `capabilities` block — under fail-closed semantics it could
  never serve anything, so accepting it would only defer the discovery;
- a capability with a missing or invalid `provenance`, or a `published`/`measured`
  capability with no `source`;
- an unknown capability key;
- a `context_window` that is not a positive integer;
- a `fallback_models` entry that is unknown, self-referential, or a duplicate;
- a fallback chain of length greater than one hop (a fallback that itself declares
  fallbacks is refused, not silently truncated to one hop).

## Behavioral Rules

1. A request is **never** dispatched to a model whose capability metadata is
   missing, assumed, or contradicts a hard requirement (§7.1, milestone exit
   criterion).
2. Silent degradation is prohibited in every form: no truncation, no dropped
   tools, no weakened `response_format`, no unrequested substitution.
3. No LLM-based request classification and no paid routing path is introduced
   (§7.3, ROADMAP M3 deliverables).
4. Cross-model fallback on a **rate limit or cooldown** remains prohibited for an
   explicit model (§7.2). Substitution is triggered by capability mismatch only.
5. The filter is a pure function of configuration and detected requirements; it
   performs no I/O and consults no provider.
6. Capability detection runs on the raw payload, before normalization strips the
   fields it inspects.
7. Detection never *adds* a field to the dispatched request. M3 reads more than it
   forwards, and refuses when that gap matters.
8. Configuration that cannot serve any request is a startup failure, not a runtime
   surprise.
9. A substitution, when enabled, is visible in the response `model` field and in
   the log.
10. Explicit-model requests keep M1/M2's dispatch behaviour in every other
    respect: key eligibility, retry budget, streaming fail-fast and quota
    reservation are untouched by M3.

## Acceptance Scenarios

### Scenario 1: An oversized request is refused locally

```text
Given google/gemini-3.5-flash-lite declares context_window 131072
And a request's estimated input is 140,000 tokens
When the client posts to /v1/chat/completions for that model
Then the response is 400 with code context_window_exceeded
And the error names the estimate and the window
And no outbound request is made
```

### Scenario 2: A request that just fits is dispatched

```text
Given a model declaring context_window 131072
And a request whose input estimate plus reserved output is 130,000 tokens
When the client posts to /v1/chat/completions
Then the request is dispatched normally
```

### Scenario 3: A tools request is refused, not stripped

```text
Given no configured model declares supports_tools with usable provenance
When a request includes a non-empty "tools" array
Then the response is 400 with code capability_unsatisfiable
And param is "tools"
And no outbound request is made
And the dispatched request was never constructed with tools removed
```

### Scenario 4: An image request is refused

```text
Given a message whose content is a list containing an image_url part
And no configured model declares supports_vision with usable provenance
When the request is submitted
Then the response is 400 with code capability_unsatisfiable and param "messages"
And no outbound request is made
```

### Scenario 5: An assumed capability does not satisfy a requirement

```text
Given a model whose supports_tools is declared with provenance "assumed"
When a request includes tools
Then that model is not eligible
And the request is refused rather than dispatched
```

### Scenario 6: Substitution is off by default

```text
Given the requested model cannot accept the request
And it declares no fallback_models
When the client submits the request
Then the response is 400
And no other configured model is dispatched
```

### Scenario 7: An opt-in fallback serves the request

```text
Given the requested model's context_window is too small
And it declares fallback_models: ["google/gemini-3.5-pro"]
And that model declares a sufficient context_window with usable provenance
When the client submits the request
Then the request is dispatched to the fallback
And the response "model" is the fallback's id, not the requested id
And an INFO log records the substitution and the requirement that forced it
```

### Scenario 8: A rate limit does not trigger substitution

```text
Given the requested model declares a fallback_models entry
And the requested model is in cooldown after a 429
When the client submits a request for it
Then the response is the existing rate-limit outcome
And the fallback is not dispatched
```

### Scenario 9: A fallback that cannot serve the request is skipped

```text
Given the requested model declares two fallbacks in order
And the first fallback's context window is also too small
And the second fallback's is sufficient
When the request is submitted
Then the second fallback serves it
```

### Scenario 10: Invalid capability config prevents startup

```text
Given a model declares supports_vision without a provenance
When the router starts
Then startup fails with a message naming the model and the field
And the process does not begin serving requests
```

### Scenario 11: Unknown capability keys are refused

```text
Given a model declares supports_audio: true
When the router starts
Then the configuration is rejected and the unknown key is named
```

### Scenario 12: Streaming is unchanged for an eligible model

```text
Given the requested model declares supports_streaming with usable provenance
When the client requests a stream
Then M2's streaming behaviour is unchanged, including fail-fast after the first
  chunk and the absence of [DONE] on an interrupted stream
```

## Edge Cases

- **`context_window` exactly equal to the request.** Fits: the comparison is
  `<=`. An off-by-one that refuses the largest legal request is worse than one
  that forwards it and lets the provider decide, because the provider's own
  boundary is authoritative.
- **An empty `tools: []`.** Not a requirement. An empty list is what many OpenAI
  SDKs send when no tools are used, and treating it as "needs tools" would refuse
  ordinary chat.
- **`tool_choice: "none"` with a non-empty `tools`.** Still a tools requirement:
  forwarding is what matters, and M3 forwards nothing. Coarse but conservative.
- **A message with both text and image parts.** Requires vision.
- **A model declaring `supports_vision: true`.** Recorded, but unreachable in M3 —
  no adapter forwards image parts, so image requests are refused for every model
  regardless (C5).
- **A fallback that is itself the requested model.** Refused at startup as
  self-referential.
- **Two models declaring each other as fallbacks.** Each is a one-hop edge, which
  is legal; single-hop evaluation means the cycle can never be traversed.
- **An unconfigured capability block added by a future schema version.** Refused as
  an unknown key rather than ignored.

## Error Behavior

All refusals are `400 Bad Request` with the standard OpenAI error envelope, so an
existing client's error handling works unchanged.

```json
{
  "error": {
    "message": "No configured model can accept this request: ...",
    "type": "invalid_request_error",
    "code": "context_window_exceeded",
    "param": "messages"
  }
}
```

- `code` values are the four in §6. They are additional to the existing codes and
  change none of them.
- `param` names the request field the client must change if it can: `tools`,
  `response_format`, `messages`, `stream`.
- The message contains the numbers (estimate, reserved output, largest window) and
  the models considered. It does not contain provider error text, since no provider
  was contacted.
- A request that fails the capability filter is **not** counted against any
  provider's quota: nothing was dispatched. It is still logged, so a client
  repeatedly sending an unservable request shape is visible.

## Security / Privacy Considerations

- Detection reads the request body, which it already has. Nothing new is stored,
  and no payload text enters a log line: the log records the *requirement* and the
  model ids, never message content (§9.1).
- Capability metadata is not secret. It contains model limits and support flags,
  which are public provider facts.
- The capability filter runs before key selection, so a refusal cannot leak which
  credentials are configured or whether they are healthy.
- Refusing locally removes an outbound call for requests that could not succeed.
  That reduces provider contact for malformed traffic, which is the §6.1 direction
  of travel.

## Explicit Non-Goals

- **Tool, vision and structured-output pass-through.** Deferred; see C5. M3 refuses
  these requests instead of forwarding them.
- **LLM-based request classification.** Prohibited for this milestone (ROADMAP §3.7)
  and not a candidate for a later one without a milestone that says so.
- **Automatic paid routing.** Prohibited outright by CONSTITUTION §2.1.
- **Quality scoring and ranking.** M4. M3 stops at step 3 of the pipeline.
- **Message truncation, summarization or compression.** Prohibited for M3;
  substitution or refusal only (ROADMAP §3.6).
- **Provider-discovered capabilities.** M3 metadata is hand-maintained with
  provenance. Discovery is a possible later source, and the schema is shaped to
  accept it, but no startup dependency on a provider is introduced.
- **Multi-hop fallback chains, retry-aware substitution, and substitution on
  provider errors.** All deferred; §5 rules 2 and 4.
- **`model = "free"` virtual routing.** M4 (CONSTITUTION §7.3).

## Clarifications Recorded

Agreed 2026-11-03 with the operator, before implementation began.

| # | Decision |
| :--- | :--- |
| C1 | Substitution is **opt-in per model**, declared via `fallback_models`, **off by default** |
| C2 | Missing or unverifiable capability data is **disqualifying** (fail closed) |
| C3 | The schema lives on **`ModelConfig`**; no new `ModelRegistry` class |
| C4 | Verification is recorded **per capability** via `provenance` + `source` |
| C5 | M3 delivers capability **awareness**, not capability **forwarding**: tool, vision and structured-output requests are refused |
| C6 | `streaming_unsupported` is retained as its own code rather than folded into `capability_unsatisfiable` |

### C1 — substitution is opt-in, and the default is refusal

CONSTITUTION §7.2 says a client that names a model gets that model, and that
cross-model fallback is prohibited for explicit requests. ROADMAP §3.6 says
oversized requests may be routed to a larger-context model "when routing policy
allows model substitution". The ROADMAP does not say who decides.

**Decision:** the operator decides, per model, with `fallback_models`. Absent means
never.

Three alternatives were rejected:

- **Substitute whenever a capable model exists.** This is cross-model fallback
  without the client asking, which is what §7.2 exists to prevent. It also makes
  the router's behaviour depend on the whole model list, so adding a model silently
  changes routing for existing ones.
- **Never substitute; always `400`.** Consistent, and it is what M3 does by
  default — but it leaves ROADMAP §3.6's "large-context candidate selection"
  unimplemented, so the deliverable would be claimed by naming it in the roadmap
  and then not building it.
- **A global `allow_substitution` flag.** One switch for every model means a
  large-context fallback added for one model changes the risk profile of all of
  them. Per-model declaration keeps the blast radius equal to the declaration.

See ADR-0009 for the full reasoning and the consequences for the response shape.

### C2 — unknown is ineligible

The alternative — treat missing data as "assume supported" — keeps existing
configurations working unchanged, which is a real benefit and was rejected anyway.

§7.1's rule is *"if a request requires capabilities that no eligible model
supports, the router must strictly reject"*. With fail-open semantics the router
cannot distinguish "no model supports tools" from "no model has been asked", so it
sends the request and finds out. That converts a local decision into a spent
request, and on a free tier the spend is the scarce resource.

The cost is accepted: every model must declare its capabilities, and a model with
an incomplete block fails at startup. That is why validation is at startup (§7).

### C3 — the schema lives on `ModelConfig`

Issue #17 says "expose capability query methods on `ModelRegistry`", but no such
class exists: the registry is `config.models` (`ModelConfig`) plus the `/v1/models`
projection.

**Decision:** capabilities are a block on `ModelConfig`, and lookups are
`Config.model_by_id` plus the free functions in `core/capabilities.py`. The
model-versus-request comparison cannot be a method on `ModelConfig` without a
`config` ↔ `capabilities` import cycle, since `RequestRequirements` is defined in
the latter; the schema lives on the model, the comparison lives in one function.

A `ModelRegistry` wrapper was considered and rejected under CONSTITUTION §2.3: it
would be a class whose entire content is three lookups over a list that is already
in memory and already validated. The seam it would provide — for provider discovery
later — is speculative, and the filter is a pure function over `config.models`,
which is already the seam.

Issue #17's wording is therefore not implemented literally, and this is recorded
as a deviation from its task text rather than quietly redefined.

### C4 — provenance per capability

Capability values are objects with `value`, `provenance` and `source` rather than
bare scalars. The verbosity is the cost and it is accepted, for one reason: a model
can have a well-documented context window and an entirely unverified tool-support
claim, and a single provenance for the whole block would mark both as verified.

`assumed` exists so an operator can record a guess **without** it becoming a
guarantee. An assumed capability is treated as unknown by the filter, which means
the guess is documented and harmless rather than silently load bearing.

This mirrors `quota.provenance` (`measured` / `published`), so the two metadata
families in `config.yaml` read the same way.

### C5 — awareness without forwarding, and the deviation it records

`core/normalization.py` honours only `model`, `messages`, `stream` and
`stream_options.include_usage`. `tools`, `tool_choice` and `response_format` are
discarded at the boundary, and both adapters send `messages` exclusively. The
Mistral adapter even carries a comment noting that its `tool_calls` handling is
unreachable because "M0 strips tools at ingress".

So M3 *cannot* make a tools request succeed: it can only detect one and refuse it.
The alternative — declaring `supports_tools: true` on a model whose adapter strips
tools — would pass the filter and then silently degrade the request, which is the
defect §7.1 names first.

**Decision:** M3 detects tools, vision and structured output and refuses any
request requiring them, with an error that says the router cannot serve the
feature. No model is declared able to serve them, because none can.

**This is a deviation from the M2 record and it is recorded here rather than
smoothed over.** `specs/003-persistent-interactive-mvp/spec.md` § A1 deferred the
OpenHands objective to M3 "where capability-aware routing and **tool support**
converge". M3 delivers the first half. OpenHands remains unreachable, because an
agentic client acts by issuing tool calls and the router cannot forward them.

The consequence is stated plainly in `tasks.md` § *Milestone Exit Criteria*: M3's
exit criterion — *a request is never deliberately sent to a model the local
registry knows cannot satisfy its hard requirements* — **is met**, and the wider
OpenHands objective **is not**. Fixing that needs adapter-level pass-through
(Google `functionDeclarations`, Mistral `tools`, streaming `tool_calls` deltas and
the `tool_calls` finish reason), which is a milestone-sized piece of work and is
carried into M4.

### C6 — `streaming_unsupported` stays its own code

Streaming is a capability like any other, and the filter could report it as
`capability_unsatisfiable`. It does not, because M2 already returns
`streaming_unsupported` and a client may match on it.

Breaking a working error contract to make a taxonomy tidier is a cost with no
benefit to the client. The filter emits `streaming_unsupported` when streaming is
the unsatisfied requirement, and `capability_unsatisfiable` otherwise.
