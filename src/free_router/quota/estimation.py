"""Token estimation, split into the part that is knowable and the part that is not.

A request's token consumption has two directions, and they are known very
differently:

- **Input** is knowable before dispatch. It is computed from the request.
- **Output** is not knowable at all. It is whatever the model decides to say.

The reservation cycle needs both (CONSTITUTION §8.2), so the output side has to
be an *assumption* — and an assumption that is hidden inside a constant is a
policy nobody can see or change. That is why the output assumption is
configuration, and why this module keeps the two directions apart rather than
folding them into one number.

The input estimate is `chars / 4`, inherited from M1. Measured against both
providers on 2026-10-20, it ran 4.4–5.1 characters per token, so it slightly
*over*-estimates — conservative in the direction that matters, because
under-reserving is what breaches a ceiling.
"""

from __future__ import annotations

import json
import math
from typing import Any

__all__ = [
    "DEFAULT_OUTPUT_TOKEN_ASSUMPTION",
    "DEFAULT_TOKENS_PER_IMAGE",
    "estimate_input_tokens",
    "estimate_total_tokens",
]

DEFAULT_OUTPUT_TOKEN_ASSUMPTION = 1025
"""Tokens assumed for a response that has not been generated yet.

Agreed 2026-10-20 (clarification B1). About 6× the measured output of a
short-paragraph response — 146 tokens on Gemini, 178 on Mistral — and roughly 8%
of Gemini's 250K TPM ceiling at the 15 RPM burst limit. It is refunded on
reconciliation, so over-reserving costs a transient reservation rather than
capacity.

**Global rather than per-model.** It is a policy number, not a measured one; the
measured part is the input estimate, which is already per-model in effect
because it is computed from the request. A per-model value would invite the
belief that it is calibrated, and a value that is not calibrated is worse than
one that obviously is not.
"""

_CHARS_PER_TOKEN = 4
"""Characters per token, for the input estimate.

Inherited from M1 and measured on 2026-10-20 at 4.4–5.1 characters per token
across both providers, so it errs slightly high — in the safe direction.
"""

_MESSAGE_FRAMING_TOKENS = 4
"""Per-message overhead for role, separator, and formatting tokens.

Gemini and OpenAI both charge a small constant per message for the turn
structure (role string, separator). 4 tokens per message is consistent with
the OpenAI tiktoken methodology and is the conservative direction.
"""

DEFAULT_TOKENS_PER_IMAGE = 258
"""Tokens assumed for one base64 image part when no size information is available.

A single low-resolution image (e.g., 256×256) is typically ~258 tokens on
Gemini's billing model. Base64 character length is NOT a token count and must
not be used as one.
"""


def estimate_input_tokens(
    request: Any,
    *,
    tokens_per_image: int = DEFAULT_TOKENS_PER_IMAGE,
) -> int:
    """Estimate the *input* tokens a request will consume.

    Args:
        request: A :class:`~free_router.core.normalization.NormalizedRequest`, a
            decoded request mapping containing ``messages``, or any object
            exposing a ``messages`` list of ``{role, content}`` dicts.
        tokens_per_image: Estimated token consumption for each image part.
            Defaults to :data:`DEFAULT_TOKENS_PER_IMAGE`.

    Returns:
        A token estimate. Never zero for a request with any content.
    """
    characters = 0
    framing_tokens = 0

    raw_messages = (
        request.get("messages") if isinstance(request, dict) else getattr(request, "messages", None)
    )
    for message in raw_messages or []:
        framing_tokens += _MESSAGE_FRAMING_TOKENS

        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            characters += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text" or "text" in part:
                    text_val = part.get("text", "")
                    if isinstance(text_val, str):
                        characters += len(text_val)
                elif part_type in ("image_url", "input_image") or "image_url" in part:
                    characters += tokens_per_image * _CHARS_PER_TOKEN

        if isinstance(message, dict) and "tool_calls" in message:
            tc = message.get("tool_calls")
            if tc:
                characters += len(json.dumps(tc))

    tools = request.get("tools") if isinstance(request, dict) else getattr(request, "tools", None)
    if tools:
        characters += len(json.dumps(tools))

    tool_choice = (
        request.get("tool_choice")
        if isinstance(request, dict)
        else getattr(request, "tool_choice", None)
    )
    if tool_choice:
        characters += len(json.dumps(tool_choice))

    response_format = (
        request.get("response_format")
        if isinstance(request, dict)
        else getattr(request, "response_format", None)
    )
    if response_format:
        characters += len(json.dumps(response_format))

    content_tokens = math.ceil(characters / _CHARS_PER_TOKEN)
    return max(1, content_tokens + framing_tokens)


def estimate_total_tokens(
    request: Any,
    assumed_output: int,
    *,
    tokens_per_image: int = DEFAULT_TOKENS_PER_IMAGE,
) -> int:
    """Estimate the *total* tokens a request will consume.

    The total is what a reservation must cover: the input plus the output the
    response is assumed to produce. The safety margin is applied by the tracker,
    not here, so that a caller cannot forget it.

    Args:
        request: The request being estimated.
        assumed_output: Tokens assumed for the response.
        tokens_per_image: Estimated token consumption for each image part.

    Returns:
        The combined estimate, before the safety margin.
    """
    return estimate_input_tokens(request, tokens_per_image=tokens_per_image) + max(
        0, assumed_output
    )
