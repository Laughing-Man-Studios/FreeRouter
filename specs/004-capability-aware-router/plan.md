# Implementation Plan: M3 Capability-Aware Router

**Feature ID:** `004-capability-aware-router`
**Companion documents:** `spec.md` (behavioral contract), `tasks.md` (batch breakdown)
**ADRs:** [ADR-0008](../../docs/adr/0008-capability-metadata-and-fail-closed.md) ·
[ADR-0009](../../docs/adr/0009-opt-in-model-substitution.md)

## Technical Approach

M3 adds a **decision layer in front of dispatch**. Nothing behind that layer
changes: the dispatcher, the adapters, the quota tracker, the breaker and the
streaming path keep their M2 behaviour byte for byte. That is deliberate — M2 was
measured at 19/20 unary and 20/20 streamed, and re-opening the dispatch path to add
a filter would put that measurement at risk for no gain.

The layer has three parts, each independently testable and each a pure function:

```text
payload ──▶ detect_requirements()  ──▶ RequestRequirements
                                              │
config  ──▶ eligible_models()      ◀──────────┘
                    │
                    ▼
             resolve_target()  ──▶ Target(model, substituted_from | mismatch)
```

### Capability metadata is configuration, and configuration already validates

`ModelConfig` is a `BaseModel` with `extra="forbid"`, validated at load by a
`YamlConfigSource` that already refuses unknown keys with a message naming them.
Capabilities slot into that existing machinery: a `CapabilityValue[T]` model and a
`Capabilities` block, so all the §7 validation rules in the spec are field
validators rather than a new validator pass.

```yaml
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
    capabilities:
      context_window:
        value: 1048576
        provenance: published
        source: "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite, input token limit, retrieved 2026-11-04"
      supports_streaming:
        value: true
        provenance: measured
        source: "T062 measurement, Google, 20/20 streamed, 2026-10-30"
      # The model has these; the router does not forward them, so they are
      # `assumed` -- recorded and unusable (spec §1, decision C5).
      supports_tools:
        value: false
        provenance: assumed
      supports_vision:
        value: false
        provenance: assumed
      supports_json_schema:
        value: false
        provenance: assumed
    fallback_models: []          # absent or empty = never substitute
```

`CapabilityValue` is a generic (`CapabilityValue[int]`, `CapabilityValue[bool]`) so
`context_window` stays typed as an integer for mypy strict, rather than every
capability value becoming `int | bool` and every comparison needing a cast.

Two validators carry the semantics:

- `source` is **required** when `provenance` is `published` or `measured`, and
  **refused** when it is `assumed` — a source on an assumption is a claim of
  verification that does not exist.
- `context_window` must be a positive integer. Zero is not a small context window,
  it is a missing one wearing a number.

### Detection is a pure function over the payload, ahead of normalization

The order matters and is the subtlest part of the design:

```text
routes.py
  1. decode JSON
  2. detect_requirements(payload)          <-- M3 reads tools/response_format here
  3. normalize_chat_completion_request()   <-- still honours only 4 fields
  4. resolve_target(config, requirements)
  5. dispatch
```

Normalization strips `tools`, `tool_choice` and `response_format`. Detection
therefore runs on the **raw payload**, one step earlier. This is the same technique
M2 used for `stream` and `stream_options.include_usage`: read a field where it is
still present, so the decision is made on real data rather than on a remainder.

The safety argument is *not* "detection is harmless because it does not forward".
It is narrower, and it is the whole point of the milestone:

> A field is read without being honoured **only** if a request requiring it is
> refused before dispatch.

That holds in M3 because no model declares usable support for tools, vision or
schema-constrained output, so those requirements always fail the filter. The moment
an adapter grows pass-through, the corresponding `supports_*` capability becomes
declarable and the refusal stops firing — the two must be implemented together, and
`tasks.md` records that they are deliberately not.

### One filter, used by both routes

