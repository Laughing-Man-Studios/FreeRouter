# ADR-0010: Auto-Routing Uses Static Operator Priors, Hard Constraints Before Scoring, and Concentration

**Date:** 2026-11-16
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

CONSTITUTION §7.3 describes a router-controlled model: when a client sends
`model = "free"`, the router filters by capability and quota, scores what remains, and
"must maintain traffic sticky to the top-ranked eligible model until quota exhaustion
or capability mismatch forces a fallback to the next-best model". It also fixes the
source of the score: "initial model quality scoring must rely on static, manually
curated weights configured by the operator. Dynamic, observation-based quality scoring
... is deferred to later milestones."

ROADMAP §3.9 gives illustrative weights for five dimensions — `answer_quality` 0.50,
`tool_support` 0.15, `reliability` 0.15, `quota` 0.15, `latency` 0.05 — and states in
the same section that "these values are illustrative only" and that "the actual
defaults should be established once the first model registry exists".

The registry now exists (M3 added capability metadata and validation), so the defaults
have to be decided. Three things in the roadmap cannot all be true:

1. Priority 1: "Free/quota availability is a **hard constraint**."
2. The weight block: `quota: 0.15`.
3. The selection behaviour: concentrate on the best model rather than distribute.

A quota term in the score steers traffic toward whichever model has the most remaining
headroom. That is proportional distribution by another name, and it directly contradicts
(3). Whichever way the tension is resolved, it has to be recorded: an operator who
copies the illustrative block should get a clear answer, not a weight that quietly does
nothing.

Two further facts bound the decision:

- **No observation exists yet.** M5 is where latency, success rates and provider health
  are recorded. Anything scored at M4 is an operator's judgement, and pretending
  otherwise would let a guess inherit a measurement's authority.
- **The intended workload is agentic.** OpenHands sends a mix of requests, some with
  tools and some without, and its continuity is better served by one model than by two.

## 2. Decision

**Auto-routing is a deterministic global ranking over hard-filtered candidates, scored
by three operator-declared dimensions, with concentration as the fixed point of that
ranking.**

1. **Capability fit and capacity are hard constraints.** A model that cannot serve the
   request, or cannot be dispatched to right now, is excluded and not scored. Capacity
   is binary: usable or not.
2. **The weight set is `quality`, `tool_support`, `latency`.** `quota` and `reliability`
   are refused as weight keys at startup, with a message naming the reason.
3. **Quality is a static 0–100 prior per model**, declared by the operator, in a block
   the operator writes. It is not measured, inferred or benchmarked.
4. **Latency is a static prior in milliseconds**, normalised against a configured
   absolute ceiling, at the lowest weight.
5. **`tool_support` is scored on every request**, not only on tools requests, as a
   preference for a model that can serve the workload's next call.
6. **The highest score wins; ties break on configuration order.** The ranking is a pure
   function of configuration and the request.
7. **No affinity state, no rotation, no randomisation.** Stickiness is not maintained;
   it is the consequence of (6) plus (1).

## 3. Reasoning

**A quota score defeats the principle the milestone is named for.** §2.4 says balancing
or spreading load is an anti-pattern. Scoring by remaining headroom does exactly that,
in the most plausible-sounding way: it looks prudent, and it prevents the router from
using the model it thinks is best while that model has capacity. §7.3 already expresses
the intended behaviour as a hard trigger ("until quota exhaustion"), not a soft
preference. The roadmap's own priority list agrees, and the illustrative block is
labelled illustrative. The priority list wins.

**Reliability has nothing to score at M4.** The health signals that exist — cooling,
breaker degradation, persistence failure — are already eligibility states, and scoring
them would count the same fact twice: once to exclude, then again to rank what is left.
M5's "track observed success/failure rates" is the milestone that creates a number worth
weighting. Accepting a `reliability` weight before then would produce a configured
dimension that does nothing, which is the same defect (in configuration) that M3 spent a
milestone removing from request handling.

**`tool_support` is retained even though it is also a filter, because it is not the
same question.** When a request requires tools, the filter narrows the set and the
weight is constant — it cannot influence that decision. Its entire effect is on requests
that do *not* require tools, where it expresses a preference for the model that could
have served the next one. Without it, a high-quality non-tool model wins an ordinary
request and the following tool call is served elsewhere, breaking continuity for a
reason the client cannot see. That is worse for an agentic client than a slightly lower
quality answer, which is precisely the trade the roadmap draws attention to.

