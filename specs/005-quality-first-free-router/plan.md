# Implementation Plan: M4 Quality-First Free Router

**Feature ID:** `005-quality-first-free-router`
**Companion documents:** `spec.md` (behavioral contract), `tasks.md` (batch breakdown)
**ADRs:** [ADR-0010](../../docs/adr/0010-auto-routing-static-priors-and-concentration.md) ·
[ADR-0011](../../docs/adr/0011-capability-pass-through-and-declared-serving.md)

## Technical Approach

M4 has two halves that are built for each other but do not depend on each other's
internals:

```text
   adapters + boundary                     routing policy
   (capability becomes real)               (choice becomes explicit)
            │                                       │
            ▼                                       ▼
   a model may declare tools              model = "free" is scored
   because the adapter forwards them      because the priors exist
```

The order in `tasks.md` is pass-through first, routing second. Not because routing
needs it to compile — it does not — but because the `tool_support` weight is only
meaningful once a model can actually declare tool support truthfully. Building the
ranking against a registry where every model scores `tool_support: 0` would produce a
weight that looks implemented and does nothing, which is the defect class M3 spent a
milestone eliminating from configuration.

### Part 1: the boundary grows, and it grows once

M3's filter is only honest if a declared `true` is servable. So M4 extends the
internal models **once**, in the batch that first forwards a capability, and then
fills in provider translations batch by batch:

```text
NormalizedRequest    + tools, tool_choice, response_format
                     + message parts/content passthrough for vision
NormalizedResponse   + tool_calls
NormalizedChunk      + tool_call deltas
FinishReason         + "tool_calls"
```

`NormalizedRequest` stays `extra="forbid"`. The reason M0 chose that is still the
reason: a model that silently accepted unknown fields could not prove that
`temperature` was stripped. The difference is that `tools` is no longer unknown — it
is a supported field with a contract, so it belongs on the model rather than in the
discarded remainder.

`normalize_chat_completion_request` gains explicit optional fields rather than a
passthrough dict. A `dict[str, Any]` field would forward everything the client sent
and make `extra="forbid"` decorative; the point of the boundary is that each honoured
field is named.

**`content` becomes optional on both response models.** A tool call has no text, and
OpenAI returns `"content": null` alongside `tool_calls`. Keeping `content: str` and
emitting `""` would be a shape a client cannot distinguish from an empty answer.

### Part 2: the provider translations are not symmetrical, and the plan says so

Mistral is close to identity: `tools`, `tool_choice`, `response_format` and the
response `tool_calls` are OpenAI-shaped, and its finish-reason vocabulary already
includes `tool_calls` (the adapter's own comment notes the value is unreachable
"because M0 strips tools at ingress"). The work there is forwarding and testing, not
translation.

Google is the work:

| OpenAI concept | Gemini form | Note |
| :--- | :--- | :--- |
| `tools[].function` | `tools[].functionDeclarations[]` | name, description, JSON-schema parameters |
| `tool_choice` `auto`/`none`/`required` | `toolConfig.functionCallingConfig.mode` `AUTO`/`NONE`/`ANY` | a named function adds `allowedFunctionNames` |
| assistant `tool_calls` | `content.parts[].functionCall` | Gemini returns **no call id** |
| `role: "tool"` result | `functionResponse` part in the next turn | exact role confirmed against the API at implementation |
| `response_format` `json_schema` | `generationConfig.responseMimeType` + `responseSchema` | OpenAPI subset, not full JSON Schema |
| `response_format` `json_object` | `generationConfig.responseMimeType` | no schema |
| `image_url` (base64) | `parts[].inlineData` | mime type from the data URL |
| `image_url` (http) | not supported | refused locally (R11) |
| `finish_reason: "tool_calls"` | inferred from `functionCall` parts | Gemini says `STOP`; the presence of a call is the signal |

Two consequences are recorded rather than smoothed:

- **Call ids are synthesised.** Gemini has no id, and OpenAI clients echo the id back
  with the tool result. The adapter generates one per call, deterministic within the
  response, and the router must not promise that a replayed conversation keeps the
  same ids across turns — the id is correlation, not identity.
