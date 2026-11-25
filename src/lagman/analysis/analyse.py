"""Turn control camera frames into latency samples.

Each video frame shows the source monitor and the LED wall. Both strips are decoded; the
latency is the source count minus the wall count, in ticks of the source refresh.

A frame that saw several counts (an exposure across a tick, or a wall that blends frames)
is read as the run of counts it saw, when one run fits every cell (``strip.decode_blend``),
and its latency is between the middles of the two runs, to half a tick. Otherwise, and
with ``blends`` off, frames where either strip is caught mid-change or cannot be read are
flagged, never guessed.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core import strip
from ..core.rates import RATE_SETS
from ..core.strip import DecodeStatus, count_difference
from ..core.timecode import to_timecode
from .roi import cell_levels
from .setup import RunInfo, Setup

OK = "ok"
TRANSITION = "transition"
UNREADABLE = "unreadable"
SUSPECT = "suspect"
IDLE = "idle"  # the generator's clock was not counting: before Start or after Stop
STATUSES = (OK, TRANSITION, UNREADABLE, SUSPECT, IDLE)
SPEED_OF_SOUND = 343.0  # m/s in air at about 20 °C
MAX_LATENCY_S = 2.0  # longer is taken as a misread region, and bounds where blends are looked for
SOURCE_REACH = 32  # ticks either side of a blended source's rough reading to look in


@dataclass(frozen=True)
class Sample:
    frame: int  # video frame index
    time_s: float  # real time from the first analysed frame, at the capture rate
    source: strip.Decoded
    wall: strip.Decoded
    status: str
    latency_ticks: float | None = None  # a half tick when read from blends
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
                 info: RunInfo, blends: bool = True) -> Sample:
    src_levels, wall_levels = cell_levels(gray, setup.source), cell_levels(gray, setup.wall)
    src, wall = strip.decode(src_levels), strip.decode(wall_levels)
    max_ticks = round(MAX_LATENCY_S * info.refresh_hz)
    if blends:
        src, wall = _read_blends(src, wall, src_levels, wall_levels, max_ticks)
    notes = []
    for name, d in (("source", src), ("wall", wall)):
        if d.status is not DecodeStatus.OK:
            notes.append(f"{name}: {d.reason}")
    if src.status is DecodeStatus.UNREADABLE or wall.status is DecodeStatus.UNREADABLE:
        return Sample(index, time_s, src, wall, UNREADABLE, note="; ".join(notes))
    if src.status is DecodeStatus.TRANSITION or wall.status is DecodeStatus.TRANSITION:
        return Sample(index, time_s, src, wall, TRANSITION, note="; ".join(notes))

    assert src.count is not None and wall.count is not None
    ticks: float = count_difference(src.count, wall.count)
    if src.span > 1 or wall.span > 1:
        ticks += (src.span - wall.span) / 2  # between the middles of the two runs
    ms = ticks * info.tick_ms
    idle = clock_note(src.count, wall.count, info)
    if idle:
        return Sample(index, time_s, src, wall, IDLE, ticks, ms, idle)
    status = OK
    if ticks < 0:
        status = SUSPECT
        notes.append("wall ahead of source: are the regions swapped?")
    elif ticks > max_ticks:
        status = SUSPECT
        notes.append(f"latency over {MAX_LATENCY_S:g} s: is a region a cell out?")
    elif not _in_run(src.count, info):
        status = SUSPECT
        notes.append("source count outside this run's range: wrong run log?")
    return Sample(index, time_s, src, wall, status, ticks, ms, "; ".join(notes))


def _read_blends(src: strip.Decoded, wall: strip.Decoded, src_levels: list[float],
                 wall_levels: list[float], max_ticks: int
                 ) -> tuple[strip.Decoded, strip.Decoded]:
    """Read either strip that did not read cleanly as a blend. The source is looked for
    near its own rough reading; the wall up to ``max_ticks`` behind the source."""
    if src.status is not DecodeStatus.OK:
        near = strip.rough_count(src_levels)
        if near is not None:
            src = strip.decode_blend(src_levels, near - SOURCE_REACH, near + SOURCE_REACH) or src
    if src.count is None or src.status not in (DecodeStatus.OK, DecodeStatus.BLEND):
        return src, wall
    last = src.count + src.span - 1
    if wall.status is not DecodeStatus.OK:
        wall = strip.decode_blend(wall_levels, src.count - max_ticks, last) or wall
    return src, wall


def clock_note(source: int, wall: int, info: RunInfo) -> str | None:
    """Why this frame shows no latency because the clock was not counting, or None.

    The count holds at the start count until Start and at the stop count after Stop, so a
    frame showing exactly one of those is not measuring the chain's delay."""
    mod = strip.COUNT_MODULUS
    start = None if info.start_count is None else info.start_count % mod
    stop = None if info.stop_count is None else info.stop_count % mod
    if source == start:
        return "clock not started"
    if source == stop:
        return "clock stopped"
    if wall == start:
        return "wall still showing the start count"
    return None


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
            info: RunInfo, blends: bool = True) -> list[Sample]:
    samples = []
    first: int | None = None
    for index, gray in frames:
        if first is None:
            first = index
        samples.append(sample_frame(index, (index - first) * info.frame_ms / 1000, gray,
                                    setup, info, blends))
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
    # Capture rate the source strip shows: the refresh over the ticks it advances per frame.
    measured_capture_fps: float | None = None
    # Latency in frames of each test camera rate of the rate set: {"25": {"median", "mean"}}
    in_frames: dict[str, dict[str, float]] = field(default_factory=dict)
    # Good samples read from frames that saw several counts (to the nearest half tick).
    blended: int = 0
    # How far the latency moves over the recording on a straight-line fit, and whether that
    # is a steady climb (or fall) rather than wander: a sign of a misread region.
    drift_ms: float | None = None
    drift_warning: bool = False
    # How far sound travels in the latency: audio from that far away arrives in step with
    # the picture. {"median", "mean"} in metres.
    sound_m: dict[str, float] = field(default_factory=dict)

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
        measured_capture_fps=measure_capture_fps(samples, info),
        blended=sum(1 for s in samples if s.status == OK
                    and DecodeStatus.BLEND in (s.source.status, s.wall.status)),
        **_drift(samples, info),
        **_equivalents(statistics.median(lat) if lat else None,
                       statistics.fmean(lat) if lat else None, info),
    )


