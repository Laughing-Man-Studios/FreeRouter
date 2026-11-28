# ADR-0012: Observed Telemetry Uses Time-Decayed EWMAs and a Bounded Reliability Multiplier

**Date:** 2026-11-28
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

CONSTITUTION §7.3 fixes the source of M4's score — "static, manually curated weights
configured by the operator" — and defers "dynamic, observation-based quality scoring
(tracking historical success/failure rates)" to a later milestone. M5 is that
milestone.

ROADMAP Milestone 5 asks the router to record provider/model latency, observed
success/failure rates, tool-call success/failure and quota prediction accuracy, and
to update reliability/availability estimates over time. It also asks for a separate
design exercise on how to learn from outcomes "without unnecessarily consuming
free-tier quota", naming rules, statistical scoring, contextual bandits and ML as
candidates without mandating one.

Four facts bound the decision:

- **Free-tier traffic is not a benchmark.** The router sees requests and responses,
  never a ground-truth answer. Semantic quality cannot be attributed to a model from
  this data.
- **A `429` is already handled.** Cooldown and quota accounting exist to protect
  provider allowances. A second penalty for the same event would double-count it.
- **Persistence must survive restarts** (CONSTITUTION §8.3) but must not put an
  analytics write in the request's critical path.
- **Explainability is a deliverable.** M4's `routing_decision` record is the contract
  an operator reads; M5's inputs must extend it rather than hide inside a model.

## 2. Decision

**M5 observes metadata-only outcomes, folds them into time-decayed EWMAs with a
sample floor, persists aggregates plus a bounded observation history, and applies
reliability to `free` scoring as a bounded multiplier after the M4 weighted score.**

1. **One observation per dispatched request**, taken where the terminal outcome is
   known, on both the unary and streaming paths. Internal retries are one request.
2. **Only provider-attributable completion outcomes count.** `429`, request-fault
   `4xx` and client aborts are recorded but do not move reliability.
3. **The estimator is an EWMA with time-based decay**:
   `alpha = 1 - 0.5 ** (elapsed / half_life)`. A metric is unused until
   `min_samples` observations exist.
4. **Reliability is a multiplier**, `floor + (1 - floor) * success_rate`, applied to
   the weighted base score. It never excludes a candidate.
5. **Observed total latency replaces the static prior** after the latency sample
   floor; TTFB is recorded but not scored.
6. **Quality stays an operator prior.** The only quality-adjacent signal is
   structural validity: tool-call parseability, JSON conformance, non-empty
   completion.
7. **Aggregates are authoritative and persisted**; observations are bounded and kept
   for replay. A telemetry write failure degrades durability, never the request.
8. **The unit is provider/model**, not key.

## 3. Reasoning

**An EWMA is the simplest estimator that both smooths and recovers.** A rolling
window has a cliff: when an old failure leaves the window the estimate jumps, which
makes the ranking discontinuous for no behavioural reason. A Bayesian model is
principled but introduces priors, credible intervals and tuning that a few dozen
observations cannot justify. The EWMA has one parameter, a recovery curve that is the
same formula run forward, and a closed-form test.

**The sample floor is separate from the smoothing.** Smoothing alone does not stop
the first observation from being 100% of the estimate. A floor turns "how much
evidence before the router acts" into one explicit, configurable number that maps to
the operator's traffic rate — which is the real constraint on this project.

**A multiplier preserves the M4 policy.** M4 refused `reliability` as a weight
because there was no signal. There is a signal now, but a fourth weight would
renormalise the other three and make every M4 score incomparable. A multiplier
leaves the operator's base ranking intact, is bounded, and is one number in the log.
A hard filter was rejected because cooldown and the breaker already remove models
that cannot be contacted; refusing a model that still answers could leave no capable
candidate.

**Excluding `429` is what keeps the two mechanisms honest.** Quota protects spend;
reliability describes service. Counting a rate limit as unreliability would make a
busy free tier look broken and would penalise exactly the providers the project
depends on. Keeping them separate also makes each independently testable.