- **A streamed Gemini tool call may arrive whole.** Gemini can deliver a complete
  `functionCall` part in one event rather than accumulating `arguments` fragments the
  way OpenAI does. The renderer therefore must handle both "one delta carrying a whole
  call" and "many deltas carrying fragments"; it must not assume OpenAI's shape.

### Part 3: one selection entry point, and the module boundary that keeps it acyclic

M3's lesson was that duplicating policy between the unary and streaming routes creates
two copies to keep in step. M4 keeps one entry point:

```python
# core/routing.py            imports capabilities, config
def resolve_request(
    config: Config,
    requirements: RequestRequirements,
    requested_model: str,
    *,
    capacity_probe: CapacityProbe | None = None,
) -> Target: ...
```

- `requested_model != FREE` → delegates to M3's `resolve_target`, unchanged.
- `requested_model == FREE` and `config.routing is not None` → scores and selects.
- `requested_model == FREE` and no policy → `UnsupportedModelError`, exactly as any
  unknown id.

The entry point lives in `routing.py` rather than in `capabilities.py` because
`routing` depends on `capabilities` (it needs `mismatch` and `eligible_models`) and
`capabilities` must not depend on `routing`. Putting the branch in `capabilities.py`
would close that cycle; putting it in the routes would duplicate it across two
handlers.

`Target` grows one field so the route can tell the three outcomes apart:

```python
SelectionKind = Literal["explicit", "substituted", "auto"]


@dataclass(frozen=True, slots=True)
class Target:
    model: ModelConfig
    selection: SelectionKind = "explicit"
    substituted_from: str | None = None
    forcing_requirement: str | None = None
    score: ModelScore | None = None  # populated only for "auto"
```

Today the route infers "substituted" from `substituted_from is not None`. Auto
selection is not a substitution, and logging it as one would tell an operator the
client pinned a model it did not. An explicit discriminant is cheaper than a
`requested_model == FREE` special case at the call site.

### Part 4: scoring is a pure function, and the ranking is the only state

```python
# core/routing.py

class RoutingWeights(BaseModel):
    quality: float = Field(default=0.60, ge=0)
    tool_support: float = Field(default=0.30, ge=0)
    latency: float = Field(default=0.10, ge=0)
    # total > 0 enforced by a model validator

class ModelScore(BaseModel):
    total: float
    contributions: dict[str, float]   # weighted, already divided by the weight total
    components: dict[str, float]      # [0,1] inputs, for the log

def score_model(model: ModelConfig, policy: RoutingConfig) -> ModelScore | None: ...
def rank_candidates(...) -> tuple[RankedCandidate, ...]: ...
def select_model(...) -> Target: ...
```

`score_model` returns `None` when the model carries no priors — the R10 exclusion —
rather than inventing a default. `rank_candidates` filters to scoreable models, sorts
by `(-total, config_index)` so ties resolve to declaration order, and returns the
exclusions with reasons.

**No state is added anywhere.** There is no "current model" pointer, no counter, no
persisted selection. Stickiness is the fixed point of a deterministic ranking over a
candidate set that only loses members when a hard constraint fires (spec R6). This is
worth stating in the plan because the obvious implementation of "stay on the best
model" is a pointer, and a pointer is state that can disagree with configuration.

### Part 5: the capacity probe becomes a sweep

M3 probed capacity for **fallback candidates only**, because the requested model's
capacity failure had to keep its M2 outcome. Auto-routing is different: every
candidate must be probed, or the router would select a model the dispatcher then
refuses.

`Dispatcher._check_capacity` is documented as entirely in-memory (cooldown, breaker,
quota counters), so a sweep over the configured models costs dictionary lookups, not
I/O. The route passes it in exactly as M3's `_capacity_probe` does; no second copy of
the rules is introduced.

When every capable candidate is out of capacity, the first capacity error in
configuration order is raised — the M3 rule, reused so the M2 status code survives.

### Part 6: the estimator becomes load-bearing

M3 recorded the estimator's bias and deliberately did not fix it, because changing the
shared reservation estimator would move M2's measured quota behaviour inside a
capability milestone. M4 cannot defer it any longer: tool definitions, tool results
and image parts are exactly the payloads whose size the current `chars / 4` estimate
ignores.

The extension is additive:

