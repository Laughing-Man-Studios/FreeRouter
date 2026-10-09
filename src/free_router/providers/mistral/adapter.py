"""Mistral provider adapter.

Translates the router's internal normalized models to and from Mistral's
``/v1/chat/completions`` API.

**This adapter is deliberately much thinner than the Google one**, and that is
a finding rather than an oversight. Verified against Mistral's published
OpenAPI spec and API reference (2026-10-08):

- ``system`` is a first-class role *inside* ``messages``
  (``SystemMessage.role`` is ``const "system"``). There is no
  ``systemInstruction`` equivalent, so nothing has to be hoisted out of the
  message list the way Gemini requires. ``to_mistral_request`` is therefore
  close to identity.
- ``finish_reason`` is already ``stop``/``length``, the OpenAI vocabulary, so
  there is no finish-reason mapping table.
- The error envelope is ``{object, message, type, param, code}`` — the same
  shape this router emits — so there is no error-body translation.

**There is deliberately no bad-key-versus-bad-payload heuristic here.** The
Google adapter needs one because Gemini returns ``400 INVALID_ARGUMENT`` with
"API key not valid" for a rejected key, which would otherwise be reported as a
payload error. Mistral documents ``401``/``403`` for authentication and uses a
distinct ``authentication_error`` type, so status alone is sufficient. Copying
Google's heuristic would add a branch that Mistral's error taxonomy can never
reach.

**Only the messages the normalizer kept are sent.** M0 honours ``model`` and
``messages`` exclusively; ``temperature`` and ``max_tokens`` are stripped at
ingress and have no representation here. Mistral accepts both, and
``temperature`` is capped at 1.5 rather than 2.0, but wiring them up is scope
for a later milestone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from free_router.core.normalization import (
    FinishReason,
    NormalizedRequest,
    NormalizedResponse,
)
from free_router.providers.timeout import PROVIDER_TIMEOUT_SECONDS

__all__ = [
    "MISTRAL_API_BASE",
    "MISTRAL_CHAT_PATH",
    "PROVIDER_TIMEOUT_SECONDS",
    "MistralAdapter",
]

logger = logging.getLogger(__name__)

MISTRAL_API_BASE = "https://api.mistral.ai"
"""Root of the Mistral REST API."""

MISTRAL_CHAT_PATH = "/v1/chat/completions"
"""Chat completion path. Unlike Google's ``v1beta`` this is unversioned here.

