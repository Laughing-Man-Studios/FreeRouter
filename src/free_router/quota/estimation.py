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

import math
from typing import Any

__all__ = [
    "DEFAULT_OUTPUT_TOKEN_ASSUMPTION",
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


def estimate_input_tokens(request: Any) -> int:
    """Estimate the *input* tokens a request will consume.

    Args:
        request: A :class:`~free_router.core.normalization.NormalizedRequest`, or
            any object exposing a ``messages`` list of ``{role, content}`` dicts.

    Returns:
        A token estimate. Never zero for a request with any content.
    """
    characters = 0
    for message in getattr(request, "messages", []) or []:
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            characters += len(content)
    return max(1, math.ceil(characters / _CHARS_PER_TOKEN))


def estimate_total_tokens(request: Any, assumed_output: int) -> int:
    """Estimate the *total* tokens a request will consume.

    The total is what a reservation must cover: the input plus the output the
    response is assumed to produce. The safety margin is applied by the tracker,
    not here, so that a caller cannot forget it.

    Args:
        request: The request being estimated.
        assumed_output: Tokens assumed for the response.

    Returns:
        The combined estimate, before the safety margin.
    """
    return estimate_input_tokens(request) + max(0, assumed_output)
