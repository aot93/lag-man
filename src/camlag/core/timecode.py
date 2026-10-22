"""Tick count to time code conversion.

The count is the number of displayed frames since midnight-equivalent zero, one tick per
refresh. Everything shown on screen is derived from the count, never from the system clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Timecode:
    hours: int
    minutes: int
    seconds: int
    frames: int
    millis: int

    def hmsf(self) -> str:
        return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}:{self.frames:02d}"

    def ms(self) -> str:
        return f".{self.millis:03d}"


def ticks_per_day(refresh_hz: int) -> int:
    return SECONDS_PER_DAY * refresh_hz


def to_timecode(count: int, refresh_hz: int, tc_rate: int) -> Timecode:
    """Time code for a count. Hours wrap at 24. ``tc_rate`` must divide ``refresh_hz``."""
    _check_rates(refresh_hz, tc_rate)
    count %= ticks_per_day(refresh_hz)
    total_s, tick = divmod(count, refresh_hz)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return Timecode(h, m, s, tick * tc_rate // refresh_hz, tick * 1000 // refresh_hz)


_TC_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})[:;.](\d{1,3})\s*$")


def parse_timecode(text: str, refresh_hz: int, tc_rate: int) -> int:
    """Parse ``HH:MM:SS:FF`` into a count. FF is in units of ``tc_rate``."""
    _check_rates(refresh_hz, tc_rate)
    m = _TC_RE.match(text)
    if not m:
        raise ValueError(f"time code must be HH:MM:SS:FF, got {text!r}")
    h, mi, s, f = (int(g) for g in m.groups())
    if h > 23 or mi > 59 or s > 59:
        raise ValueError(f"time code out of range: {text!r}")
    if f >= tc_rate:
        raise ValueError(f"frame {f} out of range for {tc_rate} fps")
    return (h * 3600 + mi * 60 + s) * refresh_hz + f * (refresh_hz // tc_rate)


def count_from_seconds(seconds_since_midnight: float, refresh_hz: int) -> int:
    """Count for a time of day, for the free-run-from-time-of-day start mode."""
    return int(seconds_since_midnight * refresh_hz) % ticks_per_day(refresh_hz)


def count_to_ms(delta_count: int, refresh_hz: int) -> float:
    return delta_count * 1000.0 / refresh_hz


def _check_rates(refresh_hz: int, tc_rate: int) -> None:
    if tc_rate <= 0 or refresh_hz % tc_rate:
        raise ValueError(f"time code rate {tc_rate} must divide refresh rate {refresh_hz}")
