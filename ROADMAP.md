# Free LLM Router: System Specification & Product Roadmap

## 1. Project Goal

Build a personal, single-user service that behaves similarly to OpenRouter's `openrouter/free` model API: it exposes an OpenAI-compatible API, maintains a pool of API keys for providers that offer free model/API tiers, routes requests to eligible free models, tracks usage, and avoids unnecessary exhaustion of provider/model limits.

The immediate purpose is to use the service with the user's own applications, especially a local OpenHands instance. The long-term goal is an intelligent router that can inspect the requirements of a request, identify the models that are capable of satisfying them, account for remaining free-tier capacity and observed provider health, and select the highest-quality eligible model rather than simply distributing work evenly.

### Core product principles

1. **Strictly $0 provider spend.** The router must never automatically fall back to paid capacity. When all eligible free capacity is exhausted, the request fails rather than incurring a charge.
2. **OpenAI compatibility first.** The public API should stay as close as practical to OpenAI conventions so applications such as OpenHands can use the router with minimal or no client-side changes.
3. **Incremental complexity.** MVP routing is intentionally simple. More sophisticated routing is introduced only after the basic proxy, key pooling, persistence, and quota tracking are reliable.
4. **Answer quality over distribution.** Future routing should prefer the model expected to produce the highest-quality answer that satisfies the request. Even distribution of requests across models is not a goal by itself.
5. **Provider adapters own provider quirks.** The internal architecture should normalize requests/responses while allowing each provider adapter to handle native API differences explicitly.
6. **No external services in MVP.** The first usable version should run as a single node with local persistence and no Redis, hosted database, queue, or other required external service.
7. **Private by default.** The MVP is intended for one user. Production-grade authentication, multi-tenancy, administration, and remote management are later milestones.
8. **Every provider must be evaluated for permitted usage.** A provider should only be enabled for pooled-key routing when the applicable provider terms and free-tier rules permit the intended usage pattern.

---

## 2. Supported Provider Strategy

The initial provider candidates identified for the project are:

- Google
- Mistral
- OpenRouter
- Groq
- Cerebras
- SambaNova
- Z.ai

### MVP providers

The first implementation will support:

- **Google**
- **Mistral**

These providers were selected as the initial adapters because they are already familiar to the user and provide a manageable starting point for validating the architecture.

The architecture should make adding the remaining providers an adapter/configuration exercise rather than a rewrite of the router core.

### Provider eligibility

Before enabling a provider in the active free-tier pool, document at least:

- supported models
- request/token limits relevant to routing
- reset/cooldown behavior
- supported request capabilities
- streaming support
- tool/function-calling support
- provider-specific request/response differences
- provider requirements or restrictions around API-key pooling

Exact provider quota values should be treated as configuration data and verified against current provider documentation rather than hard-coded assumptions in the routing engine.

---

## 3. Functional Specifications

### 3.1 Interface & Payload Normalization

#### MVP

- Expose an OpenAI-compatible API.
- Primary endpoint: `POST /v1/chat/completions`.
- Accept standard OpenAI-style request structures, beginning with the subset needed by the first supported providers and OpenHands.
- Translate normalized requests into provider-native formats through provider adapters.
- Normalize successful provider responses back into an OpenAI-compatible response.
- Return provider failures as normalized OpenAI-style errors where practical.
- Keep provider/model identifiers internally unambiguous even if the external representation remains close to OpenAI conventions.

#### Early follow-up

- Add `GET /v1/models`.
- Use it to expose the currently configured/available free models so compatible clients and tools can discover them.
- Continue to keep the primary API surface OpenAI-standard rather than introducing router-specific request syntax prematurely.

#### Later

Expand the compatibility surface only as needed:

- richer OpenAI request parameters
- tool calling
- structured outputs / JSON behavior where applicable
- additional OpenAI-compatible endpoints as justified by real use cases

