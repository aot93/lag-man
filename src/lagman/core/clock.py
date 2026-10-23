"""Clock state. The count advances only when the renderer reports a displayed frame."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .config import GeneratorConfig
from .timecode import count_from_seconds, parse_timecode


class Clock:
    def __init__(self, cfg: GeneratorConfig, now: Callable[[], datetime] = datetime.now):
        self._now = now
        self.running = False
        self.count = 0
        self.configure(cfg)

    def configure(self, cfg: GeneratorConfig) -> None:
        cfg.validate()
        self.cfg = cfg
        if not self.running:
            self.reset()

    def start_count(self) -> int:
        cfg = self.cfg
        if cfg.start_mode == "value":
            return parse_timecode(cfg.start_value, cfg.refresh_hz, cfg.tc_rate)
        if cfg.start_mode == "time_of_day":
            t = self._now()
            midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
            return count_from_seconds((t - midnight).total_seconds(), cfg.refresh_hz)
        return 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def reset(self) -> None:
        self.count = self.start_count()

    def frame_displayed(self) -> None:
        """Call once per present. Exactly one tick per displayed frame while running."""
        if self.running:
            self.count += 1
