# M4: Quality-First Free Router

**Feature ID:** `005-quality-first-free-router`
**Milestone:** M4 — Quality-First Free Router (Issue #20, milestone #5)
**Planning Issue:** #38

## Purpose

Let a client ask for *"the best free model that can serve this request"* instead of
naming one, and make the answer defensible from the logs.

M1–M3 built a proxy that honours an exact model, and a filter that refuses what that
model cannot serve. Both assume the client knows which model it wants. The intended
workload does not: OpenHands, like most agentic clients, has no basis for choosing
between `google/gemini-3.5-flash-lite` and a future Mistral entry, and pinning one
by hand is a configuration decision the router is better placed to make. M4 adds the
mechanism CONSTITUTION §7.3 already specifies — a router-controlled model — and the
quality scoring that makes "best" mean something.

Two gaps from M3 land here, and neither is optional for the milestone to mean what it
says:

1. **`model = "free"` does not exist.** A client must name a model, and the pipeline
   stops at step 3 (selection) with nothing to rank. M3 recorded this as carried work.
2. **Capabilities are declared, never served.** M3 detects tools, vision and
   structured output and refuses them, because neither adapter forwards them
   (`specs/004-capability-aware-router/spec.md` § C5). The OpenHands objective,
   deferred from M2 to M3 and from M3 to M4, is unmet for exactly this reason.

M4 closes both: the adapters learn to forward what the filter already understands, and
the router learns to choose. What it does **not** do is learn from outcomes — that is
M5 — so every quality number in M4 is an operator's prior, named as one.

## Scope

| Area | Roadmap | Batch |
| :--- | :--- | :--- |
| Estimator fidelity: message framing, tool payloads, part-shaped content | §3.5, §3.6 | 1 |
| Tool calling pass-through, unary and streaming, both adapters | §3.12, M4 deliverables | 2 |
| Structured-output pass-through, both adapters | §3.1, §3.7 | 3 |
| Vision pass-through, both adapters | §3.7, M4 deliverables | 4 |
| Routing policy: weights, priors, scoring, `model = "free"`, logs | §3.8, §3.9 | 5 |
| Milestone design (this document) | §3.8, §3.9 | 0 |

**In scope:** the router-controlled virtual model, the weight schema and its
validation, static quality and latency priors, deterministic scoring and
tie-breaking, the explainability record, and adapter pass-through for the three
capabilities M3 already detects.

**Out of scope, and deliberately:** anything that learns. Reliability and quota are
hard eligibility filters at M4, not score dimensions, because the signals a score
would need do not exist until M5 builds them. The detail is in § 3 and decision R3.

## Requirements

### 1. The Router-Controlled Model

A client **must** be able to send `"model": "free"` and receive a completion from
whichever configured model the router judges best for that request (CONSTITUTION
§7.3, ROADMAP §3.8).

- The id is exactly `free`, matched case-sensitively. Both governing documents name
  it, an OpenAI client accepts any string in `model`, and the value cannot collide
  with a real id because every real id is `provider/model`.
- `free` is served **only when routing policy is configured**. With no `routing`
  block the id is unknown, and the request fails with `unsupported_model` exactly as
  any other unknown model does. There is no separate enable flag; the presence of the
  policy is the switch, the same way `fallback_models` is the switch for substitution.
- The response `model` is the **effective** model, never `"free"`. A client that
  cannot see which model answered has received an opaque routing decision, and §7.1's
  prohibition on silent behaviour applies to router-controlled selection as much as to
  substitution.
- An explicit model id keeps M1–M3 behaviour **exactly**: §7.2, opt-in
  `fallback_models`, no ranking, no scoring, no change to failover. `free` is an
  addition to the selection step, not a rewrite of it.

### 2. Priors, Weights, and What Is Not a Weight

**Quality at M4 is a static prior, not a measurement.** No observation, benchmark or
classifier produces it: the operator assigns each model a score from 0 to 100, and the
schema names the field `quality` inside a block the operator writes, so the provenance
is structural. M5 introduces observed estimates; they will be added beside the prior,
not silently replace it.

The same reasoning applies to latency: M4 needs a number before anything has been
observed, so each model declares an operator-supplied `latency_ms`, scored against a
configured ceiling.

**The weight set is exactly three dimensions:**

```yaml
routing:
  weights:
    quality: 0.60        # answer quality, the dominant objective (§2.4)
    tool_support: 0.30   # capability fit for the project's primary workload
    latency: 0.10        # deliberately the smallest factor (§2.5)
  latency_ceiling_ms: 5000
```

- Weights are non-negative numbers whose total must be greater than zero. They are
  **relative**: the score is divided by their sum, so an operator may express them as
  fractions, percentages, or raw points. The shipped defaults sum to 1.00 but the
  router does not require that, because a schema that fails on a rounding difference
  is a schema that fails for no reason.
- An **unknown weight key is refused at startup** and named, exactly as an unknown
  capability is. Accepting `reliability: 0.15` and doing nothing with it would be the
  configuration equivalent of a stripped `tools` field.
- `quota` and `reliability` are **refused as weights with a message saying why** (R3).
  Both are hard constraints in M4: a model with no usable capacity is not a candidate
  at any score, and a model that is cooling or breaker-degraded is ineligible before
  scoring begins.

**`tool_support` is not double counting the hard filter, and the distinction matters.**
When a request requires tools, only tool-capable models survive step 1, so the weight
is constant across candidates and changes nothing. When a request does *not* require
tools, the weight still prefers tool-capable models — deliberately. An agentic
workload sends a mix, and spreading a conversation across a tool-capable model and a
non-tool-capable one forces a model switch the moment a tool call appears. Weighting
tool support for every request is how "prefer the model OpenHands can use" is
expressed without a session, an affinity table, or a classifier.

### 3. Hard Constraints Before Scoring

Selection **must** follow CONSTITUTION §7.1 and must not reorder the steps:

```text
model = "free"
   ↓
1. Capability filtering      (tools, vision, structured output, streaming, context fit)
   ↓
2. Quota & cooldown          (cooling, breaker-degraded, quota exhausted, persistence-degraded)
   ↓
3. Selection                 (router-controlled: every survivor is a candidate)
   ↓
4. Ranking                   (weighted score; highest wins; ties by config order)
```

Step 2 is **binary, not proportional**. A model either may be dispatched to right now
or may not; there is no "40% of quota left" term. That is not an omission — a quota
*ratio* in the score would spread traffic toward the model with the most headroom,
which is the even distribution CONSTITUTION §2.4 names as an anti-pattern and the
opposite of the stickiness §7.3 requires.

A model that fails step 1 or step 2 is **excluded, not scored low**. Exclusion is
recorded with its reason (§ 5) so the decision remains explainable.

### 4. Scoring

Each surviving candidate receives one score in `[0, 1]`:

```text
quality_component  = quality / 100
tool_component     = 1 if supports_tools is usable and true, else 0
latency_component  = max(0, 1 - latency_ms / latency_ceiling_ms)

score = (w_quality*quality_component
       + w_tool*tool_component
       + w_latency*latency_component) / (w_quality + w_tool + w_latency)
```

- Latency is normalised against a **configured absolute ceiling**, not against the
  candidate set. Candidate-relative normalisation makes a model's score depend on
  which other models happen to be eligible, so adding a model changes the ranking of
  models nobody touched. An absolute ceiling keeps a model's score constant.
- A model whose `latency_ms` exceeds the ceiling scores `0.0` on that dimension and
  remains eligible. Latency is a preference, not a constraint; only § 3 excludes.
- `tool_component` treats an `assumed` capability as `0`, consistent with M3's
  fail-closed reading: unknown is not capable.
- The highest score wins. **Ties are broken by configuration order**, so the same
  configuration and the same request always produce the same model. Determinism is
  what makes the log a record instead of a transcript of luck.

### 5. Concentration and Falling to the Next Best

The router must be **sticky**: it keeps using the top-ranked candidate while that
model remains eligible, and moves to the next-ranked only when step 1 or step 2
removes it (CONSTITUTION §7.3, ROADMAP §3.9 "selection behaviour").

Stickiness is **a consequence of the design, not a mechanism**:

- The ranking is a pure function of configuration and the request, so it does not
  drift between requests.
- Quota is a hard constraint, so a model is dropped only when it becomes unusable —
  which is exactly the trigger §7.3 describes.
- There is therefore **no affinity state, no session table, and no rotation counter**.
  A router that needed one would be distributing traffic, not concentrating it.

The router must **not** implement round-robin, weighted random, least-recently-used,
or "fair share" selection. Under a static ranking those mechanisms have no problem to
solve, and each of them would violate §2.4.

### 6. Explainability

Every router-controlled selection **must** emit one structured `INFO` record,
`event: "routing_decision"`, sufficient on its own to answer *why this model, and why
not the others*:

| Field | Content |
| :--- | :--- |
| `requested` | `"free"` |
| `selected` | the effective model id |
| `score` | the winning score, 3 decimal places |
| `contributions` | per-dimension weighted contribution for the winner |
| `weights` | the policy in force, so a log line is readable without the config file |
| `candidates` | every surviving model with its score, in rank order |
| `excluded` | each removed model with its reason code (`tools`, `context_window`, `cooling`, `no_quota`, `not_configured_for_routing`, …) |

- The record carries model ids, score numbers and reason codes only. No prompt or
  completion content, no message counts, no tokenised text (CONSTITUTION §9.1).
- It is emitted **before dispatch**, so a selection that then fails at the provider is
  still explained. The existing `provider_dispatched` and `request_completed` records
  continue to describe what happened next.
- Explicit-model requests do **not** emit `routing_decision`; they have no ranking and
  their existing records already name the model.
- `free` **appears in `GET /v1/models`** when routing is configured (R8). A model the
  endpoint does not list is a model clients will not discover, and discovery is the
  reason `/v1/models` exists. Its `available` field reports configuration readiness —
  at least one model carries routing priors — because a model list carries no request
  shape and therefore no size to test capacity against. Per-model capacity stays on the
  concrete entries, where it already lives.

### 7. Capability Pass-Through

M3's filter is only truthful if a declared capability is actually servable. M4 makes
three capabilities real in both adapters (ROADMAP M4 deliverables, decision R9):

**Tools.** The ingress boundary stops discarding `tools`, `tool_choice`, tool-result
messages and assistant messages carrying `tool_calls`; the internal models carry them;
and both adapters forward and translate them, unary and streaming.

- The response carries `tool_calls` in the OpenAI shape, with `finish_reason:
  "tool_calls"`, and `content` is `null` when the message is a tool call — which is
  what OpenAI does and what clients such as OpenHands expect.
- Streaming carries `delta.tool_calls` fragments and terminates with
  `finish_reason: "tool_calls"`. A chunk that carries only a tool-call delta is still
  delivered; today's renderer emits only on text, and a function call would vanish.
- **A multi-turn loop is the acceptance bar, not a single call.** The client's
  `role: "tool"` result messages must be translated into each provider's
  tool-result representation, or the second turn of an agentic exchange fails.
- Providers differ, and the difference is recorded rather than normalized away: Gemini
  returns `functionCall` parts without an id, so the adapter synthesises a stable id;
  Gemini may deliver a function call as a whole part rather than as argument
  fragments, so a streamed tool call can arrive in one delta.

**Structured output.** `response_format` of type `json_object` or `json_schema` is
forwarded and translated. Both providers constrain output through a generation
config, and a provider that rejects the constraint returns its own error, which the
router surfaces — no local fallback, no dropping of the constraint.

**Vision.** Content parts of type `image_url` are forwarded and translated. Base64
data URLs are supported; a plain `http(s)` image URL is **refused locally** with a
clear message when the provider cannot fetch it, rather than forwarded to fail
upstream (R11).

**What pass-through does not change.** A request requiring a capability no eligible
model declares still fails with M3's error, locally, before dispatch. Passing through
does not weaken the filter; it makes the filter's positive answers true.

### 8. Declaring a Capability

A model may declare `supports_tools`, `supports_vision` or `supports_json_schema` as
`true` **only once its adapter forwards that capability and the claim has been
verified** (M3 decision C5, decision R9).

- The batch that implements pass-through **measures** the capability live, then
  records `provenance: measured` with a `source` naming the measurement and its date.
- Until that measurement exists, the capability stays `assumed`, and under M3's rules
  an assumed capability is unknown and unusable. There is no "probably works" state.
- A capability that works unary but fails on a stream is not delivered. The streaming
  half is the half an agentic client uses.
- The shipped `config.yaml` updates in the same batch as the adapter, so the file never
  claims more than the code does in either direction.

### 9. Configuration Validation

All routing policy is validated at **startup**, consistent with M3 §7:

- a `routing` block with no `weights`, or weights whose total is zero or negative;
- an unknown weight key, or `quota` / `reliability`, each refused by name with the
  reason it is not a weight;
- `latency_ceiling_ms` not a positive integer;
- a model whose `routing` priors are negative, or whose `quality` exceeds 100;
- routing configured but **no model** carrying priors — a `free` request could then
  never be served, and discovering that at request time would look like a routing bug;
- `quality` and `latency_ms` are required **together**; a half-declared prior block is
  refused rather than defaulted, because a default latency would be a number nobody
  chose.

The `weights` block itself is **required**: the block's presence enables `free`, so a
policy nobody wrote is not a policy. An individual dimension *within* it may be omitted
and takes the documented default, so an operator adjusting one weight does not have to
transcribe the other two.

A model without a `routing` block is **not an error**: it stays explicitly selectable
and is excluded from `free` with a stated reason. Startup emits a `WARNING` naming
such models, so the exclusion is visible before it is discovered in a log.

## Behavioral Rules

1. `model = "free"` selects the highest-scoring **eligible** model; eligibility is
   capability fit and current capacity, and nothing else.
2. An explicit model request never enters ranking and keeps M1–M3 behaviour byte for
   byte, including §7.2 and opt-in substitution.
3. No traffic distribution: no round-robin, no random, no fairness term, no affinity
   state. The same configuration and request always select the same model.
4. Quality and latency at M4 are operator priors and are documented as such; nothing
   in M4 observes, learns, or infers them.
5. A weight that cannot be computed is refused at startup, not accepted and ignored.
6. Every router-controlled selection emits one `routing_decision` record naming the
   winner, its scores, the candidates, and each exclusion's reason.
7. The response `model` is the effective model.
8. A capability is declared servable only after its adapter forwards it and a
   measurement exists.
9. Silent degradation remains prohibited in every new path: no dropped `tools`, no
   weakened `response_format`, no unfetched image quietly sent, no truncation.
10. No paid capacity is reachable from any new path (CONSTITUTION §2.1).

## Acceptance Scenarios

### Scenario 1: The best-scoring eligible model is selected

```text
Given routing weights quality 0.6, tool_support 0.3, latency 0.1
And model A has quality 70, tools true, latency 900
And model B has quality 90, tools false, latency 1200
When the client sends a request for model "free" with no tools
Then model A is selected
And the response "model" is A's id
```

### Scenario 2: A required capability removes a model before scoring

```text
Given model B has the highest quality score
And model B does not declare supports_tools as usable
When the client sends a request for "free" carrying tools
Then model B is excluded with reason "tools"
And the highest-scoring tool-capable model serves the request
```

### Scenario 3: Capacity is a hard constraint, not a score

```text
Given the highest-scoring model is cooling after a 429
And a lower-scoring model is dispatchable
When the client sends a request for "free"
Then the lower-scoring model is selected
And exactly one outbound request is made
```

### Scenario 4: Nothing eligible fails without dispatch

```text
Given every configured model is cooling
When the client sends a request for "free"
Then the response is the M2 capacity error (429 or 503 as configured)
And no outbound request is made
And the message names the models considered
```

### Scenario 5: Selection is sticky while capacity remains

```text
Given model A scores highest and is dispatchable
When the client sends five successive requests for "free"
Then all five are served by model A
And no rotation occurs
```

### Scenario 6: Ties are broken by configuration order

```text
Given two models with identical quality, tool support and latency priors
When the client sends a request for "free"
Then the model declared first is selected
```

### Scenario 7: `free` is unknown without routing policy

```text
Given config.yaml has no routing block
When the client sends a request for "free"
Then the response is 400 with code unsupported_model and param "model"
And no outbound request is made
```

### Scenario 8: The decision is explainable from logs

```text
Given a request for "free" with two eligible and one ineligible model
When the request is dispatched
Then one routing_decision record names the selected model and its score
And it lists both candidates with their scores
And it lists the excluded model with its reason code
And it contains no message content
```

### Scenario 9: An explicit model is not ranked

```text
Given routing policy is configured
When the client requests google/gemini-3.5-flash-lite explicitly
Then that model is used regardless of any other model's score
And no routing_decision record is emitted
```

### Scenario 10: A tool call round-trips unary

```text
Given a model declares supports_tools as measured
When the client sends a tools request and the model returns a function call
Then the response carries "tool_calls" with a name and arguments
And finish_reason is "tool_calls"
And content is null
And the response "model" is the effective model
```

### Scenario 11: The agentic loop's second turn works

```text
Given the previous response returned a tool call
When the client sends the tool result back as a role "tool" message
Then the adapter translates it into the provider's tool-result form
And the model's follow-up answer is returned
```

### Scenario 12: A streamed tool call is delivered

```text
Given a streaming request that produces a function call
When the provider streams it
Then delta.tool_calls fragments reach the client
And the terminal chunk carries finish_reason "tool_calls"
And the stream ends with [DONE]
```

### Scenario 13: Structured output is forwarded, not emulated

```text
Given a model declares supports_json_schema as measured
When the client sends response_format json_schema
Then the provider receives its native structured-output configuration
And the response is returned unchanged in shape
```

### Scenario 14: Vision input is forwarded

```text
Given a model declares supports_vision as measured
When the client sends a message containing a base64 image part
Then the adapter translates it into the provider's image form
And the completion is returned
```

### Scenario 15: An unfetchable image is refused locally

```text
Given the provider cannot retrieve a remote image URL
When the client sends an http(s) image_url part
Then the response is 400 with a message naming the limitation
And no outbound request is made
```

### Scenario 16: `free` is discoverable

```text
Given routing policy is configured
When the client calls GET /v1/models
Then a "free" entry is listed alongside the concrete models
And with no routing policy it is absent
```

### Scenario 17: An agentic OpenHands run completes

```text
Given OpenHands is configured to use the router with model "free"
When it runs a task that requires at least one tool call
Then the tool call is forwarded, the result is returned, and the run terminates
And the routing_decision record explains which model served it
```

## Edge Cases

- **`free` requested with `model: "FREE"`.** Unknown, and refused as an unknown model.
  Model ids are case-sensitive; folding case would make two ids one.
- **A single configured model.** `free` works and always selects it. A one-model
  deployment is a valid configuration, not a degenerate one.
- **Every model has priors but none is tool-capable.** A tools request still fails with
  M3's `capability_unsatisfiable`; a tools-free request is served normally.
- **A model whose `latency_ms` exceeds the ceiling.** Scores zero on latency and stays
  eligible, because latency is a preference. It can still win on quality, and the log
  shows the zero so the operator can see why.
- **All weights zero.** Refused at startup: every candidate would score `0/0`.
- **A `routing` block with weights only.** The ceiling defaults to 5000 ms. The default
  is documented in `config.yaml`, not silently assumed.
- **Two models with identical priors.** Configuration order decides, and the log shows
  identical scores, so the tie is visible rather than mysterious.
- **A model removed from configuration while cooling.** It is simply absent; no state
  keyed by model id outlives the config, and `/v1/models` drops it.
- **`free` requested with `fallback_models` on the models.** Irrelevant: auto-routing
  candidates are every eligible model, not a declared list. `fallback_models` remains
  the explicit-request mechanism.
- **A tool call with no text content.** Valid, `content` null, `finish_reason`
  `tool_calls`. A client that assumes a string breaks; that is the OpenAI contract.
- **A tool result for an id the router never issued.** Forwarded as given; validating
  conversation history is the client's job and guessing would be worse.

## Error Behavior

No new error codes are introduced. Router-controlled selection reuses the existing
contract so a client's error handling keeps working:

| Situation | Status | `code` | `param` |
| :--- | :--- | :--- | :--- |
| `free` with no routing policy | 400 | `unsupported_model` | `model` |
| No model satisfies a capability | 400 | `capability_unsatisfiable` | `tools` / `response_format` / `messages` |
| No model fits the request size | 400 | `context_window_exceeded` | `messages` |
| Capable models exist but none has capacity | 429 / 503 | existing capacity codes | — |
| The client asked for streaming, no candidate streams | 400 | `streaming_unsupported` | `stream` |
| A remote image cannot be fetched by the provider | 400 | `capability_unsatisfiable` | `messages` |

What is new is the **message**, which must name the models considered and, for an
auto-routing failure, why each was excluded:

```text
No configured model can serve 'free' for this request. Considered: model-a (cooling),
model-b (does not support tools), model-c (no routing priors).
```

- A request refused before dispatch is still not charged to any provider's quota.
- A provider error from a capability that passed through is **the provider's error**,
  surfaced with its existing classification. The router does not translate a rejected
  `json_schema` into a local refusal after dispatch.

## Security / Privacy Considerations

- The `routing_decision` record carries ids, scores and reason codes. It never carries
  message content, tool names as sent by the client, or schema bodies
  (CONSTITUTION §9.1).
- Tool definitions, tool results and image data are client payload. They are forwarded
  to the provider that was selected and are never persisted or logged at standard
  levels. Debug logging stays opt-in and redacted as it is today.
- Scores and priors are not secret. They describe the operator's model preferences, not
  credentials or traffic.
- Auto-routing selects only from models whose credentials are already configured; it
  cannot reach a provider that was not declared, so the $0 boundary is unchanged.

## Explicit Non-Goals

- **Adaptive or learned routing.** No observed success rates, no dynamic quality
  estimates, no bandits, no training. M5, and M5 begins with a separate design exercise.
- **Per-task quality dimensions** (coding vs reasoning). The router has no task
  signal and is prohibited from classifying requests with an LLM (M3 non-goal,
  ROADMAP §3.7). A second number that cannot be selected is noise.
- **Quota-ratio scoring.** Explicitly an anti-pattern here; see § 3.
- **Multi-hop fallback chains** for explicit models. Still deferred.
- **Recording tool definitions, tool results or images in logs.** Prohibited.
- **Truncation or summarization** to fit a context window. Still prohibited.
- **A routing policy API or UI.** Configuration only; M6.
- **Automatic paid routing.** Prohibited outright by CONSTITUTION §2.1.
- **Fetching remote images on the client's behalf.** The router does not become an
  image proxy; unfetchable URLs are refused (R11).
- **`model = "free"` inside the OpenAI schema as a router-specific field.** The
  virtual model is a value of `model`, not a new parameter (ROADMAP §3.8).

## Clarifications Recorded

Agreed 2026-11-16 with the operator, before implementation began. These close the
three open questions #38 raised and the gaps the answers exposed.

| # | Decision |
| :--- | :--- |
| R1 | The virtual model id is exactly `free`; a `routing` block enables it, with no separate flag |
| R2 | Quality is a static operator prior (0–100), not a measurement; no task classifier |
| R3 | `quota` and `reliability` are hard filters, not weights; the weight set is quality, tool support, latency |
| R4 | `tool_support` is scored for every request as workload fit, not as a duplicate of the tools filter |
| R5 | Latency is a static prior scored against a configured absolute ceiling, at the lowest weight |
| R6 | Concentration follows from deterministic ranking plus binary quota eligibility; no affinity state |
| R7 | The response `model` is the effective model, never `free` |
| R8 | `free` is listed in `/v1/models` when routing is configured |
| R9 | A capability is declared `measured` only after pass-through exists and is verified live |
| R10 | A model without priors stays explicitly selectable and is excluded from `free` with a logged reason |
| R11 | Remote `http(s)` image URLs are refused locally; base64 image data is supported |

### R1 — the id is `free`, and the policy block is the switch

CONSTITUTION §7.3 and ROADMAP §3.8 both write `model = "free"`. A namespaced
alternative such as `router/free` would collide with the `provider/model` convention
that every other id follows and would imply a provider that does not exist.

The rejected alternative is a separate `routing.enabled` boolean. It is redundant —
a policy block that is present but disabled is a configuration state with no
behaviour, and the project's other opt-ins (`fallback_models`, capability blocks) are
already presence-based. Adding a flag would create a second way to say "off".

### R2 — quality is a prior, and saying so is the design work

The open question in #38 was *"how answer quality is scored at M4, given that M5 is
where dynamic estimates are introduced"*.

**Decision:** the operator assigns a 0–100 prior per model. Nothing observes anything.

Three alternatives were rejected:

- **A benchmark run inside the milestone.** It would spend free-tier quota to produce
  a number that is stale the moment a provider updates a model, and it would make the
  router's behaviour depend on a workload the operator did not choose.
- **Inferring quality from model size or name.** That is a guess wearing a formula,
  and it would be indistinguishable in configuration from a considered judgement.
- **Deferring `free` until M5 can measure quality.** It would leave the milestone's
  own exit criterion unmet, and quality is not the only input — capability fit and
  capacity already carry the decision in most cases.

The prior is placed in a per-model `routing` block rather than reused from
`CapabilityValue`, because `CapabilityValue`'s provenance vocabulary (`published`,
`measured`, `assumed`) has no honest member for "the operator thinks so". Reusing it
would either force `assumed` — which M3 defines as unusable — or invite a `published`
claim that no document supports.

