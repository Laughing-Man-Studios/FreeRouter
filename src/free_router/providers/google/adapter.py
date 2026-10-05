"""Google (Gemini) provider adapter.

Translates the router's internal normalized models to and from the Gemini
``generateContent`` API for ``gemini-3.5-flash-lite``.

Two details are worth knowing before changing this file.

**The timeout covers headers, not the body.** The spec requires the "initial
provider connection and response header phase" to finish inside a strict
``asyncio.timeout(0.4)`` window, protecting the <500 ms routing envelope. A
model's actual generation routinely takes seconds, so the body must be read
*after* that window closes. This is why dispatch uses ``stream=True`` and reads
with ``aread()`` instead of ``client.post()``: the convenience method reads the
entire body inside the timeout and would turn every real completion into a 504.

**Only the messages the normalizer kept are sent.** M0 honours ``model`` and
``messages`` exclusively; ``temperature`` and ``max_tokens`` were already
stripped at ingress and have no representation here. Adding a generationConfig
would be scope for a later milestone, not a fix.

Note the API surface: Gemini's newer Interactions API is not used here. The
spec mandates ``generateContent``, and confining all Gemini specifics to this
module keeps a future migration contained.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import httpx

from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from free_router.core.normalization import (
    FinishReason,
    NormalizedRequest,
    NormalizedResponse,
)

__all__ = ["GOOGLE_API_BASE", "PROVIDER_TIMEOUT_SECONDS", "GoogleAdapter"]

logger = logging.getLogger(__name__)

GOOGLE_API_BASE = "https://generativelanguage.googleapis.com"
"""Root of the Gemini REST API."""

API_VERSION = "v1beta"
"""REST path segment. Pinned because the API is versioned independently."""

PROVIDER_TIMEOUT_SECONDS = 0.4
"""Strict budget for connection and response-header receipt (spec 8, <500 ms)."""

_FINISH_REASON_MAP: Mapping[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
}
"""Gemini ``finishReason`` values mapped to the OpenAI vocabulary."""


class GoogleAdapter:
    """Dispatch chat completions to the Gemini ``generateContent`` API."""

    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        """Store the shared HTTP client.

        Args:
            http_client: The lifespan-owned ``httpx.AsyncClient`` singleton.
        """
        self._http_client = http_client

    async def chat_completion(
        self, request: NormalizedRequest, provider_model_id: str
    ) -> NormalizedResponse:
        """Send one request to Gemini and normalise the reply.

        Args:
            request: The internal normalized request.
            provider_model_id: The Gemini-side model id, e.g.
                ``gemini-3.5-flash-lite``, as resolved from configuration.

        Returns:
            The internal normalized response.

        Raises:
            ProviderTimeoutError: If connection or headers exceed 0.4 s.
            ProviderAuthError: If Gemini rejects the API key.
            ProviderValidationError: If Gemini rejects the payload.
            ProviderServerError: If Gemini returns 5xx or an unusable body.
        """
        url = f"{GOOGLE_API_BASE}/{API_VERSION}/models/{provider_model_id}:generateContent"
        headers = {
            # The key travels in a header rather than a query parameter so it
            # cannot appear in URLs, proxy logs, or access logs.
            "x-goog-api-key": _api_key(),
            "Content-Type": "application/json",
        }
        payload = to_gemini_request(request)

        response = await self._send(url, headers, payload)
        raw = await self._read_body(response)
        return from_gemini_response(raw, provider_model_id)

    async def _send(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> httpx.Response:
        """Dispatch under the strict header-receipt timeout."""
        try:
            build = self._http_client.build_request("POST", url, json=payload, headers=headers)
            async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
                # stream=True returns once response headers arrive, so the
                # timeout bounds connection + headers only. The body is read
                # afterwards, outside this window.
                response = await self._http_client.send(build, stream=True)
        except TimeoutError as exc:
            logger.warning(
                "provider_timeout",
                extra={"event": "provider_timeout", "error_category": "provider_timeout"},
            )
            raise ProviderTimeoutError(
                f"The upstream provider did not respond within {PROVIDER_TIMEOUT_SECONDS}s."
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning(
                "provider_timeout",
                extra={"event": "provider_timeout", "error_category": "provider_timeout"},
            )
            raise ProviderTimeoutError(
                f"The upstream provider did not respond within {PROVIDER_TIMEOUT_SECONDS}s."
            ) from exc

        return _raise_for_status(response)

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


def to_gemini_request(request: NormalizedRequest) -> dict[str, Any]:
    """Translate a :class:`NormalizedRequest` into a Gemini payload.

    System messages become Gemini's top-level ``systemInstruction`` because the
    API has no ``system`` role inside ``contents``. Remaining messages keep
    their order.

    Args:
        request: The internal normalized request.

    Returns:
        A JSON-ready ``generateContent`` body.
    """
    contents: list[dict[str, Any]] = []
    system_instruction: dict[str, Any] | None = None

    for message in request.messages:
        role = message.get("role")
        content = message.get("content", "")
        # Guard the invariant the ingress normalizer already enforced, so this
        # function is safe to call from a test with a hand-built request.
        if not isinstance(content, str):
            content = str(content)

        if role == "system":
            if system_instruction is None:
                system_instruction = {"parts": [{"text": content}]}
            else:
                system_instruction["parts"].append({"text": content})
            continue

        contents.append({"role": role, "parts": [{"text": content}]})

    payload: dict[str, Any] = {"contents": contents}
    if system_instruction is not None:
        payload["systemInstruction"] = system_instruction
    return payload


def from_gemini_response(raw: dict[str, Any], provider_model_id: str) -> NormalizedResponse:
    """Translate a Gemini response into a :class:`NormalizedResponse`.

    Args:
        raw: The decoded ``generateContent`` body.
        provider_model_id: The model that served the request.

    Returns:
        The internal normalized response.

    Raises:
        ProviderServerError: If the response has no usable candidate, which
            happens when safety filters empty the ``candidates`` list. Raising
            an explicit error avoids an opaque ``IndexError`` becoming a 500.
    """
    candidates = raw.get("candidates") or []
    if not candidates:
        raise ProviderServerError("The upstream provider returned no completion candidates.")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))

    raw_finish_reason = str(candidate.get("finishReason", "STOP")).upper()
    finish_reason: FinishReason = _FINISH_REASON_MAP.get(raw_finish_reason, "stop")

    return NormalizedResponse(
        content=text,
        model_used=provider_model_id,
        finish_reason=finish_reason,
    )


def _raise_for_status(response: httpx.Response) -> httpx.Response:
    """Translate a provider status code into the matching router exception.

    The response body is deliberately not echoed into the error message: it can
    contain a reflected prompt, and this project never logs prompt text.

    Returns:
        The response, when its status is a success.

    Raises:
        ProviderAuthError: On 401 or 403.
        ProviderValidationError: On 400 or other 4xx.
        ProviderServerError: On 5xx.
    """
    status = response.status_code
    if status < 400:
        return response

    if status in (401, 403):
        raise ProviderAuthError("The upstream provider rejected the API credentials.")
    if status == 400:
        raise ProviderValidationError("The upstream provider rejected the request payload.")
    if 400 <= status < 500:
        raise ProviderValidationError(
            f"The upstream provider rejected the request with status {status}."
        )
    raise ProviderServerError(f"The upstream provider returned status {status}.")


def _api_key() -> str:
    """Read the API key from the process environment.

    Reads the environment directly rather than taking a ``Config`` argument so
    the adapter stays independent of the configuration layer, matching the
    interface described in the plan.

    Raises:
        ProviderAuthError: If the key is unset. Startup already fails fast on
            this, so reaching here means the environment changed mid-run.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderAuthError("GEMINI_API_KEY is not set.")
    return key
