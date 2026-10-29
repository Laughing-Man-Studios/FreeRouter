"""Provider-agnostic SSE framing: the shared parser and the OpenAI writer.

Two layers are covered here because they meet at a single contract -- the
:class:`NormalizedChunk`. The parser turns provider bytes into chunks; the
renderer turns chunks into ``chat.completion.chunk`` events. Neither knows about
the other's provider, which is the point.

The renderer tests pin the two behaviours ADR-0007 depends on: nothing is written
until a chunk actually arrives, and ``[DONE]`` never precedes an answer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from free_router.api.streaming import DONE_EVENT, StreamRenderer
from free_router.core.normalization import NormalizedChunk, TokenUsage
from free_router.providers.sse import SSEDecodeError, iter_sse_data


async def _lines(*raw: str) -> AsyncIterator[str]:
    """Yield lines the way ``httpx.Response.aiter_lines`` does: without newlines."""
    for line in raw:
        yield line


async def _chunks(*items: NormalizedChunk) -> AsyncIterator[NormalizedChunk]:
    for item in items:
        yield item


def _payloads(events: list[str]) -> list[dict[str, Any]]:
    """Decode the JSON events, asserting every non-final event is framed as SSE."""
    decoded: list[dict[str, Any]] = []
    for event in events:
        if event == DONE_EVENT:
            continue
        assert event.startswith("data: "), event
        assert event.endswith("\n\n"), event
        decoded.append(json.loads(event[len("data: ") :]))
    return decoded


# --- The shared parser ---


async def test_parser_yields_each_data_payload() -> None:
    lines = _lines(
        'data: {"a": 1}',
        "",
        'data: {"b": 2}',
        "",
    )

    events = [event async for event in iter_sse_data(lines)]

    assert events == [{"a": 1}, {"b": 2}]


async def test_parser_stops_at_done_and_discards_what_follows() -> None:
    """``[DONE]`` is a terminator, not content. Anything after it is not ours."""
    lines = _lines('data: {"a": 1}', "data: [DONE]", 'data: {"b": 2}')

    events = [event async for event in iter_sse_data(lines)]

    assert events == [{"a": 1}]


async def test_parser_tolerates_a_stream_that_ends_without_done() -> None:
    """Gemini does not send ``[DONE]``; falling off the end is a normal ending."""
    lines = _lines('data: {"a": 1}')

    events = [event async for event in iter_sse_data(lines)]

    assert events == [{"a": 1}]


async def test_parser_ignores_comments_and_field_lines() -> None:
    lines = _lines(
        ": keep-alive",
        "event: message",
        "id: 7",
        'data: {"a": 1}',
        "",
    )

    events = [event async for event in iter_sse_data(lines)]

    assert events == [{"a": 1}]


async def test_parser_rejects_invalid_json() -> None:
    lines = _lines("data: not-json")

    with pytest.raises(SSEDecodeError):
        [event async for event in iter_sse_data(lines)]


async def test_parser_rejects_json_that_is_not_an_object() -> None:
    """A bare array or scalar is not a chunk, and indexing it would raise later."""
    lines = _lines("data: [1, 2, 3]")

    with pytest.raises(SSEDecodeError):
        [event async for event in iter_sse_data(lines)]


# --- The OpenAI writer ---


async def test_role_delta_is_emitted_before_any_content() -> None:
    renderer = StreamRenderer(model="google/gemini-3.5-flash-lite", chunk_id="c1", created=1)

    events = [event async for event in renderer.events(_chunks(NormalizedChunk(delta="hi")))]
    chunks = _payloads(events)

    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[1]["choices"][0]["delta"] == {"content": "hi"}


async def test_role_delta_is_emitted_once() -> None:
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(NormalizedChunk(delta="a"), NormalizedChunk(delta="b"))
        )
    ]

    role_chunks = [
        chunk
        for chunk in _payloads(events)
        if chunk["choices"][0]["delta"] == {"role": "assistant"}
    ]
    assert len(role_chunks) == 1


async def test_terminator_is_last() -> None:
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(NormalizedChunk(delta="hi", finish_reason="stop"))
        )
    ]

    assert events[-1] == DONE_EVENT


async def test_every_chunk_shares_one_identity() -> None:
    renderer = StreamRenderer(model="google/x", chunk_id="c1", created=123)

    events = [
        event
        async for event in renderer.events(
            _chunks(NormalizedChunk(delta="a"), NormalizedChunk(delta="b", finish_reason="stop"))
        )
    ]
    chunks = _payloads(events)

    assert {chunk["id"] for chunk in chunks} == {"c1"}
    assert {chunk["created"] for chunk in chunks} == {123}
    assert {chunk["model"] for chunk in chunks} == {"google/x"}
    assert {chunk["object"] for chunk in chunks} == {"chat.completion.chunk"}


async def test_finish_chunk_carries_the_finish_reason() -> None:
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(NormalizedChunk(delta="hi", finish_reason="length"))
        )
    ]

    finish = [chunk for chunk in _payloads(events) if chunk["choices"][0]["finish_reason"]]
    assert len(finish) == 1
    assert finish[0]["choices"][0]["finish_reason"] == "length"
    assert renderer.finish_reason == "length"


async def test_usage_rides_the_final_chunk() -> None:
    """B7: a streaming client can account for tokens without a second request."""
    usage = TokenUsage(input_tokens=11, output_tokens=7)
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(NormalizedChunk(delta="hi", finish_reason="stop", usage=usage))
        )
    ]
    chunks = _payloads(events)

    final = chunks[-1]
    assert final["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert renderer.usage == usage


async def test_usage_arriving_after_the_finish_chunk_is_still_emitted() -> None:
    """Mistral reports usage in a trailing chunk of its own."""
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(
                NormalizedChunk(delta="hi", finish_reason="stop"),
                NormalizedChunk(usage=TokenUsage(input_tokens=3, output_tokens=2)),
            )
        )
    ]
    chunks = _payloads(events)

    assert events[-1] == DONE_EVENT
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["total_tokens"] == 5


async def test_usage_is_emitted_once() -> None:
    """A provider that repeats cumulative usage must not produce duplicate counts."""
    usage = TokenUsage(input_tokens=3, output_tokens=2)
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(
                NormalizedChunk(delta="hi"),
                NormalizedChunk(finish_reason="stop", usage=usage),
                NormalizedChunk(usage=usage),
            )
        )
    ]

    with_usage = [chunk for chunk in _payloads(events) if chunk.get("usage")]
    assert len(with_usage) == 1


async def test_no_usage_is_invented_when_the_provider_reports_none() -> None:
    """A missing number leaves the tracker's estimate standing; zero would not."""
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [
        event
        async for event in renderer.events(
            _chunks(NormalizedChunk(delta="hi", finish_reason="stop"))
        )
    ]

    assert all(chunk.get("usage") is None for chunk in _payloads(events))
    assert renderer.usage is None


async def test_an_empty_stream_writes_nothing_at_all() -> None:
    """Not even ``[DONE]``: that would claim a successful completion of nothing.

    This is the state ADR-0007 calls "zero chunks", where the route may still
    retry or answer with a JSON error.
    """
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    events = [event async for event in renderer.events(_chunks())]

    assert events == []
    assert renderer.delivered is False


async def test_delivered_tracks_the_first_written_event() -> None:
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)
    assert renderer.delivered is False

    generator = renderer.events(_chunks(NormalizedChunk(delta="hi")))
    await anext(generator)
    assert renderer.delivered is True


async def test_delivered_is_false_before_the_first_chunk_is_pulled() -> None:
    """The renderer must not commit the response just by being constructed."""
    renderer = StreamRenderer(model="m", chunk_id="c1", created=1)

    generator = renderer.events(_chunks(NormalizedChunk(delta="hi")))
    assert renderer.delivered is False

    await generator.aclose()