- per-message framing overhead (a small constant per message),
- `tools` / `tool_choice` / `response_format` serialised size,
- list-shaped `content` (text parts counted; image data counted at a configured
  tokens-per-image assumption, because base64 length is not a token count).

Reservation estimates rise. That direction is safe — it reserves more of a
per-minute token window, never less — but it is a change to numbers M2 measured, so it
is measured again in the same batch and reported rather than absorbed.

## Components Affected

| Component | Change |
| :--- | :--- |
| `core/config.py` | `RoutingConfig`, `RoutingWeights`, `ModelConfig.routing` (`RoutingPrior`), cross-field validation |
| `core/routing.py` | **New.** `FREE_MODEL_ID`, `resolve_request`, `score_model`, `rank_candidates`, `RoutingDecision` |
| `core/capabilities.py` | `Target` gains `selection`/`score`; `resolve_target` unchanged in behaviour |
| `core/normalization.py` | `NormalizedRequest` gains `tools`/`tool_choice`/`response_format` and part-shaped content; `NormalizedResponse.tool_calls`; `NormalizedChunk` tool deltas; `FinishReason` gains `tool_calls`; `to_openai_completion` emits `tool_calls` and `null` content |
| `api/streaming.py` | `StreamRenderer` emits `delta.tool_calls` and delivers a chunk whose only content is a call |
| `api/routes.py` | `resolve_request` replaces `resolve_target`; branches on `Target.selection`; logs `routing_decision` |
| `api/models_route.py` | Adds the `free` virtual entry when routing is configured |
| `core/logging.py` | Extends `_ALLOWED_EXTRA_FIELDS` with the routing record's content-free fields |
| `quota/estimation.py` | Message framing, tool payloads, part-shaped content |
| `providers/google/adapter.py` | Tools, structured output, vision; call-id synthesis; `tool_calls` finish reason; streaming call deltas |
| `providers/mistral/adapter.py` | Tools, structured output, vision; remove the "unreachable" note and make it reachable |
| `core/dispatch.py` | **Unchanged** |
| `quota/tracker.py`, `cooldown.py`, `breaker.py` | **Unchanged** |
| `config.yaml` | `routing` policy, per-model priors, capability values updated from `assumed` to `measured` as each pass-through batch lands |
| `database / migrations` | **None** |

## Interfaces

```python
# core/routing.py

FREE_MODEL_ID = "free"
"""The router-controlled model id. Exact match; model ids are case-sensitive."""


class RoutingWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # The block itself is required; an omitted dimension takes the default below.
    quality: float = Field(default=0.60, ge=0)
    tool_support: float = Field(default=0.30, ge=0)
    latency: float = Field(default=0.10, ge=0)
    # validator: quality + tool_support + latency > 0


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    weights: RoutingWeights  # required: a policy must be stated
    latency_ceiling_ms: int = Field(default=5000, gt=0)


class RoutingPrior(BaseModel):
    """Operator-declared priors. Not measurements; see spec R2."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    quality: int = Field(ge=0, le=100)
    latency_ms: int = Field(gt=0)


class RankedCandidate(BaseModel):
    model: str
    score: float
    components: dict[str, float]


class Exclusion(BaseModel):
    model: str
    reason: str  # "tools", "context_window", "cooling", "no_quota", ...
    detail: str | None = None


class RoutingDecision(BaseModel):
    selected: str
    score: float
    contributions: dict[str, float]
    candidates: tuple[RankedCandidate, ...]
    excluded: tuple[Exclusion, ...]
    weights: RoutingWeights


def score_model(model: ModelConfig, policy: RoutingConfig) -> ModelScore | None: ...


def select_model(
    config: Config,
    requirements: RequestRequirements,
    *,
    capacity_probe: CapacityProbe | None = None,
) -> tuple[Target, RoutingDecision]: ...


def resolve_request(
    config: Config,
    requirements: RequestRequirements,
    requested_model: str,
    *,
    capacity_probe: CapacityProbe | None = None,
) -> tuple[Target, RoutingDecision | None]: ...
```

```python
# core/normalization.py (additions)


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: dict[str, str]  # {"name": ..., "arguments": <JSON string>}


class ToolCallDelta(BaseModel):
    index: int
    id: str | None = None
    function: dict[str, str] | None = None  # partial name / partial arguments


FinishReason = Literal["stop", "length", "content_filter", "tool_calls"]
```

