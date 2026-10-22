"""Full-screen generator: renders the clock locked to the display refresh.

Keys: Space start/stop, R reset, Esc or Q quit.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from ..core import layout as layout_mod
from ..core.clock import Clock
from ..core.config import START_MODES, GeneratorConfig
from ..core.framemon import REPEAT, FrameMonitor
from ..core.runlog import RunLog, new_run_id
from ..core.slate import slate_lines
from ..core.timecode import to_timecode
from .scene import compose, hex_colour


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="camlag-generator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="JSON config or preset file")
    p.add_argument("--rate-set", choices=("50", "60"))
    p.add_argument("--tc-rate", type=int, help="time code rate; the refresh rate is fast mode")
    p.add_argument("--start", choices=START_MODES, dest="start_mode")
    p.add_argument("--start-value", help="HH:MM:SS:FF, with --start value")
    p.add_argument("--run", action="store_true", help="start counting immediately")
    p.add_argument("--duration", type=float, help="stop and exit after this many seconds")
    p.add_argument("--windowed", metavar="WxH", help="window instead of full screen (testing)")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    p.add_argument("--snapshot", type=Path, metavar="PNG",
                   help="render one frame off-screen to a PNG and exit (no display needed)")
    p.add_argument("--size", default="1920x1080", metavar="WxH", help="snapshot size")
    p.add_argument("--count", type=int, help="snapshot count (default: the start count)")
    return p.parse_args(argv)


def build_config(args: argparse.Namespace) -> GeneratorConfig:
    data = json.loads(args.config.read_text()) if args.config else {}
    for key in ("rate_set", "tc_rate", "start_mode", "start_value"):
        value = getattr(args, key)
        if value is not None:
            data[key] = value
    if args.rate_set and args.tc_rate is None and "tc_rate" not in data:
        data["tc_rate"] = {"50": 25, "60": 30}[args.rate_set]
    return GeneratorConfig.from_dict(data)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = build_config(args)
    except (ValueError, OSError) as e:
        print(f"camlag-generator: {e}", file=sys.stderr)
        return 2

    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    if args.snapshot:
        return snapshot(cfg, args)
    import pygame
    from pygame._sdl2 import video

    from .render import WindowRenderer

    pygame.init()
    if args.windowed:
        w, h = (int(v) for v in args.windowed.lower().split("x"))
        window = video.Window("cam-lag", size=(w, h))
    else:
        window = video.Window("cam-lag", fullscreen_desktop=True)
        pygame.mouse.set_visible(False)
    width, height = window.size
    renderer = WindowRenderer(window)
    desktop_hz = _desktop_refresh(pygame)
    if not renderer.accelerated:
        print("warning: no accelerated renderer; frame timing is not trustworthy",
              file=sys.stderr)

    clock = Clock(cfg)
    run_number = 1
    lines = slate_lines(cfg, run_number, datetime.now()) if cfg.slate.mode != "off" else ()
    lay = layout_mod.compute(cfg, width, height, lines)
    for warning in lay.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    bg = hex_colour(cfg.clock.bg)

    log = RunLog(args.log_dir, new_run_id())
    log.write("run_start", run_number=run_number, config=cfg.to_dict(),
              start_count=clock.count, display={"width": width, "height": height,
                                                "desktop_refresh_hz": desktop_hz},
              slate=list(lay.slate_lines))
    if desktop_hz and desktop_hz != cfg.refresh_hz:
        _warn(log, f"display reports {desktop_hz} Hz; this rate set needs {cfg.refresh_hz} Hz")

    monitor = FrameMonitor(cfg.refresh_hz)
    if args.run:
        clock.start()
    started = time.monotonic()
    pre_roll_until = started + cfg.slate.pre_roll_s
    next_rate_check = started + 2.0
    rate_warned = False
    frame = 0

    # Collector pauses are the likeliest cause of a missed vblank, so keep them rare.
    gc.collect()
    gc.freeze()
    gc.set_threshold(100_000, 50, 50)

    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    raise KeyboardInterrupt
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        raise KeyboardInterrupt
                    if event.key == pygame.K_SPACE:
                        clock.stop() if clock.running else clock.start()
                        log.write("start" if clock.running else "stop", count=clock.count)
                    if event.key == pygame.K_r:
                        clock.reset()
                        log.write("reset", count=clock.count)

            now = time.monotonic()
            show_slate = cfg.slate.mode == "always" or (
                cfg.slate.mode == "pre_roll" and now < pre_roll_until)
            renderer.draw(compose(cfg, lay, clock.count, show_slate), bg)
            renderer.present()
            t = time.perf_counter_ns()
            shown = clock.count
            clock.frame_displayed()

            for ev in monitor.observe(frame, t):
                _log_frame_event(log, ev, shown, cfg)
            frame += 1

            if now >= next_rate_check:
                next_rate_check = now + 2.0
                if monitor.rate_mismatch() and not rate_warned:
                    rate_warned = True
                    _warn(log, f"measured refresh {monitor.measured_hz:.2f} Hz, "
                               f"expected {cfg.refresh_hz} Hz")
            if args.duration and now - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        for ev in monitor.flush():
            _log_frame_event(log, ev, clock.count, cfg)
        log.write("run_stop", count=clock.count, frames=monitor.frames,
                  repeated_refreshes=monitor.repeats, early_presents=monitor.early,
                  measured_refresh_hz=monitor.measured_hz)
        log.close()
        pygame.quit()
        print(f"{monitor.frames} frames, {monitor.repeats} repeated refreshes, "
              f"{monitor.early} early presents; log {log.path}", file=sys.stderr)
    return 0


def snapshot(cfg: GeneratorConfig, args: argparse.Namespace) -> int:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame

    from .render import SurfaceRenderer

    w, h = (int(v) for v in args.size.lower().split("x"))
    clock = Clock(cfg)
    count = clock.count if args.count is None else args.count
    lines = slate_lines(cfg, 1, datetime.now()) if cfg.slate.mode != "off" else ()
    lay = layout_mod.compute(cfg, w, h, lines)
    for warning in lay.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    surface = SurfaceRenderer(w, h).draw(compose(cfg, lay, count), hex_colour(cfg.clock.bg))
    pygame.image.save(surface, str(args.snapshot))
    return 0


def _log_frame_event(log: RunLog, ev, count: int, cfg: GeneratorConfig) -> None:
    tc = to_timecode(count, cfg.refresh_hz, cfg.tc_rate)
    log.write("frame", kind=ev.kind, frame=ev.frame, refreshes=ev.refreshes,
              gap_ms=round(ev.gap_ms, 3), count=count, timecode=tc.hmsf() + tc.ms())
    what = (f"{ev.refreshes} repeated refresh(es)" if ev.kind == REPEAT
            else "early present (vsync not holding)")
    print(f"frame {ev.frame} at {tc.hmsf()}: {what}, gap {ev.gap_ms:.2f} ms", file=sys.stderr)


def _warn(log: RunLog, message: str) -> None:
    log.write("warning", message=message)
    print(f"warning: {message}", file=sys.stderr)


def _desktop_refresh(pygame) -> int | None:
    try:
        rates = pygame.display.get_desktop_refresh_rates()
    except (AttributeError, pygame.error):
        return None
    return rates[0] if rates else None


if __name__ == "__main__":
    sys.exit(main())
