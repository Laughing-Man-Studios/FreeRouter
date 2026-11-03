# ADR-0009: Model Substitution Is Opt-In Per Model, Single-Hop, and Always Visible

**Date:** 2026-11-03
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

Two documents disagree about whether the router may serve a request from a model the
client did not name.

CONSTITUTION §7.2:

> "When a client explicitly requests a specific model identifier ... The router must
> honor the exact requested model. If the assigned API key returns a rate limit
> (`429`) or temporary error, failover is strictly constrained to other available
> keys for that identical model. Cross-model fallback is prohibited for explicit
> model requests."

ROADMAP §3.6, under *Later*:

> "Route oversized requests to a compatible larger-context model when routing policy
> allows model substitution."

ROADMAP Milestone 3 lists "large-context candidate selection" as a deliverable, and
its example selection flow removes incompatible models and then ranks the remainder
— which only makes sense if more than one candidate can be considered.

The tension is structural, not verbal. §7.2 protects a client that pinned a model
because it depends on that model's behaviour, cost or data handling. §3.6 describes
a client whose request *cannot be served at all* by the pinned model. Serving it
elsewhere is either helpful or a violation, depending on who decided.

Two further facts constrain the answer:

- `model = "free"` (CONSTITUTION §7.3) is the mechanism for router-controlled
  selection, and ROADMAP places it in **M4**. M3 has no router-controlled model.
- The dispatcher takes exactly one `(provider, model)` pair and never varies it
  across attempts — M1 made §7.2 structural rather than a guard.

## 2. Decision

**Substitution is opt-in per model, declared in configuration, off by default.**

- **`fallback_models` on a model is the opt-in.** Absent or empty means the requested
  model is the only candidate, and an unsatisfiable request is refused. This is the
  default for every model, including everything shipped.
- **Only capability and context mismatch trigger it.** A rate limit, cooldown or
  exhausted quota on the requested model keeps its existing outcome and never
  reaches a fallback — that is §7.2's explicit case and it does not change.
- **A fallback must itself be eligible**, judged by the same predicate as the
  requested model. The first eligible entry in declared order wins.
- **Single hop.** A fallback that declares its own `fallback_models` is refused at
  startup; chains are not traversed and not silently truncated.
- **The response names the model that served it.** `model` in the body is the
  effective model, and the substitution is logged with the requirement that forced
  it.
- **Substitution happens before dispatch.** The dispatcher still receives exactly one
  pair, so its single-pair invariant — and its retry behaviour — is untouched.

## 3. Reasoning

**"When routing policy allows" is an instruction to make policy explicit.** The
ROADMAP does not say the router may decide; it says substitution is available under a
policy. Reading that as "substitute whenever a capable model exists" would put the
decision in the router, which is what §7.2 forbids.

Per-model declaration is the smallest unit that keeps the decision with the operator.
A global flag would mean enabling a large-context fallback for one model changes the
risk profile of every model in the file; a per-model list keeps the blast radius
equal to the declaration.