`arguments` is a **string**, not an object, because that is the OpenAI wire format and
clients parse it. An adapter that decoded it into a mapping and re-serialised it would
change the bytes a client receives.

## Data / Persistence Changes

**None.** Priors are configuration; scores are computed per request; the routing
decision exists for the duration of one log line. `_check_capacity` is already
in-memory, so the sweep adds no writes.

The alternative — persisting the last selected model to preserve stickiness — is the
state R6 rejects: it can disagree with configuration, it survives a deployment the
config did not, and it is unnecessary given a deterministic ranking.

## External Integrations

**No new providers and no discovery call.** Two existing adapters learn fields they
already receive a request for.

Two integration details are confirmed against provider documentation during their
batch rather than asserted here, because provider APIs move:

- Gemini's `functionResponse` turn shape (role and part placement) for tool results.
- Gemini's `responseSchema` subset (OpenAPI 3.0 schema object, not full JSON Schema),
  which decides which `json_schema` requests can be forwarded faithfully.

If either turns out to be unusable, the finding is recorded and the capability stays
`assumed` — the batch does not ship a declaration its measurement contradicts.

## Error Handling

No new error codes. The routing failures reuse M3's contract:

| Situation | Code | Raised by |
| :--- | :--- | :--- |
| `free` with no policy | `unsupported_model` | `resolve_request` |
| No candidate satisfies a capability | `capability_unsatisfiable` | unchanged, from `mismatch` |
| No candidate fits the size | `context_window_exceeded` | unchanged |
| Candidates exist, none has capacity | existing capacity errors | the first probe error, config order |

Pass-through adds **no** new local refusals except R11's remote-image case. A provider
rejecting a `json_schema` or a tool payload returns its own classified error; the
router must not pre-empt it with a local guess, because that would turn a supported
capability into an unsupported one.

The routing record is emitted before dispatch, so a selection followed by a provider
failure is still explained.

## Testing Strategy

### The estimator (Batch 1)

- Table-driven: a message list's estimate rises with message count (framing), with a
  tools block, and with part-shaped content.
- An image part is counted by the configured assumption, not by base64 length.
- Existing reservation tests still pass, updated where the estimate intentionally
  changed — and the change is recorded numerically in `tasks.md`, not just asserted.

### Pass-through (Batches 2–4)

- **Round-trip, not mapping.** A unary tool call through each adapter produces an
  OpenAI-shaped `tool_calls` response with `content: null` and
  `finish_reason: "tool_calls"`.
- **Second turn.** A conversation containing a `role: "tool"` message is translated
  into the provider's tool-result form. This is the OpenHands-blocking case and gets
  its own test per adapter.
- **Streaming.** `delta.tool_calls` reaches the client, the terminal chunk says
  `tool_calls`, and a chunk with no text still renders. Gemini's whole-call-in-one-
  delta shape is covered explicitly.
- **Both providers, both paths.** A capability verified unary only is not declared.
- **No capability regression.** With a model still declaring `assumed`, the M3 refusal
  still fires — pass-through must not have made the filter permissive by accident.

### Scoring (Batch 5)

- Pure-function tests, no HTTP: a candidate table with expected ranking.
- Ties resolve to declaration order.
- A model with no priors is excluded with `not_configured_for_routing` and remains
  explicitly selectable.
- `tool_support` has no effect on a tools request (all survivors already score 1) and
  a measurable effect on a tools-free request.
- Weights that sum to zero, unknown weight keys, and `quota`/`reliability` are refused
  at startup with the key named.
- Latency above the ceiling scores 0 and does not exclude.

### Selection and wiring (Batch 5)

- Scenario-by-scenario tests from `spec.md` § *Acceptance Scenarios*, named after the
  scenario, following M3's convention.
- Every refusal asserts **zero outbound requests** via `respx`.
- The streaming route gets the same selection as the unary route — asserted by driving
  one request each through the same config and comparing the selected model.
- `routing_decision` is emitted once per auto request and not at all for an explicit
  one; its payload contains no message content.

### Parity with M1–M3

