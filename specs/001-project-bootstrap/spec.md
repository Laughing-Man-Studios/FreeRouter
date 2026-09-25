# Technical Prototype (M0)

## Purpose

To prove the core abstraction of the Free LLM Router works with the minimum amount of code. This milestone establishes the FastAPI ingress, strict internal request/response normalization, a single Google provider adapter, structured logging, the Docker development/runtime workflow, and verifiable basic OpenAI response compatibility for a single model (`gemini-3.5-flash-lite`).

## Scope

- Implement the primary `POST /v1/chat/completions` OpenAI-compatible endpoint.
- Map exactly one model: `gemini-3.5-flash-lite` (exposed externally as `google/gemini-3.5-flash-lite`).
- Define and implement strict internal Pydantic normalized request/response types.
- Implement basic payload normalization limited strictly to `model` and `messages`.
- Implement a single Google (Gemini) provider adapter.
- Establish a minimal YAML configuration mapping with strict Pydantic validation.
- Establish the multi-stage Docker development/runtime workflow using Python 3.13 and `uvloop`.
- Implement the ASGI lifespan to initialize an `httpx.AsyncClient` singleton and execute Alembic migrations for an embedded SQLite database in WAL mode.
- Add basic structured JSON logging.
- Create the first unit-test suite around normalization, configuration, and error mapping.

## Requirements

### 1. API Ingress

The router must expose a FastAPI endpoint at `POST /v1/chat/completions`.

### 2. Internal Normalization Boundary (Architectural Mandate)

- The router must define strict internal Pydantic models: `NormalizedRequest` and `NormalizedResponse`.
- The ingress layer must translate the incoming OpenAI payload into a `NormalizedRequest`.
- The provider adapter must translate the provider's native response into a `NormalizedResponse`.
- The Router Core must only interact with these internal normalized models and possess zero awareness of provider-specific schemas.

### 3. Payload Normalization

- The ingress normalizer must parse only the `model` and `messages` properties from the incoming OpenAI-style request.
- Other parameters (e.g., `temperature`, `max_tokens`, `top_p`) must be explicitly stripped and ignored for M0.

### 4. Provider Translation

- The Google adapter must translate the internal `NormalizedRequest` into the native payload format expected by the Google Gemini API for `gemini-3.5-flash-lite`.
- The adapter must translate the successful native Google response into the internal `NormalizedResponse`.

### 5. Response Normalization

The final response layer must map the internal `NormalizedResponse` back to a standard OpenAI-compatible JSON completion object.

### 6. Configuration & Validation

- The system must load a minimal `config.yaml` to register the available model.
- **Pydantic Validation:** All configuration parsing, including the injection of environment variables for secrets, must be validated via Pydantic models at startup.
- API keys must strictly be loaded from environment variables (e.g., `GEMINI_API_KEY`). Hardcoding secrets or committing them to `config.yaml` is strictly forbidden.
- **Fail Fast:** The router must refuse to start and exit immediately if the `config.yaml` is invalid, if Pydantic validation fails, or if the required Google API key environment variable is missing.

### 7. Docker & Runtime Workflow

- The project must include a multi-stage `Dockerfile`.
- The build stage must use `uv` for dependency resolution.
- The runtime stage must strictly use `python:3.13-slim` (backward compatibility with 3.12 is explicitly dropped), run as a non-root user, utilize `uvloop` as the event loop, and include a Docker `HEALTHCHECK`.

### 8. Outbound Networking & Lifespan Management

- The ASGI startup/shutdown lifecycle must explicitly manage the creation and closure of a single `httpx.AsyncClient` singleton.
- The initial provider connection and response header phase must be wrapped in a strict `asyncio.timeout(0.4)` window to guarantee the <500 ms routing envelope.
- The ASGI lifespan must initialize an embedded SQLite database using the `aiosqlite` async driver and enforce Write-Ahead Logging (WAL) via pragmas. The application must refuse to start if WAL mode cannot be enabled.
- Alembic migrations must run automatically at container startup before the app begins accepting traffic.

### 9. Structured Logging

- The application must emit structured JSON logs for operational events (e.g., request received, provider dispatched, response returned).
- Prompt and completion contents must strictly be excluded from these logs.

### 10. HTTP Status Code Mapping

The router must map provider-side errors to strict OpenAI-compatible HTTP conventions:

- Provider `401 Unauthorized` (Bad API Key) → Router returns `401`.
- Provider `400 Bad Request` (Invalid Payload) → Router returns `400`.
- Provider `5xx` (Upstream Server Error) → Router returns `502 Bad Gateway`.

## Behavioral Rules

- **Explicit Model Selection:** The router must strictly match the `model` string requested by the client (e.g., `google/gemini-3.5-flash-lite`) to the configuration.
- **No Automatic Fallback:** If the exact requested model is not found, or if the provider API fails, the router must fail gracefully and return an error. It must not attempt to route to a different model.
- **Strict Privacy:** Message contents (prompt and completion text) must strictly be excluded from standard operational logs.

## Acceptance Scenarios

### Scenario 1: Successful Chat Completion