**Capability mismatch and provider failure are different events, and only one of them
is about the model.** A rate limit says the model is momentarily unavailable, and
§7.2 confines the response to other credentials for that model. A capability mismatch
says the model can never serve this request — no credential, no retry and no amount of
waiting changes that. The two cases looking alike ("this model did not serve the
request") is exactly why the distinction has to be drawn in code: substitution that
fired on a `429` would be cross-model failover, and §7.2 names that as prohibited.

**Visibility is what makes substitution not a violation.** §7.1 prohibits silent
downgrading. A substitution the client cannot detect is a silent downgrade even when
the substitute is more capable: the client asked for one model and received another,
and the only signal it has is the `model` field. Reporting the effective model costs
nothing and converts a hidden decision into an explicit one — and it is why this
decision is defensible at all.

**Single hop is a predictability constraint.** With chains, the model that serves a
request depends on transitive configuration: a client pinned to A can be served by C
because B declared C. Two hops make the routing decision unreconstructable from the
request and the model's own entry, and the large-context case M3 solves needs exactly
one step (too small → big enough). Refusing a chain at startup rather than truncating
it means the config never claims something the router will not do.

**Doing it before dispatch is what keeps the dispatcher safe.** Substitution is a
routing decision from configuration and request shape, computed once. Failover is
behavioural — "this attempt failed, try another" — and varies the model mid-request.
Keeping substitution out of the dispatcher means M1's single-pair guarantee, retry
budget and key-failover logic stay exactly as measured.

## 4. Alternatives Considered

**Substitute whenever a capable model exists.** Rejected: cross-model fallback
without the client asking, which §7.2 prohibits. It also makes behaviour depend on the
whole model list, so adding a model silently changes routing for models already
configured.

**Never substitute in M3; always `400`.** Rejected, though it is the closest call.
It is simple and consistent, and it is what M3 does by default. It fails on the
ROADMAP: "large-context candidate selection" is an M3 deliverable, and declining to
build it while the roadmap lists it is the kind of quiet non-delivery this project
records as a deviation. It would also make `fallback_models` an M4 feature hiding in
an M4 document.

**A global `allow_substitution` boolean plus a single fallback list.** Rejected:
one switch for every model, and a fallback list that applies to whichever model is
asked — two knobs whose interaction is harder to predict than one list per model.

**Transparent substitution with no signal in the response.** Rejected outright. It is
silent degradation under §7.1, and it would make the router inconsistent with its own
error philosophy: it refuses clearly when it cannot serve a request, so it must not
quietly serve a different one.

**Fallback chains with cycle detection.** Rejected for M3: not needed for the
large-context case, and the reason a chain exists is usually a ranking policy, which
is M4's step 4. A config that needs two hops is a signal that the model list is being
used as a routing table, which §2.3 warns against.

**Substitute on provider errors as well.** Rejected: prohibited by §7.2 for rate
limits, and for other errors it would duplicate the existing bounded retry with a
different mechanism that also changes the model. Two recovery mechanisms for one
failure mode is how retry budgets become unpredictable.

## 5. Consequences

### Positive

- **§7.2 holds by default** — a `config.yaml` with no `fallback_models` behaves
  exactly as M2 does.
- **The ROADMAP §3.6 case is served**: an oversized request can reach a larger-context
  model, but only where an operator said so.
- **The decision is reconstructable.** Given a request and one model's entry, the
  serving model is determined; no transitive chains.
- **The dispatcher, retry budget and key failover are untouched**, so M2's measured
  behaviour is not re-opened by an M3 feature.

### Negative / Trade-offs

- **A client that pins a model and ignores the response `model` field can be
  surprised.** It asked for A and got B; only the body says so. This is why the
  feature is opt-in.
- **An operator can misconfigure a fallback that is worse for the client** — cheaper
  context, different data handling — and the router will use it. Configuration is
  trusted here; validating "compatible" beyond capability fit would require a policy
  model M3 does not have.
- **No ranking means declaration order is the policy.** If two fallbacks are
  eligible, the first wins regardless of quality, cost or context fit. M4's scoring
  will want to revisit this, and it should revisit the ordering rule *then*, not
  assume M3's ordering is a ranking.
- **Single hop cannot express a genuine three-tier list.** Accepted; a flat list of
  eligible fallbacks on the origin model already expresses "any of these", and the
  chain would only matter if intermediate models had their own preferences.

## 6. Verification

Discharged when:

1. Spec Scenario 6 passes: no `fallback_models` means a `400` and no substitution.
2. Spec Scenario 7 passes: a declared fallback serves an oversized request, the
   response `model` names the fallback, and an `INFO` line records the substitution.
3. Spec Scenario 8 passes: a cooldown on a model that declares fallbacks produces the
   M2 rate-limit outcome and never selects the fallback.
4. Spec Scenario 9 passes: an ineligible first fallback is skipped for an eligible
   second.
5. Startup refuses a `fallback_models` entry that is unknown, self-referential,
   duplicated, or that itself declares fallbacks.
6. `core/dispatch.py` is unchanged in the batch that implements this — asserted by
   the diff, because the decision is that substitution does not enter the dispatcher.
