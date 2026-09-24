# CONSTITUTION.md

## 1. Purpose and Scope

### 1.1 Mission

The Free LLM Router is a single-user, private routing service designed to present a unified, OpenAI-compatible API layer over free-tier LLM providers. Its primary goal is to intelligently route client requests—specifically for personal automation tools and local agents such as OpenHands—to eligible free models while tracking, preserving, and optimizing free-tier capacity.

### 1.2 System Boundary

This service operates strictly as an intermediary request-and-response proxy. It handles API compatibility, provider payload translation, key pooling, quota reservation, and intelligent model selection. Out-of-scope concerns for early milestones include multi-tenant billing, commercial SaaS features, public user authentication, and enterprise access controls.

---

## 2. Core Principles & Hard Invariants

### 2.1 Strict $0 Spend Invariant

Under no circumstances shall the service route requests to paid API endpoints or incur financial charges. When eligible free capacity across configured keys and models is exhausted, requests must fail gracefully with a normalized error rather than attempting paid fallback.

### 2.2 Private & Single-User by Default

The system is built for a single trusted operator. Network access must remain restricted to private or local environments. Multi-tenancy, client-facing rate limits, and remote user management are deferred to later milestones.

### 2.3 Incremental Complexity & Anti-YAGNI

Architecture and abstractions must strictly reflect the current roadmap milestone. Developers and AI agents must not build speculative features, premature abstractions, or multi-node distributed primitives (such as Redis queues, hosted databases, or microservice meshes) until explicitly mandated by milestone progression.

### 2.4 Answer Quality over Traffic Distribution

Model selection must optimize for the probability of producing the highest-quality, most correct completion that satisfies the request's structural requirements. Artificially balancing or spreading load across providers or models is explicitly an anti-pattern.

### 2.5 Quality over Latency

While operational overhead should remain low (targeting <500 ms for simple routing decisions), correctness and intelligence take precedence over raw speed. The architecture shall not sacrifice higher answer quality or capability compliance merely to minimize routing latency.

---

## 3. Core Technology Stack & Infrastructure

### 3.1 Technology Mandate

To maintain operational simplicity and runtime stability, the system stack is strictly fixed for the initial milestones:

- **Language:** Python 3.12+
- **Service Framework:** FastAPI / ASGI
- **Data Modeling & Validation:** Pydantic
- **Outbound HTTP Client:** HTTPX (async)
- **Persistence Engine:** SQLite (embedded ACID database)

*Concurrency Directive:* The MVP must utilize SQLite in **WAL (Write-Ahead Logging) mode** paired with an async driver (e.g., `aiosqlite`) to safely handle concurrent agent workloads. The long-term architectural direction is to evolve toward an **in-memory quota state** with asynchronous, batched flushing to SQLite.

### 3.2 Single-Node Deployment

The system must run as a single-node process packaged for Docker deployment without requiring external infrastructure services.

---

## 4. Architectural Isolation & Layer Decoupling

The router core must remain decoupled from specific external API formats and deployment tooling. System components must communicate across strict boundaries:

```text
Incoming OpenAI API Request
            │
            ▼
 ┌──────────────────────────────┐
 │ Interface Normalizer         │
 └──────────┬───────────────────┘
            │
            ▼ Internal Normalized Request
 ┌──────────────────────────────┐
 │ Router Core                  │
 │ (Filtering & Selection)      │
 └──────────┬───────────────────┘
            │
            ▼ Model/Key Assignment
 ┌──────────────────────────────┐
 │ Key & Quota Manager          │
 └──────────┬───────────────────┘
            │
            ▼ Provider Specific Request
 ┌──────────────────────────────┐
 │ Provider Adapter             │
 └──────────┬───────────────────┘
            │
            ▼
 Outbound Provider Communication
```

### 4.1 Boundary Rules

- **Router Core:** Interacts only with normalized internal data models. It possesses no awareness of provider-specific payload schemas or vendor HTTP header formats.
- **Provider Adapters:** Enclose all provider-specific quirks, payload translation, response mapping, and raw error handling. Adding or modifying a provider adapter must never require structural refactoring of the router core.
- **Key & Quota Management:** Operates independently from model selection logic. Key allocation and rate-limit state tracking must expose a generic internal interface.