Explicit-model requests must be untouched. The strongest check is that the existing
suite passes unchanged with a `routing` block added to `config.yaml`; every existing
route test is a regression test for auto-routing leaking into explicit selection.

## Risks / Tradeoffs

### Quality priors are unfalsifiable

A prior is an opinion. It can be wrong, and it will be wrong the moment a provider
retires or updates a model. M4 accepts this because M5 exists to replace it, and
because the alternative — no number at all — leaves the milestone's exit criterion
unmet. The guard is naming: `routing.quality` sits in a block the operator writes, and
the spec says "prior" wherever it could be mistaken for a measurement.

### The ranking is only as good as the priors

A deterministic global ranking concentrates every request on whichever model the
operator scored highest. If that prior is wrong, M4 makes the problem *worse* than
even distribution — the whole workload lands on the wrong model until quota or a
capability mismatch moves it. That is the price of §2.4's principle, and it is the
reason R6's stickiness must be visible in the log rather than inferred.

### Pass-through widens the privacy surface

Tools, tool results and images are payload. The router already refuses to log payload
at standard levels, and the log allowlist in `core/logging.py` is what enforces it
structurally. The new `routing_decision` fields must be added to that allowlist
deliberately — ids, scores, reason codes — and nothing else.

### Google is the larger and less certain half

Gemini's tool model differs from OpenAI's in three ways that each need a decision (no
call ids, whole-part streaming, `functionResponse` turns). Any one of them can turn
out to be unusable in a batch, and the honest outcome is then an `assumed` capability
and a recorded finding, not a declaration.

### Vision has no consumer yet

OpenHands needs tools, not images. Vision is a ROADMAP deliverable, so it is planned,
but if it must slip it slips **explicitly** — recorded in the milestone report as
undelivered — rather than being dropped from the plan and quietly forgotten. M3's
report is the precedent for why that matters.

### Tool payloads are the largest context risk in the project

An agentic turn can carry a whole file in a tool result. `chars / 4` under-estimates
structured payloads, so the estimator work is a prerequisite for tools, not a
nice-to-have: without it the router can pass a request that the provider then rejects
on context, having spent a call to learn it. The estimator change is measured before
pass-through ships.

### Streaming tool calls are a second streaming contract

M2's fail-fast rule is unchanged, but the chunk stream now carries non-text deltas.
A renderer that emits only on `delta` text would silently drop an entire function
call while looking healthy. This is covered by a dedicated test rather than left to
review.

## Architectural Decisions

- **ADR-0010** — auto-routing uses static operator priors, hard constraints before
  scoring, a three-weight set, and concentration as the fixed point of a deterministic
  ranking.
- **ADR-0011** — capability pass-through lives in adapters, declarations follow
  verification, and call ids are synthesised where the provider has none.

Decisions R1 (id and switch), R7 (response identity), R8 (`/v1/models`), R10 (priors
optional) and R11 (remote images) are recorded in `spec.md` rather than as ADRs: they
are contract placements with one defensible answer, not architecture with alternatives
worth re-litigating.

## Sequencing

| Batch | Delivers | Changes request handling? |
| :--- | :--- | :--- |
| 0 | These documents and ADR-0010/0011 | no |
| 1 | Estimator fidelity | no (estimates only) |
| 2 | Tool pass-through, both adapters, unary and streaming | yes — tools requests become servable |
| 3 | Structured-output pass-through | yes — `response_format` becomes servable |
| 4 | Vision pass-through | yes — image requests become servable |
| 5 | Routing policy, scoring, `free`, logs, `/v1/models` | yes — a new model id |
| 6 | Exit-criteria verification, OpenHands run, report | no |

Batch 1 precedes 2–4 because every one of them adds payload the estimator must size.
Batch 2 precedes 3 and 4 because it is the OpenHands unblocker and the largest
provider-translation risk; if the milestone has to lose something, it should lose the
smallest thing, and finding that out early is cheaper.

Batch 5 could be built before 2–4. It is sequenced after because `tool_support` is a
dead weight until a model can declare the capability truthfully (see *Technical
Approach*).

No batch touches `core/dispatch.py`, the tracker, the cooldown manager or the breaker.
Selection happens before dispatch, exactly as substitution does. If a batch needs to
touch the dispatcher, the design is wrong and that is a finding to record.