**Structural validity is the only quality claim the data supports.** The router can
tell whether a tool call parses and whether a schema response conforms. It cannot
tell whether the answer is right. Reporting a structural failure as a reliability
failure is defensible; calling it "quality" would overstate the measurement, so the
word stays attached to the operator prior.

**Persistence is deliberately weaker than quota persistence.** Quota and cooldown
writes protect provider accounts and must be written through. An estimate is a
ranking input; losing a row costs a little accuracy. Blocking a completion to record
that a completion happened is the wrong trade, so the request path only updates
memory and enqueues.

**Provider/model is the unit because a key is a credential, not a model.** Per-key
estimates would split a small sample, make a rotation look like a behaviour change,
and duplicate M2's key-health machinery.

## 4. Alternatives Considered

**Rolling window counters.** Rejected: simple but discontinuous at the window edge,
and a single failure can move the estimate by a large step.

**Bayesian/Beta reliability with the static prior as a belief.** Rejected for M5:
more concepts and tuning than the data volume can support. It remains a reasonable
future refinement once there is enough traffic to justify a prior.

**Reliability as a fourth weight.** Rejected: renormalises the M4 weights, makes
scores incomparable, and invites re-tuning of unrelated dimensions.

**Reliability as a hard eligibility threshold.** Rejected: duplicates cooldown and
the breaker, and can remove the only capable model.

**Counting `429` as a failure.** Rejected: double-penalises capacity, which is
already handled by cooldown and quota.

**Semantic quality from LLM-as-judge, benchmark probes, or user feedback.** Rejected
for M5: each adds a quota cost, a second model in the routing path, or a client API
that does not exist. The roadmap does not mandate them.

**Per-key telemetry.** Rejected: splits the sample and duplicates key-health state.

**Synchronous persistence on the request path.** Rejected: trades real latency for a
small accuracy gain on data that is not a safety invariant.

**In-memory-only estimates with a snapshot.** Rejected: loses the aggregate on an
unclean stop and cannot support replay.

**An `enabled` boolean.** Rejected: every other opt-in in the project is
presence-based, and a present-but-disabled block is a state with no behaviour.

## 5. Consequences

### Positive

- **The milestone's deliverables are measurable.** Latency, success, tool validity
  and quota error have a persisted home and a defined update rule.
- **M4's contract survives.** Weights, hard constraints, stickiness and the
  decision record keep their meaning; one multiplier is added.
- **The privacy guarantee is structural.** The observation type has no payload field,
  so no code path can store prompt or completion text.
- **A telemetry failure cannot take the router down.** The request path never waits
  on the writer.
- **The change is reversible by configuration.** Removing the `telemetry` block
  returns exact M4 behaviour.

### Negative / Trade-offs

- **Small samples make fine-grained estimates unreliable.** At a few dozen requests
  a day, the floor and half-life dominate. The report must say so.
- **A transient fault can demote a good model for a window**, and recovery is
  deliberately gradual. The floor bounds the damage; replay measures the window.
- **Structural validity is not correctness.** A model can return valid JSON that is
  wrong and keep a high multiplier.
- **A crash can lose queued observations.** Estimates regress to the last successful
  write.
- **The observation table adds storage.** Retention bounds it, but the cap is a new
  operational number.
- **Streaming wiring is the riskiest part.** The terminal outcome is known only after
  the client consumes the iterator, which is where a double or missing record can
  hide.

## 6. Verification

Discharged when:

1. Spec Scenarios 1–12 and 14–15 pass, including the `429` exclusion, recovery,
   restart restore, retention and persistence-failure cases.
2. A request with telemetry enabled produces exactly one observation with no payload
   column populated.
3. A model with a zero success rate retains `floor` of its base score and is still
   eligible.
4. Observed latency substitutes only after the floor, and the decision record names
   the source.
5. A failing writer leaves the response body and status unchanged.
6. Removing the `telemetry` block reproduces the M4 score exactly.
7. `make check` and `make test-hermetic` are green.
