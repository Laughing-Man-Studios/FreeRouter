# ADR-0008: Capability Metadata Is Hand-Maintained, Provenance-Tagged, and Missing Data Is Disqualifying

**Date:** 2026-11-03
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

ROADMAP §3.7 lists the capabilities a model may need to declare — context window,
streaming, tool calling, vision/multimodal input, structured output — and §3.6 asks
that a request never be sent to a model known to be incapable of accepting it.

Both assume a source of truth that does not exist yet. `ModelConfig` today carries
`id`, `provider`, `provider_id`, `cooldown_seconds`, `quota` and `key_aliases`:
everything needed to *reach* a model and nothing about what it can *do*. The
router's only capability knowledge is structural — `quota` declares what the free
tier allows, which is a capacity fact, not a capability one.

Three questions have to be answered before any filter can be written:

1. **Where do capability values come from?** Hand-maintained configuration, or
   discovery from the provider's own model listing?
2. **How is "verified" recorded?** Issue #17 requires "verified capability specs",
   which is unverifiable unless the config says how each value was established.
3. **What does missing data mean?** A model with no declared capability could be
   treated as capable (permissive) or incapable (disqualifying).

## 2. Decision

**Capability metadata is hand-maintained configuration, every value carries a
provenance, and missing or assumed data is disqualifying.**

- **Configuration is the source of truth for M3.** No discovery call is made, at
  startup or at request time. The schema is shaped so a discovery source could later
  populate the same fields, but no such source exists yet.
- **Each capability is an object**: `value`, `provenance` (`published` | `measured`
  | `assumed`), and `source`. `published` and `measured` require a `source` naming
  the document and date, or the measurement; `assumed` refuses one.
- **`assumed` means unusable.** The filter treats an assumed capability as unknown,
  so it can never satisfy a hard requirement. The label exists so a guess can be
  recorded without becoming a guarantee.
- **Missing is ineligible.** A model with no capability block fails at startup; a
  model with a capability that cannot be resolved for a requirement fails that
  requirement.
- **The schema is closed.** Unknown capability keys are refused, not ignored.

## 3. Reasoning

**Hand-maintained beats discovery at this scale, for a reason that is about
correctness rather than effort.** The router configures two providers and a handful
of models. A discovery call would add a startup dependency on a provider being
reachable and would produce a second source of truth for facts that also live in
config — the two drift the first time a provider changes a listing without changing
behaviour. M2 already took this position for `/v1/models`, where the listing is a
projection of configuration rather than a cached discovery result; capability data
is the same kind of fact.

Discovery also would not answer question 2. A provider's model listing is a
*published* claim — it says what the provider intends, not what was observed — so it
would still need provenance to be trustworthy, and it would still be one provider's
word. Making it the source of truth would upgrade "the provider says so" into "we
verified it", which is a distinction the whole milestone rests on.

**Provenance is the difference between a checked value and a guessed one.**
`context_window: 1000000` in a config file is a claim with no author, no date and no
evidence. It could be copied from a documentation page, remembered from another
model, or invented to make startup succeed. Once the filter makes that number a
routing decision, those three cases behave identically and are indistinguishable —
which is precisely the failure that makes capability filters untrustworthy in
practice.

Recording `provenance` and `source` costs one line per capability and converts the
claim into an auditable one. It also mirrors `quota.provenance`, which already
distinguishes `measured` from `published` for the same reason: an unverified limit is
omitted rather than declared, and the two metadata families should read the same way.

**`assumed` exists because the alternative is a lie in one direction or the other.**
Without it, an operator who has a reasonable belief and no documentation has two bad
options: omit the field (and fail startup) or label the belief `published` (and
launder a guess as fact). A third label that says "recorded, not trusted" keeps the
information without granting it authority.

**Fail-closed follows from CONSTITUTION §7.1 rather than from preference.** The rule
is *"if a request requires capabilities ... that no eligible model supports, the
router must strictly reject"*. With permissive semantics the router cannot tell "no
model supports tools" from "nobody has asked yet", so it dispatches and finds out —
converting a local decision into a spent request against a scarce free-tier quota.
On a metered product that would be a cost argument; on a free tier the request
itself is the scarce resource.

**A closed schema is the anti-silent-degradation rule applied to configuration.** A
capability key that is misspelled (`support_tools`) would, under an open schema, be
accepted and ignored — the model would be silently treated as not declaring the
capability, or worse, the operator would believe they had declared it. §7.1 forbids
silent capability downgrade at request time; refusing the typo at startup is the
same rule applied earlier, where it is cheap.

## 4. Alternatives Considered

**Provider discovery as the source of truth.** Rejected. Adds a startup dependency,
creates two sources of truth, and still cannot distinguish published from verified.
Deferred as a possible *additional* source behind the same provenance fields.

**Open mapping of capability name to value** (`dict[str, bool | int]`). Rejected. It
cannot type-check `context_window` as an integer under `mypy --strict`, it accepts
typos, and it makes "what capabilities exist" a runtime question rather than a
reviewable one. Extensibility does not require an open schema; adding a field is
additive and cheap.

**A single provenance for the whole capability block.** Rejected. A model can have a
well-documented context window and an entirely unverified tool-support claim. One
label for the block would mark both as verified, which is exactly the confusion the
mechanism exists to prevent.

**Permissive default (absent means supported).** Rejected — see §3. It also has a
second failure: the router would send tool requests to models that cannot accept
them, the provider would reject or ignore the tools, and the client would receive an
answer that ignored its instructions. That is the defect M3 exists to remove.

**Infer capabilities from observed provider errors.** Rejected. It is
observation-based scoring, which CONSTITUTION §7.3 defers, and it makes a
deterministic decision depend on history that may be stale, incomplete or wrong. A
provider that rejects one oversized request does not prove its window.

## 5. Consequences

### Positive

- **The milestone's claim becomes checkable.** "Never sent to a model known incapable"
  is only meaningful if "known" means something; here it means declared with a
  provenance that is not `assumed`.
- **Configuration is the audit trail.** A reviewer can ask where a context window
  came from and get a document and a date.
- **Typos and omissions fail at startup**, naming the model and field, rather than
  producing a router that quietly routes wrongly.
- **The schema can accept a future discovery source** without a redesign, because the
  provenance fields already describe where a value came from.

### Negative / Trade-offs

- **Every model's block is verbose** — five objects where five scalars would read
  more naturally.
- **Operators must research every model they add.** Fail-closed makes a capability
  block mandatory, so adding a model is no longer a two-line change.
- **Capability data goes stale silently.** A provider that raises a context window
  leaves the config conservative until someone re-reads the documentation. The
  `source` date makes staleness visible on inspection, but nothing detects it
  automatically.
- **`assumed` is a soft failure mode.** An operator can mark everything `assumed`,
  start successfully, and have a router that refuses every gated request. It is
  visible (the refusals are loud and specific) but not prevented.

## 6. Verification

Discharged when:

1. Every capability in the shipped `config.yaml` carries a `provenance` and, where
   `published` or `measured`, a `source` with a date.
2. Startup fails, naming the model and field, for: a missing capability block, a
   `published` capability with no source, and an unknown capability key.
3. A model whose only claim to tool support is `assumed` is ineligible for a tools
   request (spec Scenario 5).
4. The capability block round-trips through YAML with types intact under
   `mypy --strict`.
