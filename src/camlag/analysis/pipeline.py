"""One analysis run from video to outputs, shared by the command line and the app."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .analyse import Sample, Summary, analyse, summarise
from .overlay import draw, jpeg
from .report import pick_review, write_outputs
from .setup import RunInfo, Setup
from .video import Video

Progress = Callable[[int, int], None]  # frames done, frames total


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Result:
    samples: list[Sample]
    summary: Summary
    paths: dict[str, Path]
    warnings: list[str]


def run(video_path: Path, setup: Setup, info: RunInfo, out_dir: Path,
        start_s: float | None = None, end_s: float | None = None,
        progress: Progress | None = None, cancel: threading.Event | None = None) -> Result:
    warnings: list[str] = []
    with Video(video_path) as video:
        if setup.video_size and tuple(setup.video_size) != (video.width, video.height):
            warnings.append(f"regions were marked on a {setup.video_size[0]}x"
                            f"{setup.video_size[1]} video; this one is "
                            f"{video.width}x{video.height}")
        start = round(start_s * info.capture_fps) if start_s else 0
        end = round(end_s * info.capture_fps) if end_s else None
        total = max(0, (min(end, video.frame_count) if end else video.frame_count) - start)
        samples = analyse(_watched(video.frames(start, end), total, progress, cancel),
                          setup, info)
        summary = summarise(samples, info)
        review = [(title, s, jpeg(draw(video.read(s.frame), setup, s, info, 960)))
                  for title, s in pick_review(samples)]
    paths = write_outputs(out_dir, samples, summary, info, video_path.name, review)
    return Result(samples, summary, paths, warnings)


def _watched(frames: Iterator[tuple[int, np.ndarray]], total: int, progress: Progress | None,
             cancel: threading.Event | None) -> Iterator[tuple[int, np.ndarray]]:
    done = 0
    for item in frames:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        yield item
        done += 1
        if progress and (done % 25 == 0 or done == total):
            progress(done, total)
