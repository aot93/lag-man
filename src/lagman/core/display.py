"""Display modes (SPEC.md, "Display mode").

A mode is a resolution and a whole-number refresh rate, written ``1920x1080@50``. The
generator offers only modes whose refresh a rate set can count at. The renderer supplies a
``DisplayControl`` that lists and sets the modes the display reports; ``SimulatedDisplay``
stands in for it in headless runs and tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .rates import RATE_SETS

_MODE_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*@\s*(\d+)\s*$")


@dataclass(frozen=True)
class DisplayMode:
    width: int
    height: int
    hz: int

    def __str__(self) -> str:
        return f"{self.width}x{self.height}@{self.hz}"

    @classmethod
    def parse(cls, text: str) -> DisplayMode:
        m = _MODE_RE.match(text) if isinstance(text, str) else None
        if not m or 0 in (w := int(m[1]), h := int(m[2]), hz := int(m[3])):
            raise ValueError(f"display mode must be WIDTHxHEIGHT@HZ, like 1920x1080@50; "
                             f"got {text!r}")
        return cls(w, h, hz)

    def rate_sets(self) -> list[str]:
        """The rate sets that can count at this mode's refresh."""
        return [k for k, rs in RATE_SETS.items() if self.hz in rs.refresh_rates]


def usable_modes(modes: list[DisplayMode]) -> list[DisplayMode]:
    """Modes a rate set can count at, without duplicates: fastest refresh first, then
    largest. Displays list the same mode more than once (59.94 and 60, for example)."""
    unique = {m for m in modes if m.rate_sets()}
    return sorted(unique, key=lambda m: (-m.hz, -m.width * m.height, -m.width))


class DisplayControl(Protocol):
    can_set: bool
    note: str  # how modes are set, or why they cannot be

    def modes(self) -> list[DisplayMode]: ...

    def current(self) -> DisplayMode | None: ...

    def set_mode(self, mode: DisplayMode) -> DisplayMode:
        """Switch to ``mode`` and return the mode the display reports afterwards."""
        ...


class SimulatedDisplay:
    """A display with a fixed list of modes, for headless runs and tests."""

    can_set = True
    note = "Simulated display: no output is changed."

    def __init__(self, current: DisplayMode, modes: list[DisplayMode] | None = None):
        self._current = current
        self._modes = list(modes) if modes is not None else [
            DisplayMode(w, h, hz) for hz in (120, 100, 60, 50)
            for w, h in ((3840, 2160), (1920, 1080))]
        if current not in self._modes:
            self._modes.insert(0, current)

    def modes(self) -> list[DisplayMode]:
        return list(self._modes)

    def current(self) -> DisplayMode:
        return self._current

    def set_mode(self, mode: DisplayMode) -> DisplayMode:
        if mode not in self._modes:
            raise ValueError(f"{mode} is not a mode this display offers")
        self._current = mode
        return mode
