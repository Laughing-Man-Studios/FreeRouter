# Task Breakdown: M0 Technical Prototype

This document outlines the implementation tasks for the M0 Technical Prototype, structured to align directly with the GitHub Project issues.

**Agent Instruction:**

- Do not attempt to implement all tasks at once.
- Implement **one batch at a time**.
- At the end of each batch, execute the explicit **Verification & Stop** criteria.
- Use the `gh` CLI to update the corresponding GitHub Issue status.
- Do not proceed to the next batch until the current batch is verified and human review is complete.

---

## Batch 1: Configuration & Logging

**Maps to GitHub Issue:** `#4 Configuration Loading (config.yaml) & Structured Application Logging`

- [ ] **T001: Project Setup & Bootstrap Artifacts.** Initialize the project using `uv`, pinned to CPython 3.13. Set up the project structure (`src/free_router/api`, `src/free_router/core`, `tests/`); the `db`, `providers`, and `migrations` packages are created by the batches that first use them, since git cannot track empty directories. Install dependencies (FastAPI, uvicorn, uvloop, pydantic, pydantic-settings, pyyaml, httpx, aiosqlite, alembic, pytest, respx) and configure `ruff` and `mypy` (strict) in `pyproject.toml` so the codebase is linted and typed from the first commit. Create the root-level `config.yaml` (with the minimal M0 model mapping) and a `.env.example` file documenting required environment variables (e.g., `GEMINI_API_KEY`).
- [ ] **T002: Configuration Loader.** Implement `src/free_router/core/config.py`. Create a manual YAML parser that reads `config.yaml` and feeds it into Pydantic `BaseSettings` models, ordered so environment variables strictly override `config.yaml` values. Ensure the app fails fast if `GEMINI_API_KEY` is missing or if the schema is invalid. **Crucial:** The loader must reject any `${VAR}` secret reference or secret-named field in `config.yaml` (spec 6), and must report field locations without echoing field values.
- [ ] **T003: Structured Logging.** Implement `src/free_router/core/logging.py` using the standard library. Create a custom JSON formatter that copies only an allowlist of operational fields off a record, so prompt and completion text are structurally unable to reach a log line. Add a FastAPI middleware in `src/free_router/api/main.py` to inject `contextvars` (`request_id`, `model`, `latency_ms`, `http_status`) into logs, and a minimal ASGI app whose lifespan configures logging before loading configuration. **Crucial:** Ensure prompt and completion text are strictly excluded from all log outputs. T003 creates this minimal app; T007 extends the same lifespan with the database engine and HTTP client, and T008 adds routes.
- [ ] **T004: Config & Logging Tests.** Write `pytest` unit tests to verify successful YAML loading, environment variable overrides, fast-failures on missing keys/invalid schemas (Scenarios 2 & 3), and verify logs do not contain prompt data.

**Verification & Stop:**

1. Run `pytest tests/test_config.py tests/test_logging.py`.
2. Run `ruff check .`, `mypy src/`, and `ruff format --check .`.
3. Verify the application fails to start (exits with non-zero code) if `GEMINI_API_KEY` is missing.
4. Inspect a sample log output to confirm it is valid JSON and contains no prompt data.
5. **STOP**, wait for human review, and use `gh` to mark Issue #4 as complete.

---

## Batch 2: HTTP Ingress & Normalized Types

**Maps to GitHub Issue:** `#2 HTTP Ingress & Normalized Request/Response Types`

- [x] **T005: Normalization & Exceptions.** Implement `src/free_router/core/normalization.py` (strict `NormalizedRequest` and `NormalizedResponse` models that strip unsupported params like `temperature`). Implement `src/free_router/core/exceptions.py` (custom hierarchy) and add global exception handlers in `src/free_router/api/main.py` to format them into OpenAI-compatible JSON.
- [x] **T006: Database Schema & Engine.** Implement `src/free_router/db/schema.py` using SQLAlchemy Core (no ORM) for `models` and `request_logs` tables. Initialize Alembic. **Crucial:** Extract the database initialization and pragma enforcement into a standalone, testable function (e.g., `init_db_engine()`) that explicitly enforces WAL pragmas and raises an exception if WAL mode cannot be enabled.
- [x] **T007: ASGI Lifespan & Dependency Injection.** Extend the lifespan introduced in T003 in `src/free_router/api/main.py` to call the standalone `init_db_engine()`, initialize the `httpx.AsyncClient` singleton, and attach singletons to `app.state`. Implement hybrid dependency getters in `src/free_router/api/routes.py`.
- [x] **T008: Chat Completions Endpoint.** Implement `POST /v1/chat/completions` in `src/free_router/api/routes.py`. **Crucial:** The check to validate the requested model against the configuration must happen *inside this FastAPI route handler* before invoking the adapter. Apply ingress normalization, invoke a placeholder/mock adapter, and return the egress-normalized OpenAI response.
- [x] **T009: Ingress & Routing Tests.** Write tests verifying that unsupported parameters are stripped, empty message arrays are rejected (Scenarios 5 & 6), exception handlers return correct JSON structures, and the route correctly rejects unsupported models (Scenario 7).

