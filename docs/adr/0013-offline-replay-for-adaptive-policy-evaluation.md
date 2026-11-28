# ADR-0013: Adaptive Policies Are Evaluated by Offline Replay of Recorded Decisions

**Date:** 2026-11-28
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

ROADMAP Milestone 5 asks the router to "experiment with adaptive routing policies"
and to do so "without unnecessarily consuming free-tier quota". The milestone's
separate research topic repeats the constraint: design a learning system that
estimates performance "without unnecessarily consuming free-tier quota". Issue #39
records the open question directly: *"Whether a simulation/replay harness is worth
building before trusting live adaptive behaviour."*

Three evaluation methods are available:

1. **Live shadow or canary traffic.** Run an alternative policy against real
   requests and compare outcomes.
2. **Synthetic simulation.** Generate latency and failure distributions and test
   policies against them.
3. **Replay of recorded observations.** Recompute estimates and rankings from data
   the router already recorded.

The project is a single-operator, free-tier router. Every live request spends an
allowance the operator cannot replenish by paying, and a shadow selection is a
selection the operator did not ask for.

## 2. Decision

**Adaptive estimator and scoring settings are evaluated offline by replaying
metadata-only observations the router recorded. Replay is a development tool, not a
production endpoint, and it never contacts a provider.**

1. **The recorder stores a decision snapshot** for every `free` request: candidate
   model ids, each candidate's normalized components (quality, tool support,
   latency), the weights in force, and the selected model.
2. **Replay recomputes** the EWMA state, the reliability multiplier, and the ranking
   for each stored candidate set under supplied settings.
3. **The harness reads SQLite read-only**, writes nothing, and makes no HTTP call.
4. **The report states its boundary**: it can vary estimator settings and weights,
   but not the latency prior or the scoring formula, because those inputs are already
   folded into the stored components.
5. **Live traffic validates the chosen policy**, but is not used to search the policy
   space.

## 3. Reasoning

**Replay is the only method that spends no quota and is reproducible.** The data was
already paid for by requests the operator chose to make. Replaying it under a
different half-life costs nothing and produces the same answer every time, which is
what makes a policy change reviewable.

**Shadow traffic is the wrong trade for this project.** It would spend allowance to
learn something replay can often answer, and it would expose the operator to
selections they did not request. The roadmap's wording — "without unnecessarily
consuming free-tier quota" — points away from it.

**Synthetic simulation is complementary, not sufficient.** It is useful for
questions replay cannot answer (a different latency prior, a different estimator),
but its distributions are guesses. Replay is grounded in the deployment's actual
traffic; synthetic data is grounded in assumptions.

**Storing components rather than payload is what makes replay safe.** The snapshot
contains model ids and normalized numbers. It cannot leak a prompt, a tool
declaration or a completion. This is the same privacy boundary as the
`routing_decision` log record, persisted in a bounded table.

**The limitation is stated rather than hidden.** A replay tool that silently
pretended to evaluate changes it cannot evaluate would be worse than no tool. The
report names the settings it applied and the inputs it could not vary.

## 4. Alternatives Considered

**Live shadow/canary experiments.** Rejected: spends quota, exposes the operator to
unrequested selections, and makes the result depend on traffic that arrives during
the experiment.

**Synthetic simulation only.** Rejected as the sole method: the distributions are
guesses, and the project already has real observations. A synthetic harness remains a
reasonable separate tool for questions outside replay's boundary.

**Evaluate in production and roll back if it looks worse.** Rejected: the operator
experiences the regression, and "looks worse" has no baseline without replay.

**Store full request/response payloads to enable richer replay.** Rejected outright
by CONSTITUTION §9.1. Components and metadata are sufficient for the questions M5
asks.

**A production replay API.** Rejected: replay is an operator development tool, not a
request-serving feature. M7's UI is the place for visibility.

**Contextual bandits or ML for policy selection.** Rejected for M5: no labelled
outcomes, no traffic volume to train on, and no way to explain a learned policy to
the operator. The roadmap lists them as future candidates, not requirements.

## 5. Consequences

### Positive

- **Policy evaluation costs nothing** and cannot spend provider quota.
- **Results are reproducible** from the retained observations and the settings.
- **The stored snapshot is privacy-safe** by construction.
- **The defaults can be justified with the deployment's own data**, not intuition.
- **The tool's limits are explicit**, so a question outside them is recognised as a
  different experiment.

### Negative / Trade-offs

- **Replay cannot evaluate every change.** A different latency prior or a different
  estimator formula needs synthetic data or a new field in the snapshot.
- **The snapshot adds storage per request.** Retention bounds it.
- **The observation table is now load-bearing for evaluation**, so a long retention
  gap weakens the tool. The report must state the window it used.
- **Replay is not a guarantee about the future.** It shows what would have happened
  to the recorded traffic, not what will happen to the next request.
- **A replay bug can mislead a decision.** It is tested against the recorder's own
  estimates on a hand-built sequence, and that parity test is the guard.

## 6. Verification

Discharged when:

1. Spec Scenario 13 passes: replay reads the database read-only, makes no outbound
   request, and reports changed selections.
2. Replay reproduces the recorder's estimates exactly on a hand-built observation
   sequence.
3. A test installs an HTTP transport that fails on use, proving no quota can be
   spent.
4. The report includes the settings used, the observation window, and the stated
   limitation.
5. Batch 3 records the shipped-default evaluation with numbers in `tasks.md`.
6. `make check` and `make test-hermetic` are green.
