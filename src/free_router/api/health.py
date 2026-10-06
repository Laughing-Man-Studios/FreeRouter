"""Liveness endpoint for the container healthcheck.

Deliberately trivial and dependency-free. It reports that the ASGI app is
serving traffic, which is what a Docker ``HEALTHCHECK`` needs to distinguish
"still starting" from "crashed".

It deliberately does **not** check provider reachability or configuration
beyond what startup already validated. A healthcheck exists to restart a dead
container, and Docker restarts on *unhealthy*, so a probe that failed on a
transient upstream outage would cause restart loops against a router that is
working exactly as intended.

It is also mounted at ``/health`` rather than under ``/v1``: it is an
operational endpoint, not part of the OpenAI-compatible surface, and a client
library pointed at this base URL should not see it advertised as an OpenAI
capability.
"""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["health_router"]

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health() -> dict[str, str]:
    """Return liveness status.

    Reaching this handler at all proves the app started: the lifespan has
    already completed, and it refuses to serve traffic if configuration or the
    database failed.

    Returns:
        A minimal status document.
    """
    return {"status": "ok"}
