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

- [ ] **T001: Project Setup & Bootstrap Artifacts.** Initialize the project using `uv`. Set up the project structure (`src/api`, `src/core`, `src/db`, `src/providers/google`, `tests/`). Install dependencies (FastAPI, uvicorn, pydantic, pydantic-settings, pyyaml, httpx, aiosqlite, alembic, pytest, respx). Create the root-level `config.yaml` (with the minimal M0 model mapping) and a `.env.example` file documenting required environment variables (e.g., `GEMINI_API_KEY`).
- [ ] **T002: Configuration Loader.** Implement `src/core/config.py`. Create a manual YAML parser that reads `config.yaml` and feeds it into Pydantic `BaseSettings` models. Ensure the app fails fast if `GEMINI_API_KEY` is missing or if the schema is invalid.
- [ ] **T003: Structured Logging.** Implement `src/core/logging.py` using the standard library. Create a custom JSON formatter. Add a FastAPI middleware in `src/api/main.py` to inject `contextvars` (request ID, model, latency) into logs. **Crucial:** Ensure prompt and completion text are strictly excluded from all log outputs.
- [ ] **T004: Config & Logging Tests.** Write `pytest` unit tests to verify successful YAML loading, environment variable overrides, fast-failures on missing keys/invalid schemas (Scenarios 2 & 3), and verify logs do not contain prompt data.

**Verification & Stop:**

1. Run `pytest tests/test_config.py tests/test_logging.py`.
2. Verify the application fails to start (exits with non-zero code) if `GEMINI_API_KEY` is missing.
3. Inspect a sample log output to confirm it is valid JSON and contains no prompt data.
4. **STOP**, wait for human review, and use `gh` to mark Issue #4 as complete.

---

## Batch 2: HTTP Ingress & Normalized Types

**Maps to GitHub Issue:** `#2 HTTP Ingress & Normalized Request/Response Types`

- [ ] **T005: Normalization & Exceptions.** Implement `src/core/normalization.py` (strict `NormalizedRequest` and `NormalizedResponse` models that strip unsupported params like `temperature`). Implement `src/core/exceptions.py` (custom hierarchy) and add global exception handlers in `src/api/main.py` to format them into OpenAI-compatible JSON.
- [ ] **T006: Database Schema & Engine.** Implement `src/db/schema.py` using SQLAlchemy Core (no ORM) for `models` and `request_logs` tables. Initialize Alembic. **Crucial:** Extract the database initialization and pragma enforcement into a standalone, testable function (e.g., `init_db_engine()`) that explicitly enforces WAL pragmas and raises an exception if WAL mode cannot be enabled.
- [ ] **T007: ASGI Lifespan & Dependency Injection.** Implement the FastAPI lifespan in `src/api/main.py` to call the standalone `init_db_engine()`, initialize the `httpx.AsyncClient` singleton, and attach singletons to `app.state`. Implement hybrid dependency getters in `src/api/routes.py`.
- [ ] **T008: Chat Completions Endpoint.** Implement `POST /v1/chat/completions` in `src/api/routes.py`. **Crucial:** The check to validate the requested model against the configuration must happen *inside this FastAPI route handler* before invoking the adapter. Apply ingress normalization, invoke a placeholder/mock adapter, and return the egress-normalized OpenAI response.
- [ ] **T009: Ingress & Routing Tests.** Write tests verifying that unsupported parameters are stripped, empty message arrays are rejected (Scenarios 5 & 6), exception handlers return correct JSON structures, and the route correctly rejects unsupported models (Scenario 7).

**Verification & Stop:**

1. Run `pytest tests/test_normalization.py tests/test_db.py tests/test_lifespan.py tests/test_routes.py`.
2. Verify the database initialization tests pass, including the failure case for WAL enforcement (Scenario 4).
3. Verify route tests pass for empty messages and unsupported models.
4. **STOP**, wait for human review, and use `gh` to mark Issue #2 as complete.

---

## Batch 3: Provider Adapter

**Maps to GitHub Issue:** `#3 Google Adapter Implementation`

- [ ] **T010: Adapter Protocol.** Implement `src/providers/base.py` defining the abstract `ProviderAdapter` interface protocol. It must take a `NormalizedRequest` and `httpx.AsyncClient`, and return a `NormalizedResponse`.
- [ ] **T011: Google Adapter.** Implement `src/providers/google/adapter.py`. Map the internal request to the Gemini API format. **Crucial:** Wrap the initial provider connection and dispatch in a strict `asyncio.timeout(0.4)` block. Catch `httpx` errors and raise the corresponding custom router exceptions (`ProviderAuthError`, `ProviderServerError`, etc.).
- [ ] **T012: Adapter Tests.** Write `respx` unit tests for the Google adapter simulating deterministic responses: Success (200), Auth Error (401), Server Error (500), and Timeout (Scenarios 8, 9, & 10).

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
- [ ] **T016: CI Pipeline.** Create `.github/workflows/ci.yml` defining the GitHub Actions workflow. **Crucial:** Explicitly lock in `ruff` for linting/formatting and `mypy` for strict typing. The pipeline must run `ruff check`, `mypy`, the `pytest` suite, and a dry-run Docker build.

**Verification & Stop:**

1. Run `ruff check .`, `mypy src/`, and `pytest` locally.
2. Run `docker compose build` and `docker compose up` to verify the local dev workflow functions correctly and the app starts.
3. Verify the GitHub Actions workflow file syntax is valid.
4. **STOP**, wait for human review, and use `gh` to mark Issue #5 (and the parent Epic #1) as complete.
