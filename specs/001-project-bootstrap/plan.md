# Implementation Plan: M0 Technical Prototype

## Technical Approach

Milestone 0 will establish the foundational architecture of the Free LLM Router using a single Uvicorn process running FastAPI on Python 3.13 with `uvloop`. The implementation will strictly enforce the architectural boundary between the external OpenAI-compatible API and internal routing logic using `NormalizedRequest` and `NormalizedResponse` Pydantic models.

Dependency management will rely exclusively on `uv`. Configuration will be loaded via a **manual YAML parser** that reads `config.yaml` and passes the resulting dictionary to Pydantic `BaseSettings`, allowing native environment variable overrides (e.g., `GEMINI_API_KEY`).

The ASGI lifespan will execute a **synchronous-to-asynchronous database handoff**: it will open a standard synchronous `sqlite3` connection strictly to run Alembic migrations, close it, and then initialize the `aiosqlite` engine with strict WAL pragmas for the async runtime.

Logging will use the standard library `logging` module equipped with a custom JSON formatter and **`contextvars`** (injected via FastAPI middleware) to attach request-scoped metadata (e.g., `request_id`, `model`, `latency`) without passing logger objects through the call stack, while explicitly dropping prompt and completion data.

Dependency injection will use a **hybrid approach**: singletons (`httpx.AsyncClient`, `Config`, `aiosqlite` engine) are created and stored on `app.state` during the lifespan, and exposed to route handlers via lightweight FastAPI `Depends()` getter functions.

## Components Affected

The project structure will be established under the `src/` directory with clear domain boundaries:

```text
src/
 ├── api/
 │   ├── main.py                # FastAPI app, ASGI lifespan (sync DB -> async DB), global exception handlers, contextvars middleware
 │   └── routes.py              # POST /v1/chat/completions ingress, uses Depends() for state access
 ├── core/
 │   ├── config.py              # Manual YAML loader + Pydantic BaseSettings models
 │   ├── exceptions.py          # Custom exception hierarchy (RouterBaseError, ProviderAuthError, ProviderTimeoutError, ProviderServerError, ValidationError)
 │   ├── logging.py             # Stdlib JSON logging configuration + contextvars filter
 │   └── normalization.py       # NormalizedRequest and NormalizedResponse strict Pydantic models
 ├── db/
 │   ├── engine.py              # aiosqlite initialization and WAL pragmas enforcement
 │   └── schema.py              # SQLAlchemy Core Table definitions (no ORM)
 ├── providers/
 │   ├── base.py                # Abstract adapter interface protocol
 │   └── google/
 │       └── adapter.py         # Gemini-specific translation, raises custom exceptions, enforces asyncio.timeout(0.4)
 └── migrations/                 # Alembic configuration and initial M0 migration script
```

*Note: Multi-stage `Dockerfile` and `.github/workflows/ci.yml` (linting, typing, testing, Docker build verification) are explicitly in scope for this milestone.*

## Interfaces

### Ingress & Egress

- **Ingress Layer**: Parses the incoming OpenAI payload. Extracts *only* `model` and `messages`. Silently discards `temperature`, `max_tokens`, and other unsupported parameters. Validates that `messages` is not empty. Produces a `NormalizedRequest`.
- **Egress Layer**: Converts the `NormalizedResponse` back into a standard OpenAI-compatible JSON completion object.

### Internal Normalization

- `NormalizedRequest`: Strict Pydantic model containing `model: str` and `messages: list[dict]`.
- `NormalizedResponse`: Strict Pydantic model containing `content: str`, `model_used: str`, and `finish_reason: str`.

### Provider Adapter

- `ProviderAdapter` Protocol: Defines `async def chat_completion(request: NormalizedRequest, client: httpx.AsyncClient) -> NormalizedResponse`.
- `GoogleAdapter`: Implements the protocol. Translates `NormalizedRequest` to Gemini API format. **Crucially**, the initial provider connection and response header phase must be wrapped in a strict `asyncio.timeout(0.4)` window. Catches `httpx` errors and raises the appropriate custom exception (e.g., `ProviderAuthError`, `ProviderTimeoutError`).

### Dependency Injection (Hybrid)

- `get_config(request: Request) -> Config`: Returns `request.app.state.config`
- `get_http_client(request: Request) -> httpx.AsyncClient`: Returns `request.app.state.http_client`
- `get_db_engine(request: Request) -> aiosqlite.Connection`: Returns `request.app.state.db_engine`

## Data / Persistence Changes

The embedded SQLite database will reside at a fixed path (e.g., `/data/router.db`). The ASGI lifespan will strictly follow this sequence:

1. Load and validate `config.yaml` + env vars. Fail fast if `GEMINI_API_KEY` is missing or schema is invalid.
2. Open a **synchronous** `sqlite3` connection.
3. Run Alembic migrations synchronously.
4. Close the synchronous connection.
5. Open an **asynchronous** `aiosqlite` connection.
6. Execute pragmas: `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;`. **The application must refuse to start and exit immediately if WAL mode cannot be enabled.**
7. Instantiate the `httpx.AsyncClient` singleton with aggressive timeouts: `connect=0.25`, `pool=0.05`, `write=1.0`, `read=10.0`.
8. Attach `config`, `db_engine`, and `http_client` to `app.state`.

