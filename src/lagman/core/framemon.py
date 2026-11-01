"""Dropped and repeated frame detection from present timestamps.

The renderer calls ``observe`` with a monotonic timestamp taken right after each vsynced
present returns. A gap of about N refresh periods means the previous image stayed on screen
for N refreshes, so N - 1 refreshes repeated a frame while the count fell behind real time.
A gap of well under one period means a present did not wait for vsync, so a frame may never
have been shown (dropped) or may have torn.

Scheduling jitter can delay one timestamp by a period, which looks like a long gap followed
by a short one. The monitor holds a long gap for one frame and discards the pair when the
two gaps sum to about two periods.

A display running at the wrong rate (60Hz under a 100Hz rate set) would make every present
look like a repeat. Once the median interval shows that, the monitor reports one condition,
``mismatched``, instead of an event per frame, until the rate matches again.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

REPEAT = "repeat"
EARLY = "early"


@dataclass(frozen=True)
class FrameEvent:
    kind: str  # REPEAT or EARLY
    frame: int  # index of the frame whose present was late or early
    refreshes: int  # REPEAT: refreshes the previous image was held for beyond one
    gap_ms: float


class FrameMonitor:
    def __init__(self, nominal_hz: int, window: int = 240):
        self.nominal_hz = nominal_hz
        self.period_ns = 1e9 / nominal_hz
        self._last_t: int | None = None
        self._pending: tuple[int, int, float] | None = None  # frame, refreshes, gap_ns
        self._gaps: deque[float] = deque(maxlen=window)
        self.frames = 0
        self.repeats = 0
        self.early = 0
        self.mismatched = False

    def observe(self, frame: int, t_ns: int) -> list[FrameEvent]:
        events: list[FrameEvent] = []
        last, self._last_t = self._last_t, t_ns
        self.frames += 1
        if last is None:
            return events
        gap = float(t_ns - last)
        self._gaps.append(gap)
        self.mismatched = self.rate_mismatch()
        if self.mismatched:
            self._pending = None
            return events
        n = round(gap / self.period_ns)

        if self._pending is not None:
            pf, pn, pgap = self._pending
            self._pending = None
            if n == 0 and round((pgap + gap) / self.period_ns) == 2 and pn == 2:
                return events  # jitter: one late timestamp, then the next on time
            events.append(self._repeat(pf, pn, pgap))

        if n >= 2:
            self._pending = (frame, n, gap)
        elif gap < 0.5 * self.period_ns:
            self.early += 1
            events.append(FrameEvent(EARLY, frame, 0, gap / 1e6))
        elif n == 1 and abs(gap / self.period_ns - 1) < 0.25:
            # Track the display's true period, which is rarely exactly nominal.
            self.period_ns += 0.01 * (gap - self.period_ns)
        return events

    def flush(self) -> list[FrameEvent]:
        if self._pending is None:
            return []
        pf, pn, pgap = self._pending
        self._pending = None
        return [self._repeat(pf, pn, pgap)]

    def _repeat(self, frame: int, n: int, gap: float) -> FrameEvent:
        self.repeats += n - 1
        return FrameEvent(REPEAT, frame, n - 1, gap / 1e6)

    @property
    def measured_hz(self) -> float | None:
        """Refresh rate from the median recent present interval."""
        if len(self._gaps) < 10:
            return None
        return 1e9 / statistics.median(self._gaps)

    def rate_mismatch(self, tolerance: float = 0.005) -> bool:
        hz = self.measured_hz
        return hz is not None and abs(hz - self.nominal_hz) / self.nominal_hz > tolerance
