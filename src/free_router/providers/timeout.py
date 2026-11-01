"""The provider dispatch timeout, shared by every adapter.

This lives outside the adapters because the value is a cross-provider policy:
one environment variable, one default, one validation rule. Resolving it
per-adapter would leave two sources of truth that drift the first time the
default is retuned from measurement.

**The timeout covers provider I/O, not routing decisions.** Spec 8 originally
wrapped connection and response headers in a 400 ms window, on the theory that
this protected the <500 ms routing envelope. It does not. Measured live against
Google, provider headers alone take p50 743 ms and up to 2893 ms, so a 400 ms
window failed almost every real request with a 504. The router's own decision
path is 0.003 ms, so the <500 ms target (CONSTITUTION 2.5, ROADMAP section 4)
has ample headroom and is now documented rather than enforced. Spec 8 has been
amended accordingly.

The window bounds *connection and headers*, not generation: a model's actual
generation can take many seconds, so the body must be read *after* it closes.
That is why adapters dispatch with ``stream=True`` and read with ``aread()``
rather than ``client.post()``, which reads the entire body inside the timeout
and would turn every real completion into a 504.

**It applies to the streaming call only, and that distinction was learned the
hard way.** "Headers" means the handshake on ``:streamGenerateContent``, which
arrives in under a second and is exactly what a short window should retry
cheaply. On ``:generateContent`` there is no handshake: the provider sends
nothing until the answer exists, so the "header" phase *is* the generation.
Applying the same 5 s window there capped unary generation at five seconds --
measured on 2026-10-30 as one `504 provider_timeout` in twenty requests, where
two attempts of a few seconds each exhausted a 10 s attempt budget. Earlier
measurement had recorded generations of 14.7 s to 66.7 s, all of which would
have failed unary while the identical prompt streamed fine.

So the unary path passes ``header_window=None`` and is bounded by the client's
read timeout, ``provider_read_timeout_seconds`` (120 s, configurable), which is
the bound that is documented to span generation. A single window cannot mean
"handshake" and "whole generation" at once; scoping it is what separates them.

Scoping was not the whole answer. The handshake window then stayed at 5 s on the
strength of a p90 measured on short prompts, and T062 caught one streaming
attempt stalling exactly at that boundary and succeeding on retry. The retry is
the safety net, so the window was raised to 10 s *with* the attempt budget, which
moved from 10 s to 15 s to keep a second attempt reachable:
``attempt_budget_seconds >= window + backoff``. Raising the window alone would
have silently disabled retries, which is worse than a long wait.

``ROUTER_ROUTING_OVERHEAD_BUDGET_MS`` is deliberately *not* wired up. The
routing path is far too fast for a 500 ms assertion to ever fire, and the
mechanism would be dead code. Reintroduce it when routing logic is real.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "PROVIDER_TIMEOUT_SECONDS",
    "resolve_provider_timeout",
]

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 10.0
"""Default streaming-handshake budget: connection and response headers.

Applies to ``:streamGenerateContent`` / streaming chat completions only. Unary
requests do not use it (see the module docstring): their headers are the
generation, and they are bounded by the configured read timeout instead.

Sized from live measurement, not from the routing-overhead target. Fifteen live
calls to ``gemini-3.5-flash-lite`` measured p50 743 ms, p90 2332 ms, and
2893 ms maximum, so the previous 400 ms window failed real traffic roughly seven
times out of eight. Ten seconds is 4.3x the measured p90, which is the headroom
that matters for the cases the p90 does not cover: a cold start, or a prompt
large enough that processing finishes before the stream produces its first byte.

It was 5 s until 2026-10-31. That value was chosen from the p90 alone and left
2.1x headroom over it, which looked sufficient until T062 measured one streaming
attempt stalling at exactly the old window and recovering on retry. The retry is
what makes this survivable, so the raise is paired with
``attempt_budget_seconds`` (15 s, one retry) rather than made alone: a window
that consumes the whole budget turns ``max_attempts`` into decoration and a
stall into an immediate failure. See ``core.config.DispatchConfig`` for the
invariant, which is now enforced rather than documented.

That 400 ms came from spec 8, which wrapped the provider call in the same
budget that CONSTITUTION 2.5 and ROADMAP section 4 set for *routing decisions*.
Those are different things.

Overridable via ``ROUTER_PROVIDER_TIMEOUT`` so M1 can give each failover
attempt its own budget without editing this constant.
"""


def resolve_provider_timeout() -> float:
    """Read ``ROUTER_PROVIDER_TIMEOUT``, falling back to the default.

    Raises:
        ValueError: If the override is not a positive number. Failing here is
            better than silently accepting a zero or negative budget, which
            would time out every request immediately.
    """
    raw = os.environ.get("ROUTER_PROVIDER_TIMEOUT")
    if raw is None:
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"ROUTER_PROVIDER_TIMEOUT must be a number, got '{raw}'.") from exc
    if value <= 0:
        raise ValueError(f"ROUTER_PROVIDER_TIMEOUT must be positive, got {value}.")
    return value


PROVIDER_TIMEOUT_SECONDS = resolve_provider_timeout()
"""Effective provider timeout, in seconds."""
