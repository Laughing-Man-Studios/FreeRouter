# ADR-0011: Capability Pass-Through Lives in the Adapters, and a Declaration Follows Verification

**Date:** 2026-11-16
**Status:** Accepted
**Driver:** Robert Gibb (Wan Toc)

## 1. Context

M3 built a capability filter that refuses requests no configured model can serve. It
also recorded, as decision C5, that no model may declare `supports_tools`,
`supports_vision` or `supports_json_schema` as serviceable, because neither adapter
forwards them: `core/normalization.py` honoured four fields and discarded `tools`,
`tool_choice` and `response_format`, and both adapters sent `messages` only. The
Mistral adapter even carried a comment noting its `tool_calls` handling was unreachable
"because M0 strips tools at ingress".

The consequence is recorded rather than smoothed: the OpenHands objective, deferred
from M2 to M3, is still unmet, because an agentic client acts by issuing tool calls and
the router cannot forward them.

ROADMAP M4 now carries the pass-through work and the objective. It also states the
mirror rule M3's C5 implies: a model may declare these capabilities as `true` "only
once its adapter forwards them", so the filter admits the request instead of refusing
it.

Two facts shape the implementation:

- **The providers are not symmetrical.** Mistral's tool, structured-output and image
  APIs are OpenAI-shaped. Gemini's are not: tools are `functionDeclarations`, calls come
  back as `functionCall` parts with **no id**, results go back as `functionResponse`
  parts, and structured output is a generation config constrained by an OpenAPI subset.
- **The internal boundary is deliberately narrow.** `NormalizedRequest` and
  `NormalizedResponse` are `extra="forbid"` specifically so the project can prove which
  fields reach a provider.

## 2. Decision

**Pass-through is adapter work behind an explicitly extended boundary, and a capability
declaration of `true` requires both the forwarding code and a measurement.**

1. **The boundary grows by named fields, once.** `NormalizedRequest` gains `tools`,
   `tool_choice`, `response_format` and part-shaped message content;
   `NormalizedResponse` gains `tool_calls`; `NormalizedChunk` gains a tool-call delta;
   `FinishReason` gains `tool_calls`. `extra="forbid"` stays.
2. **Each adapter translates its own provider's form**, including the response side and
   the streaming side. The router core learns nothing provider-specific.
3. **A capability is declared `measured` in the batch that makes it servable and
   verifies it**, per model and per provider. Until then it stays `assumed`, which M3
   defines as unknown and unusable.
4. **Streaming counts.** A capability that works unary and fails when streamed is not
   delivered, because the agentic client uses the streamed path.
5. **Call ids are synthesised where the provider has none**, deterministic within a
   response.
6. **A remote `http(s)` image URL is refused locally** rather than fetched by the
   router.
7. **A provider rejecting a forwarded capability surfaces its own error.** The router
   does not pre-empt it with a local refusal after the capability was declared servable.

## 3. Reasoning

**The declaration is the milestone's honesty mechanism.** M3's C5 established that
declaring `true` without forwarding is a lie that produces silent degradation. The
converse is equally damaging in the other direction: shipping the forwarding code but
leaving the capability `assumed` delivers code the router will never select, and the
milestone's deliverable becomes unreachable while appearing complete. Code and metadata
therefore move together, and the measurement is the gate between them.

**Naming the fields is what keeps the boundary meaningful.** A `dict[str, Any]`
passthrough would forward whatever the client sent, which is precisely what
`extra="forbid"` exists to prevent. The reason M0 stripped `temperature` was to make
"what reaches a provider" a decidable question; the answer changes, the decidability
must not.

**Provider asymmetry is the reason this is adapter work rather than a normalization
concern.** Every difference — `functionDeclarations`, missing call ids, whole-part
streaming, `functionResponse` turns, the OpenAPI subset — is a fact about one vendor.
Putting any of it in the core would be the boundary violation CONSTITUTION §4 forbids,
and would make the next adapter inherit Gemini's shape.

**Synthesised ids are a compromise and are labelled as one.** OpenAI clients echo a
`tool_call_id` back with the result, and Gemini has no equivalent, so the adapter must
invent one. It is stable within a response, which is all the client needs to correlate
the call with its result, and it is *not* promised stable across independent requests.
Promising more would be a claim the provider cannot support.