Do not build a broad compatibility layer for every OpenAI feature before the project has a concrete need for it.

---

### 3.2 Model & Provider Identity

Internally, every model should be represented by an unambiguous provider/model identity.

Recommended conceptual form:

```text
provider/model
```

Examples:

```text
google/<provider-model-id>
mistral/<provider-model-id>
```

The external API should remain as close as practical to OpenAI conventions. Initially, callers select an exact model. A future virtual model name such as `free` may be introduced later to grant the router permission to choose the model automatically.

The important semantic distinction should remain:

```text
Explicit model
    = honor the requested model; only use another model when the configured fallback policy explicitly permits it.

Router-controlled model
    = choose the best eligible model according to routing policy.
```

---

### 3.3 `/v1/models`

Include `GET /v1/models` early because the implementation is expected to be small and it may improve compatibility with tools that discover available models.

The initial response can be generated directly from configuration/model-registry state; it does not need dynamic provider discovery.

---

### 3.4 Key Pooling & Selection

Maintain an internal pool of API keys per provider/model.

For MVP:

- The user supplies and operates the keys.
- The system is single-user.
- The initial target is two pooled keys total, approximately one key for each MVP provider.
- Multiple keys for the same provider/model must be supported by the architecture even though the first test deployment may only have one key per provider.
- Key state is local and persisted.
- Key selection uses a simple deterministic strategy such as LRU among eligible keys.
- A key in cooldown/exhausted state is excluded from selection.

The key manager should be independent of the eventual intelligent model-selection engine so the two concerns can evolve separately.

---

### 3.5 Quota Awareness & Usage Tracking

The core purpose of quota tracking is not simply analytics; it is to proactively avoid wasting free-tier capacity and reduce unnecessary 429 responses.

#### MVP quota model: Level 1

Start with a small, static set of dimensions appropriate to Google and Mistral, such as:

- requests within a configured time window
- tokens within a configured time window where the provider exposes meaningful token limits
- daily/request-count limits where applicable
- cooldown state

The quota engine should use a generic internal representation so that additional dimensions can be added later without rewriting the routing engine.

#### Reservation flow

Use a simple pre-dispatch reservation pattern:

```text
Incoming request
    ↓
Estimate request token usage
    ↓
Find eligible provider/model/key
    ↓
Reserve estimated quota
    ↓
Dispatch request
    ↓
Record actual outcome/usage
    ↓
Reconcile estimate with actual usage when available
```

The MVP algorithm should be deliberately simple. It does not need exact provider accounting and may tolerate small estimation error in exchange for low implementation complexity.

#### Future quota intelligence

Progressively move toward:

- provider-specific reset windows
- provider response headers and metadata
- observed 429/reset behavior
- more precise token accounting
- adaptive estimates of remaining quota
- provider/model-specific quota dimensions
- proactive selection based on remaining capacity rather than only cooldown state

---

### 3.6 Context Limits & Payload Management

Context awareness should evolve in stages.

#### Early implementation

- Maintain configured context-window metadata per model.
- Estimate incoming request size before dispatch.
- Do not send a request to a model that is known to be incapable of accepting it.
- Return a clear error if no eligible configured model can satisfy the request.

#### Later

- Route oversized requests to a compatible larger-context model when routing policy allows model substitution.
- Add more sophisticated message truncation only after there is evidence it is useful.
- Preserve system instructions and recent user context when truncation is eventually implemented.

The router should not silently truncate user content in MVP. Model substitution or a clear error is preferable until truncation semantics have been deliberately designed and tested.

---

### 3.7 Capability Matching

The initial intelligent-routing foundation should be rules-based and capability-aware, not LLM-based.

Candidate capabilities include:

- context-window size
- streaming
- tool calling
- vision/multimodal input
- structured output support
- other request features required by supported applications

The selection process should first eliminate models that cannot satisfy hard requirements, then compare the remaining eligible models according to routing policy.

For example:

