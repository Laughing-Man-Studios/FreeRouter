"""Google (Gemini) provider adapter.

Translates the router's internal normalized models to and from the Gemini
``generateContent`` API for ``gemini-3.5-flash-lite``.

Two details are worth knowing before changing this file.

**The timeout is shared, not local.** ``PROVIDER_TIMEOUT_SECONDS`` is resolved
in :mod:`free_router.providers.timeout` and re-exported here, because it is a
cross-provider policy read from a single environment variable. Resolving it
per-adapter would leave two sources of truth that drift the first time the
default is retuned from measurement. See that module for why the window covers
provider I/O rather than routing decisions, and why it bounds headers but not
generation.

That window applies to the **streaming** call only. ``generateContent`` sends no
headers until the answer exists, so its header phase *is* the generation; the
unary path passes ``header_window=None`` and is bounded by the client's read
timeout instead. A single window cannot mean both things, and a 5 s one silently
capped unary generation at five seconds.

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
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from free_router.core.debug_logging import debug_payload
from free_router.core.exceptions import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from free_router.core.normalization import (
    FinishReason,
    NormalizedChunk,
    NormalizedRequest,
    NormalizedResponse,
    TokenUsage,
)
from free_router.keys.manager import KeyRef
from free_router.providers.sse import SSEDecodeError, iter_sse_data, response_lines
from free_router.providers.timeout import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    PROVIDER_TIMEOUT_SECONDS,
)
from free_router.quota.breaker import is_context_length_error
from free_router.quota.cooldown import parse_retry_after_seconds

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

_FINISH_REASON_MAP: Mapping[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
}
"""Gemini ``finishReason`` values mapped to the OpenAI vocabulary."""


def _timeout_message(header_window: float | None) -> str:
    """Describe which bound the provider exceeded.

    A message that always names the same number would be wrong half the time
    now that the two phases are bounded differently: a unary request has no
    header window at all, and reporting "5s" for a request that waited two
    minutes would send whoever reads it looking at the wrong setting.
    """
    if header_window is None:
        return "The upstream provider did not respond within the configured read timeout."
    return f"The upstream provider did not respond within {header_window}s."


class GoogleAdapter:
    """Dispatch chat completions to the Gemini ``generateContent`` API."""

    supports_streaming = True
    """Gemini streams via ``:streamGenerateContent``. See ``stream_chat_completion``."""

    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        """Store the shared HTTP client.

        Args:
            http_client: The lifespan-owned ``httpx.AsyncClient`` singleton.
        """
        self._http_client = http_client

    async def chat_completion(
        self, request: NormalizedRequest, provider_model_id: str, key: KeyRef
    ) -> NormalizedResponse:
        """Send one request to Gemini and normalise the reply.

        Args:
            request: The internal normalized request.
            provider_model_id: The Gemini-side model id, e.g.
                ``gemini-3.5-flash-lite``, as resolved from configuration.
            key: The credential to dispatch with. The material comes from the key
                pool, not from the environment, so the environment variable name
                is never handled here.

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
            "x-goog-api-key": key.secret.get_secret_value(),
            "Content-Type": "application/json",
        }
        payload = to_gemini_request(request)

        # The headers are deliberately not passed here: the credential travels in
        # x-goog-api-key, and the debug stream's redaction is a backstop for
        # unanticipated cases, not a licence to hand it a header mapping.
        debug_payload(
            phase="wire_request", provider="google", model=provider_model_id, data=payload
        )
        # No header window: see ``_send``. A unary Gemini response's headers are
        # its generation, so the read timeout is the bound that fits.
        response = await self._send(url, headers, payload, header_window=None)
        raw = await self._read_body(response)
        debug_payload(phase="wire_response", provider="google", model=provider_model_id, data=raw)
        return from_gemini_response(raw, provider_model_id)

    async def stream_chat_completion(
        self, request: NormalizedRequest, provider_model_id: str, key: KeyRef
    ) -> AsyncIterator[NormalizedChunk]:
        """Stream one completion from Gemini's ``:streamGenerateContent``.

        ``alt=sse`` is not optional. Without it Gemini answers with a JSON array
        that arrives only once the whole answer is generated, which would defeat
        streaming entirely while looking like it worked.

        The method is an async generator, so the header timeout in :meth:`_send`
        still runs before the first chunk, but the connection is held open for as
        long as generation takes. That asymmetry is why the route, not this
        adapter, enforces the fail-fast policy: only the route knows whether a
        chunk reached the client.

        Args:
            request: The internal normalized request.
            provider_model_id: The Gemini-side model id.
            key: The credential to dispatch with.

        Yields:
            One :class:`NormalizedChunk` per Gemini event carrying text, a finish
            reason, or usage.

        Raises:
            ProviderTimeoutError: If connection or headers exceed 0.4 s.
            ProviderAuthError: If Gemini rejects the API key.
            ProviderValidationError: If Gemini rejects the payload.
            ProviderServerError: If Gemini returns 5xx, an undecodable event, or
                the connection fails mid-stream.
        """
        url = (
            f"{GOOGLE_API_BASE}/{API_VERSION}/models/{provider_model_id}"
            ":streamGenerateContent?alt=sse"
        )
        headers = {
            "x-goog-api-key": key.secret.get_secret_value(),
            "Content-Type": "application/json",
        }
        payload = to_gemini_request(request)

        # Only the request is logged. A streamed body has no single response to
        # log, and logging every chunk would turn one debug line into one line per
        # token -- too much noise to read and too much content to justify keeping.
        debug_payload(
            phase="wire_request", provider="google", model=provider_model_id, data=payload
        )
        response = await self._send(url, headers, payload)

        try:
            async for event in iter_sse_data(response_lines(response)):
                chunk = _chunk_from_gemini(event)
                if chunk is not None:
                    yield chunk
        except SSEDecodeError as exc:
            # A malformed event is an upstream fault, so it joins the rest of the
            # provider failures rather than escaping as a decode error.
            raise ProviderServerError(
                "The upstream provider returned a stream event that could not be decoded."
            ) from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            # A mid-stream silence is a timeout, not a server fault: the provider
            # accepted the request and then stopped sending. Classified
            # separately so the pre-delivery status is 504 rather than 502.
            raise ProviderTimeoutError("The upstream provider stream stalled.") from exc
        except httpx.HTTPError as exc:
            raise ProviderServerError("The upstream provider stream failed.") from exc

    async def _send(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        header_window: float | None = PROVIDER_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        """Dispatch, optionally under the strict header-receipt timeout.

        Args:
            url: The endpoint.
            headers: Request headers.
            payload: The provider-shaped request body.
            header_window: Seconds allowed for connection and response headers,
                or ``None`` to leave the wait bounded by the client's read
                timeout instead.

                ``None`` is correct for a **unary** request and wrong for a
                streaming one, because the same phase means two different
                things. ``:generateContent`` sends no headers until the answer
                has been generated, so bounding its headers bounds the whole
                generation; a 5 s window there means a 6 s generation cannot be
                served unary however patient the client is. ``:streamGenerateContent``
                sends headers as soon as it starts talking, so the same window
                bounds only the handshake -- and a short one is exactly what
                makes a stalled handshake cheap to retry.

        Raises:
            ProviderTimeoutError: If connection or headers exceed the budget.
            ProviderServerError: If the provider cannot be reached at all.
        """
        try:
            build = self._http_client.build_request("POST", url, json=payload, headers=headers)
            if header_window is None:
                # Bounded by the client's read timeout, which is the configured
                # span for generation (``provider_read_timeout_seconds``).
                response = await self._http_client.send(build, stream=True)
            else:
                async with asyncio.timeout(header_window):
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
            raise ProviderTimeoutError(_timeout_message(header_window)) from exc
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
            raise ProviderTimeoutError(_timeout_message(header_window)) from exc

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


def _usage_from_gemini(raw: dict[str, Any]) -> TokenUsage | None:
    """Translate Gemini's ``usageMetadata`` into token counts.

    Gemini reports the counts as **strings**, not numbers, so they are parsed
    rather than trusted.

    Returns ``None`` when the body carries no usage at all, which leaves the
    reservation's estimate standing rather than releasing capacity that may
    well have been used.
    """
    metadata = raw.get("usageMetadata")
    if not isinstance(metadata, dict):
        return None

    def _count(name: str) -> int:
        value = metadata.get(name)
        if value is None:
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    total = metadata.get("totalTokenCount")
    try:
        total_count = None if total is None else max(0, int(total))
    except (TypeError, ValueError):
        total_count = None

    input_tokens = _count("promptTokenCount")
    output_tokens = _count("candidatesTokenCount")
    if input_tokens == 0 and output_tokens == 0 and total_count is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_count,
    )


