"""Wall-clock time enters the server here and nowhere else.

Every timestamp a caller sees, and every audit event, reads one injected clock.
A test pins it with ``FixedClock`` instead of matching against ``datetime.now``.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


SYSTEM_CLOCK = SystemClock()
"""The stateless default. A shared instance keeps it out of argument defaults."""


@dataclass(frozen=True)
class FixedClock:
    """A deterministic clock for tests."""

    moment: datetime

    def now(self) -> datetime:
        return self.moment
