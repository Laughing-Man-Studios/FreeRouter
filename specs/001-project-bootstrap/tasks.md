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
> `metadata.create_all`, which suffices for a single revision. A versioned migration arrived as
> **T017** in Batch 4, once the Docker workflow made the add-only limitation a real risk; see that
> entry for how the adoption path works. `ROUTER_DB_PATH` was added as a configuration env var to
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

> **Note (T012 gap):** All M0 scenarios are proven against `respx`, including both timeout
> directions. **No request has been made against the live Gemini API**, because that needs a real
> `GEMINI_API_KEY`. This is a real risk rather than deferred polish: a wrong request field name would
> pass every mock test and fail live. M0's exit criterion is literally a successful live round trip,
> so a single manual call is tracked as a Batch 4 closing step.

---

## Batch 4: Test Suite Finalization & Docker/CI Setup

**Maps to GitHub Issue:** `#5 Unit Test Suite & Docker Development Environment Setup`

**Carry-over from earlier batches, tracked here so all M0 work lives in one place:**

- [x] **T013: End-to-End Integration Tests.** Write comprehensive tests for the full ingress-to-egress routing flow using FastAPI's `TestClient` and `respx` to mock the external Google API at the HTTP boundary. Cover a successful chat completion (Scenario 1) and verify the full error mapping pipeline.
- [x] **T014: Dockerfile.** Create a multi-stage `Dockerfile` using `uv` for dependency resolution. The runtime stage must strictly use `python:3.13-slim`, run as a non-root user, utilize `uvloop`, and include a Docker `HEALTHCHECK`.
- [x] **T015: Docker Compose.** Create a `docker-compose.yml` file for local development orchestration. It should handle mounting the SQLite data volume and passing the `.env` variables (like `GEMINI_API_KEY`) to the container.
- [x] **T016: Migrate `db/engine.py` to `aiosqlite`.** Replace the synchronous `pysqlite` engine with `sqlite+aiosqlite`, applying pragmas and verifying WAL through an async connection. **Why:** CONSTITUTION §3.1 and spec §8 both require the `aiosqlite` async driver for the MVP; the current synchronous engine is a known divergence, scheduled for correction *before* T014 so the container is not built against a non-compliant database layer. `check_same_thread=False` becomes unnecessary. **Trigger to move earlier:** any request-path write to `request_logs`, since a synchronous write inside an async handler blocks every other in-flight request. `_enforce_wal` is already driver-agnostic and is the part that must be preserved.
- [x] **T017: Reintroduce Alembic when a second revision exists.** Initialise Alembic with an initial migration and run migrations at container startup per spec §8 and ADR-0001. **Why deferred:** `alembic` is not a declared dependency while `metadata.create_all` suffices for a single revision. `create_all` only ever *adds* tables and columns — it never alters or drops them — so a container starting against an older volume can silently disagree with the code. **Trigger:** a second schema revision, or T014 shipping an image that persists a volume across upgrades.
- [x] **T018: CI Pipeline.** Create `.github/workflows/ci.yml` defining the GitHub Actions workflow, wiring in the `ruff` and `mypy` (strict) configuration established in T001. **Crucial:** The pipeline must run `ruff check`, `mypy`, the `pytest` suite, and a dry-run Docker build.
- [ ] **T019: Live Gemini round trip.** Send one real `POST /v1/chat/completions` request through the containerised router to `google/gemini-3.5-flash-lite` using a valid `GEMINI_API_KEY`, and confirm a normalized OpenAI response. **Why:** every scenario is currently proven only against `respx`; a wrong request field name would pass all mock tests and fail here. This is M0's literal exit criterion. Also confirm the shipped `docker-compose.yml` workflow starts cleanly with `docker compose up`.

**Suggested order:** T016 → T017 → T013 → T014 → T015 → T018 → T019. The database layer is corrected before the container is built, so the image is not built against a knowingly non-compliant database.

