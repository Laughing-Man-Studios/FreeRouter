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

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
"""Default budget for provider connection and response receipt, in seconds.

Sized from live measurement, not from the routing-overhead target. Fifteen live
calls to ``gemini-3.5-flash-lite`` measured p50 743 ms, p90 2332 ms, and
2893 ms maximum, so the previous 400 ms window failed real traffic roughly seven
times out of eight.

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
