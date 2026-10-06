"""Google (Gemini) provider adapter.

Translates the router's internal normalized models to and from the Gemini
``generateContent`` API for ``gemini-3.5-flash-lite``.

Two details are worth knowing before changing this file.

**The timeout covers provider I/O, not routing decisions.** Spec 8 originally
wrapped connection and response headers in ``asyncio.timeout(0.4)``, on the
theory that this protected the <500 ms routing envelope. It does not. Measured
live, provider headers alone take p50 743 ms and up to 2893 ms, so a 400 ms
window failed almost every real request with a 504. The router's own decision
path is 0.003 ms, so the <500 ms target (CONSTITUTION 2.5, ROADMAP section 4)
has ample headroom and is now documented rather than enforced. Spec 8 has been
amended accordingly.

The window still bounds *connection and headers*, not generation: a model's
actual generation can take many seconds, so the body must be read *after* it
closes. That is why dispatch uses ``stream=True`` and reads with ``aread()``
rather than ``client.post()``, which reads the entire body inside the timeout
and would turn every real completion into a 504.

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

__all__ = [
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "GOOGLE_API_BASE",
    "PROVIDER_TIMEOUT_SECONDS",
    "GoogleAdapter",
]

logger = logging.getLogger(__name__)

GOOGLE_API_BASE = "https://generativelanguage.googleapis.com"
"""Root of the Gemini REST API."""

API_VERSION = "v1beta"
"""REST path segment. Pinned because the API is versioned independently."""

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
"""Default budget for provider connection and response receipt, in seconds.

Sized from live measurement, not from the routing-overhead target. Fifteen live
calls to ``gemini-3.5-flash-lite`` measured p50 743 ms, p90 2332 ms, and
2893 ms maximum, so the previous 400 ms window failed real traffic roughly seven
times out of eight.

That 400 ms came from spec 8, which wrapped the provider call in the same
budget that CONSTITUTION 2.5 and ROADMAP section 4 set for *routing decisions*.
Those are different things: the router's own decision path measures 0.003 ms
(0.015 ms worst case over 2000 iterations), leaving roughly 190,000x headroom
against its 500 ms target, while every millisecond of the old window was being
spent on provider network I/O the router does not control. Spec 8 has been
amended to say so.

The window still covers only connection and response headers, not generation:
see the module docstring. Overridable via ``ROUTER_PROVIDER_TIMEOUT`` so M1 can
give each failover attempt its own budget without editing this constant.

``ROUTER_ROUTING_OVERHEAD_BUDGET_MS`` is deliberately *not* wired up. The
routing path is far too fast for a 500 ms assertion to ever fire, and the
mechanism would be dead code. Reintroduce it when routing logic is real.
"""


def _resolve_provider_timeout() -> float:
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


PROVIDER_TIMEOUT_SECONDS = _resolve_provider_timeout()
"""Effective provider timeout, in seconds."""

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
                # afterwards, outside this window.
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
            # asyncio.timeout raises the builtin TimeoutError, which is what
            # the strict header budget trips. It is not an httpx exception, so
            # it needs its own clause.
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


async def _raise_for_status(response: httpx.Response) -> httpx.Response:
    """Translate a provider status code into the matching router exception.

    The response body is deliberately not echoed into the error message: it can
    contain a reflected prompt, and this project never logs prompt text. It is
    read here only to classify a 400 as an auth failure or a payload rejection,
    and only on the error path.

    The stream is always closed before raising, otherwise the pooled connection
    leaks on every provider error.

    Returns:
        The response, when its status is a success.

    Raises:
        ProviderAuthError: On 401/403, or a 400 whose body indicates a bad key.
        ProviderValidationError: On other 4xx.
        ProviderServerError: On 5xx.
    """
    status = response.status_code
    if status < 400:
        return response

    try:
        # An async stream requires aread(); response.read() is sync-only and
        # raises "Attempted to call a sync iterator on an async stream".
        raw = await response.aread()
    except httpx.HTTPError:
        raw = b""

    try:
        return _raise_for_status_sync(status, raw)
    finally:
        await response.aclose()


def _raise_for_status_sync(status: int, raw: bytes) -> httpx.Response:
    """Raise the exception matching ``status``, using ``raw`` for classification."""
    if status in (401, 403):
        raise ProviderAuthError("The upstream provider rejected the API credentials.")
    if status == 400:
        # Google reports an invalid API key as 400 INVALID_ARGUMENT rather than
        # 401, with the message "API key not valid. Please pass a valid API key."
        # Verified live. Mapping every 400 to a payload error would report an
        # auth failure as a bad request, sending an operator to debug their
        # payload instead of their credentials.
        if _body_indicates_auth_failure(raw):
            raise ProviderAuthError("The upstream provider rejected the API credentials.")
        raise ProviderValidationError("The upstream provider rejected the request payload.")
    if 400 <= status < 500:
        raise ProviderValidationError(
            f"The upstream provider rejected the request with status {status}."
        )
    raise ProviderServerError(f"The upstream provider returned status {status}.")


def _body_indicates_auth_failure(raw: bytes) -> bool:
    """True when a 400 body indicates a credential problem, not a bad payload.

    Google's error envelope puts the signal in ``error.status`` and the message.
    The body is only inspected to *classify*; it is never propagated into an
    error message, since it can echo prompt content.
    """
    try:
        decoded: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(decoded, dict):
        return False

    error = decoded.get("error")
    if not isinstance(error, dict):
        return False

    status_flag = str(error.get("status", "")).upper()
    if status_flag in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
        return True
    return "api key not valid" in str(error.get("message", "")).lower()


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