def _drift(samples: list[Sample], info: RunInfo, min_samples: int = 20) -> dict[str, Any]:
    """The latency's straight-line change over the recording, and whether it is a steady
    trend big enough to doubt the reading: more than five ticks and a quarter of the
    median, with the samples close to the line."""
    good = [(s.time_s, s.latency_ms) for s in samples
            if s.status == OK and s.latency_ms is not None]
    if len(good) < min_samples:
        return {}
    t = np.array([g[0] for g in good])
    v = np.array([g[1] for g in good])
    span = float(t.max() - t.min())
    if span <= 0 or v.std() == 0:
        return {"drift_ms": 0.0}
    slope = float(np.polyfit(t, v, 1)[0])
    drift = slope * span
    r = float(np.corrcoef(t, v)[0, 1])
    big = abs(drift) > max(5 * info.tick_ms, 0.25 * float(np.median(v)))
    return {"drift_ms": round(drift, 1), "drift_warning": bool(big and abs(r) > 0.7)}


def _equivalents(median_ms: float | None, mean_ms: float | None,
                 info: RunInfo) -> dict[str, Any]:
    """The latency in test camera frames and as a distance sound travels."""
    if median_ms is None or mean_ms is None:
        return {}
    rs = RATE_SETS.get(info.rate_set)
    rates = rs.test_camera_fps if rs else ()
    return {
        "in_frames": {str(fps): {"median": round(median_ms * fps / 1000, 2),
                                 "mean": round(mean_ms * fps / 1000, 2)} for fps in rates},
        "sound_m": {"median": round(median_ms * SPEED_OF_SOUND / 1000, 1),
                    "mean": round(mean_ms * SPEED_OF_SOUND / 1000, 1)},
    }


def measure_capture_fps(samples: list[Sample], info: RunInfo,
                        min_span: int = 50) -> float | None:
    """The capture rate the recording really has, from how fast the source count advances
    between the first and last counting frames that read. None if too few frames read."""
    counting = [s for s in samples
                if s.source.status is DecodeStatus.OK and s.status != IDLE]
    if len(counting) < 2 or counting[-1].frame - counting[0].frame < min_span:
        return None
    ticks = count_difference(counting[-1].source.count, counting[0].source.count)
    if ticks <= 0:
        return None
    return round(info.refresh_hz * (counting[-1].frame - counting[0].frame) / ticks, 1)
