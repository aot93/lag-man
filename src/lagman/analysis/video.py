"""Reading control camera recordings with OpenCV."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np


class Video:
    def __init__(self, path: Path):
        self.path = path
        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            raise ValueError(f"cannot open video {path}")
        self.frame_count = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.file_fps = float(self._cap.get(cv2.CAP_PROP_FPS))
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def frames(self, start: int = 0, end: int | None = None) -> Iterator[tuple[int, np.ndarray]]:
        """Grey frames in order, decoded sequentially from ``start`` to ``end`` (exclusive)."""
        self._seek(start)
        index = start
        while end is None or index < end:
            ok, frame = self._cap.read()
            if not ok:
                return
            yield index, to_gray(frame)
            index += 1

    def read(self, index: int) -> np.ndarray:
        """One colour frame. Seeking in long-GOP H.264 relies on the FFmpeg backend landing
        on the exact frame; TODO(verify) against the S5 files."""
        self._seek(index)
        ok, frame = self._cap.read()
        if not ok:
            raise IndexError(f"frame {index} is beyond the end of {self.path.name}")
        return frame

    def _seek(self, index: int) -> None:
        if int(self._cap.get(cv2.CAP_PROP_POS_FRAMES)) != index:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, index)

    def close(self) -> None:
        self._cap.release()

    def __enter__(self) -> Video:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def to_gray(frame: np.ndarray) -> np.ndarray:
    return frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