def _chunk_from_gemini(event: dict[str, Any]) -> NormalizedChunk | None:
    """Translate one Gemini stream event, or drop it if it carries nothing.

    Gemini's stream is not one event per token with a uniform shape. It sends
    events with no candidates at all (usage only), and ``finishReason`` appears
    on the same event as the last text rather than on one of its own. Returning
    ``None`` for an event with no content, no finish reason and no usage keeps
    the writer from emitting empty deltas the client would have to filter.

    Args:
        event: One decoded ``data:`` payload.

    Returns:
        The normalized chunk, or ``None`` if the event carries nothing usable.
    """
    candidates = event.get("candidates") or []
    delta = ""
    finish_reason: FinishReason | None = None

    if candidates and isinstance(candidates[0], dict):
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        delta = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        raw_finish_reason = candidate.get("finishReason")
        if raw_finish_reason:
            # An unrecognised reason maps to "stop" for the same reason the unary
            # path does it: the OpenAI vocabulary has three values and a client
            # needs one of them.
            finish_reason = _FINISH_REASON_MAP.get(str(raw_finish_reason).upper(), "stop")

    usage = _usage_from_gemini(event)
    if not delta and finish_reason is None and usage is None:
        return None
    return NormalizedChunk(delta=delta, finish_reason=finish_reason, usage=usage)


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
        usage=_usage_from_gemini(raw),
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
        return _raise_for_status_sync(status, raw, response.headers)
    finally:
        await response.aclose()