```text
Request includes tools
    ↓
remove models without tool support
    ↓
Request requires 64k context
    ↓
remove models below 64k
    ↓
Evaluate remaining models by quality/quota policy
```

---

### 3.8 Client Model Selection

#### MVP

Clients explicitly select a model.

Example conceptually:

```json
{
  "model": "google/<model-id>",
  "messages": [ ... ]
}
```

The router does not yet attempt to understand the task or select among models automatically.

#### Later

Add a router-controlled model option, conceptually:

```text
model = "free"
```

This gives the router permission to choose the best currently eligible model.

Keep routing policy out of the primary OpenAI-compatible request schema initially. Routing policy should begin in configuration and later become manageable through an Administrative API/UI.

---

### 3.9 Routing Policy

The eventual routing engine should optimize first for the probability of producing the best answer while satisfying all hard constraints.

The rough priority is:

1. **Free/quota availability is a hard constraint.** A model with no usable free capacity cannot be selected.
2. **Answer quality is the dominant optimization objective.** Coding and reasoning ability are dimensions of answer quality rather than separate top-level objectives.
3. **Tool capability is highly important** because OpenHands uses tools heavily.
4. **Capability fit matters.** Context size and required request features must be satisfied.
5. **Reliability and provider health matter.** Recent failures should affect eligibility or scoring.
6. **Latency matters, but is intentionally lower priority.** The project prefers a slower correct answer to a faster incorrect answer.

### Configurable weighting system

The first automatic model-selection algorithm should support configurable weights for dimensions such as:

```yaml
routing:
  weights:
    answer_quality: 0.50
    tool_support: 0.15
    reliability: 0.15
    quota: 0.15
    latency: 0.05
```

These values are illustrative only. The actual defaults should be established once the first model registry exists.

The score should only be calculated among models that satisfy all hard requirements and have usable free capacity.

### Selection behavior

Prefer the same best model while it remains eligible rather than deliberately distributing requests evenly.

Conceptually:

```text
Choose highest-quality eligible model
        ↓
keep using it while free capacity remains
        ↓
quota becomes unavailable
        ↓
select next-best eligible model
```

This means the router intentionally concentrates traffic on the preferred model until a hard constraint forces a change.

---

### 3.10 Error Handling & Failover

#### MVP

Keep failover intentionally simple.

Basic policy:

- `429` / quota-related response → mark key cooling and try the next eligible key/model according to configured fallback rules.
- Selected transient `5xx` response → simple retry/failover according to configured retry budget.
- General `4xx` request errors → do not blindly retry the same payload across providers.
- Unsupported/malformed requests should fail before dispatch when the router can identify the problem locally.
- If no eligible free option remains, return a normalized error rather than wait in a queue.

The MVP should not implement an elaborate cascade engine.

#### Later

Add:

- provider-specific status/error classification
- Retry-After interpretation
- provider-specific reset behavior
- smarter alternative-model selection
- retry budgets based on elapsed time/attempt count
- health scoring
- more sophisticated streaming recovery

---

### 3.11 Streaming

Streaming is desirable early because it improves practical usability with OpenHands and other interactive clients.

For the first implementation:

- Support normalized OpenAI-style streaming where the selected provider can support it.
- Translate provider-native streaming chunks to OpenAI-compatible SSE chunks.
- Track quota/usage as accurately as the provider integration permits.

### MVP streaming failure policy

Fail fast once a streaming response has begun.

Do not attempt transparent cross-provider replay after partial content has already been delivered. Later phases may explore more sophisticated recovery mechanisms.

---

### 3.12 Tool Calling

Tool calling is **not an MVP requirement**, but it should be added shortly afterward because it is important to the intended OpenHands workflow.

The provider adapter architecture should therefore avoid making tool support impossible to add later.

Eventually, tool support should become a hard capability requirement during model selection:

```text
Request requires tools
    ↓
Only models with compatible tool support remain eligible
```