**An absolute latency ceiling, not a relative one.** Normalising latency across the
candidate set is the obvious implementation and it is wrong for this design: it makes
every model's score depend on the others, so adding a fast model lowers the score of
models nobody edited, and a request's decision changes when an unrelated model is
rate-limited out of the set. A configured ceiling makes the latency score a property of
the model. The cost is one number the operator must choose; 5000 ms is anchored to the
only observed latency data this project has (M0's 482–5364 ms provider header spread).

**Concentration needs no mechanism.** The obvious implementation of "stay on the best
model" is a pointer: a current-model variable, persisted or in memory, updated when the
selection changes. It is rejected. Given a deterministic ranking and binary capacity
eligibility, the top-ranked eligible model *is* the same model on every request until a
constraint removes it, so stickiness is an invariant of the ranking. A pointer would add
state that can disagree with configuration after a restart and change the result for no
reason.

**Determinism is an explainability requirement, not a nicety.** A decision record is
only a record if replaying the same request against the same configuration produces the
same answer. Ties broken by configuration order give that; weighted random selection
does not.

**Declaring priors in a block the operator writes is what keeps them honest.** M3's
`CapabilityValue` carries provenance (`published` / `measured` / `assumed`) because a
capability is a fact that can be verified. Quality is not a fact; it is a judgement, and
there is no honest provenance value for "the operator thinks so". Reusing the capability
schema would either force `assumed` — which M3 defines as unusable — or invite a
`published` claim no document supports. A separate block, in a document that says
"prior" wherever the number appears, is the truthful representation.

## 4. Alternatives Considered

**Keep the roadmap's five weights, including `quota` and `reliability`.** Rejected: the
quota term distributes traffic (§2.4, §7.3) and the reliability term has no signal.
Both would be configuration that claims a behaviour the code does not have.

**Score by remaining quota ratio.** Rejected for the same reason, more directly. It is
the most defensible-looking way to violate the concentration principle: it always has a
rationale ("use the model with more headroom") and it always produces the wrong shape
of traffic.

**Weighted random selection across eligible models.** Rejected: it maximises
distribution, makes the decision untestable, and makes a log line a sample rather than
an explanation. ROADMAP §3.9 explicitly rejects even distribution as a goal.

**Round-robin or least-recently-used selection.** Rejected: these distribute by
construction, and they exist to solve a fairness problem this project does not have.

**Benchmark each model inside the milestone.** Rejected: it would spend free-tier quota
to produce a number that goes stale when a provider updates a model, and it would make
routing depend on a workload the operator did not choose.

**Infer quality from model size or name.** Rejected: a guess wearing a formula, and
indistinguishable in configuration from a considered judgement.

**Defer `free` until M5 can observe quality.** Rejected: it leaves the milestone's own
exit criterion unmet, and quality is not the only input — capability fit and capacity
already decide most requests.

**Make priors mandatory for every model.** Rejected: adding a model would become a
two-step operation with a broken intermediate state. Instead the model is excluded from
`free` with a stated reason, remains explicitly selectable, and startup warns. The one
hard requirement is that some model carries priors when routing is configured.

**A separate `routing.enabled` flag.** Rejected: a policy block that is present but
disabled is a state with no behaviour, and the project's other opt-ins are already
presence-based.

**Return `"free"` in the response `model` field.** Rejected: the client then cannot know
which model answered, and the response would contradict the log. Returning the effective
model is the same rule M3 applies to substitution (ADR-0009).

## 5. Consequences

### Positive

- **The milestone's exit criterion is reachable without inventing data.** Every score
  input is declared in configuration and visible in the log.
- **Explicit requests are untouched.** Auto-routing is a branch in the selection step,
  not a rewrite of dispatch, so M1–M3 behaviour holds by construction.
- **The decision is reproducible** from the configuration and the request.
- **M5 has a place to land.** Observed reliability, latency and quality can be added as
  score inputs without changing the selection mechanism or the log contract.
- **The roadmap inconsistency is resolved in writing.** An operator who copies the
  illustrative block gets a startup error that explains itself.

### Negative / Trade-offs

- **Priors are opinions and can be wrong**, and a wrong prior is worse under
  concentration than under distribution: the whole workload goes to the wrong model
  until a constraint moves it. That is the price of §2.4, and it is why the log record is
  a deliverable rather than a debugging aid.
- **Two roadmap dimensions are deferred.** `quota` and `reliability` are absent from the
  weight schema, and a reader of the roadmap would expect them. The refusal message and
  this ADR are the explanation.
- **A latency ceiling is a judgement too.** It is one more number the operator must
  choose, and the default is anchored to M0 data rather than measured now.
- **`tool_support` can outrank a meaningfully better model** for a tools-free request.
  Deliberate, for workload continuity, but it is a real quality cost in a deployment
  whose workload is not agentic.
- **No per-task quality dimensions.** A coding-heavy deployment cannot express "this
  model is better at code" until something can identify a coding request, and no such
  signal is in scope.

## 6. Verification

Discharged when:

1. Spec Scenarios 1–9 pass: selection, exclusion, capacity, stickiness, tie-breaking,
   the unknown-`free` case, log content, and explicit requests being unranked.
2. Spec Scenario 16 passes: `free` appears in `/v1/models` only when routing is
   configured.
3. Startup refuses an unknown weight key, `quota`, `reliability`, zero total weight, and
   routing with no scoreable model.
4. A repeated request against an unchanged configuration selects the same model.
5. The `routing_decision` record contains model ids, numbers and reason codes only.
6. `core/dispatch.py` is unchanged in the batch that implements this, asserted by the
   diff: selection happens before dispatch, exactly as substitution does.
