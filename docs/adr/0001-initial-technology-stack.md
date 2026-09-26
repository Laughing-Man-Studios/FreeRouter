# ADR-001: Core Technology Stack & Implementation Strategy

**Date:** 2026-09-25  
**Status:** Accepted  
**Driver:** Robert Gibb (Wan Toc)  

## 1. Context

The Free LLM Router is a single-user, private routing service designed to present a unified, OpenAI-compatible API layer over free-tier LLM providers. The architecture must strictly support $0 provider spend, maintain low latency (<500 ms routing overhead), and safely handle concurrent agent workloads.

Before development of Milestone 0 begins, the baseline technology stack, execution environment, and strict operational boundaries must be explicitly defined. The initial target providers for MVP are **Google (Gemini)** and **Mistral**.

## 2. Decision

### 2.1 Runtime & Containerization

* **Language Runtime:** Standard **CPython 3.13** exactly. Backward compatibility with 3.12 is explicitly dropped to leverage 3.13 performance and async improvements.
* **Package Management:** **`uv`** is the exclusive package manager. `pip`, `poetry`, and `conda` are forbidden.
* **Containerization Strategy:** Multi-stage Docker builds.
  * **Build Stage:** `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` (used strictly for dependency resolution and wheel building).
  * **Runtime Stage:** `python:3.13-slim` (Debian-based). The final image must contain no build tooling, run as a non-root user, and include a Docker `HEALTHCHECK`.
* **Execution Model:** **Single Uvicorn process** for MVP. No multi-worker scaling or process managers (like Gunicorn) will be used.

### 2.2 Security & Configuration

* **Network Security:** The service will bind to `0.0.0.0` internally but relies strictly on network-level isolation (localhost or private Docker networks). **No API authentication token** is required for MVP.
* **Configuration Source of Truth:**
  * Base configuration (models, routing weights, timeouts) is defined in `config.yaml`.
  * **Environment variables strictly override** `config.yaml` values.
* **Secrets Management:** Provider API keys must be injected via environment variables. Hardcoding secrets or committing them to `config.yaml` is strictly forbidden.
* **Validation & Reload:** Configuration is validated via Pydantic at startup. The application will **fail fast** if configuration is invalid. Hot-reloading is out of scope for MVP.

### 2.3 Framework & Concurrency

* **Web Framework:** FastAPI running on Uvicorn.
* **Event Loop:** **`uvloop`** is mandatory in the Docker runtime to maximize async throughput.
* **Validation:** Pydantic v2 for structured request/response validation. To minimize serialization latency, **SSE stream chunk parsing will bypass full Pydantic validation**, relying on minimal structural checks or raw JSON parsing.
* **ASGI Lifespan:** Startup/shutdown lifecycle events must explicitly manage the creation and closure of the `httpx.AsyncClient` singleton, SQLite connection initialization, and Alembic migration execution.

### 2.4 Outbound Networking & Failover

* **HTTP Client:** A single, application-lifetime persistent **`httpx.AsyncClient`** singleton.
* **Timeout Strategy:** Aggressively tuned to guarantee the <500 ms routing envelope:
  * `connect=0.25` (250 ms TCP/TLS establishment)
  * `pool=0.05` (50 ms connection acquisition)
  * `write=1.0` (1,000 ms prompt JSON push)
  * `read=10.0` (10-second idle timeout between SSE tokens)
* **Routing Envelope:** The initial connection and response header phase is wrapped in a strict `asyncio.timeout(0.4)` window.
* **Failover Rules:**
  * **Pre-stream only:** If a timeout, 5xx, or 429 occurs before the first byte is written to the client, the router catches the error and attempts the next eligible key/model.
  * **Post-stream fail-fast:** Once streaming begins, the stream is locked. Upstream failures must fail fast and inject a synthetic terminal SSE error event. Cross-provider replay mid-stream is forbidden.
  * **Retry-After:** 429 responses with `Retry-After` headers will place the affected key into a persisted cooldown state.

### 2.5 Persistence & State Management

* **Database:** Embedded **SQLite** via the `aiosqlite` async driver. The database file will reside at a fixed mounted volume path (e.g., `/data/router.db`).
* **Concurrency Enforcements:** Connection listeners will enforce Write-Ahead Logging via Pragmas (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;`). **The application will refuse to start** if WAL mode cannot be enabled.
* **MVP Write Strategy:** Direct SQLite writes per request. In-memory batching is deferred to post-MVP.
* **Query Layer:** **SQLAlchemy Core only**. The SQLAlchemy ORM is explicitly prohibited for MVP to eliminate state-tracking overhead. Route handlers must use a repository/data-access layer; raw SQL in routes is forbidden.
* **Migrations:** **Alembic** will manage schema evolutions. Migrations will run **automatically at container startup** before the app begins accepting traffic.

### 2.6 Testing & Verification

* **Frameworks:** `pytest` and `pytest-asyncio`.
* **Network Mocking:** **`respx`** is deemed sufficient for MVP to simulate deterministic provider responses (429s, 5xx, streaming interruptions). The complex "fake-provider harness" is deferred to post-MVP.
* **Code Quality:** **`ruff`** will be used for both linting and formatting. Strict type hinting is required.
* **CI Pipeline:** Must enforce linting, type checking, test execution, and Docker build verification.

## 3. Consequences

### Positive

* **Implementation Clarity:** Developers and AI agents have exact Docker images, strict timeout values, and explicit boundaries (e.g., no ORM, no multi-worker), eliminating architectural ambiguity during Milestone 0.
* **Guaranteed Low Latency:** Bypassing Pydantic for SSE streaming, utilizing `uvloop`, and enforcing the 400ms `asyncio.timeout` wrapper guarantees the system meets its operational overhead targets.
* **Zero-Downtime Schema Updates:** Automatic Alembic migrations at startup ensure the database schema is always current without manual CLI intervention during deployments.
* **Concurrency Safety:** Forcing SQLite into WAL mode and refusing to start if it fails ensures that state reconciliation from concurrent agent requests will not lock the primary event loop.

### Negative / Trade-offs

* **Single Process Bottleneck:** Running a single Uvicorn process means the router is bound to the performance of a single CPU core. If token estimation or payload transformation becomes CPU-heavy, it will block the event loop. (Mitigation: Keep MVP logic strictly I/O bound).
* **No Auth Layer:** Relying purely on network isolation means if the Docker port is accidentally exposed to the public internet, the API is completely open.
* **Startup Latency:** Running Alembic migrations synchronously at startup may add a few seconds to container boot time, though this is acceptable for a single-node private tool.

## 4. Constitutional Compliance Check

| Constitution Directive | Compliance Validation |
| :--- | :--- |
| **3.1 Technology Mandate** | **Pass:** Utilizes Python 3.13, FastAPI, Pydantic, HTTPX, and SQLite. |
| **3.1 Concurrency Directive** | **Pass:** SQLite WAL mode explicitly enforced via `aiosqlite` connection pragmas; app refuses to start if it fails. |
| **3.2 Single-Node Deployment** | **Pass:** Architecture remains contained within a standard, multi-stage Docker environment with no external DB or queues. |
| **2.5 Quality over Latency** | **Pass:** The 400ms timeout envelope protects the <500ms routing overhead target while preserving the failover mechanism for high-quality fallback routing. |
| **9.2 Key Management** | **Pass:** Secrets strictly via environment variables; `config.yaml` used for non-secret overrides. |

***
