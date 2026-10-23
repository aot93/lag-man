"""Regions of interest: a strip seen by the control camera, as a four-corner quad.

The camera sees each screen at an angle, so a strip is a quadrilateral in the video frame.
Sampling warps it back to a rectangle and averages the inner part of each cell, which keeps
the read clear of blur and moiré at the cell edges.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from ..core import strip

CELL_PX = 12  # rectified samples per cell, horizontally and vertically
INNER_X = (0.25, 0.75)  # sampled part of each cell, as fractions of its width
INNER_Y = (0.2, 0.8)

Point = tuple[float, float]


@dataclass(frozen=True)
class Quad:
    """Corners of a strip in video pixels: along the top edge, then back along the bottom.

    Which end comes first does not matter (the strip format is direction-safe), but the
    first edge must run along the strip's length.
    """

    points: tuple[Point, Point, Point, Point]

    @classmethod
    def from_points(cls, pts: Sequence[Sequence[float]]) -> Quad:
        """Accept four corners clicked in any order and put them in loop order, long edge first."""
        if len(pts) != 4:
            raise ValueError("a region needs exactly four corners")
        p = [(float(x), float(y)) for x, y in pts]
        cx = sum(x for x, _ in p) / 4
        cy = sum(y for _, y in p) / 4
        p.sort(key=lambda q: math.atan2(q[1] - cy, q[0] - cx))
        if _dist(p[0], p[1]) < _dist(p[1], p[2]):
            p = p[1:] + p[:1]
        quad = cls(tuple(p))  # type: ignore[arg-type]
        if quad.length < 2 * strip.CELLS / 10 or quad.height < 1:
            raise ValueError("region is too small to hold a strip")
        return quad

    @property
    def length(self) -> float:
        a, b, c, d = self.points
        return (_dist(a, b) + _dist(d, c)) / 2

    @property
    def height(self) -> float:
        a, b, c, d = self.points
        return (_dist(a, d) + _dist(b, c)) / 2

    def to_json(self) -> list[list[float]]:
        return [[round(x, 2), round(y, 2)] for x, y in self.points]

    def adjusted(self, start: float, end: float) -> Quad:
        """Move the two ends along the strip by fractions of a cell (positive is outward)."""
        a, b, c, d = self.points
        cell_top = ((b[0] - a[0]) / strip.CELLS, (b[1] - a[1]) / strip.CELLS)
        cell_bot = ((c[0] - d[0]) / strip.CELLS, (c[1] - d[1]) / strip.CELLS)
        return Quad((
            (a[0] - start * cell_top[0], a[1] - start * cell_top[1]),
            (b[0] + end * cell_top[0], b[1] + end * cell_top[1]),
            (c[0] + end * cell_bot[0], c[1] + end * cell_bot[1]),
            (d[0] - start * cell_bot[0], d[1] - start * cell_bot[1]),
        ))


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def rectify(gray: np.ndarray, quad: Quad, cells: int = strip.CELLS) -> np.ndarray:
    w, h = cells * CELL_PX, CELL_PX
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    m = cv2.getPerspectiveTransform(np.float32(quad.points), dst)
    return cv2.warpPerspective(gray, m, (w, h), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)


def cell_levels(gray: np.ndarray, quad: Quad, inner_x: tuple[float, float] = INNER_X
                ) -> list[float]:
    """Mean level of the inner part of each cell, left to right along the quad."""
    r = rectify(gray, quad).astype(np.float32)
    y0, y1 = (round(f * CELL_PX) for f in INNER_Y)
    x0, x1 = (round(f * CELL_PX) for f in inner_x)
    cells = r[y0:y1].reshape(y1 - y0, strip.CELLS, CELL_PX)[:, :, x0:x1]
    return cells.mean(axis=(0, 2)).tolist()


def read(gray: np.ndarray, quad: Quad) -> strip.Decoded:
    return strip.decode(cell_levels(gray, quad))


def margin(levels: list[float]) -> float:
    """How cleanly a strip reads: the smallest distance of a data cell from mid-level, 0..0.5.

    Negative when the strip does not decode.
    """
    norm = _normalised(levels)
    return -1.0 if norm is None else min(abs(v - 0.5) for v in norm)


def _normalised(levels: list[float]) -> list[float] | None:
    d = strip.decode(levels)
    if d.status is not strip.DecodeStatus.OK:
        return None
    if d.reversed:
        levels = levels[::-1]
    whites = [levels[0], levels[-1], levels[-2]]
    blacks = [levels[1], levels[-3]]
    lo, hi = sum(blacks) / len(blacks), sum(whites) / len(whites)
    return [(v - lo) / (hi - lo) for v in levels[2:-3]]


def refine(frames: Sequence[np.ndarray], quad: Quad, reach: float = 0.5,
           step: float = 0.05) -> tuple[Quad, int]:
    """Nudge the ends of a hand-marked quad so the cells line up with the strip.

    Tries moving each end up to ``reach`` cells in or out and keeps the position where the
    strip's edges (between unlike cells, and at both ends against the dark surround) fall on
    the predicted cell boundaries. Ties keep the smallest move. Returns the quad and how
    many of the frames it reads cleanly.
    """
    n = round(reach / step)
    steps = sorted((round(i * step, 3) for i in range(-n, n + 1)), key=abs)
    best, best_score = quad, _edge_score(frames, quad)
    for s in steps:
        for e in steps:
            q = quad.adjusted(s, e)
            score = _edge_score(frames, q)
            if score > best_score * (1 + 1e-4):
                best, best_score = q, score
    return best, sum(read(f, best).status is strip.DecodeStatus.OK for f in frames)


def _edge_score(frames: Sequence[np.ndarray], quad: Quad) -> float:
    """Total step in level across every predicted cell boundary, including both ends."""
    wide = quad.adjusted(1, 1)  # one cell of surround each side
    y0, y1 = (round(f * CELL_PX) for f in INNER_Y)
    bounds = np.arange(1, strip.CELLS + 2) * CELL_PX
    total = 0.0
    for f in frames:
        prof = rectify(f, wide, strip.CELLS + 2)[y0:y1].astype(np.float32).mean(axis=0)
        total += float(np.abs(prof[bounds] - prof[bounds - 1]).sum())
    return total