---

### 3.13 Client Management

#### MVP

- Single trusted user.
- No client authentication layer is required beyond whatever network-level protection is used for the private deployment.
- No multi-tenant accounting.
- No internal client API-key issuance.
- No client-facing rate-limit management.

Do not expose the service publicly without adding an authentication/security layer later.

#### Later

- internal client API keys
- per-client rate limits
- usage attribution
- RBAC
- tenant isolation
- administrative controls

---

### 3.14 Persistence

The MVP must have persistent local state so a restart does not immediately reset all quota accounting.

Persist enough information to reconstruct recent usage and key availability, including conceptually:

```text
provider
model
key identity/reference
last request time
cooldown_until
request counts
estimated/actual token counts when available
error counts
status
quota-window timestamps
```

The MVP should use exactly one local persistence implementation and no external persistence service.

An external persistence dependency may be introduced in a later phase. The specific technology should be selected after real usage patterns and requirements are known rather than prematurely locking the design to Redis.

---

### 3.15 Configuration & Secrets

#### MVP

Use `config.yaml` for configuration.

API keys should be referenced from environment variables or another mechanism that avoids committing secrets directly to source control. The configuration can define provider/model metadata and quota estimates while secret values remain outside version-controlled configuration.

Example conceptual structure:

```yaml
providers:
  google:
    enabled: true
    keys:
      - ${GOOGLE_KEY_1}
    models:
      - id: <provider-model-id>
        context_window: <configured-value>
        capabilities:
          streaming: true
          tools: false
        quotas:
          rpm: <configured-value>
          tpm: <configured-value>
          rpd: <configured-value>

  mistral:
    enabled: true
    keys:
      - ${MISTRAL_KEY_1}
```

For MVP, configuration changes may require a restart.

#### Later

- encrypted persistent API-key storage
- Administrative API for key management
- Web UI for key management
- reduced reliance on manually edited files
- eventual single-container deployment driven primarily by environment variables and managed persistent state

---

### 3.16 Logging & Observability

The service should support both structured operational logging and optional detailed debug logging from MVP.

#### Structured application logs

Include fields such as:

```text
request_id
provider
model
key reference
routing decision
selection reason
estimated tokens
attempt number
HTTP status
latency
cooldown state
error category
```

#### Debug request/response logging

Provide configurable debug-level logging of request/response information for development and troubleshooting.

Prompt and completion content should not be persisted or logged at normal levels.

A future Admin UI should make routing decisions and key health understandable without requiring users to parse raw logs.

---

## 4. Non-Functional Requirements

### MVP requirements

- Single-node deployment.
- Docker-based deployment.
- No required external services.
- Local persistent state.
- OpenAI-compatible API.
- Initial support for Google and Mistral.
- Two-key target deployment: approximately one key per provider.
- Exact model selection.
- Simple key pooling and LRU selection.
- Static quota estimates.
- Conservative cooldowns.
- Simple failover.
- Unit tests included in MVP.
- Manual integration testing initially.
- Routing overhead target: **<500 ms** for the simple MVP decision path.
- MVP success criterion: approximately **50% successful requests** under realistic free-tier/provider availability during early testing. This is a deliberately modest proof-of-concept target rather than a long-term service-level objective.

### Long-term qualities

The architecture should be able to grow toward:

- many keys per provider/model
- many providers
- capability-aware routing
- quota-aware routing
- model-quality scoring
- adaptive performance learning
- streaming and robust tool calling
- encrypted secret storage
- external persistence
- Administrative API
- Web UI
- multi-user support
- streamlined provider onboarding

---

# 5. Technical Roadmap

The roadmap is intentionally milestone-driven. Each milestone should leave the service in a usable state rather than requiring a large “big bang” release.