---

## 5. API Compatibility Standards

### 5.1 Standard OpenAI Protocol

The public HTTP API must maintain strict fidelity to OpenAI conventions. The primary endpoints are `POST /v1/chat/completions` and `GET /v1/models`. Clients (e.g., OpenHands) should require zero client-side modifications or custom headers to interact with the service.

### 5.2 Error Normalization

All provider-side failures, rate-limit exclusions (`429`), upstream service outages (`5xx`), or bad requests (`4xx`) must be caught by provider adapters and returned to the client as standard OpenAI-compliant error payloads.

### 5.3 Streaming Invariants

Streaming responses must translate upstream chunks into OpenAI-compatible Server-Sent Events (SSE).

- **Pre-Stream Failover:** If an upstream failure occurs before any HTTP headers or content chunks have been written to the client, the router is permitted to attempt key or model failover.
- **Post-Stream Fail-Fast:** Once the first SSE chunk has been flushed to the wire, the stream is locked. Any subsequent upstream failure must fail fast.
- **Safe Stream Termination:** To prevent client-side agent crashes, the router must **inject a synthetic final SSE chunk** containing a normalized OpenAI-standard error object and a terminal `finish_reason` (e.g., `error` or `router_error`). Abruptly severing the HTTP connection without a terminal SSE event is prohibited. Cross-provider payload replay mid-stream is strictly forbidden.

---

## 6. Provider Integration & Compliance

### 6.1 Provider Eligibility & TOS Risk Mitigation

Before any provider adapter is merged or enabled, a compliance evaluation must document the provider's terms of service regarding API key pooling and automated usage.

To balance personal utility with the risk of account suspension, the system enforces a **Conservative Compliance Mode**:

- If a provider explicitly permits key pooling, standard pooling logic applies.
- If a provider's Terms of Service prohibit multi-key pooling, the system must strictly limit the configuration to a **maximum of two (2) API keys** per installation.
- Under Conservative Compliance Mode, the routing rules must enforce **mandatory request pacing/throttling** (spacing out calls) to mimic human usage patterns and minimize the risk of IP bans or account suspension.

### 6.2 Provider Adapter Responsibilities

Each provider adapter must cleanly implement:

- Bi-directional payload transformation (OpenAI ↔ Provider Native).
- Streaming chunk normalization.
- Standardized error classification (`RATE_LIMIT`, `TRANSIENT_SERVER_ERROR`, `INVALID_REQUEST`, `CONTEXT_LENGTH_EXCEEDED`, `AUTH_ERROR`).

---

## 7. Model Selection & Routing Rules

### 7.1 Hard Constraints Before Optimization

Model selection must follow a strict sequential pipeline. Hard requirements must eliminate non-viable candidates before any scoring or ranking takes place:

```text
Incoming Request
       │
       ▼
1. Capability Filtering (Tools, Vision, Streaming, Context Size)
       │
       ▼
2. Quota & Cooldown Availability (Exclude exhausted keys/models)
       │
       ▼
3. Model Selection Strategy (Explicit or Router-Controlled)
       │
       ▼
4. Candidate Ranking / Quality Scoring (Among remaining eligible options)
```

**Strict Refusal over Silent Degradation:**
If a request requires capabilities (e.g., Tool Calling) or context lengths that no eligible model supports, the router must strictly reject the request with a `400 Bad Request`. Silent stripping of tools, silent truncation of context, or silent downgrading of capabilities is strictly prohibited, as it will break downstream agents.

### 7.2 Explicit Model Selection Behavior

When a client explicitly requests a specific model identifier (e.g., `google/gemini-1.5-flash`):

- The router must honor the exact requested model.
- If the assigned API key returns a rate limit (`429`) or temporary error, failover is strictly constrained to other available keys for that identical model.
- Cross-model fallback is prohibited for explicit model requests.

### 7.3 Router-Controlled Model Selection (`free`)

When a client requests a virtual/router-controlled model name (e.g., `model = "free"`):

- The router evaluates all configured models using hard capability filters and quota availability.
- The candidate with the highest expected answer quality score is selected.
- The router must maintain traffic sticky to the top-ranked eligible model until quota exhaustion or capability mismatch forces a fallback to the next-best model.
- **Quality Scoring Evolution:** Initial model quality scoring must rely on static, manually curated weights configured by the operator. Dynamic, observation-based quality scoring (tracking historical success/failure rates) is deferred to later milestones.