> **Completed in T016 (Batch 4):** the engine is now `sqlite+aiosqlite`, closing the CONSTITUTION §3.1
> and spec §8 divergence. `init_db_engine` is a coroutine returning an `AsyncEngine`; pragmas, WAL
> enforcement, and schema work run through `conn.run_sync`. `_enforce_wal` kept its original logic
> verbatim, including re-reading the live journal mode, because SQLite does not raise when WAL cannot
> be enabled — that re-read is the only thing turning silent degradation into a startup failure.
> `check_same_thread=False` was removed as it guarded nothing under an async driver. `journal_mode_of`
> stays synchronous on raw `sqlite3` so it verifies the file independently of the engine.
>
> **One dependency surprise:** `sqlalchemy[asyncio]` is required, not plain `sqlalchemy`. The async
> layer imports `greenlet`, so declaring `aiosqlite` alone is insufficient — `create_async_engine`
> fails at import. Recorded in `pyproject.toml` so the extra is not "simplified" away.
>
> Verified beyond unit tests: Scenario 4 still aborts startup end to end through the async path; and
> 200 sequential writes took 48 ms while a concurrent 1 ms ticker ran 90 times, so the loop is not
> blocked. A blocking driver would have scored 0.

> **Completed in T017 (Batch 4):** Alembic initialised with revision `0001_initial`. Migrations run
> over a synchronous `pysqlite` connection before the async engine opens — the sync-to-async handoff
> `plan.md` describes. There is no reason to carry an async migration toolchain for a startup-only
> step that must finish before traffic is accepted, and both drivers open the same file. The URL is
> passed programmatically so the migration target cannot drift from the engine's.
>
> A database with user tables but no `alembic_version` is **stamped at head rather than upgraded**.
> That is the adoption path for a volume written before migrations existed: its schema can only be
> the initial revision, and a plain upgrade fails trying to create tables that already exist. Without
> this, upgrading an existing `docker compose` volume would break startup.
>
> `env.py` uses `render_as_batch` throughout, since SQLite cannot `ALTER` most things in place.

> **Completed in T013 (Batch 4):** 35 integration tests exercising the seams between layers — an
> OpenAI body in, a Gemini request on the wire, a normalized response back, an OpenAI completion out.
> They found **two real bugs**, both invisible to the layer-level suites:
>
> - **A provider 400 was returning 502.** `PROVIDER_ERROR_STATUS_MAP` had no entry for
>   `ProviderValidationError`, and that class does not subclass `ValidationError`, so `_status_for`
>   fell through to the generic `ProviderBaseError` branch. Spec §10 requires 400. The map is now
>   ordered most-specific-first and says so, because its failure mode is silent: a missing entry
>   inherits a neighbour's status rather than raising.
> - **Transport errors escaped as 500.** The adapter caught `httpx.TimeoutException` but not
>   `httpx.HTTPError` broadly, so a connection failure (refused, DNS, TLS, reset) bypassed status
>   mapping and reached the generic handler as a 500 `unhandled_error` — misattributing a network
>   problem to the router itself. `TimeoutException` is now matched ahead of `HTTPError`, since the
>   former subclasses the latter and must map to 504.
>
> Two tests exist purely to prevent regressions of that class:
> `test_no_exception_is_missing_from_the_status_map` asserts every concrete `RouterBaseError`
> declares a status, and `test_database_is_ready_before_traffic_is_served` reads the schema through
> plain `sqlite3` so it proves migrations are committed on disk. Test count: 108 → 143.

> **Completed in T014 (Batch 4):** multi-stage `uv` build onto `python:3.13-slim`, non-root
> (uid 10001), `uvloop` named explicitly on the `uvicorn` command line, plus a Docker `HEALTHCHECK`.
>
> **A subtask was added: `GET /health`.** Spec §7 mandates a healthcheck and there was nothing to point
> it at. It is deliberately trivial and does **not** check provider reachability: Docker restarts on
> unhealthy, so a probe that failed on a transient upstream outage would cause restart loops against a
> router working as intended. It sits at `/health`, not under `/v1`, so it is not advertised as part of
> the OpenAI-compatible surface.
>
> Two build failures were hit, both of which would have shipped a broken image:
>
> - `COPY --from=builder /app/config.yaml` failed — only `pyproject.toml`, `uv.lock`, `README.md`, and
>   `src/` are copied into the builder. The config holds no secrets (spec §6), so it is copied from the
>   build context instead.
> - The container started then died with `ModuleNotFoundError: free_router`. `uv sync` installs the
>   project as a `.pth` pointing at `/app/src`, which only resolves because the source happens to be
>   in the builder image. Copying only the venv discards `/app/src`. Fixed with `--no-editable`, which
>   makes it a real install that survives the venv copy.
>
> Verified by building and running, not assumed: runs as uid 10001; `/health` returns 200 and the
> container reports `healthy`; migrations run on a fresh volume and are **skipped** on restart; the
> database lands in the mounted volume with WAL active; `uvloop` is the configured loop. Runtime image
> is 300MB, of which the venv is 74MB.