The unary route and the streaming route are separate functions and M2 already
duplicated capacity gating between them, with a parity test to stop the copies
drifting. M3 does not add a second duplication:

`resolve_target(config, requirements, requested_model)` is called by both routes and
returns either a target or a mismatch. It performs steps 1–3 of the pipeline:

```python
@dataclass(frozen=True)
class Target:
    model: ModelConfig
    substituted_from: str | None       # None when the request was honoured exactly

def resolve_target(...) -> Target: ...
```

Eligibility for the requested model, eligibility for each fallback, the
capability-only trigger rule, and the single-hop limit all live inside that one
function, so the streaming path cannot implement a slightly different policy.

### Substitution without touching the dispatcher

The dispatcher takes exactly one `(provider, model)` pair and never varies it
(CONSTITUTION §7.2, M1 made it structural). Substitution therefore happens
**before** the dispatcher is called: `resolve_target` decides the model, and the
dispatcher is handed that one pair exactly as it is today.

This is why substitution is a routing decision rather than a failover mechanism.
Failover is behavioural — "this attempt failed, try another" — and varies the model
mid-request. Substitution is decided up front from configuration and the request's
shape, so the dispatcher's single-pair invariant is preserved and its retry logic
is untouched.

### Response identity: the model that served it

Both response paths currently echo the requested id, because there is only one
model. `to_openai_completion(..., request_model=...)` and
`StreamingRenderer(model=...)` take that id from `normalized.model`; M3 passes the
**effective** model id instead, and logs the requested id separately.

Nothing else in the response shape changes, so an OpenAI client sees an ordinary
completion. The `model` field disagreeing with the request is the honest signal,
and §5 rule 5 of the spec makes it the contract rather than a side effect.

## Components Affected

| Component | Change |
| :--- | :--- |
| `core/config.py` | `CapabilityValue[T]`, `Capabilities`, `ModelConfig.capabilities`, `ModelConfig.fallback_models`, `Config.model_by_id`, cross-model fallback validation |
| `core/capabilities.py` | **New.** `RequestRequirements`, `detect_requirements`, `eligibility`, `resolve_target`, `Target` |
| `core/exceptions.py` | `CapabilityUnsatisfiableError`, `ContextWindowExceededError` (both `ValidationError` subclasses → 400) |
| `api/routes.py` | Detect + resolve before normalization/dispatch; pass effective model id; log substitution |
| `api/streaming.py` | Render the effective model id |
| `api/models_route.py` | Unchanged in behaviour; capability block is not exposed in the OpenAI shape (see *Risks*) |
| `core/dispatch.py` | **Unchanged** |
| providers | **Unchanged** |
| quota | **Unchanged** |
| `config.yaml` | Capability blocks for the declared model; the commented Mistral entry gains one too |
| database / migrations | **None** |

## Interfaces

```python
# core/capabilities.py


class RequestRequirements(BaseModel):
    """What a request needs, derived locally from its payload. No I/O."""

    requires_tools: bool
    requires_vision: bool
    requires_json_schema: bool
    requires_streaming: bool
    input_tokens: int
    reserved_output: int


def detect_requirements(payload: dict[str, Any], assumed_output: int) -> RequestRequirements: ...


class Mismatch(BaseModel):
    """Why a model cannot serve a request. Maps 1:1 to an error in spec §6."""

    code: Literal["capability_unsatisfiable", "context_window_exceeded", "streaming_unsupported"]
    param: str
    detail: str


def mismatch(model: ModelConfig, requirements: RequestRequirements) -> Mismatch | None: ...


def eligible_models(
    config: Config,
    requirements: RequestRequirements,
) -> list[ModelConfig]: ...  # every model whose mismatch() is None, in config order


def resolve_target(
    config: Config,
    requirements: RequestRequirements,
    requested_model: str,
) -> Target: ...  # raises UnsupportedModelError / CapabilityUnsatisfiable* / ContextWindowExceeded
```

