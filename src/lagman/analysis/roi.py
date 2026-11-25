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

CELL_PX = 24  # rectified samples per cell, horizontally and vertically
INNER_X = (0.25, 0.75)  # sampled part of each cell, as fractions of its width
INNER_Y = (0.2, 0.8)
GAMMA = 2.2  # video code values to light, near enough for Rec.709 and sRGB footage

Point = tuple[float, float]

# Code value -> linear light, scaled back to 0-255. A cell lit for half an exposure is half
# as bright in light but about 0.73 of white in code values, so it must be averaged and
# thresholded in light to read as mid-change rather than as a confident 1.
_LINEAR = (255 * (np.arange(256) / 255.0) ** GAMMA).astype(np.float32)


def linear_levels(levels: Sequence[float]) -> list[float]:
    """Code-value levels (0-255) as linear light on the same scale."""
    return [255 * (max(0.0, v) / 255.0) ** GAMMA for v in levels]


@dataclass(frozen=True)
class Quad:
    """Corners of a strip in video pixels: along the top edge, then back along the bottom.

    Which end comes first does not matter (the strip format is direction-safe), but the
    first edge must run along the strip's length. ``bow`` moves the cell grid along the
    strip: the middle cell boundary moves by ``bow`` cells (positive towards the end), the
    ends stay put, and the cells between grow or shrink steadily from one end to the other,
    as perspective does to a strip seen at an angle. Lens distortion bends a strip in much
    the same way.
    """

    points: tuple[Point, Point, Point, Point]
    bow: float = 0.0

    @classmethod
    def from_points(cls, pts: Sequence[Sequence[float]], bow: float | None = 0.0) -> Quad:
        """Accept four corners clicked in any order and put them in loop order, long edge first.

        ``bow`` is measured from the end the first corner is at, so it changes sign if the
        loop order puts that end last. None estimates it from the corners (``perspective``),
        which only suits corners that were fitted, not clicked.
        """
        if len(pts) != 4:
            raise ValueError("a region needs exactly four corners")
        given = [(float(x), float(y)) for x, y in pts]
        p = list(given)
        cx = sum(x for x, _ in p) / 4
        cy = sum(y for _, y in p) / 4
        p.sort(key=lambda q: math.atan2(q[1] - cy, q[0] - cx))
        if _dist(p[0], p[1]) < _dist(p[1], p[2]):
            p = p[1:] + p[:1]
        if bow is None:
            bow = perspective(p)
        elif given[0] in (p[1], p[2]):
            bow = -float(bow)
        bow = float(bow)
        if not math.isfinite(bow) or abs(bow) > 2:
            raise ValueError("bow must be within 2 cells")
        quad = cls(tuple(p), bow)  # type: ignore[arg-type]
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

    def boundaries(self, cells: int = strip.CELLS) -> np.ndarray:
        """Cell boundary positions along the strip, in cells (``cells + 1`` of them)."""
        return cells * _bowed(np.arange(cells + 1) / cells, np.asarray(self.bow), cells)

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
        ), self.bow)


def perspective(points: Sequence[Point], cells: int = strip.CELLS) -> float:
    """The bow a strip with these corners (loop order, long edge first) has if its ends
    differ in height because one is further away: height goes as 1/distance and spacing
    along the strip as 1/distance squared. Only a starting point: the ends' heights are
    known to a pixel or two."""
    a, b, c, d = points
    near, far = _dist(a, d), _dist(b, c)
    if near <= 0 or far <= 0:
        return 0.0
    k = near / far - 1
    return float(np.clip(cells * k / (4 + 2 * k), -2, 2))


