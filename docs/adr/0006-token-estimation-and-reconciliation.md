# ADR-0006: Token Estimation and Reconciliation

**Date:** 2026-10-19
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

ROADMAP §3.5 requires a pre-dispatch reservation:

```text
Estimate request token usage → reserve → dispatch → record actual → reconcile
```

and says plainly:

> "The MVP algorithm should be deliberately simple. It does not need exact provider
> accounting and may tolerate small estimation error in exchange for low
> implementation complexity."

CONSTITUTION §8.2 makes this a resource-protection mechanism rather than analytics.

M1 already reserves `ceil(chars / 4)` over message content, with a `Decimal` safety
margin applied inside `reserve()` so a caller cannot forget it. Two problems:

**The estimate covers only input.** Output tokens are unknown before generation,
and M1's estimate silently assumes they are zero. The TPM ceilings in play
(Gemini 250K/min, Mistral 1.3M/min) are shared across both directions, so
under-reserving output is what actually breaches them.

**"Tolerate small estimation error" is currently unquantified.** An accepted error
with no number attached is an open-ended concession.

Issue #13 asks for the method, the reconciliation rules, and the tolerated error to
be stated explicitly.

## 2. Decision

**Separate input from output:**

```text
reserved = ceil((estimated_input + assumed_output) * safety_margin)
```

- **Input** is estimated from the normalized request's message content, retaining
  M1's `chars / 4` calibration as the starting point.
- **Output** is an **explicitly configured assumption** per deployment, not a
  constant.
- **Reconciliation supersedes the estimate** where the provider reports usage, for
  the entries the reservation created and no others.
- **Absence of reported usage leaves the estimate standing.** Capacity is not
  released on the assumption it went unused.

**Tolerated error, stated per dimension:**

| Dimension | Nature | Tolerated error |
| :--- | :--- | :--- |
| Requests | **Exact** — one dispatched attempt reserves one request | **None.** There is no estimation error to absorb. |
| Tokens | Estimated pre-dispatch, corrected at reconcile where reported | Up to the configured safety margin, with residual drift **measured** against the provider console rather than assumed |

## 3. Reasoning

**A global tolerance would be unfalsifiable.** Stating "±20%" across both dimensions
would be true of tokens and meaningless for requests, which are counted exactly. The
error budget belongs to the dimension that actually has one.

**Under-reserving output is the real risk.** Input is knowable before dispatch;
output is not. A ceiling breached by output tokens is also the harder failure to
diagnose, because the request looked small.

**The output assumption must be configurable because it is a policy choice.** How
much headroom to buy is a deployment decision — a client sending 4K completions
needs a different assumption from one sending 100-token replies. A hidden constant
would make that policy invisible and unchangeable without editing code, and the
whole failure mode is invisible until a limit is hit.

**Reconciliation corrects only what it reserved into.** This is what the M1 code
already does deliberately, so agreeing it costs nothing. Retroactive rewriting of
historical windows cannot be implemented honestly: a rolling window may already
have pruned the bucket, and a fixed daily window is a *record of what occurred*
rather than a mutable tally. Correcting the present by revising the past would
require a different data model — one that retains every event indefinitely — which
is a worse trade than a bounded, explained residual.

**Absence of usage must not release capacity.** Every dispatched attempt consumes
quota whether or not it produced output. A run of 5xx that reported no usage would
otherwise silently drain a 500-per-day budget while the router believed it had
capacity left. This is the same reasoning that made M1 reconcile on every terminal
path including failures.

## 4. Alternatives Considered

**A real tokenizer per provider (tiktoken, Gemini's own).** Rejected. It is the
accurate answer and the wrong milestone: it couples the router to each provider's
tokenizer, which is neither published identically nor stable, and the accuracy
buys little when provider-reported usage already corrects the estimate at
reconcile time. Accurate *post hoc* is sufficient; accurate *pre hoc* is not worth
the coupling.

**Keep M1's input-only estimate and rely on the safety margin.** Rejected. The
margin would have to absorb the entire output size to be safe, which either makes
every reservation far too conservative or fails exactly when a long completion
arrives. Separating the two terms makes the conservative part explicit and tunable.

**Release capacity when no usage is reported.** Rejected. See §3. This is the one
option that would make the accounting *lie in the router's favour* on precisely the
requests that failed.

**Store every request event and reconstruct windows on restore.** Rejected. Correct
but expensive, and it makes a corrupt row fatal to startup. Counter buckets persist
directly; see ADR-0005.

**Retroactive reconciliation across windows.** Rejected for §3's reasons.

## 5. Consequences

### Positive

- The request dimension is exact, so quota refusal on request counts is trustworthy.
- Token reservation covers the direction that actually breaches ceilings.
- The output assumption is visible, documented and tunable without a code change.
- Residual error is **measurable**: the console cross-check compares accumulated
  persisted counts against what the provider bills.

### Negative / Trade-offs

- **`chars / 4` is calibrated for English prose.** Code, JSON, and non-Latin scripts
  diverge, sometimes substantially. The safety margin absorbs ordinary error; a
  systematically wrong workload is caught by reconcile-time correction and made
  visible by the cross-check.
- **The estimator will be wrong for some workloads** and M2 does not fix that. It
  makes the wrongness *visible*, which is the achievable goal.
- **Conservative output assumptions reduce effective capacity.** A large assumed
  output against a tight TPM ceiling reserves capacity that may never be used. That
  is the deliberate direction of the error, since the alternative is a breach.
- **Reconciliation cannot correct history**, so drift persists in persisted
  counters. Bounded and explained, not eliminated.

## 6. Verification

Discharged when:

1. `spec.md` Scenarios 5–7 pass, including that `actual_tokens=None` leaves the
   estimate standing rather than releasing capacity.
2. A test asserts the reservation covers input **plus** the configured output
   assumption — the regression M1's estimator would have allowed.
3. **The console cross-check is re-run once counters are durable**, comparing
   accumulated persisted counts against AI Studio's figure. M1 found a delta of +1
   over 54 dispatches; a systematically growing delta means the estimator is
   drifting and must be recalibrated rather than tuned away. This is the only
   independent check available and it is cheap.

## 7. Open Item Carried Forward

**Mistral's daily ceiling remains unmeasured.** It is unmeasured in M1 and M2 does
not resolve it — but M2 is the first milestone in which persistent counters could
answer it empirically, by observing when a 429 arrives that the console never
predicted. Worth revisiting once counters are durable and a real workload has run
long enough for the daily window to fill.