`mismatch` is the single place the capability rules live: provenance handling,
`assumed` meaning unknown, the `input + reserved_output <= context_window` test, and
the streaming flag. `resolve_target` composes it over the requested model and then
over the fallbacks, so a fallback is judged by exactly the same code.

The `ModelConfig` case belongs to `capabilities.py` rather than to a method on
`ModelConfig`: `capabilities` imports `config`, so a method reaching back into
`RequestRequirements` would close a cycle. The *schema* is still on `ModelConfig`
(decision C3); only the comparison is a free function.

## Data / Persistence Changes

**None.** Capabilities are static configuration, read at load and never written.
There is no new table, no migration, and no reason for one: nothing about the
capability model is learned at runtime.

The alternative — deriving capabilities from observed failures and persisting them
— is exactly the observation-based scoring CONSTITUTION §7.3 defers, and it would
turn a deterministic decision into a stale one.

## External Integrations

**None new.** M3 makes no provider discovery call and adds no outbound request. The
capability values are hand-maintained with provenance; `published` means a human
read the provider's documentation and recorded the date.

Provider discovery was considered and rejected for this milestone: it would make
startup depend on a provider being reachable, give two models of truth for the same
fact, and its data would still need provenance to be trustworthy (a discovery
response is a `published` claim with a fresher date). It is a candidate later, and
the schema is shaped so it can populate the same fields.

## Error Handling

Two new exceptions, both `ValidationError` subclasses so the existing handler in
`api/main.py` renders them as a `400` with the standard envelope and no new
plumbing:

| Exception | `code` | `param` |
| :--- | :--- | :--- |
| `CapabilityUnsatisfiableError` | `capability_unsatisfiable` | `tools` / `response_format` / `messages` |
| `ContextWindowExceededError` | `context_window_exceeded` | `messages` |

`StreamingUnsupportedError` and `UnsupportedModelError` are reused, not
re-implemented — a model that cannot stream still fails with the M2 code
(decision C6).

Errors are raised **before** key selection, so a refusal never touches a
credential, never increments a counter and never contacts a provider. Tests assert
that no HTTP request was made, using the same `respx` route-absence technique M1
used for the "no cross-model fallback" test.

## Testing Strategy

### The schema (Batch 1)

- Every capability field round-trips through YAML and is typed correctly.
- `source` is required for `published`/`measured`, refused for `assumed`.
- `context_window` rejects zero and negatives.
- An unknown capability key is refused with the key named.
- A missing `capabilities` block is refused, naming the model.
- `fallback_models` rejects unknown ids, self-reference, duplicates, and a
  fallback that declares its own fallbacks.

### Detection (Batch 2)

Table-driven over payloads, because this is where a parser is most likely to be
wrong in a way that reads as correct:

| Payload | Expected |
| :--- | :--- |
| no tools key | `requires_tools=False` |
| `tools: []` | `requires_tools=False` |
| one function tool | `requires_tools=True` |
| `tool_choice: "none"` + tools | `requires_tools=True` |
| string content | `requires_vision=False` |
| content parts with `image_url` | `requires_vision=True` |
| `response_format: {"type": "json_object"}` | `requires_json_schema=True` |
| `max_tokens: 4096` | `reserved_output=4096`, not the global assumption |
| `max_completion_tokens` present | same |
| neither | `reserved_output = output_token_assumption` |

Detection is tested against the payload **after** the route's JSON decode and
**before** normalization, with a test asserting a tools request still reaches
detection even though `normalize_chat_completion_request` would have dropped it —
that ordering is the design, so it is asserted rather than assumed.

### The filter (Batch 3)

- Context boundary: exactly at the window fits; one token over does not.
- `assumed` provenance is ineligible even when the value is `true`.
- Missing capability is ineligible (fail closed).
- Rate-limit/cooldown state does **not** trigger substitution, asserted by
  calling `resolve_target` with a cooling model and checking the error is the M2
  rate-limit outcome and that the fallback was never selected.