### R3 — quota and reliability are constraints, and the roadmap's own priorities say so

ROADMAP §3.9 lists five illustrative weights, including `quota: 0.15` and
`reliability: 0.15`, and states in the same section that the values are illustrative
and not final. It also states, as priority 1, that free/quota availability is a **hard
constraint**. Both cannot hold as written.

**Decision:** the priority list wins over the illustrative block. The weight set is
the three dimensions that are computable at M4.

- **Quota as a weight would defeat concentration.** Scoring by remaining headroom
  steers traffic toward the emptiest model, which is even distribution by another
  name — §2.4's anti-pattern and the opposite of §7.3's stickiness.
- **Reliability has no signal at M4.** The only health state that exists is cooling,
  breaker degradation and persistence failure — all already eligibility states. A
  `reliability` weight would either duplicate them or invent a number. M5's "track
  observed success/failure rates" is the milestone that creates something to score.
- **Refusing them is kinder than accepting them.** An operator who copies the
  roadmap's block gets a startup error naming the key and the reason, rather than a
  weight that silently does nothing.

`tool_support` is retained despite also being a hard filter, for the reason in § 2:
the filter handles "tools are required", the weight handles "prefer a model that
could serve the next call".

### R4 — tool support is workload fit, not a second filter

Recorded separately from R3 because it is the subtlest of the three weights and the
one most likely to be "simplified" later by accident.

