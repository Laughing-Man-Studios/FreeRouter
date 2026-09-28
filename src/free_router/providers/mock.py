"""Placeholder adapter used until the Google adapter lands in Batch 3.

It exists so Batch 2 can exercise the full ingress-to-egress path with no
outbound network call. The interface it implements is the same one
:mod:`free_router.providers.base` formalises in T010, so swapping in the real
Google adapter does not change the route handler.
"""

from __future__ import annotations

import httpx

from free_router.core.normalization import NormalizedRequest, NormalizedResponse

__all__ = ["MockAdapter"]

MOCK_CONTENT = "mock response"


class MockAdapter:
    """Return a deterministic response without contacting a provider."""

    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        """Store the shared client so the signature matches the real adapter."""
        self._http_client = http_client

    async def chat_completion(self, request: NormalizedRequest) -> NormalizedResponse:
        """Return a fixed completion for the given request.

        Args:
            request: The internal normalized request.

        Returns:
            A deterministic normalized response.
        """
        return NormalizedResponse(
            content=MOCK_CONTENT,
            model_used=request.model.split("/", maxsplit=1)[-1],
            finish_reason="stop",
        )
