"""Full-screen generator: renders the clock locked to the display refresh and serves the
web control UI.

Keys: Space start/stop a run, R reset the clock, Esc or Q quit.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from ..core import layout as layout_mod
from ..core.clock import Clock
from ..core.config import START_MODES, GeneratorConfig
from ..core.display import DisplayMode, SimulatedDisplay
from ..core.framemon import REPEAT
from ..core.slate import slate_lines
from .engine import IDLE, CommandError, Engine
from .scene import compose, hex_colour


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="lagman-generator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="JSON config or preset file")
    p.add_argument("--rate-set", choices=("50", "60"))
    p.add_argument("--tc-rate", type=int, help="time code rate; the refresh rate is fast mode")
    p.add_argument("--start", choices=START_MODES, dest="start_mode")
    p.add_argument("--start-value", help="HH:MM:SS:FF, with --start value")
    p.add_argument("--run", action="store_true", help="start a run immediately")
    p.add_argument("--duration", type=float, help="stop and exit after this many seconds")
    p.add_argument("--mode", metavar="WxH@HZ",
                   help="display mode, like 1920x1080@50 (Pi console only; remembered)")
    p.add_argument("--windowed", metavar="WxH", help="window instead of full screen (testing)")
    p.add_argument("--headless", metavar="WxH",
                   help="no display: pace frames with a timer so the web UI can be developed "
                        "and tested (timing is not frame-accurate)")
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    p.add_argument("--presets-dir", type=Path, default=Path("presets"))
    p.add_argument("--host", default="0.0.0.0", help="web UI address (default all interfaces)")
    p.add_argument("--port", type=int, default=8080, help="web UI port (default 8080)")
    p.add_argument("--no-web", action="store_true", help="do not start the web UI")
    p.add_argument("--network", action="store_true",
                   help="let the web UI change this computer's network through NetworkManager "
                        "(for the Pi; headless runs simulate it)")
    p.add_argument("--password", default=os.environ.get("LAGMAN_PASSWORD"),
                   help="web UI password (or LAGMAN_PASSWORD); any user name is accepted")
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


def _size(text: str) -> tuple[int, int]:
    w, h = (int(v) for v in text.lower().split("x"))
    return w, h


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = build_config(args)
    except (ValueError, OSError) as e:
        print(f"lagman-generator: {e}", file=sys.stderr)
        return 2

    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    if args.snapshot:
        return snapshot(cfg, args)
    if args.headless:
        w, h = _size(args.headless)
        return run(cfg, args, None, (w, h), SimulatedDisplay(DisplayMode(w, h, cfg.refresh_hz)))

    import pygame
    from pygame._sdl2 import video

    from .display import SDLDisplay
    from .render import WindowRenderer

    pygame.display.init()  # not pygame.init(): no audio needed
    pygame.font.init()
    if args.windowed:
        window = video.Window("lag-man", size=_size(args.windowed))
    else:
        window = video.Window("lag-man", fullscreen_desktop=True)
        pygame.mouse.set_visible(False)
    renderer = WindowRenderer(window)
    if not renderer.accelerated:
        print("warning: no accelerated renderer; frame timing is not trustworthy",
              file=sys.stderr)
    try:
        return run(cfg, args, renderer, window.size, SDLDisplay(window, not args.windowed))
    finally:
        pygame.quit()


def run(cfg: GeneratorConfig, args: argparse.Namespace, renderer, size: tuple[int, int],
        display=None) -> int:
    """The frame loop. ``renderer`` None means headless: frames are paced by a timer."""
    width, height = size
    engine = Engine(cfg, width, height, args.log_dir, simulated=renderer is None,
                    display=display, mode_request=args.mode)
    for note in engine.notes:
        print(f"warning: {note}", file=sys.stderr)
    for warning in engine.layout.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    mode = engine.display_info()["current"]
    if mode:
        print(f"display {mode}; counting at {engine.cfg.refresh_hz} Hz", file=sys.stderr)
    if engine.display_warning():
        print(f"warning: {engine.display_warning()}", file=sys.stderr)

    server = None
    if not args.no_web:
        from ..web.presets import PresetStore
        from ..web.server import ControlServer

        server = ControlServer(engine, PresetStore(args.presets_dir), args.host, args.port,
                               args.password, _network(args, headless=renderer is None))
        server.start()
        print(f"web control on http://{args.host}:{server.port}/", file=sys.stderr)
        from ..web.network import NetworkSlate

        net_slate = NetworkSlate(engine, server.network, server.port)
        net_slate.start()

    if args.run:
        engine.apply("start")

    started = time.monotonic()
    rate_warned = False
    next_rate_check = started + 2.0
    reported = 0
    sim_start, sim_ns = time.perf_counter(), 0

    # Collector pauses are the likeliest cause of a missed vblank, so keep them rare.
    gc.collect()
    gc.freeze()
    gc.set_threshold(100_000, 50, 50)

    try:
        while True:
            if renderer is not None and not _handle_keys(engine):
                break
            ops, bg = engine.frame()
            if renderer is not None:
                renderer.draw(ops, bg)
                renderer.present()
                engine.presented(time.perf_counter_ns())
            else:
                # No display to measure: report ideal present times so the monitor stays
                # quiet, and pace roughly with sleep.
                sim_ns += round(1e9 / engine.cfg.refresh_hz)
                delay = (sim_start + sim_ns / 1e9) - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                engine.presented(sim_ns)

            events = engine.monitor.repeats + engine.monitor.early
            if events != reported:
                reported = events
                _print_last_event(engine)
            now = time.monotonic()
            if now >= next_rate_check:
                next_rate_check = now + 2.0
                if engine.monitor.mismatched and not rate_warned:
                    rate_warned = True
                    print(f"warning: measured refresh {engine.monitor.measured_hz:.2f} Hz, "
                          f"expected {engine.cfg.refresh_hz} Hz; frame warnings are paused "
                          "until the rates match", file=sys.stderr)
                elif not engine.monitor.mismatched:
                    rate_warned = False
            if args.duration and now - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            net_slate.stop()
            server.stop()
        engine.close()
        mon = engine.monitor
        print(f"{mon.frames} frames, {mon.repeats} repeated refreshes, "
              f"{mon.early} early presents; logs in {args.log_dir}", file=sys.stderr)
    return 0


def _network(args: argparse.Namespace, headless: bool):
    """Network control for the web UI, or None to leave it off."""
    from ..web.network import NetworkControl, NMBackend, SimulatedNetwork

    if headless:
        return NetworkControl(SimulatedNetwork(),
                              note="Simulated network: nothing on this computer is changed.")
    if not args.network:
        return None
    if not shutil.which("nmcli"):
        print("warning: --network needs NetworkManager (nmcli); network control is off",
              file=sys.stderr)
        return NetworkControl(None, note="Network control needs NetworkManager (nmcli), "
                                         "which is not installed.")
    return NetworkControl(NMBackend(), note="Changes go through NetworkManager.")


def _handle_keys(engine: Engine) -> bool:
    import pygame

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_q):
                return False
            try:
                if event.key == pygame.K_SPACE:
                    engine.apply("stop" if engine.state != IDLE else "start")
                elif event.key == pygame.K_r:
                    engine.apply("reset")
            except CommandError as e:
                print(f"warning: {e}", file=sys.stderr)
    return True


def _print_last_event(engine: Engine) -> None:
    events = engine.status()["recent_events"]
    if not events:
        return
    ev = events[-1]
    what = (f"{ev['refreshes']} repeated refresh(es)" if ev["kind"] == REPEAT
            else "early present (vsync not holding)")
    print(f"frame {ev['frame']} at {ev['timecode']}: {what}, gap {ev['gap_ms']:.2f} ms",
          file=sys.stderr)


def snapshot(cfg: GeneratorConfig, args: argparse.Namespace) -> int:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame

    from .render import SurfaceRenderer

    w, h = _size(args.size)
    clock = Clock(cfg)
    count = clock.count if args.count is None else args.count
    lines = slate_lines(cfg, 1, datetime.now()) if cfg.slate.mode != "off" else ()
    lay = layout_mod.compute(cfg, w, h, lines)
    for warning in lay.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    surface = SurfaceRenderer(w, h).draw(compose(cfg, lay, count), hex_colour(cfg.clock.bg))
    pygame.image.save(surface, str(args.snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