A tools request is already narrowed to tool-capable models; the weight cannot affect
it. The weight's entire effect is on requests that do **not** carry tools, where it
prefers a model that could have served one. Without it, a high-quality non-tool model
can win an ordinary request, and the next call — the one that needs a tool — is served
by a different model. For an agentic client that is worse than a slightly lower
quality answer, because it breaks the conversation's continuity for no reason the
client can see.

### R5 — an absolute latency ceiling, not a relative one

Normalising latency across the candidate set is the obvious implementation and it is
wrong here: it makes every model's score depend on the others, so introducing a fast
new model lowers the score of models nobody edited, and a request's decision changes
when an unrelated model is rate-limited out of the set.

The configured ceiling makes a model's latency score a property of that model. The
cost is that the operator must pick a ceiling; the default of 5000 ms is anchored to
M0's measured provider header spread (482–5364 ms), which is the only observed
latency data this project has.

### R6 — concentration without state

§7.3 requires stickiness. The straightforward implementation is a "current best
model" pointer persisted or held in memory, and it is rejected: it is state that can
disagree with configuration after a restart, and it is unnecessary.

With a deterministic global ranking and binary capacity eligibility, the top-ranked
eligible model *is* the same model on every request until a constraint removes it.
Stickiness is then an invariant of the ranking rather than a behaviour to maintain.