> **Completed in T018 (Batch 4):** `.github/workflows/ci.yml` with six jobs. Each mirrors a local
> command, so a green run means the same checks pass on a developer machine:
>
> - `lint` — `ruff check` and `ruff format --check`, kept as separate steps so a failure names which
>   half disagreed
> - `typecheck` — `mypy --strict src/`. Strict is already set in `pyproject.toml`; the flag is
>   repeated so a future config change cannot silently relax CI
> - `test` — `pytest`, with **no** `GEMINI_API_KEY` set. A follow-up step asserts the variable is
>   absent, which is what proves the suite is fully mocked and cannot reach a provider
> - `docker` — builds the image, then asserts the contract mechanically: a non-empty `User` that is
>   not root, a `HEALTHCHECK` present, and Python 3.13 at runtime. It then runs the container and
>   polls `/health` for up to 30 s rather than sleeping a fixed interval, so a slow runner does not
>   produce a flaky failure
> - `markdown` — `markdownlint-cli2`, using the existing `.markdownlint-cli2.yaml`
> - `security` — `pip-audit`, installed standalone rather than added as a project dependency. It is
>   **advisory, not a gate**: `pip-audit` exits 1 on any finding, so the result is captured and
>   surfaced as a warning instead of failing the job
>
> `pr-review.yml` was **replaced**, not extended. Both prior actions are gone (`cirolini/genai-code-review@v3`,
> `qodo-ai/pr-agent@main`); a single `Laughing-Man-Studios/FreeReview@v1` job replaces them, kept
> separate from `ci.yml` because it is advisory while `ci.yml` is the actual gate.
>
> Three details there are load-bearing, taken from the action's own `action.yml` rather than guessed:
>
> - `github_token` is wired explicitly to `${{ github.token }}`. GitHub does not expose `GITHUB_TOKEN`
>   to an action invoked with `uses:`, so without it the action can read the pull request but cannot
>   post the review.
> - `max_output_tokens: 4000`. Lowering it does not save money — the binding constraint is requests
>   per day, and a `:free` endpoint prices at zero per token. At 1500 the bundled reasoning model
>   returned no content on 4 of 4 attempts because it spends the budget reasoning before answering.
> - `privacy_mode: strict` stays on, sending `provider.zdr=true` so source code is not retained by a
>   provider.
>
> A bot-sender guard is carried over from the old `pr_agent_job`. An agent that opens a pull request
> would otherwise trigger a review of its own work, which is circular and spends quota.
>
> **Behaviour change worth noting:** the new action never approves, blocks, or fails a build, so
> merging is no longer gated on review completion. That is the point of adopting it, but it does mean
> a cancelled or failing review no longer shows as a red check.

> **Completed in T015 (Batch 4):** compose stack using a **named volume**, not a bind mount. The
> container runs as uid 10001 and a bind mount inherits host directory ownership, so a non-root
> container cannot write to it unless the host directory happens to match. The bind-mount case was
> tested and only worked because the test host directory was already owned by the invoking user — it
> would fail on a host that is not.
>
> The port is bound to `127.0.0.1` only. The MVP has no authentication layer (CONSTITUTION §3.4), so
> publishing on all interfaces would expose an unauthenticated proxy to the network. `GEMINI_API_KEY`
> is required at interpolation time, so `docker compose up` fails with a clear message instead of
> starting a container that will crash on startup.
>
> A live request through the container reached the real Gemini API and returned
> `provider_validation_error` / 400 — the pipeline works end to end and the provider genuinely rejected
> the placeholder key. T019 remains open with a real key.

**Verification & Stop:**

1. Run `ruff check .`, `mypy src/`, and `pytest` locally.
2. Run `docker compose build` and `docker compose up` to verify the local dev workflow functions correctly and the app starts.
3. Verify the GitHub Actions workflow file syntax is valid.
4. **STOP**, wait for human review, and use `gh` to mark Issue #5 (and the parent Epic #1) as complete.