- **Given** the router is running with a valid `GEMINI_API_KEY` and valid `config.yaml`
- **And** the configuration maps `google/gemini-3.5-flash-lite` to the Google provider
- **When** a valid OpenAI-style `POST /v1/chat/completions` request is received for `model: "google/gemini-3.5-flash-lite"` with a valid `messages` array
- **Then** the ingress normalizer creates a `NormalizedRequest`
- **And** the adapter translates it and sends it to the Google API
- **And** the adapter translates the response into a `NormalizedResponse`
- **And** the router returns an HTTP 200 with an OpenAI-compatible JSON completion object.

### Scenario 2: Missing API Key on Startup

- **Given** the `GEMINI_API_KEY` environment variable is not set
- **When** the Uvicorn application process attempts to start
- **Then** the Pydantic configuration validator fails
- **And** the application logs a fatal error and immediately exits before accepting traffic.

### Scenario 3: Invalid Configuration Schema on Startup

- **Given** the `config.yaml` file is missing a required field or contains an invalid data type
- **When** the Uvicorn application process attempts to start
- **Then** the Pydantic configuration validator raises an exception
- **And** the application logs the validation error and immediately exits.

### Scenario 4: Database Initialization Failure on Startup

- **Given** the embedded SQLite database cannot enable WAL mode
- **When** the Uvicorn application process attempts to start
- **Then** the ASGI lifespan initialization fails
- **And** the application logs a fatal error and immediately exits before accepting traffic.

### Scenario 5: Normalizer Strips Unsupported Parameters

- **Given** the router is running and accepting traffic
- **When** a request is received containing `model`, `messages`, `temperature: 0.7`, and `max_tokens: 100`
- **Then** the ingress normalizer creates a `NormalizedRequest` containing only `model` and `messages`
- **And** the `temperature` and `max_tokens` parameters are completely discarded and not passed to the provider adapter.

### Scenario 6: Empty Messages Array

- **Given** the router is running and accepting traffic
- **When** a request is received with an empty `messages` array (`[]`)
- **Then** the router must reject the request before dispatching it to the provider
- **And** the router must return an HTTP 400 with a strict OpenAI-compatible error JSON object.

### Scenario 7: Unsupported Model Requested

- **Given** the router is configured only for `google/gemini-3.5-flash-lite`
- **When** a request is received for `model: "gpt-4"`
- **Then** the router must return an HTTP 400 or 404 with a strict OpenAI-compatible error JSON object.

### Scenario 8: Provider Auth Error Mapping

- **Given** the router dispatches a request to the Google API
- **And** the Google API returns an HTTP 401 Unauthorized (e.g., invalid API key)
- **When** the adapter processes the error
- **Then** the router must return an HTTP 401 to the client
- **And** the response body must be a strict OpenAI-compatible error JSON object.

### Scenario 9: Provider Server Error Mapping

- **Given** the router dispatches a request to the Google API
- **And** the Google API returns an HTTP 500 Internal Server Error
- **When** the adapter processes the error
- **Then** the router must return an HTTP 502 Bad Gateway to the client
- **And** the response body must be a strict OpenAI-compatible error JSON object.

### Scenario 10: Provider Timeout Exceeded

- **Given** the router dispatches a request to the Google API
- **And** the provider fails to respond within the strict 400ms `asyncio.timeout` window
- **When** the adapter catches the timeout exception
- **Then** the router must return an HTTP 504 Gateway Timeout or 502 Bad Gateway to the client.

## Edge Cases

- **Missing API Keys:** Must be caught at startup via Pydantic validation rather than failing at request time.
- **Empty Message Arrays:** Must be caught by the ingress normalizer and returned as an OpenAI-compatible error without making an outbound API call.
- **Malformed JSON Payload:** FastAPI/Pydantic must catch this at the ingress layer and return a standard 422 or 400 OpenAI-compatible error.

## Error Behavior

All provider-side failures (e.g., Google 400, 401, or 500 errors) or local validation failures (e.g., empty messages, unknown model) must bypass standard FastAPI HTML/text exception pages and strictly return an OpenAI-compatible error payload with the correct HTTP status code mapping:

```json
{
  "error": {
    "message": "Description of the error",
    "type": "invalid_request_error",
    "param": null,
    "code": "specific_error_code"
  }
}
```

## Security / Privacy Considerations

- **No Hardcoded Secrets:** Provider API keys must be injected via environment variables. Hardcoding secrets or committing them to `config.yaml` is strictly forbidden.
- **No Payload Logging:** Prompt text, system instructions, and completion contents must never be recorded in standard operational logs.
- **Container Security:** The Docker runtime image must not run as the root user.

## Explicit Non-Goals for M0

- Streaming support (deferred to M2).
- Actual quota tracking state management (deferred to M1/M2). *(Note: Database shell initialization is implemented, but token reservation/reconciliation logic is deferred).*
- Multi-key pooling or intelligent failover (deferred to M1).
- Tool calling / function calling.
- Advanced routing policies (`model = free`).
- Dynamic provider discovery or `/v1/models` endpoint (deferred to M2).
- Multi-worker scaling or process managers (e.g., Gunicorn).

## Minimal Configuration Structure (`config.yaml`)

```yaml
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
```
