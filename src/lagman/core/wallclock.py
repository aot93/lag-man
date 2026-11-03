"""Time of day for the slate, the time-of-day start and run names.

The Pi has no clock battery by default, so with no internet (hotspot mode) its system clock
is wrong after a boot. A browser can lend its time instead: after ``sync`` the time of day is
the browser's time plus the monotonic time since, so a later step of the system clock (a time
server reached at last) does not move it. This is never used for the count, which advances
only from displayed frames.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta


class WallClock:
    def __init__(self, now: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic):
        self._now, self._mono = now, monotonic
        self._anchor: tuple[datetime, float] | None = None
        self.synced_at: datetime | None = None

    @property
    def source(self) -> str:
        return "system" if self._anchor is None else "browser"

    def now(self) -> datetime:
        if self._anchor is None:
            return self._now()
        at, mono = self._anchor
        return at + timedelta(seconds=self._mono() - mono)

    def sync(self, true_now: datetime) -> None:
        """Take ``true_now`` (local wall time, naive) as the time of day from here on."""
        self._anchor = (true_now, self._mono())
        self.synced_at = true_now

    def clear(self) -> None:
        self._anchor = None
        self.synced_at = None

    def offset_s(self) -> float:
        """How far the time of day is ahead of the system clock."""
        return (self.now() - self._now()).total_seconds()