def _bowed(t: np.ndarray, bow: np.ndarray, cells: int = strip.CELLS) -> np.ndarray:
    """Where fraction ``t`` of the way along the strip is seen, for a ``bow`` (in cells, at
    the middle): the spacing a flat strip has in perspective, ends fixed. Broadcasts."""
    k = 4 * bow / (cells - 2 * bow)
    return t * (1 + k) / (1 + k * t)


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def rectify(gray: np.ndarray, quad: Quad, cells: int = strip.CELLS) -> np.ndarray:
    """The strip straightened out, ``cells`` cells of ``CELL_PX`` square.

    Bilinear between the corners, not a perspective warp: a strip is a few pixels tall, so
    its height at each end is only known to a pixel or two, and a perspective warp would
    turn that into a squeeze along the strip of up to a cell. Foreshortening along the strip
    is, to first order, a bow, which the fit finds from the cell edges themselves.
    """
    w, h = cells * CELL_PX, CELL_PX
    a, b, c, d = (np.array(p, np.float32) for p in quad.points)
    u = ((np.arange(w, dtype=np.float32) + 0.5) / w)[None, :, None]
    v = ((np.arange(h, dtype=np.float32) + 0.5) / h)[:, None, None]
    pts = (1 - v) * ((1 - u) * a + u * b) + v * ((1 - u) * d + u * c) - 0.5
    return cv2.remap(gray, pts[..., 0], pts[..., 1], cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def _profile(gray: np.ndarray, quad: Quad, margin: int = 0) -> np.ndarray:
    """Light level along the strip (plus ``margin`` cells each side), averaged across it."""
    r = rectify(gray, quad.adjusted(margin, margin) if margin else quad,
                strip.CELLS + 2 * margin)
    y0, y1 = (round(f * CELL_PX) for f in INNER_Y)
    return _LINEAR[r[y0:y1]].mean(axis=0)


def _span_means(profile: np.ndarray, x0: np.ndarray, x1: np.ndarray) -> np.ndarray:
    """Mean of ``profile`` over each [x0, x1), with fractional ends."""
    integral = np.concatenate([[0.0], np.cumsum(profile, dtype=np.float64)])
    grid = np.arange(len(integral))
    return (np.interp(x1, grid, integral) - np.interp(x0, grid, integral)) / (x1 - x0)


def cell_levels(gray: np.ndarray, quad: Quad, inner_x: tuple[float, float] = INNER_X
                ) -> list[float]:
    """Mean light level of the inner part of each cell, left to right along the quad."""
    b = quad.boundaries() * CELL_PX
    width = np.diff(b)
    return _span_means(_profile(gray, quad), b[:-1] + inner_x[0] * width,
                       b[:-1] + inner_x[1] * width).tolist()


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


def refine(frames: Sequence[np.ndarray], quad: Quad, reach: float = 0.6,
           step: float = 0.05, indices: Sequence[int] | None = None,
           ticks_per_frame: float | None = None) -> tuple[Quad, int]:
    """Fit a hand-marked quad to the strip seen in ``frames``.

    First each end is snapped to the white reference block there (``snap_ends``), which
    takes out the shear and tilt of corners clicked a few pixels off. Then the cell grid is
    fitted along the strip: each end moves up to ``reach`` cells and the middle bows up to
    ``BOW_REACH`` cells (perspective), keeping the grid whose boundaries best match the
    strip's edges. Frames spread across a recording work best: more of the code's bits
    change, so more of the boundaries show. Last, the region is nudged along and across the
    strip, and its bow, for the most frames read (``_nudge``): on small, soft cells a pixel
    either way decides whether a frame reads.

    Given the frames' ``indices`` in the video and the clock ``ticks_per_frame``, a frame
    only counts as read if its count fits the others: a grid a cell out can read cleanly
    but wrongly, with counts about half or twice the true ones. Returns the quad and the
    number of frames read.
    """
    def score(q: Quad) -> int:
        return reads(frames, q, indices, ticks_per_frame)

    starts = [(quad, reach)]
    snapped = snap_ends(frames, quad)
    if snapped is not None:
        starts.insert(0, (snapped, min(reach, 0.3)))
    # The corners' heights also suggest a bow (a screen at an angle), but clicked corners
    # are a pixel or two out, so that is only tried as a second start, never trusted.
    guess = perspective(quad.points)
    if abs(guess - quad.bow) > 0.2:
        starts += [(Quad(q.points, guess), r) for q, r in starts]
    best: tuple[Quad, int] | None = None
    for q, r in starts:
        # Coarse, with the bow free to take up the foreshortening; then fine.
        q = _fit(frames, q, r, step, bow_reach=max(r, BOW_REACH))
        q = _fit(frames, q, step * 2, step / 4)
        q, clean = _nudge(q, score, len(frames))
        if best is None or clean > best[1]:
            best = (q, clean)
    assert best is not None
    return best


def reads(frames: Sequence[np.ndarray], quad: Quad, indices: Sequence[int] | None = None,
          ticks_per_frame: float | None = None, tolerance: float = 6.0) -> int:
    """How many ``frames`` read cleanly. With their ``indices`` and the clock's
    ``ticks_per_frame``, only reads whose counts lie on one line at that rate, within
    ``tolerance`` ticks (the wall's delay wanders a little), count."""
    counts = [(i, d.count) for i, d in zip(range(len(frames)) if indices is None else indices,
                                          (read(f, quad) for f in frames), strict=True)
              if d.status is strip.DecodeStatus.OK]
    if ticks_per_frame is None or indices is None:
        return len(counts)
    offsets = np.array([c - ticks_per_frame * i for i, c in counts])
    if not len(offsets):
        return 0
    return int(max(np.sum(np.abs(offsets - o) <= tolerance + ticks_per_frame)
                   for o in offsets))


NUDGE_ALONG = (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3)  # cells
NUDGE_ACROSS = (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3)  # strip heights


def _nudge(quad: Quad, score, frames: int, rounds: int = 3) -> tuple[Quad, int]:
    """Move the start, the end, the bow and the whole region across the strip, one at a
    time, keeping a move only if it reads clearly more frames (a frame or two can read by
    chance with the cells sampled off centre), and the smallest move that does."""
    gain = max(2, frames // 10)
    params = [0.0, 0.0, quad.bow, 0.0]  # start, end, bow, across

    def build(p: list[float]) -> Quad:
        q = quad.adjusted(p[0], p[1])
        a, b, c, d = (np.array(pt, float) for pt in q.points)
        along = (b - a) + (c - d)
        across = np.array([-along[1], along[0]]) / np.linalg.norm(along)
        if np.dot(across, (d - a) + (c - b)) < 0:
            across = -across
        shift = across * p[3] * quad.height
        return Quad(tuple(tuple(float(v) for v in pt + shift) for pt in (a, b, c, d)),
                    float(np.clip(p[2], -2, 2)))

    best = score(quad)
    for _ in range(rounds):
        moved = False
        for k, options in ((0, NUDGE_ALONG), (1, NUDGE_ALONG), (2, NUDGE_ALONG),
                           (3, NUDGE_ACROSS)):
            base, pick = params[k], None
            for delta in sorted(options, key=abs):
                if not delta:
                    continue
                trial = list(params)
                trial[k] = base + delta
                got = score(build(trial))
                if got >= best + gain and (pick is None or got > pick[0]):
                    pick = (got, trial)
            if pick is not None:
                (best, params), moved = pick, True
        if not moved:
            break
    return build(params), best


def snap_ends(frames: Sequence[np.ndarray], quad: Quad) -> Quad | None:
    """Move each end of ``quad`` onto the outer corners of the white block there.

    Both ends of the strip are reference cells that are always white (one cell at the
    start, two at the end, with an always-black cell inside each), so on the mean of a few
    frames each end is a bright block on the dark screen. The block is the one under the
    point half a cell in from the marked end, so the strip may run either way. None if a
    block is not found or a corner would move implausibly far.
    """
    img = np.mean(np.stack([f.astype(np.float32) for f in frames]), axis=0)
    a, b, c, d = (np.array(p, float) for p in quad.points)
    along = ((b - a) + (c - d)) / 2
    length = float(np.linalg.norm(along))
    u = along / length
    v = np.array([-u[1], u[0]])
    if np.dot(v, (d - a) + (c - b)) < 0:
        v = -v  # v points from the top edge towards the bottom edge
    cell, height = length / strip.CELLS, quad.height
    corners = []
    for top, bot, outward in ((a, d, -u), (b, c, u)):
        mid = (top + bot) / 2
        found = _end_block(img, mid - outward * cell / 2, cell, height)
        if found is None:
            return None
        # Only the part of the block in a band around the marked strip: text or a bright
        # patch just above or below can join it (the milliseconds sit right over the end).
        across = (found - mid) @ v
        found = found[np.abs(across) <= 0.75 * height + 1]
        if len(found) < 4:
            return None
        # Outer corners: furthest out along the strip, then furthest up or down. Scale
        # across by cell/height so a tall thin block weighs both directions alike.
        out_ = found @ outward / cell
        down = found @ v / height
        new_top, new_bot = found[np.argmax(out_ - down)], found[np.argmax(out_ + down)]
        for new, old in ((new_top, top), (new_bot, bot)):
            if np.linalg.norm(new - old) > max(1.5 * cell, height):
                return None
        corners.append((new_top, new_bot))
    (ta, da), (tb, cb) = corners
    try:
        return Quad.from_points([ta, tb, cb, da], quad.bow)
    except ValueError:
        return None


def _end_block(img: np.ndarray, centre: np.ndarray, cell: float,
               height: float) -> np.ndarray | None:
    """Pixel centres of the bright block under ``centre``, in a window around it."""
    r = int(max(3.5 * cell, 1.5 * height)) + 2
    x0, y0 = max(0, int(centre[0]) - r), max(0, int(centre[1]) - r)
    win = img[y0:int(centre[1]) + r, x0:int(centre[0]) + r]
    cx, cy = int(round(centre[0])) - x0, int(round(centre[1])) - y0
    if win.size == 0 or not (0 <= cy < win.shape[0] and 0 <= cx < win.shape[1]):
        return None
    # Top-hat: take away the background (an opening wider than the block), so glare
    # across the screen does not join the block to a bright patch beside it.
    size = int(1.5 * max(2 * cell, height)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    hat = cv2.morphologyEx(win, cv2.MORPH_TOPHAT, kernel)
    w8 = cv2.normalize(hat, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    _, bw = cv2.threshold(w8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, labels = cv2.connectedComponents(bw)
    k = labels[cy, cx]
    if k == 0:
        return None
    ys, xs = np.nonzero(labels == k)
    if len(xs) > 0.5 * labels.size:  # not a block: the threshold found the background
        return None
    return np.stack([xs + x0, ys + y0], axis=1).astype(float)


BOW_REACH = 1.5  # cells: a screen well off to the side foreshortens its strip about this much


def _fit(frames: Sequence[np.ndarray], quad: Quad, reach: float, step: float,
         bow_reach: float | None = None) -> Quad:
    # The score is a sum over frames, so add up each frame's edge strength once.
    edges = sum(np.abs(np.diff(_profile(f, quad, margin=1))) for f in frames)
    n = round(reach / step)
    grid = np.arange(-n, n + 1) * step
    nb = round((reach if bow_reach is None else bow_reach) / step)
    bows = np.clip(np.arange(-nb, nb + 1) * step + quad.bow, -2, 2)
    a, b, c = np.meshgrid(grid, grid, bows, indexing="ij")
    a, b, c = a.ravel(), b.ravel(), c.ravel()
    t = np.arange(strip.CELLS + 1) / strip.CELLS
    # Boundary positions in the widened profile: start shift a, length change b, bow c.
    pos = (1 + strip.CELLS * _bowed(t, c[:, None]) + a[:, None] + b[:, None] * t) * CELL_PX
    idx = np.clip(np.round(pos).astype(int) - 1, 0, len(edges) - 1)
    score = edges[idx].sum(axis=1)
    near_best = np.flatnonzero(score >= score.max() * (1 - 1e-4))
    i = near_best[np.argmin(np.abs(a[near_best]) + np.abs(b[near_best])
                            + np.abs(c[near_best] - quad.bow))]
    fitted = quad.adjusted(-a[i], a[i] + b[i])
    return Quad(fitted.points, round(float(c[i]), 3))