The alternative — weighted random selection — was rejected because it maximizes
distribution, which is the thing this project says it does not want.

### R7 — the response names the model that served it

Same rule as M3's substitution (decision C1, ADR-0009 §2), extended: a client that
asks for `free` receives a concrete id back. Returning `"free"` would leave the
effective model unknown to the only party that might want to know, and would make the
response inconsistent with what the logs say.

### R8 — `free` is discoverable

`/v1/models` exists (M2) so that tools can discover what this router serves. A virtual
model that is not listed is one a client cannot select without out-of-band knowledge,
which defeats the endpoint's purpose.

Its `available` value reports configuration readiness rather than capacity, because
the endpoint carries no request shape and therefore no size to test. Reporting
`true` while every model is cooling would be worse than reporting the narrower fact;
capacity for a concrete model is already on its own entry.

### R9 — measured, not declared

M3's C5 recorded that a model declaring `supports_tools: true` while no adapter
forwards tools would pass the filter and then silently degrade. The mirror rule
applies to the fix: a capability may be declared `true` only in the batch that makes
it servable, and only with a `measured` provenance naming the verification.

This is why every pass-through batch in `tasks.md` ends with a declaration step rather
than leaving it implicit. The metadata and the code move together, in one reviewable
change, in both directions.

### R10 — models without priors are excluded, not fatal

A newly added model has no prior until the operator forms one. Failing startup would
make adding a model a two-step operation with a broken intermediate state; defaulting
the prior would invent the very number R2 says must be a judgement.

**Decision:** absent priors mean "not a candidate for `free`", the model remains
explicitly selectable, the exclusion is named in the routing log, and startup warns.
The one hard requirement is that *some* model carries priors when routing is
configured; otherwise `free` could never be served.

### R11 — the router is not an image proxy

Gemini accepts inline base64 image data and provider-hosted file references, not an
arbitrary remote URL. Fetching a client-supplied URL server-side would make the router
an SSRF surface and an egress proxy — a security boundary this project has no reason
to cross.

**Decision:** base64 (`data:`) image parts are forwarded; a plain `http(s)` URL is
refused before dispatch with a message saying the provider cannot fetch it and the
client should inline the data. Mistral is checked at implementation time and gets the
same rule if its capability is equivalent; a provider that does fetch URLs does not
change the rule, because one consistent contract is worth more than provider-specific
leniency.
