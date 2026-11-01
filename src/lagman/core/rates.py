"""Rate sets (SPEC.md, "Rate sets"). Fractional rates are out of scope.

Each rate set counts at its standard refresh (100 or 120Hz) or, on a display that cannot
reach it, at a fallback of half that (50 or 60Hz). The count is always ticks of the refresh
the display actually runs at, which the run log records.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RateSet:
    key: str
    label: str
    refresh_hz: int
    test_camera_fps: tuple[int, ...]
    control_camera_fps: int
    fallback_hz: int

    @property
    def tick_ms(self) -> float:
        return 1000.0 / self.refresh_hz

    @property
    def refresh_rates(self) -> tuple[int, ...]:
        """Display refresh rates this rate set can count at, preferred first."""
        return (self.refresh_hz, self.fallback_hz)

    @property
    def timecode_rates(self) -> tuple[int, ...]:
        """Selectable time code rates: the test camera rates plus fast mode (the refresh rate)."""
        return self.timecode_rates_at(self.refresh_hz)

    def timecode_rates_at(self, refresh_hz: int) -> tuple[int, ...]:
        rates = [r for r in self.test_camera_fps if refresh_hz % r == 0]
        return tuple(dict.fromkeys((*rates, refresh_hz)))

    def refresh_for(self, display_hz: float | None) -> int:
        """The refresh to count at on a display running at ``display_hz``. A rate this set
        cannot use gives the standard rate, so the frame monitor reports the mismatch."""
        if display_hz is not None and round(display_hz) in self.refresh_rates:
            return round(display_hz)
        return self.refresh_hz

    def fit_tc_rate(self, tc_rate: int, refresh_hz: int) -> int:
        """A time code rate the display cannot show becomes fast mode at its refresh."""
        return tc_rate if tc_rate in self.timecode_rates_at(refresh_hz) else refresh_hz


RATE_SETS: dict[str, RateSet] = {
    "50": RateSet("50", "50Hz regions (Europe)", 100, (25, 50), 100, 50),
    "60": RateSet("60", "60Hz regions (non-European)", 120, (30, 60), 120, 60),
}


def get_rate_set(key: str) -> RateSet:
    try:
        return RATE_SETS[key]
    except KeyError:
        raise ValueError(f"unknown rate set {key!r}; expected one of {sorted(RATE_SETS)}") from None
