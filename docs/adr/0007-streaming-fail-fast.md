# ADR-0007: Streaming Fail-Fast After the First Delivered Chunk

**Date:** 2026-10-19
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

ROADMAP §3.11, in full on the policy:

> "Fail fast once a streaming response has begun. Do not attempt transparent
> cross-provider replay after partial content has already been delivered. Later
> phases may explore more sophisticated recovery mechanisms."

Issue #36 makes "transparent cross-provider replay after a stream has started" an
explicit non-goal, and lists as an open question "how much of the M2 streaming work
is needed before OpenHands is genuinely usable".

CONSTITUTION §7.2 prohibits cross-model failover for an explicitly requested model.
M1 made that prohibition structural: the dispatcher's signature takes exactly one
`(provider, model)` pair and never varies it across attempts.

A client requests a specific model. If the provider dies mid-stream, the only way to
"recover" is to send the request elsewhere — which means either a different model,
violating §7.2, or the same model on a second credential, which is the same
conversation the client already saw part of.

## 2. Decision

**Fail-fast, with the boundary at the first chunk actually written to the client.**

- **Before any chunk is delivered:** an interruption is an ordinary transient fault.
  It is retried within the existing bounded budget, exactly as in M1. Nothing has
  been committed to the client.
- **After any chunk is delivered:** the connection is terminated, the failure is
  recorded, and the reservation is reconciled. **No request is dispatched to any
  other provider or credential.**
- **A zero-chunk stream is not "after stream start."** It has delivered nothing, so
  it is retryable. This is what makes the rule precise rather than merely
  conservative.
- **The boundary is enforced in the route layer**, not the adapter, because the
  adapter cannot know whether anything reached the client.
- **A provider that cannot stream fails clearly** (`400 streaming_unsupported`)
  before any dispatch, rather than silently buffering and replaying.

## 3. Reasoning

**A merged response is worse than an error.** A client that received 400 tokens of
one answer and then unrelated content from another provider has no way to tell
where one answer ended and the next began. Truncation is detectable; a
silently-concatenated answer is not, and it can read as a coherent — but wrong —
response. For an agentic client that acts on what it reads, that is the more
dangerous failure.

**The alternative requires violating a constitutional rule.** Recovery after
stream start means dispatching elsewhere. §7.2 forbids a different model for an
explicit request. The same model on a second credential is permitted, but Google's
quota is per-project (Issue #51), so a second key draws on the same bucket — the
attempt would usually fail too, having spent a request to learn nothing.

**The distinction between zero-chunk and first-chunk is the useful part.** Without
it, "fail fast" would mean failing fast on connection failures too, which are the
*most* recoverable case and the ones retry already handles. Drawing the line at
delivery rather than at dispatch means retry keeps working exactly where it works
today, and gives up only where retry cannot help.

**Termination must still reconcile.** An interrupted stream has consumed quota. A
reservation left pending would leak capacity permanently — and under streaming,
where reservations are held far longer, that is a materially bigger leak than M1's
unary case.

## 4. Alternatives Considered

**Replay from a second credential on the same model.** Rejected on §3: permitted by
§7.2, but per-project quota makes it usually futile, and it doubles the tokens spent
on a request that already failed.

**Replay from a different model.** Rejected. Directly prohibited by §7.2 for an
explicitly requested model, and M1 made that structural rather than a guard.

**Resume the stream from a provider offset.** Rejected. Neither Gemini nor Mistral
offers a resumable stream offset on the free tier. Recording it as unavailable is
more honest than designing against an assumed capability.

**Buffer the whole response, then emit as SSE.** Rejected. It would appear to
stream while behaving like a unary request — no first-token latency improvement,
which is the entire practical reason to stream — and it would make fail-fast
unreachable because nothing is delivered until completion.

**Keep the connection open and retry internally.** Rejected. It trades a visible
error for an invisible stall, and a client that cannot distinguish a slow stream
from a hung one will time out on its own schedule.

## 5. Consequences

### Positive

- **No client ever receives concatenated answers** from two providers.
- **§7.2 stays structurally intact** on the streaming path — there is no code path
  that varies the model after delivery begins.
- **Retry is preserved where it works**: every failure before the first chunk.
- **The failure is visible** as a stream error rather than as wrong content.

### Negative / Trade-offs

- **A client sees an error it would have preferred not to see**, in the case where
  a second credential might have succeeded. Under M1's measured conditions this is
  rare — 2 transient faults in 104 dispatches, both recovered by retry — but
  streaming holds connections open longer, so the exposure is higher and the unary
  measurement does not transfer.
- **Streaming success rate must be measured separately.** Reusing M1's 100/100 for
  streamed traffic would be unfounded.
- **Partial output is wasted.** Tokens already generated are paid for and discarded.
  Unavoidable, and small relative to the value of not fabricating a merged answer.
- **OpenHands may retry the whole turn**, losing the partial work. Acceptable for
  an agent that re-plans, unacceptable for a human reader mid-answer — worth
  noting as a UX cost rather than a correctness one.

## 6. Verification

Discharged when:

1. `spec.md` Scenario 9 passes: an interruption after the first chunk terminates the
   stream and **never dispatches to a second provider** — asserted against every
   call's URL and body, as M1's cross-model test did.
2. A zero-chunk interruption **is** retried within budget (Scenario 10's
   neighbourhood), proving the boundary is drawn at delivery rather than dispatch.
3. An interrupted stream leaves no pending reservation.
4. The streamed success rate is measured and recorded with its own conditions,
   under `spec.md` § *Measuring the success rate*.

## 7. Open Item Carried Forward

**OpenHands usability is unresolved and may not be reachable in M2.** See
`spec.md` § *Open Decisions Requiring Sign-Off* D3. Streaming and `/v1/models`
support *conversational* use; agentic use requires tool calling, which #36 places
outside M2's boundary. This ADR makes streaming correct, but does not make OpenHands
an agent. That is a scoping decision for the operator, not an implementation
detail.
