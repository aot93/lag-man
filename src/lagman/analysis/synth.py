"""Synthetic control camera recordings with a known latency, for tests and demos.

Each camera frame shows the generator output twice: on a "source monitor" and, delayed, on
an "LED wall", both seen at an angle, then blurred, dimmed and noised. Some frames blend two
consecutive counts in light, as a real exposure does when it spans a screen update. An
optional wide-lens distortion bows the strips, as on real footage. The result is
written as a video plus the exact strip regions and a matching run log.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from ..core import layout as layout_mod
from ..core.config import GeneratorConfig
from ..core.rates import get_rate_set
from ..generator.scene import Fill, compose
from .roi import GAMMA, Quad
from .setup import Setup

SCREEN = (960, 540)


@dataclass(frozen=True)
class Truth:
    frame: int
    source: int
    wall: int
    source_blend: float  # 0: shows ``source`` cleanly; otherwise mixes in source + 1
    wall_blend: float


def render_screen(cfg: GeneratorConfig, lay: layout_mod.Layout, count: int) -> np.ndarray:
    """Generator output as a grey image: fills exactly, digits approximately (Hershey font)."""
    img = np.zeros((lay.height, lay.width), np.uint8)
    for op in compose(cfg, lay, count):
        r = op.rect
        if isinstance(op, Fill):
            img[r.y:r.bottom, r.x:r.right] = op.colour[0]
        else:
            scale = cv2.getFontScaleFromHeight(cv2.FONT_HERSHEY_SIMPLEX, int(op.px * 0.72))
            cv2.putText(img, op.text, (r.x, r.y + int(op.px * 1.0)), cv2.FONT_HERSHEY_SIMPLEX,
                        scale, 255, max(1, op.px // 12), cv2.LINE_AA)
    return img


def _place(frame: np.ndarray, screen: np.ndarray, corners: np.ndarray, gain: float,
           offset: float) -> None:
    h, w = screen.shape
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), corners)
    size = (frame.shape[1], frame.shape[0])
    warped = cv2.warpPerspective(screen.astype(np.float32), m, size, flags=cv2.INTER_AREA)
    mask = cv2.warpPerspective(np.ones_like(screen, np.float32), m, size)
    frame[:] = frame * (1 - mask) + (warped * gain + offset) * mask


def _strip_quad(lay: layout_mod.Layout, corners: np.ndarray, size: tuple[int, int],
                lens: float) -> Quad:
    """The strip's corners as seen. With lens distortion the corners are exact but the
    middle bows away from the straight edges, which is what ``refine`` has to find."""
    w, h = lay.width, lay.height
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), corners)
    s = lay.strip
    pts = np.float32([[[s.x, s.y], [s.right, s.y], [s.right, s.bottom], [s.x, s.bottom]]])
    out = cv2.perspectiveTransform(pts, m)[0]
    if lens:
        out = [_distort(p, size, lens) for p in out]
    return Quad(tuple((float(x), float(y)) for x, y in out))  # type: ignore[arg-type]


def _barrel_map(size: tuple[int, int], k: float) -> tuple[np.ndarray, np.ndarray]:
    """remap() tables: each output pixel samples further out, so the picture pulls in."""
    w, h = size
    cx, cy, norm = w / 2, h / 2, math.hypot(w / 2, h / 2)
    x, y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    r2 = ((x - cx) ** 2 + (y - cy) ** 2) / norm ** 2
    f = 1 + k * r2
    return cx + (x - cx) * f, cy + (y - cy) * f


def _distort(p, size: tuple[int, int], k: float) -> tuple[float, float]:
    """Where an undistorted point lands in the output of ``_barrel_map``."""
    w, h = size
    cx, cy, norm = w / 2, h / 2, math.hypot(w / 2, h / 2)
    px, py = float(p[0]) - cx, float(p[1]) - cy
    qx, qy = px, py
    for _ in range(20):  # solve q * (1 + k|q|^2) = p
        f = 1 + k * (qx * qx + qy * qy) / norm ** 2
        qx, qy = px / f, py / f
    return cx + qx, cy + qy


def _open_writer(out: Path, fps: float, size: tuple[int, int]) -> cv2.VideoWriter:
    """Open a grey video writer, with errors that say what went wrong.

    OpenCV only reports that it failed, so check the folder first: the common cause is
    running from a folder the user cannot write to (PowerShell as administrator starts in
    C:\\Windows\\System32).
    """
    out = out.resolve()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb"):
            pass
    except OSError as e:
        raise OSError(f"cannot create {out}: {e.strerror or e}. Choose a folder you can write "
                      f"to, for example your Documents folder.") from None
    for codec in ("mp4v", "avc1", "MJPG"):
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*codec), fps, size, False)
        if writer.isOpened():
            return writer
    raise OSError(f"OpenCV could not encode video to {out} with any of mp4v, avc1 or MJPG; "
                  f"try a .avi file name")


def make(out: Path, seconds: float = 4.0, rate_set: str = "50",
         latency_ticks: Callable[[int], int] | int = 8, start_count: int = 360_000,
         size: tuple[int, int] = (1280, 720), transition_rate: float = 0.15,
         noise: float = 4.0, blur: float = 1.2, seed: int = 1,
         mirror_wall: bool = False, lens: float = 0.0,
         progress: Callable[[int, int], None] | None = None) -> tuple[Setup, list[Truth], Path]:
    """Write ``out`` (a video), ``out.setup.json`` and ``out.run.jsonl``.

    The file is written at half the capture rate, as the S5 does for 100/120fps capture.
    ``lens`` is barrel distortion: how far, as a fraction, the frame's corners pull in.
    """
    rs = get_rate_set(rate_set)
    rng = np.random.default_rng(seed)
    cfg = GeneratorConfig(rate_set=rate_set, tc_rate=rs.test_camera_fps[0])
    lay = layout_mod.compute(cfg, *SCREEN)
    delay = latency_ticks if callable(latency_ticks) else (lambda _f: latency_ticks)

    w, h = size
    src_corners = np.float32([[0.04 * w, 0.18 * h], [0.46 * w, 0.24 * h],
                              [0.45 * w, 0.62 * h], [0.05 * w, 0.66 * h]])
    wall_corners = np.float32([[0.52 * w, 0.12 * h], [0.97 * w, 0.08 * h],
                               [0.96 * w, 0.78 * h], [0.53 * w, 0.72 * h]])
    if mirror_wall:
        wall_corners = wall_corners[[1, 0, 3, 2]]
    setup = Setup(_strip_quad(lay, src_corners, (w, h), lens),
                  _strip_quad(lay, wall_corners, (w, h), lens), (w, h), 0)
    remap = _barrel_map((w, h), lens) if lens else None

    frames = round(seconds * rs.control_camera_fps)
    step = rs.refresh_hz // rs.control_camera_fps
    file_fps = rs.control_camera_fps / 2
    writer = _open_writer(out, file_fps, (w, h))
    truths = []
    try:
        for f in range(frames):
            src = start_count + f * step
            wall = src - delay(f)
            sb = rng.uniform(0.2, 0.8) if rng.random() < transition_rate else 0.0
            wb = rng.uniform(0.2, 0.8) if rng.random() < transition_rate else 0.0
            truths.append(Truth(f, src, wall, sb, wb))
            frame = np.full((h, w), 18.0, np.float32)  # dim studio background
            for count, blend, corners, gain, offset in (
                    (src, sb, src_corners, 0.85, 6.0), (wall, wb, wall_corners, 0.6, 22.0)):
                screen = render_screen(cfg, lay, count).astype(np.float32)
                if blend:
                    nxt = render_screen(cfg, lay, count + 1).astype(np.float32)
                    light = (1 - blend) * (screen / 255) ** GAMMA + blend * (nxt / 255) ** GAMMA
                    screen = 255 * light ** (1 / GAMMA)
                _place(frame, screen, corners, gain, offset)
            if remap is not None:
                frame = cv2.remap(frame, *remap, cv2.INTER_LINEAR)
            if blur:
                frame = cv2.GaussianBlur(frame, (0, 0), blur)
            frame += rng.normal(0, noise, frame.shape)
            writer.write(np.clip(frame, 0, 255).astype(np.uint8))
            if progress and ((f + 1) % 10 == 0 or f + 1 == frames):
                progress(f + 1, frames)
    finally:
        writer.release()

    setup.save(out.with_suffix(".setup.json"))
    run_log = out.with_suffix(".run.jsonl")
    t = datetime.now().astimezone().isoformat()
    slate = ["TEST      synthetic", "CAMERA    Synthetic cam", f"FPS       {cfg.tc_rate}",
             "RUN       1", f"CLOCK     {cfg.tc_rate} fps TC / {rs.refresh_hz} Hz"]
    records = [
        {"event": "run_start", "t": t, "run_id": out.stem, "run_number": 1,
         "config": cfg.to_dict(), "start_count": start_count - 500,
         "refresh_hz": rs.refresh_hz, "tc_rate": cfg.tc_rate, "slate": slate},
        {"event": "run_stop", "t": t, "count": start_count + frames * step + 500},
    ]
    run_log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return setup, truths, run_log