**Refusing remote images is a security decision, not a capability limitation.** Fetching
a client-supplied URL server-side turns the router into an SSRF surface and an egress
proxy. The router has never made an outbound call the configuration did not declare, and
this would be the first exception. Inlining the data is a small cost for the client and
removes a whole class of problem.

**Surfacing the provider's error is the correct failure mode once the capability is
declared.** If the router refused locally after declaring support, it would be second-
guessing a capability it just claimed to have verified, and it would hide the provider's
actual constraint from the operator.

## 4. Alternatives Considered

**Decode and re-encode tool arguments as objects.** Rejected: the OpenAI wire format is
a JSON **string**, and a client parses it. Re-serialising changes the bytes the client
receives and can change key order or whitespace, which some clients hash.

**Normalise tool calls into a provider-neutral "call" object and render per adapter.**
This is what `NormalizedResponse` already does at the top level, and it was rejected for
the nested payload: the differences are structural (ids present or absent, fragments or
whole calls), so a neutral shape would have to be the union of every provider's, which
is a worse contract than the OpenAI shape every client already understands.

**Declare capabilities `true` optimistically when the code is written.** Rejected: a
declaration is a promise the filter acts on, and an unverified promise is exactly what
M3's C5 exists to prevent.

**Accept remote image URLs and let the provider fail.** Rejected: it spends a request to
learn something the adapter could decide, and for Gemini the failure is a 400 that reads
like a bad payload.

**Fetch remote images in the adapter.** Rejected: SSRF surface, unbounded egress, and a
new failure mode in a component that currently only forwards what it was given.

**Make the boundary a passthrough mapping and translate in the core.** Rejected:
CONSTITUTION §4 puts provider translation in adapters, and a core that understands
`functionDeclarations` cannot be extended without understanding the next vendor too.

**Fold tool support into the existing M3 refusal without forwarding.** That is M3, and
it is why the OpenHands objective is unmet. Explicitly rejected as a way to claim the
deliverable.

## 5. Consequences

### Positive

- **The filter's positive answers become true.** A model that declares tool support can
  actually serve a tool request, so M3's metadata stops being a statement about
  potential.
- **The OpenHands objective becomes reachable.** A tool call and its result can complete
  a multi-turn loop through the router.
- **Explicit refusal remains intact** for capabilities no model declares: pass-through
  widens what is servable, not what is admissible without support.
- **The next adapter inherits a boundary, not a vendor.** It implements the same named
  fields and its own translation.
- **The security posture is unchanged**: no outbound call the configuration did not
  declare.

### Negative / Trade-offs

- **Gemini's shape is a real risk to each pass-through batch.** The `functionResponse`
  turn, the OpenAPI subset, and whole-part streaming each need confirmation against the
  provider's current documentation, and any can turn out unusable. The honest outcome
  then is an `assumed` capability and a recorded finding, which means a batch can end
  with less delivered than planned.
- **`content: null` changes a response shape** that M0–M3 always populated with a
  string. A client that assumes a string will break on a tool call — which is the OpenAI
  contract, not a router choice, but it is a change.
- **Synthesised call ids are not stable across requests.** A client that persists
  conversation history and expects the same id on replay is relying on a guarantee no
  provider here offers.
- **Vision has no consumer.** It is a ROADMAP deliverable with no workload behind it
  yet, so it is the batch most likely to slip, and it is planned anyway rather than
  quietly omitted.
- **Payload surface grows.** Tool definitions, tool results and image data are the
  largest payloads the router will have carried. The logging allowlist is what keeps
  them out of records, and it becomes more load-bearing than it was.

## 6. Verification

Discharged when:

1. Spec Scenario 10 passes for both adapters: a unary tool call returns `tool_calls`,
   `content: null`, and `finish_reason: "tool_calls"`.
2. Spec Scenario 11 passes for both adapters: a `role: "tool"` result produces a normal
   follow-up answer.
3. Spec Scenario 12 passes: a streamed call delivers `delta.tool_calls`, terminates with
   `finish_reason: "tool_calls"`, and ends with `[DONE]`.
4. Spec Scenarios 13–15 pass: structured output and base64 vision are forwarded, and a
   remote image URL is refused locally with no outbound request.
5. With a capability left `assumed`, M3's refusal still fires — pass-through has not made
   the filter permissive by accident.
6. Every capability declared `true` in `config.yaml` carries `provenance: measured` and a
   `source` naming the measurement and its date.
7. `core/dispatch.py` and the quota, cooldown and breaker modules are unchanged in the
   batches that implement this, asserted by the diff.