- Fallback ordering: first eligible wins, not first declared.
- Single hop: a fallback's own fallbacks are never considered.
- Response identity: a substituted unary response and a substituted streamed
  response both report the effective model id.
- No outbound call on any refusal (route-level tests with `respx` asserting zero
  requests rather than a mocked 200).

### Parity with M2

The M2 suite must pass unchanged except where behaviour is intentionally added.
Requests that declare nothing — no tools, no images, no schema, in-context — must
dispatch exactly as before. The strongest check is that the existing 602-test suite
still passes with the shipped `config.yaml` once it carries capability blocks;
every M2 test that posts an ordinary chat completion is a regression test for the
filter being too eager.

## Risks / Tradeoffs

### The estimator under-counts message structure

`estimate_input_tokens` counts characters in `content` **only when it is a
string**, and ignores per-message overhead (`role`, framing, roughly a few tokens
per message). For a request with thousands of short messages it under-estimates,
and an under-estimate is the unsafe direction for a context check.

Not fixed in M3, deliberately: the same function is the reservation estimator, and
changing it would move M2's measured quota behaviour inside a capability milestone.
The failure mode is bounded — an under-estimate means the router dispatches a
request the provider then rejects, which is today's behaviour, not a new one. The
follow-up (per-message overhead) belongs with the M4 estimation work.

Related and noted: for a list-shaped `content` (vision), the estimator counts zero
characters and returns 1. Harmless in M3 because those requests are refused, but it
would matter the moment vision is forwarded.

### Refusals are a behaviour change for existing clients

A client that has been sending `tools` and receiving quietly-degraded answers will
now receive a `400`. That is the point, and it is still a break: it converts
invisible wrongness into visible failure. It is called out in the milestone report
rather than shipped quietly.

### Every model now requires capability data

Fail-closed means a `config.yaml` without capability blocks does not start. The
shipped file gains them in the same batch, so a default deployment is unaffected,
but an operator with a hand-maintained config must fill the block in. Validation
names the model and field, so the fix is mechanical.

### Substitution can surprise a client that pins a model

Mitigated by making it opt-in and by reporting the effective model, but a client
that ignores the response `model` field and caches by request will see one
inconsistency. Accepted: the alternative is silent substitution.

### Per-capability provenance is verbose

Five objects where five scalars would do, in every model block. Accepted for the
auditability the milestone claims; see spec C4.

### The capability filter cannot make tools work

The milestone's centre of gravity is awareness, and awareness alone will not make
OpenHands work. This is recorded as a deviation from spec 003's A1 wording in
`spec.md` § C5, and it is the single largest gap carried out of M3.

## Architectural Decisions

- **ADR-0008** — capability metadata is hand-maintained, provenance-tagged, and
  missing data is disqualifying.
- **ADR-0009** — model substitution is opt-in per model, single-hop, capable-only,
  and always visible in the response.

Decision C3 (schema on `ModelConfig`, no `ModelRegistry` class) and C6 (retain
`streaming_unsupported`) are recorded in `spec.md` rather than as ADRs: they are
interface placements and code taxonomy, not architecture with alternatives worth
re-litigating.

## Sequencing

| Batch | Issue | Delivers |
| :--- | :--- | :--- |
| 0 | #37 | These documents and the two ADRs. No code. |
| 1 | #17 | Capability schema, config validation, `config.yaml` metadata for every declared model |
| 2 | #18 | Local requirement detection and the request-size estimate |
| 3 | #19 | Filter, substitution, error codes, route wiring |
| 4 | — | Exit-criteria verification and the milestone report |

Batch 1 must land before Batch 2, because detection has nowhere to put its result
until a model can express capability. Batch 3 depends on both, and is the only
batch that changes request handling.

No batch touches `core/dispatch.py`, the adapters, the tracker or the breaker. If
one needs to, the design is wrong and that is a finding to record, not a refactor
to make quietly.