```text
M0  Technical Prototype
 |  HTTP ingress + one adapter + one model + basic response normalization
 v
M1  MVP OpenAI-Compatible Proxy
 |  Google + Mistral + exact model selection + key pools + basic failover
 v
M2  Persistent & Interactive MVP
 |  local persistence + quota tracking + /v1/models + streaming
 v
M3  Capability-Aware Router
 |  context/capability filtering + configurable model selection policies
 v
M4  Quality-First Free Router
 |  weighted scoring + quota-aware model fallback + explainable decisions
 v
M5  Adaptive Router
 |  observed provider performance + dynamic quality/reliability estimates
 v
M6  Managed Router
 |  encrypted keys + Admin API + external persistence + Web UI
 v
M7  Extensible Provider Platform
 |  streamlined provider onboarding + expanded provider/model ecosystem
```

The schedule is intentionally loose. A reasonable working expectation is roughly **three times** the duration suggested by a typical weekend/1–2-week/1-month progression. That translates to a roughly three-month development horizon for the sequence, but milestones should be allowed to move based on actual complexity and available personal development time.

---

## Milestone 0: Technical Prototype

### Objective

Prove that the core abstraction works with the minimum amount of code.

### Deliverables

- Select implementation stack.
- Create basic project structure.
- Implement OpenAI-compatible HTTP ingress.
- Implement internal normalized request/response types.
- Implement one Google adapter.
- Send one known request to one model and normalize the response.
- Add configuration loading from `config.yaml`.
- Add basic structured logs.
- Create the first unit-test suite around normalization and configuration.
- Establish Docker development/runtime workflow.

### Explicit non-goals

- intelligent routing
- persistent quota accounting
- multi-key pooling beyond the simplest abstraction
- advanced retries
- tool calling
- authentication
- UI

### Exit criteria

A local OpenAI-compatible client can send one chat-completion request through the router to Google and receive a normalized response.

---

## Milestone 1: MVP OpenAI-Compatible Proxy

### Objective

Create the smallest genuinely useful version of the service.

### Deliverables

- Google adapter.
- Mistral adapter.
- Provider/model identity registry.
- Multiple API-key support per provider/model.
- Initial two-key deployment support.
- Exact model selection through the standard `model` field.
- Simple LRU key selection.
- In-process concurrency control around key selection/reservation.
- Simple quota counters based on static configured limits.
- Conservative cooldown state.
- Basic `429` handling.
- Basic `5xx` handling.
- Do not blindly retry ordinary `4xx` errors.
- Normalized OpenAI-style errors.
- Structured application logging.
- Debug request/response logging.
- Unit tests for routing, key selection, cooldown, and error behavior.
- Docker deployment.

### MVP routing model

```text
Client
  ↓
OpenAI-compatible endpoint
  ↓
Validate/normalize request
  ↓
Explicit provider/model
  ↓
Eligible key pool
  ↓
LRU selection
  ↓
Quota reservation
  ↓
Provider adapter
  ↓
Normalized response
```

### Exit criteria

The service can reliably pass requests between OpenHands/another OpenAI-compatible client and Google/Mistral while selecting among configured keys and avoiding obviously unavailable keys.

---

## Milestone 2: Persistent & Interactive MVP

### Objective

Make the MVP practical for repeated local use and early OpenHands testing.

### Deliverables

- Local persistent state using one implementation only.
- Restore recent usage/cooldown state after restart.
- More complete quota-window accounting.
- Token estimation before dispatch.
- Simple estimated-vs-actual quota reconciliation when usage is available.
- `GET /v1/models`.
- OpenAI-compatible streaming support where supported by the provider adapters.
- Streaming translation to SSE.
- Streaming fail-fast behavior after stream start.
- Improved provider error classification.
- Manual integration-testing matrix covering success and failure cases.

### OpenHands objective

Begin using the router with the local OpenHands instance as an actual workload as early as practical.

Tool calling remains outside the MVP boundary but should be the next compatibility feature after streaming.

### Exit criteria

