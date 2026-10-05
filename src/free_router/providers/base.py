"""The provider adapter interface and its selection factory.

This module is the boundary the router core depends on. The core hands an
adapter a :class:`NormalizedRequest` and receives a
:class:`NormalizedResponse`; it has no knowledge of Gemini payloads, auth
headers, or provider error codes. That is the separation the long-term
architecture requires, and it is what lets a second provider be added as an
adapter rather than a rewrite (CONSTITUTION 3, ROADMAP section 11).

Adapters are constructed per request rather than held as singletons because
they are stateless: the only shared resource is the ``httpx.AsyncClient``
singleton from the lifespan, which is passed in.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from free_router.core.exceptions import UnsupportedModelError
from free_router.core.normalization import NormalizedRequest, NormalizedResponse

__all__ = ["ProviderAdapter", "build_adapter", "supported_providers"]


@runtime_checkable
class ProviderAdapter(Protocol):
    """The interface every provider adapter implements."""

    async def chat_completion(
        self, request: NormalizedRequest, provider_model_id: str
    ) -> NormalizedResponse:
        """Translate, dispatch, and normalise one chat completion.

        Implementations translate the request into their provider's native
        payload, call the provider through the supplied client, and translate
        the reply back into a :class:`NormalizedResponse`.

        Args:
            request: The provider-agnostic request.
            provider_model_id: The provider-native model id resolved from
                configuration, e.g. ``gemini-3.5-flash-lite``.

        Returns:
            The provider-agnostic response.

        Raises:
            ProviderBaseError: If the provider rejects the request, fails, or
                does not respond inside the routing envelope.
        """
        ...


def supported_providers() -> frozenset[str]:
    """Return the provider names this build can dispatch to."""
    return frozenset({"google"})


def build_adapter(provider: str, http_client: httpx.AsyncClient) -> ProviderAdapter:
    """Construct the adapter for a configured provider.

    Imported lazily so that the adapter module is only loaded when a request
    actually needs it, and so a future provider can be added without widening
    this module's import-time dependencies.

    Args:
        provider: The provider name from ``config.yaml``, e.g. ``google``.
        http_client: The shared ``httpx.AsyncClient`` singleton.

    Returns:
        An adapter satisfying :class:`ProviderAdapter`.

    Raises:
        UnsupportedModelError: If no adapter is registered for the provider.
    """
    match provider:
        case "google":
            from free_router.providers.google.adapter import GoogleAdapter

            # The provider model id is resolved from configuration by the route
            # handler and passed to chat_completion, so the adapter itself stays
            # provider-agnostic about which model it serves.
            return GoogleAdapter(http_client=http_client)

    raise UnsupportedModelError(
        f"No adapter is registered for provider '{provider}'. "
        f"Supported providers: {', '.join(sorted(supported_providers()))}.",
        param="model",
    )