Mistral publishes the endpoint as ``/v1/chat/completions`` and documents no
beta variant for chat completions, so there is nothing to pin.
"""

# Mistral documents these finish reasons: "stop", "length", "model_length",
# "tool_calls", "content_filter". The first three are the only ones M1 can
# reach, since tool calls are stripped at ingress. Anything unrecognised falls
# back to "stop" rather than raising: an unknown termination reason is not a
# reason to fail a completion the provider already produced.
_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "model_length": "length",
    "content_filter": "content_filter",
}


class MistralAdapter:
    """Dispatch chat completions to the Mistral ``/v1/chat/completions`` API."""

    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        """Store the shared HTTP client.

        Args:
            http_client: The lifespan-owned ``httpx.AsyncClient`` singleton.
        """
        self._http_client = http_client

    async def chat_completion(
        self, request: NormalizedRequest, provider_model_id: str
    ) -> NormalizedResponse:
        """Send one request to Mistral and normalise the reply.

        Args:
            request: The internal normalized request.
            provider_model_id: The Mistral-side model id, e.g.
                ``ministral-3b-2512``, as resolved from configuration.

        Returns:
            The internal normalized response.

        Raises:
            ProviderTimeoutError: If connection or headers exceed the budget.
            ProviderRateLimitError: If Mistral returns 429.
            ProviderAuthError: If Mistral rejects the API key.
            ProviderValidationError: If Mistral rejects the payload.
            ProviderServerError: If Mistral returns 5xx or an unusable body.
        """
        url = f"{MISTRAL_API_BASE}{MISTRAL_CHAT_PATH}"
        headers = {
            # Bearer, per Mistral's documented curl example. The key travels in
            # a header rather than a query parameter so it cannot appear in
            # URLs, proxy logs, or access logs.
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = to_mistral_request(request, provider_model_id)

        response = await self._send(url, headers, payload)
        raw = await self._read_body(response)
        return from_mistral_response(raw, provider_model_id)

    async def _send(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> httpx.Response:
        """Dispatch under the strict header-receipt timeout.

        Raises:
            ProviderTimeoutError: If connection or headers exceed the budget.
            ProviderServerError: If the provider cannot be reached at all.
        """
        try:
            build = self._http_client.build_request("POST", url, json=payload, headers=headers)
            async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
                # stream=True returns once response headers arrive, so the
                # timeout bounds connection + headers only. The body is read
                # afterwards, outside this window. See providers.timeout for why
                # that distinction matters.
                response = await self._http_client.send(build, stream=True)
        except httpx.TimeoutException as exc:
            # Checked before the broader httpx.HTTPError below, since a timeout
            # is a subclass of it and must map to 504, not 502.
            logger.warning(
                "provider_timeout",
                extra={"event": "provider_timeout", "error_category": "provider_timeout"},
            )
            raise ProviderTimeoutError(
                f"The upstream provider did not respond within {PROVIDER_TIMEOUT_SECONDS}s."
            ) from exc
        except httpx.HTTPError as exc:
            # Transport failures (connection refused, DNS, TLS, reset) are
            # raised as exceptions rather than returned as responses, so they
            # bypass status mapping entirely. Uncaught they reach the generic
            # handler and are reported as a router fault, which misattributes a
            # network problem to the router itself.
            logger.warning(
                "provider_transport_error",
                extra={
                    "event": "provider_transport_error",
                    "error_category": "provider_transport_error",
                },
            )
            raise ProviderServerError("The upstream provider could not be reached.") from exc
        except TimeoutError as exc:
            # asyncio.timeout raises the builtin TimeoutError, which is what the
            # strict header budget trips. It is not an httpx exception, so it
            # needs its own clause.
            logger.warning(
                "provider_timeout",
                extra={"event": "provider_timeout", "error_category": "provider_timeout"},
            )
            raise ProviderTimeoutError(
                f"The upstream provider did not respond within {PROVIDER_TIMEOUT_SECONDS}s."
            ) from exc

        try:
            return await _raise_for_status(response)
        except Exception:
            # _raise_for_status closes the stream before raising, so this is
            # only a backstop for an unexpected failure inside it.
            await response.aclose()
            raise

    async def _read_body(self, response: httpx.Response) -> dict[str, Any]:
        """Read and decode the response body outside the header timeout.

        The stream must always be closed, including when a timeout interrupts
        the read, or the pooled connection leaks.

        Raises:
            ProviderTimeoutError: If reading the body times out.
            ProviderServerError: If the body is not a JSON object.
        """
        try:
            raw = await response.aread()
        except TimeoutError as exc:
            raise ProviderTimeoutError("Timed out reading the provider response body.") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Timed out reading the provider response body.") from exc
        finally:
            # Always release the connection, including when a timeout interrupts
            # the read, or the pooled connection leaks.
            await response.aclose()

        try:
            decoded: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderServerError(
                "The upstream provider returned a response that could not be decoded."
            ) from exc

        if not isinstance(decoded, dict):
            raise ProviderServerError(
                "The upstream provider returned an unexpected response shape."
            )
        return decoded


def to_mistral_request(request: NormalizedRequest, provider_model_id: str) -> dict[str, Any]:
    """Translate a :class:`NormalizedRequest` into a Mistral payload.

    Messages pass through in order with their roles intact, because ``system``
    is a native role here rather than something to be lifted out. The only
    substitution is the model: the router's external ``provider/model`` id is
    replaced by the provider-native id resolved from configuration.

    Args:
        request: The internal normalized request.
        provider_model_id: The Mistral-side model id.

    Returns:
        A JSON-ready ``chat.completions`` body.
    """
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        content = message.get("content", "")
        # Guard the invariant the ingress normalizer already enforced, so this
        # function is safe to call from a test with a hand-built request.
        if not isinstance(content, str):
            content = str(content)
        messages.append({"role": message.get("role"), "content": content})

    # stream=False is explicit rather than omitted. The response is read with
    # aread() outside the header timeout, which requires a complete body, and
    # relying on Mistral's default would make that an implicit coupling.
    return {"model": provider_model_id, "messages": messages, "stream": False}


def from_mistral_response(raw: dict[str, Any], provider_model_id: str) -> NormalizedResponse:
    """Translate a Mistral response into a :class:`NormalizedResponse`.

    Args:
        raw: The decoded ``chat.completions`` body.
        provider_model_id: The model that served the request.

    Returns:
        The internal normalized response.

    Raises:
        ProviderServerError: If the response carries no choices, or the first
            choice has no message. Raising an explicit error avoids an opaque
            ``IndexError`` or ``TypeError`` becoming a 500.
    """
    choices = raw.get("choices") or []
    if not choices:
        raise ProviderServerError("The upstream provider returned no completion choices.")

    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderServerError("The upstream provider returned an unexpected response shape.")

    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderServerError("The upstream provider returned a choice without a message.")

    content = message.get("content")
    if not isinstance(content, str):
        # A null content is how Mistral represents a message carrying only
        # tool_calls. M0 strips tools at ingress, so this is not reachable, and
        # an empty string is a safer answer than raising on a complete response.
        content = "" if content is None else str(content)

    finish_reason: FinishReason = _FINISH_REASON_MAP.get(
        str(choice.get("finish_reason", "stop")), "stop"
    )

    return NormalizedResponse(
        content=content,
        model_used=provider_model_id,
        finish_reason=finish_reason,
    )


async def _raise_for_status(response: httpx.Response) -> httpx.Response:
    """Translate a provider status code into the matching router exception.

    The response body is deliberately not read and never echoed into the error
    message: it can contain a reflected prompt, and this project never logs
    prompt text. Mistral's envelope does identify auth failures with a
    ``type`` of ``authentication_error``, but its status codes already
    distinguish them, so the body is not needed for classification here.

    The stream is always closed before raising, otherwise the pooled connection
    leaks on every provider error.

    Returns:
        The response, when its status is a success.

    Raises:
        ProviderRateLimitError: On 429.
        ProviderAuthError: On 401/403.
        ProviderValidationError: On other 4xx, including 422.
        ProviderServerError: On 5xx.
    """
    status = response.status_code
    if status < 400:
        return response

    try:
        # An async stream requires aread(); response.read() is sync-only and
        # raises "Attempted to call a sync iterator on an async stream".
        await response.aread()
    except httpx.HTTPError:
        pass
    finally:
        await response.aclose()

    if status == 429:
        raise ProviderRateLimitError("The upstream provider is rate limiting this key.")
    if status in (401, 403):
        raise ProviderAuthError("The upstream provider rejected the API credentials.")
    if 400 <= status < 500:
        # 422 is included here: Mistral's OpenAPI spec documents it as a
        # FastAPI-shaped validation error ({detail: [...]}) rather than the
        # documented {object, message, type, param, code} envelope, so the body
        # is not parsed for classification.
        raise ProviderValidationError(
            f"The upstream provider rejected the request with status {status}."
        )
    raise ProviderServerError(f"The upstream provider returned status {status}.")


def _api_key() -> str:
    """Read the API key from the process environment.

    Reads the environment directly rather than taking a ``Config`` argument so
    the adapter stays independent of the configuration layer, matching the
    interface described in the plan and the existing Google adapter.

    Raises:
        ProviderAuthError: If the key is unset. Startup already fails fast on
            this, so reaching here means the environment changed mid-run.
    """
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise ProviderAuthError("MISTRAL_API_KEY is not set.")
    return key
