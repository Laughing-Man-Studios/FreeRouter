# ADR-0002: Secrets Strictly via Environment Variables

**Date:** 2026-09-26
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

CONSTITUTION §9.2 states that provider API keys "must be loaded via environment variables **or uncommitted external configuration files (`config.yaml`)**", and forbids only hardcoded secrets in source control.

ROADMAP §3.15 goes further and shows a conceptual configuration shape in which keys are written as environment variable references inside `config.yaml`:

```yaml
providers:
  google:
    keys:
      - ${GOOGLE_KEY_1}
```

GitHub Issue #4 inherited that idea and made "environment variable interpolation for API keys" an acceptance criterion.

Three problems arise with resolving secrets from `config.yaml`:

1. **The protection is weaker than it appears.** Whether a key is "committed" depends on a `.gitignore` entry and on every contributor's discipline, not on anything the program enforces. A single `git add -f` leaks the key permanently.
2. **It multiplies the secret's surface.** A key then exists in the process environment *and* on disk in a file that tooling, editors, backups, and container image layers all touch.
3. **It couples secrets to the config schema.** The loader grows an interpolation language, and every new secret needs both a schema field and a placeholder convention.

The specification for M0 (`specs/001-project-bootstrap/spec.md` §6) resolves this in the stricter direction: "API keys must strictly be loaded from environment variables (e.g., `GEMINI_API_KEY`). Hardcoding secrets or committing them to `config.yaml` is strictly forbidden."

## 2. Decision

Secrets are read from the process environment only. The loader **rejects** a `config.yaml` that would put a secret into configuration, and it fails fast at startup when it finds one.

Specifically:

- A provider key is supplied through an environment variable, e.g. `GEMINI_API_KEY`. An uncommitted local `.env` file may supply it for developer convenience, but it is not an alternative source of truth and is never tracked.
- `config.yaml` carries non-secret configuration only: model identifiers and their provider mapping.
- The configuration loader raises a configuration error, and the process refuses to start, when `config.yaml`:
  - references a secret through a `${VAR}` placeholder, or
  - defines a secret-named field (`gemini_api_key`, `api_key`, `api_keys`, `keys`, `token`, `secret`, `password`).
- The key is held as a `SecretStr`, so it cannot appear in a `repr()`, a serialised configuration, or an incidental log line.
- Configuration validation errors report field *locations* and messages only, never field *values*, so a malformed secret is never echoed into a log or a crash message.

## 3. Relationship to CONSTITUTION §9.2

This ADR deliberately **narrows** an option that §9.2 permits: keys in an uncommitted `config.yaml` are no longer supported. The change is a restriction, not a relaxation, so it remains compliant with the Constitution, which forbids hardcoded secrets in source control and requires that keys be loaded from the environment or an uncommitted file. CONSTITUTION §9.2 is not amended; this ADR records why the permitted second option was declined.

## 4. Consequences

### Positive

- **Enforced rather than conventional.** Whether a key can reach `config.yaml` is decided by the program, not by `.gitignore` hygiene.
- **Single location for secrets.** A key lives in the process environment, which is also what the Docker runtime and CI inject.
- **Simpler loader.** No interpolation language, and one less failure mode to reason about.
- **Consistent with the specification.** The M0 spec, this ADR, and the implementation now agree.

### Negative / Trade-offs

- **Slightly less ergonomic for local setup.** A developer must export variables or create an uncommitted `.env`. This is mitigated by shipping `.env.example`.
- **An operator who prefers a gitignored key file must use `.env`** rather than a key section inside `config.yaml`. The mechanism still exists; only its location changed.
- **Rejects a plausible configuration style.** A future milestone that wants multiple named keys per provider must introduce a deliberate, reviewed mechanism rather than inheriting `${VAR}` interpolation by default.

## 5. Constitutional Compliance Check

| Constitution Directive | Compliance Validation |
| :--- | :--- |
| **9.2 Key Management** | **Pass:** Keys are loaded via environment variables. The uncommitted-file option is declined by choice, not by omission. |
| **2.3 Incremental Complexity** | **Pass:** No interpolation language is introduced before a milestone requires it. |
| **§6 Configuration & Validation (spec)** | **Pass:** The loader rejects secret material in `config.yaml` and fails fast. |