The initial M0 Alembic migration will create two foundational tables using SQLAlchemy Core:

- `models`: Stores provider mapping configuration (e.g., matching `google/gemini-3.5-flash-lite` to the Google provider).
- `request_logs`: Stores basic metadata for operational auditing (timestamp, model requested, provider used, latency, HTTP status code), **strictly omitting** prompt and completion text.

## External Integrations

- **Google Gemini API**: Integration with the `gemini-3.5-flash-lite` model.
- **Authentication**: Handled strictly via the `GEMINI_API_KEY` environment variable.
- **Networking**: Managed by the globally shared `httpx.AsyncClient` singleton, instantiated and closed within the ASGI lifespan.

## Error Handling

FastAPI global exception handlers will be overridden to bypass default HTML/text pages and strictly return OpenAI-compatible JSON error payloads:

```json
{
  "error": {
    "message": "Description of the error",
    "type": "invalid_request_error", // or "api_error", "timeout_error"
    "param": null,
    "code": "specific_error_code"
  }
}
```

- **Startup Failures**: Invalid config, missing API key, or failed WAL initialization raise immediate exceptions, crashing the process before port binding.
- **Validation Errors (400)**: Empty message arrays or unsupported models caught by ingress, returning a 400 OpenAI-style error.
- **Provider Errors**: The adapter catches `httpx` errors and raises custom exceptions. Global handlers map them as follows:
  - `ProviderAuthError` (HTTP 401) → Router returns `401 Unauthorized`.
  - `ProviderValidationError` (HTTP 400) → Router returns `400 Bad Request`.
  - `ProviderServerError` (HTTP 5xx) → Router returns `502 Bad Gateway`.
  - `ProviderTimeoutError` (asyncio.TimeoutError) → Router returns `504 Gateway Timeout`.

## Testing Strategy

- **Configuration & Validation**: `pytest` suites will verify the manual YAML loader correctly merges with environment variables, and that the app fails fast on invalid schemas or missing `GEMINI_API_KEY`.
- **Normalization**: Unit tests will verify that unsupported OpenAI parameters (`temperature`, `max_tokens`) are stripped, and that empty message arrays are rejected with a 400 error.
- **Network Mocking**: The `respx` library will intercept `httpx` calls to simulate deterministic Google API responses:
  - Success (200) → Verify correct `NormalizedResponse` and OpenAI JSON egress.
  - Unauthorized (401) → Verify adapter raises `ProviderAuthError` and router returns 401.
  - Server Error (500) → Verify adapter raises `ProviderServerError` and router returns 502.
  - Timeout → Verify the `asyncio.timeout(0.4)` triggers, adapter raises `ProviderTimeoutError`, and router returns 504.
- **Database**: Verify that the synchronous migration succeeds and that the async engine enforces WAL mode (test will assert failure if WAL pragma is artificially blocked).

## Risks / Tradeoffs

- **Synchronous Migrations**: Running Alembic synchronously during the ASGI lifespan adds marginal container startup time. This is an accepted tradeoff for a single-node, private service to guarantee schema consistency before traffic is accepted.
- **Single Process Architecture**: A single Uvicorn process means CPU-bound tasks could block the event loop. Given M0's strictly I/O-bound nature, this risk is mitigated.
- **Manual YAML Loader**: Adds slight boilerplate compared to third-party config libraries, but guarantees strict, predictable Pydantic `BaseSettings` environment variable override behavior without hidden merging logic.

## Architectural Decisions

1. **Hybrid Dependency Injection**: Using `app.state` for lifecycle management guarantees clean startup initialization and graceful shutdown cleanup, while `Depends()` provides clean, testable route handler signatures.
2. **Sync-to-Async DB Handoff**: Using a standard `sqlite3` connection strictly for the Alembic migration phase avoids async Alembic complexity, while handing off to `aiosqlite` ensures optimal async performance during the app's runtime.
3. **Contextvars for Logging**: Utilizing `contextvars` allows request-scoped metadata injection into standard library `logging` records without polluting function signatures with custom logger objects.
4. **Custom Exception Hierarchy**: Decouples provider-specific `httpx` error handling from FastAPI response formatting, ensuring a single source of truth for OpenAI-compatible error mapping.
5. **Strict Timeout Envelope**: Enforcing `asyncio.timeout(0.4)` *inside* the Google Adapter's dispatch method guarantees the <500ms routing overhead target is respected at the network boundary, independent of the `httpx` read/write timeouts.
6. **M0 Scope Includes Docker/CI**: Bundling the multi-stage `uv` Dockerfile and CI pipeline into this milestone ensures the prototype is immediately verifiable in a reproducible, production-like environment from day one.
