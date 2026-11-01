"""UTC timestamp helpers.

Two representations are in play and they are not interchangeable:

- **Wall-clock instants** survive a reboot and a container recreate, so they
  are what gets persisted. A monotonic value is only meaningful against the
  current boot, so persisting one and restoring it after a reboot would restore
  nonsense.
- **Monotonic instants** are what durations are measured against, because they
  are immune to an NTP step.

The conversion between them happens where the boundary is, not scattered
through the domain.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["from_iso", "to_iso"]


def to_iso(wall_seconds: float) -> str:
    """Render a wall-clock instant as a UTC ISO-8601 string."""
    return datetime.fromtimestamp(wall_seconds, tz=UTC).isoformat().replace("+00:00", "Z")


def from_iso(text: str) -> float:
    """Parse a UTC ISO-8601 string back to wall-clock seconds.

    Raises:
        ValueError: If the text is not a timestamp this module wrote.
    """
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