The router survives a restart without immediately losing its quota state and can serve interactive OpenHands workloads with streaming where supported.

---

## Milestone 3: Capability-Aware Router

### Objective

Move from “send this exact model a request” toward “select from models that can actually satisfy this request.”

### Deliverables

- Expand model registry metadata.
- Configurable context-window metadata.
- Capability metadata such as streaming, tools, vision, and structured-output support where relevant.
- Local request capability detection.
- Hard requirement filtering.
- Large-context candidate selection.
- Configurable fallback model relationships.
- No LLM-based request classifier.
- No automatic paid routing.

### Example selection flow

```text
Request
  ↓
Determine hard requirements
  ↓
Remove incompatible models
  ↓
Remove models without usable free quota
  ↓
Rank remaining candidates
```

### Exit criteria

A request is never deliberately sent to a model that the local registry already knows cannot satisfy its hard requirements.

---

## Milestone 4: Quality-First Free Router

### Objective

Introduce the first true `model = free` style routing behavior.

### Deliverables

- Router-controlled virtual model option.
- Configurable routing-policy weights.
- Quality-first model scoring.
- Quota availability as a hard constraint.
- Reliability/health as a scoring dimension or eligibility filter.
- Tool support as a strongly weighted requirement.
- Context fit as a hard constraint.
- Latency as a deliberately low-weight factor.
- Stable selection of the current best model while its free capacity remains available.
- Fall to the next-best model when the preferred model becomes unavailable.
- Explainable routing decisions in logs.

### Illustrative routing sequence

```text
model = free
   ↓
identify request requirements
   ↓
filter incompatible models
   ↓
filter exhausted/cooling models
   ↓
score remaining models
   ↓
choose highest-quality candidate
```

### Exit criteria

The router can choose a free model without the client having to name the provider/model explicitly, while keeping the decision understandable from structured logs.

---

## Milestone 5: Adaptive Router

### Objective

Move from static model metadata toward a router that learns from observed runtime behavior.

### Deliverables

- Record provider/model latency.
- Track observed success/failure rates.
- Track tool-call success/failure behavior where supported.
- Track quota prediction accuracy.
- Update reliability/availability estimates over time.
- Explore dynamic model quality estimates.
- Experiment with adaptive routing policies.
- Preserve hard $0 constraint.
- Preserve explainability of major routing decisions.

### Separate research/brainstorming topic

Design an adaptive learning system that can estimate model performance from real workloads without unnecessarily consuming free-tier quota.

This should be treated as a separate design exercise before implementation because the user wants to brainstorm the best approach for learning from outcomes.

Potential future approaches may include rules, statistical scoring, contextual bandits, or machine-learning techniques, but no specific approach is mandated by this roadmap yet.

### Performance consideration

As routing becomes more sophisticated, the routing-overhead target may be relaxed beyond the MVP's `<500 ms` target when the additional computation produces meaningful quality improvements.

---

## Milestone 6: Managed Router

### Objective

Convert the personal router into a maintainable managed service without changing its core routing concepts.

### Deliverables

- Encrypted persistent storage for provider API keys.
- Administrative API.
- Key lifecycle management.
- Provider/model configuration management.
- Routing-policy management.
- Quota configuration management.
- Health/status inspection.
- Admin audit logging.
- External persistence dependency selected and introduced based on demonstrated requirements.
- Ability to manage configuration without editing files directly.
- Single-container deployment model driven primarily by environment variables and persistent storage where practical.

### Persistence technology decision

Do not commit to Redis as a requirement in advance.

The original roadmap proposed Redis in Phase 3; this revised roadmap intentionally defers the choice so the project can first determine what the persistent state actually requires in a single-node application. fileciteturn0file0L62-L66

Candidate technologies can be evaluated later based on:

- persistence semantics
- concurrency requirements
- operational simplicity
- container deployment model
- migration tooling
- performance
- backup/recovery needs
- compatibility with the Admin API/UI

