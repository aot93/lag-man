"""Rate sets (SPEC.md, "Rate sets"). Fractional rates are out of scope."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RateSet:
    key: str
    label: str
    refresh_hz: int
    test_camera_fps: tuple[int, ...]
    control_camera_fps: int

    @property
    def tick_ms(self) -> float:
        return 1000.0 / self.refresh_hz

    @property
    def timecode_rates(self) -> tuple[int, ...]:
        """Selectable time code rates: the test camera rates plus fast mode (the refresh rate)."""
        return (*self.test_camera_fps, self.refresh_hz)


RATE_SETS: dict[str, RateSet] = {
    "50": RateSet("50", "50Hz regions (Europe)", 100, (25, 50), 100),
    "60": RateSet("60", "60Hz regions (non-European)", 120, (30, 60), 120),
}


def get_rate_set(key: str) -> RateSet:
    try:
        return RATE_SETS[key]
    except KeyError:
        raise ValueError(f"unknown rate set {key!r}; expected one of {sorted(RATE_SETS)}") from None
