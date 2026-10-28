"""OpenAI-compatible SSE rendering for streamed completions.

Adapters produce :class:`NormalizedChunk` values -- what the provider said. This
module produces what the *client* asked for: ``chat.completion.chunk`` objects on
an event stream, terminated by ``data: [DONE]`` (spec 6, B7).

**The split matters.** An adapter that emitted OpenAI envelopes would encode the
client protocol in two more places and would have to know about ids, ``object``
discriminators and role deltas -- none of which a provider has an opinion about.
Conversely this module knows nothing about Gemini or Mistral: it consumes chunks
that are already provider-agnostic.

Two behaviours here are deliberate rather than incidental.

**The role delta is written lazily, not eagerly.** The first event is only emitted
once a provider chunk has actually arrived, so "nothing has been written yet" is
a real state. That is the condition ADR-0007 turns on: with zero delivered chunks
the route can still retry or return a JSON error, and after one it cannot. An
eager role delta would commit the response before the provider had produced
anything, and every first-chunk failure would become an unrecoverable truncation.

**``usage`` rides the final chunk.** The spec guarantees it is there so a client
can account for tokens without a second request (B7). Mistral only reports usage
when asked, which is what ``stream_options.include_usage`` is forwarded for -- the
flag is honoured by making the number exist, not by deciding whether to emit it.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from free_router.core.normalization import FinishReason, NormalizedChunk, TokenUsage

__all__ = ["DONE_EVENT", "StreamRenderer", "openai_chunk"]

DONE_EVENT = "data: [DONE]\n\n"
"""The terminator OpenAI clients stop on. Not valid JSON, and not a chunk."""


def openai_chunk(
    *,
    chunk_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: FinishReason | None = None,
    usage: TokenUsage | None = None,
) -> dict[str, Any]:
    """Build one ``chat.completion.chunk`` object.

    Args:
        chunk_id: The completion id, shared by every chunk in one response.
        created: Unix timestamp, shared by every chunk in one response.
        model: The model the client asked for, echoed back.
        delta: The increment for this chunk.
        finish_reason: Set only on the chunk that terminates generation.
        usage: Token counts, when they are known and not yet emitted.

    Returns:
        A JSON-ready chunk object.
    """
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason},
        ],
        "usage": _usage_object(usage) if usage is not None else None,
    }


def _usage_object(usage: TokenUsage) -> dict[str, int]:
    """Render token counts in OpenAI's vocabulary.

    The unary envelope reports zeros by design, so the field names here are the
    only thing shared with it. ``total_tokens`` is derived when the provider
    reported no total, because a client summing the two other fields would
    otherwise silently disagree with the total it was given.
    """
    total = usage.resolved_total()
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": total,
    }


def _sse(payload: dict[str, Any]) -> str:
    """Frame one chunk as an SSE event."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


class StreamRenderer:
    """Translate normalized chunks into OpenAI-compatible SSE events.

    Stateful on purpose: the route needs to know, after a failure, whether
    anything reached the client (:attr:`delivered`) and what the provider said
    about the ending (:attr:`finish_reason`, :attr:`usage`).

    Attributes:
        delivered: Whether any event has been produced. ``False`` means the
            response is still uncommitted and the route may retry, replace the
            response with a JSON error, or both.
        finish_reason: The termination reason the provider reported, if the
            stream reached one.
        usage: The token counts the provider reported, if any.
    """

    def __init__(
        self,
        *,
        model: str,
        chunk_id: str | None = None,
        created: int | None = None,
    ) -> None:
        """Prepare a renderer for one response.

        Args:
            model: The model the client asked for, echoed on every chunk.
            chunk_id: Override for the completion id, for deterministic tests.
            created: Override for the timestamp, for deterministic tests.

        Note:
            ``include_usage`` is *not* a parameter. It is a provider-facing
            preference -- it decides whether Mistral reports usage at all -- and
            is forwarded by the adapter. Whether to *emit* usage is not a choice:
            B7 requires it on the final chunk whenever it is known.
        """
        self._model = model
        self._chunk_id = chunk_id or f"chatcmpl-{uuid.uuid4().hex}"
        self._created = created if created is not None else int(time.time())

        self.delivered = False
        self.finish_reason: FinishReason | None = None
        self.usage: TokenUsage | None = None
        self._usage_emitted = False

    async def events(self, chunks: AsyncIterator[NormalizedChunk]) -> AsyncIterator[str]:
        """Render chunks as SSE events, ending with ``data: [DONE]``.

        Args:
            chunks: Normalized chunks from an adapter, in arrival order.

        Yields:
            Framed SSE events. Nothing at all if ``chunks`` is empty -- an empty
            stream must not look like a started response.

        Raises:
            ProviderBaseError: Whatever the adapter raised while iterating.
                Deliberately not caught: the route decides what a partial
                response means, and swallowing it here would hide it.
        """
        finish_emitted = False

        async for chunk in chunks:
            if chunk.usage is not None:
                self.usage = chunk.usage

            if not self.delivered:
                self.delivered = True
                yield _sse(self._chunk({"role": "assistant"}))

            if chunk.delta:
                yield _sse(self._chunk({"content": chunk.delta}))

            if chunk.finish_reason is not None:
                self.finish_reason = chunk.finish_reason
                usage = self.usage if not self._usage_emitted else None
                yield _sse(self._chunk({}, finish_reason=chunk.finish_reason, usage=usage))
                self._usage_emitted = usage is not None
                finish_emitted = True
            elif self.usage is not None and finish_emitted and not self._usage_emitted:
                # Mistral reports usage in a chunk of its own *after* the finish
                # chunk, so the number arrives once generation is already
                # declared over. Emitting it here keeps the guarantee that usage
                # is on the final chunk without making the client wait for it.
                yield _sse(
                    {
                        "id": self._chunk_id,
                        "object": "chat.completion.chunk",
                        "created": self._created,
                        "model": self._model,
                        "choices": [],
                        "usage": _usage_object(self.usage),
                    }
                )
                self._usage_emitted = True

        if not self.delivered:
            # Nothing was produced, so nothing is framed -- not even [DONE].
            # A [DONE] would tell the client the response completed successfully
            # when in fact the provider returned nothing at all.
            return

        if self.usage is not None and not self._usage_emitted:
            yield _sse(
                {
                    "id": self._chunk_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [],
                    "usage": _usage_object(self.usage),
                }
            )
            self._usage_emitted = True

        yield DONE_EVENT

    def _chunk(
        self,
        delta: dict[str, Any],
        *,
        finish_reason: FinishReason | None = None,
        usage: TokenUsage | None = None,
    ) -> dict[str, Any]:
        """Build a chunk carrying this response's identity."""
        return openai_chunk(
            chunk_id=self._chunk_id,
            created=self._created,
            model=self._model,
            delta=delta,
            finish_reason=finish_reason,
            usage=usage,
        )