---

## 8. Quota Accounting & State Persistence

### 8.1 Resource Protection Imperative

Quota management is a proactive resource-protection system to avoid upstream rate limits (`429`) and provider penalties, not an analytics feature.

### 8.2 Reservation & Reconciliation Cycle

Every request dispatch must follow a strict state loop:

1. **Estimate:** Calculate expected token usage based on input payload.
2. **Reserve:** Reserve estimated capacity in local quota tracking before outbound dispatch.
3. **Dispatch:** Send request via selected provider key.
4. **Reconcile:** Adjust recorded state with actual token usage reported in provider response headers or completion payloads.

**Estimation Safety Margin:**
Token estimation must include a configurable safety buffer (e.g., `estimated_tokens * 1.1`) to account for hidden prompt/tool token overhead and prevent provider-side context rejections.

**Context Overload Circuit Breaker:**
If a provider returns a hard `400 Context Length Exceeded` error, the router must automatically mark the affected model as temporarily degraded or unavailable for large-context requests to protect remaining quota.

### 8.3 State Durability & Concurrency

Quota counts, key cooldown timestamps, and provider availability states must be persisted to SQLite. The system must survive unexpected process restarts without losing rate-limit history or immediately overshooting provider limits upon boot.
To support concurrent agent workloads (e.g., OpenHands), the database layer must not block the async event loop.

---

## 9. Privacy, Observability & Security

### 9.1 Privacy by Default

- **Payload Exclusion:** Prompt text, system instructions, tools declarations, and completion contents must never be stored in persistent databases or recorded in standard operational logs.
- **Operational Logging:** Standard logs are restricted to operational metadata (e.g., `request_id`, `provider`, `model`, `key_alias`, `estimated_tokens`, `latency_ms`, `http_status`, `routing_decision_reason`).
- **Debug Logging:** Full payload logging must remain strictly opt-in via explicit environment configuration and isolated from production log streams.

### 9.2 Key Management

Provider API keys must be loaded via environment variables or uncommitted external configuration files (`config.yaml`). Hardcoded secrets in source control are strictly forbidden.

---

## 10. Software Quality & Verification

### 10.1 Automated Unit Testing

Unit test coverage is mandatory for all core milestones. Tests must cover:

- Request and response normalization accuracy.
- Key selection and LRU cooldown rotation.
- Quota estimation, reservation, and reconciliation logic.
- Context limit and capability filtering algorithms.

### 10.2 Deterministic Integration Testing

A deterministic fake-provider test harness must be implemented early in the post-MVP roadmap to simulate rate limits (`429`), server faults (`5xx`), network timeouts, and streaming interruptions without consuming real free-tier provider limits.

---

## 11. Governance & AI Agent Compliance Directives

### 11.1 Authority

This Constitution defines the permanent engineering boundaries for the Free LLM Router project. Specifications, feature plans, pull requests, and generated code must comply with these rules.

### 11.2 Guidance for AI Coding Agents

Future AI development sessions, code changes, and task breakdowns must be validated against the following checks:

- **Spend Check:** Does this change introduce any path to paid API usage? *(Must be NO)*
- **Boundary Check:** Does this PR put provider-specific API logic inside the router core? *(Must be NO)*
- **Fallback Check:** Does an explicit model request fall back to a different model? *(Must be NO)*
- **Privacy Check:** Does standard operational logging include prompt or response text? *(Must be NO)*
- **Dependency Check:** Does this milestone add external service infrastructure like Redis or external queues? *(Must be NO for MVP/early milestones)*
- **Stack Check:** Does the implementation use Python, FastAPI, Pydantic, HTTPX, and SQLite? *(Must be YES)*
- **Capability Check:** Does the router silently strip tools or truncate context instead of strictly rejecting incompatible requests? *(Must be NO)*
- **Compliance Check:** Does the system enforce key limits and request pacing for providers with strict anti-pooling TOS? *(Must be YES)*

### 11.3 Amendments

Amendments to this Constitution require explicit documentation of rationale and an update to this file. Major architectural shifts must update the Constitution before implementation begins.