def _raise_for_status_sync(
    status: int,
    raw: bytes,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Raise the exception matching ``status``, using ``raw`` for classification.

    Gemini's own vocabulary is carried on every error as ``provider_code`` --
    ``INVALID_ARGUMENT``, ``RESOURCE_EXHAUSTED``, ``UNAVAILABLE`` -- because the
    HTTP status alone cannot separate a payload Google disliked from a
    credential it rejected, and both arrive as ``400``.

    Args:
        status: The HTTP status.
        raw: The error body. Read only to classify; never echoed into a message.
        headers: The response headers, for a standard ``Retry-After``.

    Returns:
        Never: every branch raises.
    """
    envelope = _google_error_envelope(raw)
    provider_code = _google_provider_code(envelope)

    if status in (401, 403):
        raise ProviderAuthError(
            "The upstream provider rejected the API credentials.",
            provider_code=provider_code,
        )
    if status == 429:
        # A rate limit is a capacity signal, not a malformed request. Mapping it
        # to the generic 4xx branch reported it to the client as
        # 400 invalid_request_error, which reads as "your payload is wrong".
        #
        # The provider's own hint is taken where it exists -- the header first,
        # then google.rpc.RetryInfo in the body, which Gemini sends when the
        # header is absent. Trusting it is A4 item 4; guessing is what the
        # cooldown's configured floor is for.
        raise ProviderRateLimitError(
            "The upstream provider is rate limiting this key.",
            retry_after=parse_retry_after_seconds(headers) or _retry_info_seconds(envelope),
            provider_code=provider_code,
        )
    if status == 400:
        # Google reports an invalid API key as 400 INVALID_ARGUMENT rather than
        # 401, with the message "API key not valid. Please pass a valid API key."
        # Verified live. Mapping every 400 to a payload error would report an
        # auth failure as a bad request, sending an operator to debug their
        # payload instead of their credentials.
        if _body_indicates_auth_failure(raw):
            raise ProviderAuthError(
                "The upstream provider rejected the API credentials.",
                provider_code=provider_code,
            )
        raise ProviderValidationError(
            "The upstream provider rejected the request payload.",
            context_length=body_indicates_context_length_error(raw),
            provider_code=provider_code,
        )
    if 400 <= status < 500:
        raise ProviderValidationError(
            f"The upstream provider rejected the request with status {status}.",
            context_length=body_indicates_context_length_error(raw),
            provider_code=provider_code,
        )
    # A 5xx is retryable *only* when it reads as overload rather than a defect
    # (A4 item 3). Retrying a provider bug spends another call and learns
    # nothing, which is precisely the quota this router exists not to burn.
    raise ProviderServerError(
        f"The upstream provider returned status {status}.",
        provider_code=provider_code,
        retryable=status in _RETRYABLE_SERVER_STATUSES
        or provider_code in _RETRYABLE_GOOGLE_STATUSES,
    )


_RETRYABLE_SERVER_STATUSES = frozenset({502, 503, 504})
"""HTTP 5xx statuses that mean "busy", not "broken"."""

_RETRYABLE_GOOGLE_STATUSES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED"})
"""Gemini's own retryable set, per its published error guidance.

``INTERNAL`` is deliberately absent: it maps to 500 and is the defect case
A4 item 3 asks to stop retrying.
"""


def _google_error_envelope(raw: bytes) -> dict[str, Any]:
    """Extract ``error`` from a Gemini error body, or an empty mapping."""
    try:
        decoded: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    error = decoded.get("error")
    return error if isinstance(error, dict) else {}


def _google_provider_code(envelope: Mapping[str, Any]) -> str | None:
    """Gemini's canonical error code, e.g. ``INVALID_ARGUMENT``.

    ``status`` rather than the numeric ``code``: the string is the value people
    search for and the one Google's own guidance is written in terms of.
    """
    code = envelope.get("status")
    return str(code) if code else None


def _retry_info_seconds(envelope: Mapping[str, Any]) -> float | None:
    """Read ``google.rpc.RetryInfo.retryDelay`` such as ``"37s"``.

    Gemini frequently omits ``Retry-After`` while still saying how long to wait
    in the body, so ignoring this field would discard the only hint available.
    """
    details = envelope.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            continue
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return max(0.0, float(delay[:-1]))
            except ValueError:
                return None
    return None


def body_indicates_context_length_error(raw: bytes) -> bool:
    """True when a ``400`` body is a context-length refusal rather than a bad payload.

    CONSTITUTION §8.2 requires a context-length refusal to degrade the model for
    large-context requests rather than be retried. The classification has to happen
    where the body is read, because once the exception is raised the body is gone
    and the breaker (Batch 5) would have nothing to classify.

    The body is inspected only to classify and is never propagated into an error
    message, since it can echo prompt content.
    """
    try:
        decoded: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False
    return is_context_length_error(decoded) is not None


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
