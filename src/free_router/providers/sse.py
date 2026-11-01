"""Server-sent-events parsing, shared by the provider adapters.

Both providers stream as SSE but disagree on almost everything inside it: Gemini
sends ``usageMetadata`` with string counts and puts ``finishReason`` on the
candidate, Mistral sends OpenAI-shaped ``choices`` with numeric counts and only
reports usage when asked. The *framing*, however, is identical, so it lives here
once rather than twice.

**Only the framing is shared.** Nothing in this module knows what a chunk means;
it yields the decoded payload of each ``data:`` line and leaves interpretation to
the adapter. A shared *semantic* parser would be the mistake -- the two providers
differ precisely where a shared parser would have to branch.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

__all__ = ["SSEDecodeError", "iter_sse_data"]

DONE_SENTINEL = "[DONE]"
"""What OpenAI-compatible streams send instead of a final JSON payload."""


class SSEDecodeError(Exception):
    """A ``data:`` line was not valid JSON.

    Adapters translate this into their own error type rather than letting it
    escape, because an undecodable chunk is a provider fault and must be
    classified with the rest of them.
    """


async def iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield the decoded payload of each ``data:`` event.

    Args:
        lines: Raw lines from an HTTP response body, e.g. ``aiter_lines()``.

    Yields:
        Each ``data:`` payload that decodes to a JSON object.

    Raises:
        SSEDecodeError: If a payload is not valid JSON, or is JSON that is not an
            object.

    Notes:
        A ``data:`` line that is exactly ``[DONE]`` ends the iteration quietly.
        It is a terminator, not content, and a provider that omits it -- Gemini
        does -- simply falls off the end instead. Both are normal endings; a
        stream that stops without either is the route layer's problem, not this
        parser's, because only the route knows whether a ``finish_reason`` was
        ever seen.

        Comment lines (``:``), ``event:``/``id:`` fields, and blank separators are
        skipped. They carry no payload, and treating them as content would make
        the parser reject a valid stream.
    """

    async for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            # event:, id:, retry: -- SSE fields this router does not use. Both
            # providers send only `data:`, so anything else is ignorable rather
            # than an error.
            continue

        payload = line.removeprefix("data:").strip()
        if payload == DONE_SENTINEL:
            return
        if not payload:
            continue

        try:
            decoded: Any = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SSEDecodeError(
                "The provider sent a stream event that was not valid JSON."
            ) from exc

        if not isinstance(decoded, dict):
            raise SSEDecodeError("The provider sent a stream event that was not a JSON object.")
        yield decoded


async def response_lines(response: httpx.Response) -> AsyncIterator[str]:
    """Iterate a streamed response's lines, always closing it afterwards.

    Kept here so both adapters release the pooled connection on every exit path,
    including a consumer that stops early. A leaked connection is invisible until
    the pool is exhausted, which under streaming -- where responses stay open far
    longer -- would arrive much sooner than it did for unary calls.

    Args:
        response: A response opened with ``stream=True``.

    Yields:
        Decoded lines.
    """
    try:
        async for line in response.aiter_lines():
            yield line
    finally:
        await response.aclose()