---

## Milestone 7: Web UI & Provider Platform

### Objective

Make provider/model administration and future provider expansion substantially easier.

### Deliverables

- Web UI backed by the Admin API.
- Key management screens.
- Provider/model registry management.
- Routing policy editor.
- Quota/cooldown visibility.
- Routing decision explorer.
- Request and provider health analytics.
- Provider adapter health tests.
- Streamlined process for adding a new provider.
- Provider capability templates.
- Provider-specific quota/reset configuration.
- Optional guided provider onboarding.

### Automatic provider onboarding

Treat automatic or highly streamlined onboarding as a long-term convenience feature rather than an MVP capability.

A possible future workflow:

```text
Add provider
   ↓
enter credentials
   ↓
discover available models where supported
   ↓
load/confirm capabilities
   ↓
load/confirm free-tier limits
   ↓
validate adapter health
   ↓
enable provider/model(s)
```

The precise level of automation depends on what each provider exposes reliably.

---

# 6. Data & Fallback Flow Matrix

| Event / Trigger | MVP Action | Later Enhancement | Terminal Action |
|---|---|---|---|
| Incoming exact-model request | Validate, select eligible key, reserve quota, dispatch | Capability-aware model selection | Return normalized error if no eligible key/model |
| Incoming router-controlled request | Not supported initially | Filter, score, and select best free model | Return normalized error if no eligible model |
| Key available | Select eligible key using simple LRU | Quota-aware / health-aware selection | — |
| Key cooldown active | Exclude key | Provider-specific reset prediction | Continue to next eligible key/model |
| Upstream 429 | Mark key/model cooling; simple retry/failover | Retry-After and provider-specific reset behavior | Return normalized error when free capacity is exhausted |
| Upstream 5xx | Simple failover/retry | Provider-specific classification and health scoring | Return normalized error |
| Upstream ordinary 4xx | Do not blindly retry | Better local capability validation | Return normalized client/provider error |
| Payload exceeds target model context | Reject or use configured compatible alternative in later milestone | Capability-aware large-context routing and optional controlled truncation | Return context error if no compatible free model exists |
| Stream begins and then fails | Fail fast | Explore partial-stream recovery | Terminate stream |
| Router restart | Restore persisted state | More robust state recovery/consistency | — |
| All free capacity exhausted | Fail synchronously | More accurate reset prediction | No automatic paid fallback |

---

# 7. Testing Strategy

## MVP unit tests

Unit tests are required in MVP even though broader automated integration testing follows shortly afterward.

Cover at least:

- request normalization
- response normalization
- provider/model identity handling
- key selection
- LRU behavior
- cooldown behavior
- quota reservation
- quota reconciliation
- concurrency/locking behavior
- error normalization
- retry/failover policy
- context-limit validation
- configuration parsing

## Early post-MVP automated tests

Build a deterministic fake-provider harness that can simulate:

- successful responses
- 429 responses
- 4xx errors
- 5xx errors
- timeouts
- slow responses
- quota exhaustion
- stream interruptions
- different capability sets
- concurrent requests

This should become the primary way to test routing and failover logic without consuming real free-tier quota.

## Manual testing

Manual provider testing remains useful initially for validating:

- real request/response translation
- provider authentication
- streaming behavior
- OpenHands compatibility
- provider-specific quirks not captured by the fake-provider harness

---

# 8. Security & Privacy Roadmap

## MVP

Assume a trusted, private deployment.

- Do not persist prompts by default.
- Do not persist completions by default.
- Do not log message contents at normal levels.
- Treat API keys as secrets.
- Avoid committing secrets to source control.
- Restrict network exposure appropriately for a private service.

## Later

- encrypted API-key storage
- Admin API authentication
- client API keys
- RBAC
- audit logs
- secret rotation
- stronger separation between administrative and request-plane operations

---

# 9. Administrative API/UI Roadmap

