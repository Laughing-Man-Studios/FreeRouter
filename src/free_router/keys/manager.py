"""Key identity and the key pool.

ADR-0003 defines how key material reaches the router: ``config.yaml`` names
*slots*, the environment holds the material, and a derived variable name
addresses each slot. This module holds the runtime side of that — the credential
reference and the selection policy over a pool.

Three decisions here are load-bearing.

**A key is a credential, not capacity.** Issue #51 established that Google applies
limits per *project*, so two keys in one project draw on one bucket. The pool
therefore provides **credential redundancy** — a revoked key must not stop the
router — and nothing else. Adding a second Google key does not add capacity, and
nothing here may be written or documented as though it did.

**Cooldown and quota are not this module's business.** They belong to
``quota.cooldown`` and ``quota.tracker``, both keyed on ``(provider, model)``
because the capacity is shared. Per-key cooldown would be actively wrong: a rate
limit on one Google key applies to its sibling too, so cooling only one would
send the next request straight into the same exhausted bucket. What *is* per
credential is suspension — the credential itself is no longer usable, because its
authentication was rejected or it was revoked. That is the only per-key state
here, and it is the only state a key carries.

**Selection is least-recently-used, never round-robin.** Concentration on one key
is correct; the goal is to use a key until it cannot be used. Spreading requests
evenly would rotate onto a credential that may be broken and would make LRU
meaningless. Selection does not mark a key used: a request that then fails must
not leave a working credential looking cold. :meth:`KeyManager.mark_used` is
called after a dispatch succeeds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import SecretStr

__all__ = [
    "KeyManager",
    "KeyRef",
    "KeySelection",
    "SelectionReason",
]


class SelectionReason(StrEnum):
    """Why a key was chosen, for the ``selection_reason`` log field.

    Stable machine-readable codes. Never provider text, which can echo prompt
    content (CONSTITUTION §9.1).
    """

    ONLY_KEY = "only_key"
    """One eligible key, so least-recently-used had nothing to choose between."""

    LEAST_RECENTLY_USED = "least_recently_used"

    NONE_AVAILABLE = "none_available"
    """The pool is empty or every key in it is suspended."""


@dataclass(frozen=True, slots=True)
class KeyRef:
    """A single credential slot.

    Carries ``model_id`` in addition to the plan's ``(alias, provider, secret)``.
    A key is pooled per ``(provider, model)``, and without the model on the
    reference, ``mark_used`` would have to search every pool to find which one
    this key belonged to. The field is non-secret, so nothing is lost by it.

    Attributes:
        alias: The slot name. The **only** key identifier permitted in a log line
            or the database (CONSTITUTION §9.1).
        provider: Owning provider.
        model_id: The provider-native model id this credential is pooled under.
        secret: Key material. Never logged, never serialised, never compared.
    """

    alias: str
    provider: str
    model_id: str
    secret: SecretStr

    @property
    def pool_key(self) -> tuple[str, str]:
        """The ``(provider, model_id)`` pool this key belongs to."""
        return (self.provider, self.model_id)

    def describe(self) -> str:
        """Render for a log line. Never includes the secret."""
        return f"key_alias={self.alias}"

    def __repr__(self) -> str:
        """Redact the secret in any accidental repr.

        The dataclass repr would render ``SecretStr('**********')``, which pydantic
        masks today. That is one library default change away from leaking, and a
        credential reference is exactly the kind of object that ends up in an
        exception message. Stated rather than assumed.
        """
        return (
            f"KeyRef(alias={self.alias!r}, provider={self.provider!r}, "
            f"model_id={self.model_id!r}, secret=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class KeySelection:
    """A selection result, carrying the reason for the log field."""

    key: KeyRef | None
    reason: SelectionReason

    def describe(self) -> str:
        """Render for a log line."""
        alias = self.key.alias if self.key is not None else "none"
        return f"key_alias={alias} selection_reason={self.reason.value}"


@dataclass(slots=True)
class _Pool:
    """Per-``(provider, model)`` credential state."""

    keys: list[KeyRef]
    last_used: dict[str, int] = field(default_factory=dict)
    """Ordinal of each key's most recent use.

    **An ordinal, not a timestamp.** Ordering LRU by timestamp silently degrades
    to alphabetical whenever two marks land in the same clock tick, which a coarse
    timer makes routine rather than exotic. The failure is quiet: it looks like
    working LRU while pinning traffic onto whichever alias sorts first. A counter
    is exact and immune to clock resolution.
    """
    last_used_at: dict[str, float] = field(default_factory=dict)
    """Position of each use in the injected clock. Diagnostics only, never ordering."""
    suspended: set[str] = field(default_factory=set)


class KeyManager:
    """Selects credentials from the pools declared in configuration.

    **State is in memory and does not survive a restart** (ADR-0004 records the
    same gap for quota state). A suspension is forgotten on restart, so a revoked
    key is tried once more and re-suspended when the provider refuses it. That is
    the right failure mode: an operator's remedy is a restart.
    """

    def __init__(
        self,
        material: Mapping[tuple[str, str], Mapping[str, SecretStr]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a manager from resolved key material.

        Args:
            material: Key material keyed by ``(provider, model_id)`` then alias,
                as returned by ``Config.resolve_key_material()``.
            clock: Monotonic time source, injected so tests do not sleep and so
                ordering does not depend on wall-clock adjustments.
        """
        self._pools: dict[tuple[str, str], _Pool] = {}
        for (provider, model_id), slots in material.items():
            self._pools[(provider, model_id)] = _Pool(
                keys=[
                    KeyRef(
                        alias=alias,
                        provider=provider,
                        model_id=model_id,
                        secret=secret,
                    )
                    for alias, secret in slots.items()
                ]
            )
        self._clock = clock
        self._sequence = 0
        """Monotonic ordinal for LRU ordering. Independent of the clock."""
        self._lock = asyncio.Lock()
        """Guards selection and state mutation against each other.

        Held **only** across in-memory updates, never across an await of provider
        I/O -- the same event-loop discipline CONSTITUTION §8.3 requires of the
        database. A lock held across a network call would serialise every
        dispatch behind the slowest provider response.
        """

    def pool_size(self, provider: str, model_id: str) -> int:
        """Number of keys configured for a model, suspended ones included."""
        pool = self._pools.get((provider, model_id))
        return 0 if pool is None else len(pool.keys)

    def aliases(self, provider: str, model_id: str) -> tuple[str, ...]:
        """Configured aliases for a model, in declaration order."""
        pool = self._pools.get((provider, model_id))
        if pool is None:
            return ()
        return tuple(key.alias for key in pool.keys)

    def is_suspended(self, provider: str, model_id: str, alias: str) -> bool:
        """True when a credential has been taken out of rotation."""
        pool = self._pools.get((provider, model_id))
        return pool is not None and alias in pool.suspended

    async def select(self, provider: str, model_id: str) -> KeySelection:
        """Choose the least recently used eligible key.

        Eligibility here is **suspension only**. Cooldown and quota are model-level
        and belong to :mod:`free_router.quota.cooldown` and
        :mod:`free_router.quota.tracker`; the dispatch path composes them before
        calling this, so a model with no capacity never reaches selection.

        Returns:
            A :class:`KeySelection`. ``key`` is ``None`` when nothing is
            eligible, with reason ``none_available``. The caller turns that into a
            normalized capacity error; it must not substitute a different key.
        """
        async with self._lock:
            pool = self._pools.get((provider, model_id))
            if pool is None or not pool.keys:
                return KeySelection(None, SelectionReason.NONE_AVAILABLE)

            eligible = [key for key in pool.keys if key.alias not in pool.suspended]
            if not eligible:
                return KeySelection(None, SelectionReason.NONE_AVAILABLE)

            if len(eligible) == 1:
                return KeySelection(eligible[0], SelectionReason.ONLY_KEY)

            # An unused key sorts least-recently-used first, which is the correct
            # bootstrap: a fresh process tries each key once before settling on
            # any of them. The alias breaks ties, which can now only happen if two
            # keys have genuinely never been used, so selection is deterministic.
            chosen = min(
                eligible,
                key=lambda key: (pool.last_used.get(key.alias, -1), key.alias),
            )
            return KeySelection(chosen, SelectionReason.LEAST_RECENTLY_USED)

    async def mark_used(self, key: KeyRef) -> None:
        """Record a successful dispatch, making this key most recently used."""
        async with self._lock:
            pool = self._pools.get(key.pool_key)
            if pool is not None:
                self._sequence += 1
                pool.last_used[key.alias] = self._sequence
                pool.last_used_at[key.alias] = self._clock()

    async def suspend(self, provider: str, model_id: str, alias: str) -> None:
        """Take a credential out of rotation.

        For when the credential itself is unusable — rejected authentication, or
        revoked. This is the only per-key failure state, because a rate limit
        belongs to the shared bucket rather than to the key.
        """
        async with self._lock:
            pool = self._pools.get((provider, model_id))
            if pool is not None:
                pool.suspended.add(alias)

    async def restore(self, provider: str, model_id: str, alias: str) -> None:
        """Return a suspended credential to rotation."""
        async with self._lock:
            pool = self._pools.get((provider, model_id))
            if pool is not None:
                pool.suspended.discard(alias)
