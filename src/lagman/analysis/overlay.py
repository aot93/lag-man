"""Review images: a video frame with both strip regions outlined and their decoded values."""

from __future__ import annotations

import cv2
import numpy as np

from .analyse import Sample
from .roi import Quad
from .setup import RunInfo, Setup

# Brand series colours (design/tokens.css --s2, --s1), as BGR.
SOURCE_BGR = (224, 125, 46)
WALL_BGR = (55, 39, 238)


def _label(sample: Sample | None, which: str, info: RunInfo) -> str:
    if sample is None:
        return which.upper()
    d = sample.source if which == "source" else sample.wall
    row = sample.row(info)
    if d.count is not None:
        text = f"{which.upper()} {row[f'{which}_tc']} ({d.count})"
    elif d.candidates:
        text = f"{which.upper()} mid-change {min(d.candidates)}/{max(d.candidates)}"
    else:
        text = f"{which.upper()} unreadable: {d.reason}"
    return text + (" [reversed]" if d.reversed else "")


def draw(frame: np.ndarray, setup: Setup, sample: Sample | None, info: RunInfo,
         max_width: int = 1280) -> np.ndarray:
    """The frame, cropped to the two regions with a margin, outlines and labels drawn on."""
    img = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    img = img.copy()
    quads = ((setup.source, SOURCE_BGR, "source"), (setup.wall, WALL_BGR, "wall"))
    pts = np.array([p for q, _, _ in quads for p in q.points])
    h, w = img.shape[:2]
    margin = int(0.08 * max(w, h))
    x0, y0 = (max(0, int(v) - margin) for v in pts.min(axis=0))
    x1, y1 = (int(v) + margin for v in pts.max(axis=0))
    x1, y1 = min(w, x1), min(h, y1)
    crop = img[y0:y1, x0:x1].copy()
    scale = min(1.0, max_width / max(1, crop.shape[1]))
    if scale < 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    thick = max(2, round(crop.shape[1] / 500))
    font_scale = max(0.5, crop.shape[1] / 1600)
    for quad, colour, which in quads:
        poly = _scaled(quad, x0, y0, scale)
        cv2.polylines(crop, [poly], True, colour, thick, cv2.LINE_AA)
        text = _label(sample, which, info)
        (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
        tx = int(np.clip(poly[:, 0].min(), 0, max(0, crop.shape[1] - tw - 8)))
        ty = int(poly[:, 1].max()) + th + 10
        if ty + base + 4 > crop.shape[0]:
            ty = int(poly[:, 1].min()) - 10
        cv2.rectangle(crop, (tx - 4, ty - th - 6), (tx + tw + 4, ty + base + 2), (0, 0, 0), -1)
        cv2.rectangle(crop, (tx - 4, ty - th - 6), (tx + tw + 4, ty + base + 2), colour, 1)
        cv2.putText(crop, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, font_scale,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return crop


def _scaled(quad: Quad, x0: int, y0: int, scale: float) -> np.ndarray:
    return np.array([[round((x - x0) * scale), round((y - y0) * scale)]
                     for x, y in quad.points], np.int32)


def jpeg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()