Administrative management is explicitly postponed until after the routing architecture has stabilized.

The future Admin API should eventually manage:

- providers
- provider keys
- models
- model capabilities
- quota estimates
- cooldown policies
- routing weights
- fallback policies
- service status
- health information
- request statistics

The Web UI should consume the Admin API rather than bypassing it. This keeps administrative logic available to both humans and automation.

A particularly valuable future UI feature is an **explainable routing view** showing why a model was selected and why alternatives were excluded.

---

# 10. Explicit Non-Goals / Deferred Features

The following are intentionally outside the foreseeable MVP and early roadmap unless a concrete use case changes priorities:

- automatic paid-provider fallback
- public anonymous SaaS access
- billing/subscriptions
- multi-region deployment
- high-availability clustering
- Kubernetes as a requirement
- enterprise SSO
- mobile application
- queue-based deferred request execution
- broad support for every OpenAI endpoint
- image generation
- audio APIs
- embeddings unless a concrete application requires them
- fine-tuning infrastructure as part of the router itself

### Fine-tuning clarification

Fine-tuning refers to adapting/customizing a model with additional training data. It is not required for the core router, whose purpose is selecting and proxying existing free-tier models. It can remain a separate future project/use case unless the router eventually needs to expose a unified interface for custom models.

### Potentially valuable future features

These are not commitments, but are valid long-term ideas:

- streamlined/automatic provider onboarding
- richer capability discovery
- automated model metadata validation
- adaptive routing experiments
- model performance benchmarking
- routing simulation/replay against historical requests
- provider-specific quota/reset plugins
- richer analytics

---

# 11. Long-Term Architecture Direction

The intended evolution is:

```text
                  ┌──────────────────────────────┐
                  │ OpenAI-Compatible API        │
                  │ /v1/chat/completions        │
                  │ /v1/models                  │
                  └──────────────┬───────────────┘
                                 │
                                 v
                  ┌──────────────────────────────┐
                  │ Request Normalizer           │
                  └──────────────┬───────────────┘
                                 │
                                 v
                  ┌──────────────────────────────┐
                  │ Routing Engine               │
                  │                              │
                  │ MVP: exact model             │
                  │ Later: capability filtering  │
                  │ Later: quality scoring       │
                  │ Later: adaptive routing      │
                  └───────┬───────────┬──────────┘
                          │           │
                    Key/Quota     Model Registry
                    Manager
                          │           │
                          └─────┬─────┘
                                │
                    ┌───────────┼───────────┐
                    │           │           │
                    v           v           v
             Google Adapter  Mistral     Future Adapters
                              Adapter
                    │           │           │
                    └───────────┼───────────┘
                                │
                                v
                     Free Provider APIs
```

Supporting subsystems introduced over time:

```text
MVP
  Config → Local State → Logs

Later
  Config/Admin API → Persistent Store → Metrics → Web UI
```

The key architectural separation to preserve is:

```text
Request compatibility
        ≠
Provider translation
        ≠
Key selection
        ≠
Quota accounting
        ≠
Model selection
        ≠
Administrative management
```

Each concern should have a narrow interface so that the project's routing intelligence can become substantially more sophisticated without destabilizing the basic OpenAI-compatible proxy.

---

# 12. Current Project Definition

At the end of this requirements/grill-me session, the project can be summarized as follows:

> **Build a strictly $0, single-user, single-node LLM routing proxy that presents an OpenAI-compatible API, initially forwards exact-model requests through Google and Mistral using a small pooled set of API keys, persists enough local state to avoid immediately overshooting free-tier limits after restarts, and progressively evolves into a capability-aware, quality-first intelligent router that selects the best available free model for each request.**

The first release should be deliberately boring: it needs to pass requests correctly, keep simple accounting, survive restarts, provide useful logs, and work with the user's real OpenHands workload. The sophistication belongs in later milestones, after each underlying layer has proven itself.