> **Note (T006 deviation):** Alembic is not initialised in this batch. The schema is created with
> `metadata.create_all`; a versioned migration is deferred to the batch that first needs to evolve the
> schema, since M0 has exactly one revision. `ROUTER_DB_PATH` was added as a configuration env var to
> keep the database location out of the working directory during tests.

**Verification & Stop:**

1. Run `pytest tests/test_normalization.py tests/test_db.py tests/test_lifespan.py tests/test_routes.py`.
2. Verify the database initialization tests pass, including the failure case for WAL enforcement (Scenario 4).
3. Verify route tests pass for empty messages and unsupported models.
4. **STOP**, wait for human review, and use `gh` to mark Issue #2 as complete.

---

## Batch 3: Provider Adapter

**Maps to GitHub Issue:** `#3 Google Adapter Implementation`

- [x] **T010: Adapter Protocol.** Implement `src/free_router/providers/base.py` defining the abstract `ProviderAdapter` interface protocol. It must take a `NormalizedRequest` and `httpx.AsyncClient`, and return a `NormalizedResponse`.
- [x] **T011: Google Adapter.** Implement `src/free_router/providers/google/adapter.py`. Map the internal request to the Gemini API format. **Crucial:** Wrap the initial provider connection and dispatch in a strict `asyncio.timeout(0.4)` block. Catch `httpx` errors and raise the corresponding custom router exceptions (`ProviderAuthError`, `ProviderServerError`, etc.).
- [x] **T012: Adapter Tests.** Write `respx` unit tests for the Google adapter simulating deterministic responses: Success (200), Auth Error (401), Server Error (500), and Timeout (Scenarios 8, 9, & 10).

> **Notes on T010–T012:**
>
> - **The 0.4 s window covers headers only, by decision.** The spec scopes it to "the initial
>   provider connection and response header phase", which is confirmed as the intended reading.
>   Dispatch therefore uses `client.send(request, stream=True)` and reads the body with `aread()`
>   *after* the window closes. Using the convenience `client.post()` reads the body inside the window
>   and would 504 every real multi-second completion.
> - **`build_adapter(provider, http_client)` factory** resolves an adapter from the configured
>   provider name, and `resolve_model` now returns `(provider, provider_id)`. This closes the seam
>   where `routes.py` hardcoded `MockAdapter` and a literal `provider="google"`.
> - **`providers/mock.py` is deleted.** The placeholder is no longer referenced by any code path.
> - **Auth uses the `x-goog-api-key` header**, not a `?key=` query parameter, so the secret cannot
>   appear in URLs or access logs. A test asserts the key never reaches the URL.
> - **Still on `generateContent`** (`v1beta/models/{id}:generateContent`) per spec. Google now
>   presents the newer Interactions API (`v1beta2/interactions`) as the going-forward path;
>   migrating is deferred to a later milestone and confined to the adapter module.
> - **`gemini-3.5-flash-lite` confirmed** as a real, supported model id. No `config.yaml` change.

**Verification & Stop:**

1. Run `pytest tests/test_google_adapter.py`.
2. Verify all adapter tests pass, including the strict 400ms timeout enforcement (Scenario 10).
3. **STOP**, wait for human review, and use `gh` to mark Issue #3 as complete.

---

## Batch 4: Test Suite Finalization & Docker/CI Setup

**Maps to GitHub Issue:** `#5 Unit Test Suite & Docker Development Environment Setup`

- [ ] **T013: End-to-End Integration Tests.** Write comprehensive tests for the full ingress-to-egress routing flow using FastAPI's `TestClient` and `respx` to mock the external Google API at the HTTP boundary. Cover a successful chat completion (Scenario 1) and verify the full error mapping pipeline.
- [ ] **T014: Dockerfile.** Create a multi-stage `Dockerfile` using `uv` for dependency resolution. The runtime stage must strictly use `python:3.13-slim`, run as a non-root user, utilize `uvloop`, and include a Docker `HEALTHCHECK`.
- [ ] **T015: Docker Compose.** Create a `docker-compose.yml` file for local development orchestration. It should handle mounting the SQLite data volume and passing the `.env` variables (like `GEMINI_API_KEY`) to the container.
- [ ] **T016: CI Pipeline.** Create `.github/workflows/ci.yml` defining the GitHub Actions workflow, wiring in the `ruff` and `mypy` (strict) configuration established in T001. **Crucial:** The pipeline must run `ruff check`, `mypy`, the `pytest` suite, and a dry-run Docker build.

**Verification & Stop:**

1. Run `ruff check .`, `mypy src/`, and `pytest` locally.
2. Run `docker compose build` and `docker compose up` to verify the local dev workflow functions correctly and the app starts.
3. Verify the GitHub Actions workflow file syntax is valid.
4. **STOP**, wait for human review, and use `gh` to mark Issue #5 (and the parent Epic #1) as complete.
