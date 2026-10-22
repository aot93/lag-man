"""Turn control camera frames into latency samples.

Each video frame shows the source monitor and the LED wall. Both strips are decoded; the
latency is the source count minus the wall count, in ticks of the source refresh. Frames
where either strip is caught mid-change or cannot be read are flagged, never guessed.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..core import strip
from ..core.strip import DecodeStatus, count_difference
from ..core.timecode import to_timecode
from .roi import read
from .setup import RunInfo, Setup

OK = "ok"
TRANSITION = "transition"
UNREADABLE = "unreadable"
SUSPECT = "suspect"
STATUSES = (OK, TRANSITION, UNREADABLE, SUSPECT)


@dataclass(frozen=True)
class Sample:
    frame: int  # video frame index
    time_s: float  # real time from the first analysed frame, at the capture rate
    source: strip.Decoded
    wall: strip.Decoded
    status: str
    latency_ticks: int | None = None
    latency_ms: float | None = None
    note: str = ""

    def row(self, info: RunInfo) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "time_s": round(self.time_s, 4),
            "source_count": self.source.count,
            "source_tc": _tc(self.source.count, info),
            "wall_count": self.wall.count,
            "wall_tc": _tc(self.wall.count, info),
            "latency_ticks": self.latency_ticks,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 3),
            "status": self.status,
            "note": self.note,
        }


def _tc(count: int | None, info: RunInfo) -> str:
    if count is None:
        return ""
    tc = to_timecode(count, info.refresh_hz, info.tc_rate or info.refresh_hz)
    return tc.hmsf() + tc.ms()


def sample_frame(index: int, time_s: float, gray: np.ndarray, setup: Setup,
                 info: RunInfo) -> Sample:
    src, wall = read(gray, setup.source), read(gray, setup.wall)
    notes = []
    for name, d in (("source", src), ("wall", wall)):
        if d.status is not DecodeStatus.OK:
            notes.append(f"{name}: {d.reason}")
    if src.status is DecodeStatus.UNREADABLE or wall.status is DecodeStatus.UNREADABLE:
        return Sample(index, time_s, src, wall, UNREADABLE, note="; ".join(notes))
    if src.status is DecodeStatus.TRANSITION or wall.status is DecodeStatus.TRANSITION:
        return Sample(index, time_s, src, wall, TRANSITION, note="; ".join(notes))

    assert src.count is not None and wall.count is not None
    ticks = count_difference(src.count, wall.count)
    ms = ticks * info.tick_ms
    status = OK
    if ticks < 0:
        status = SUSPECT
        notes.append("wall ahead of source: are the regions swapped?")
    elif not _in_run(src.count, info):
        status = SUSPECT
        notes.append("source count outside this run's range: wrong run log?")
    return Sample(index, time_s, src, wall, status, ticks, ms, "; ".join(notes))


def _in_run(count: int, info: RunInfo) -> bool:
    if info.start_count is None:
        return True
    start = info.start_count % strip.COUNT_MODULUS
    after_start = count_difference(count, start)
    if after_start < 0:
        return False
    if info.stop_count is None:
        return True
    return after_start <= count_difference(info.stop_count % strip.COUNT_MODULUS, start)


def analyse(frames: Iterable[tuple[int, np.ndarray]], setup: Setup,
            info: RunInfo) -> list[Sample]:
    samples = []
    first: int | None = None
    for index, gray in frames:
        if first is None:
            first = index
        samples.append(sample_frame(index, (index - first) * info.frame_ms / 1000, gray,
                                    setup, info))
    return samples


@dataclass(frozen=True)
class Summary:
    frames: int
    counts: dict[str, int]
    n: int
    min_ms: float | None
    max_ms: float | None
    mean_ms: float | None
    median_ms: float | None
    stdev_ms: float | None
    source_breaks: int  # consecutive good source reads that did not advance as expected
    generator_events: int  # repeated/early frames logged by the generator in this window
    first_tc: str
    last_tc: str

    def to_json(self) -> dict[str, Any]:
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def summarise(samples: list[Sample], info: RunInfo) -> Summary:
    counts = {s: 0 for s in STATUSES}
    for s in samples:
        counts[s.status] += 1
    lat = [s.latency_ms for s in samples if s.status == OK and s.latency_ms is not None]

    # The source strip should advance refresh/capture ticks per camera frame (1 at 100/100).
    step = info.refresh_hz / info.capture_fps
    breaks = 0
    prev: Sample | None = None
    for s in samples:
        if s.source.status is not DecodeStatus.OK:
            continue
        if prev is not None:
            expected = (s.frame - prev.frame) * step
            if abs(count_difference(s.source.count, prev.source.count) - expected) > 1:
                breaks += 1
        prev = s

    good = [s.source.count for s in samples if s.source.status is DecodeStatus.OK]
    gen_events = 0
    if good and info.events:
        lo, hi = good[0], good[-1]
        gen_events = sum(1 for e in info.events
                         if isinstance(e.get("count"), int)
                         and 0 <= count_difference(e["count"] % strip.COUNT_MODULUS, lo)
                         <= count_difference(hi, lo))
    return Summary(
        frames=len(samples), counts=counts, n=len(lat),
        min_ms=min(lat) if lat else None, max_ms=max(lat) if lat else None,
        mean_ms=statistics.fmean(lat) if lat else None,
        median_ms=statistics.median(lat) if lat else None,
        stdev_ms=statistics.stdev(lat) if len(lat) > 1 else None,
        source_breaks=breaks, generator_events=gen_events,
        first_tc=_tc(good[0], info) if good else "", last_tc=_tc(good[-1], info) if good else "",
    )
