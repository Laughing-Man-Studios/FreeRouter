# ADR-0003: Pooled API Key Configuration

**Date:** 2026-10-06
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)
**Supersedes:** nothing. **Constrained by:** ADR-0002 (secrets via environment variables)

## 1. Context

M0 supports exactly one credential per provider: `Config.gemini_api_key: SecretStr`. M1 requires several keys per provider/model so a rate-limited or cooling key can be avoided (#8).

ADR-0002 predicted this precisely. Its recorded trade-off states:

> "A future milestone that wants multiple named keys per provider must introduce a deliberate, reviewed mechanism rather than inheriting `${VAR}` interpolation by default."

That mechanism does not currently exist, and there is a concrete obstacle. ADR-0002 made the loader actively **reject** secret-shaped configuration, and ROADMAP §3.15's illustrative shape is exactly what it rejects:

```yaml
providers:
  google:
    keys:
      - ${GOOGLE_KEY_1}
```

`core/config.py` defines `_FORBIDDEN_YAML_KEYS` containing `api_key`, `api_keys`, `keys`, `gemini_api_key`, `token`, `secret`, and `password`. A `keys:` list is refused as a configuration error before validation runs.

So M1 must introduce multi-key support without weakening the guarantee that a secret cannot reach `config.yaml`.

A second constraint shapes the design: CONSTITUTION §6.1 caps pooled keys at **2 per installation** where a provider's terms prohibit pooling, and Issue #51 must determine whether that cap applies to Google, Mistral, or both.

## 2. Decision

`config.yaml` describes **key slots** by alias. Key **material** stays in the environment, addressed by a deterministic naming convention.

```yaml
models:
  - id: "google/gemini-3.5-flash-lite"
    provider: "google"
    provider_id: "gemini-3.5-flash-lite"
    key_aliases: ["primary", "secondary"]
```

```bash
# One environment variable per alias, per provider.
GOOGLE_KEY_PRIMARY=<secret>
GOOGLE_KEY_SECONDARY=<secret>
MISTRAL_KEY_PRIMARY=<secret>
```

The loader derives the variable name from the provider and alias rather than accepting one from configuration, so `config.yaml` cannot be used to make the router read an arbitrary environment variable.

### Rules

- A model may list one or more `key_aliases`. One key is the normal case and costs nothing to express.
- Aliases must be unique within a model. They are the only key identifier permitted in logs or the database.
- The number of aliases a model may declare is capped at the limit Issue #51 concludes, and the cap is **enforced by configuration validation** — exceeding it is a startup failure, not a warning.
- A declared alias with no corresponding environment variable is a startup failure naming the alias, never the value.
- An environment variable matching the convention but not declared in configuration is ignored, so an unrelated `GOOGLE_KEY_...` variable in a developer's shell cannot silently join the pool.
- `key_aliases` is added to the loader's vocabulary as a permitted structural field. It names slots, not secrets, and the ADR-0002 rejection of `keys`/`api_key`/`api_keys` is retained unchanged.

## 3. Reasoning

**Aliases separate identity from material.** The pool needs a stable identifier to rank by LRU, record cooldown against, and write to `request_logs`. That identifier must be loggable. A key cannot serve both roles without either leaking or becoming unrankable.

**Derived names prevent configuration from steering environment reads.** Letting `config.yaml` say `env: SOME_VAR` reintroduces the interpolation language ADR-0002 rejected, and makes the config file an indirect way to name secrets. Deriving `<PROVIDER>_KEY_<ALIAS>` keeps configuration declarative.

**An explicit allowlist beats discovery.** Requiring each alias to be declared means a stray environment variable cannot silently become a pooled key. Auto-discovery from the environment is more convenient and considerably harder to reason about when a key behaves unexpectedly.

**The cap is enforced, not documented.** CONSTITUTION §6.1's key limit exists to reduce account-suspension risk. A limit honoured by convention fails exactly when it is under pressure.

## 4. Alternatives Considered

**`${VAR}` placeholders in `config.yaml`** (ROADMAP §3.15's illustrative shape). Rejected by ADR-0002 and actively blocked by the loader. Restoring it would reverse an accepted ADR to gain ergonomics that `.env` already provides.

**Auto-discovery of `*_KEY_*` environment variables.** Rejected: the pool becomes invisible to configuration review, and an unrelated variable silently becomes a credential source.

**Secrets held in an encrypted local file.** Deferred. It is a plausible M6 direction if key management moves to an Admin API, but it adds a key-management problem to a milestone whose purpose is reliability.

**A single comma-separated environment variable** holding several keys. Rejected: positional identity is not stable enough to rank, log, or attach cooldown state to.

## 5. Consequences

### Positive

- Secrets never enter configuration, preserving ADR-0002.
- Aliases are safe to log, giving the key manager and `request_logs` a usable identifier.
- Adding a key is a configuration change; no code change.
- The §6.1 cap is testable.

### Negative / Trade-offs

- Alias-to-variable naming is a convention, not a schema. Renaming a provider means renaming variables.
- The number of keys is fixed at deployment. There is no runtime key management until M6.
- Requiring declaration makes bulk setup slightly more verbose than discovery would.
- Two providers with the same alias produce distinct variables by construction (`<PROVIDER>_KEY_<ALIAS>`), so no collision, but the names must be remembered per provider.

## 6. Constitutional Compliance Check

| Constitution Directive | Compliance Validation |
| :--- | :--- |
| **9.2 Key Management** | **Pass:** Key material is environment-only. `config.yaml` names slots, never secrets. ADR-0002's rejection of `${VAR}` and secret-named fields is retained. |
| **6.1 Conservative Compliance Mode** | **Pass:** The key cap is enforced by configuration validation, failing startup when exceeded. |
| **2.3 Incremental Complexity** | **Pass:** One naming convention and one new structural field. No secret store is introduced. |
| **9.1 Privacy by Default** | **Pass:** Only aliases are exposed to logs and persistence. |
