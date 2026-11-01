"""Generator state machine, shared by the render loop and the web server.

The render loop owns all state. Other threads call ``submit`` to queue a command, which the
render loop applies between frames, so a frame never sees a half-applied change. ``status``
and ``preview`` read snapshots that the render loop publishes once per frame.

The count runs at the display's refresh: the rate set's standard rate, or its fallback when
the display mode is 50 or 60Hz (SPEC.md, "Display mode").
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core import layout as layout_mod
from ..core.clock import Clock
from ..core.config import GeneratorConfig
from ..core.display import DisplayControl, DisplayMode, usable_modes
from ..core.framemon import FrameEvent, FrameMonitor
from ..core.rates import get_rate_set
from ..core.runlog import RunLog, new_run_id
from ..core.slate import slate_lines
from ..core.timecode import parse_timecode, to_timecode
from .scene import Fill, Op, compose, hex_colour

IDLE = "idle"
PRE_ROLL = "pre_roll"
RUNNING = "running"
RECENT_EVENTS = 20


class CommandError(Exception):
    """A command that cannot be applied in the current state; reported to the caller."""


@dataclass
class _Command:
    name: str
    args: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Exception | None = None


class Engine:
    def __init__(self, cfg: GeneratorConfig, width: int, height: int, log_dir: Path,
                 now: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic, simulated: bool = False,
                 display: DisplayControl | None = None, mode_request: str | None = None):
        self.width, self.height = width, height
        self.simulated = simulated
        self.log_dir = log_dir
        self._now, self._mono = now, monotonic
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._lock = threading.Lock()
        self.notes: list[str] = []  # startup messages for the console
        self.display = display
        self._mode = display.current() if display else None
        self._modes = usable_modes(display.modes()) if display else []
        self.cfg = self._fit(cfg)
        self.clock = Clock(self.cfg, now=now)
        self.monitor = FrameMonitor(self.cfg.refresh_hz)
        self._mismatch = False
        self.state = IDLE
        state = self._load_state()
        self.run_number = _int(state.get("next_run_number"), 1)
        self._saved_mode = state.get("display_mode")
        self.run_log: RunLog | None = None
        self.run_id: str | None = None
        self._pre_roll_until = 0.0
        self._frame = 0
        self._shown = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENTS)
        self._unacked = 0
        self._warnings: list[str] = []
        self._slate: tuple[str, ...] = ()
        self._config_rev = 0
        self._relayout()
        self._restore_mode(mode_request)
        self._publish_display()
        self._publish()

    # ---- called from any thread -------------------------------------------------------

    def submit(self, name: str, timeout: float = 2.0, **args: Any) -> Any:
        """Queue a command for the render loop and wait for it to be applied."""
        cmd = _Command(name, args)
        self._commands.put(cmd)
        if not cmd.done.wait(timeout):
            raise TimeoutError("generator did not respond; is the render loop running?")
        if cmd.error:
            raise cmd.error
        return cmd.result

    def apply(self, name: str, **args: Any) -> Any:
        """Apply a command immediately. Render loop thread only (keyboard, --run)."""
        cmd = _Command(name, args)
        self._run(cmd)
        if cmd.error:
            raise cmd.error
        return cmd.result

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._status))

    def preview(self) -> dict[str, Any]:
        """The current frame as draw operations, for the web UI to draw on a canvas."""
        with self._lock:
            cfg, lay, count, show_slate = self._preview_src
        ops = compose(cfg, lay, count, show_slate)
        return {"width": lay.width, "height": lay.height, "bg": cfg.clock.bg,
                "ops": [_op_json(op) for op in ops]}

    # ---- called from the render loop --------------------------------------------------

    def frame(self) -> tuple[list[Op], tuple[int, int, int]]:
        """Apply queued commands and compose the next frame."""
        self._drain()
        if self.state == PRE_ROLL and self._mono() >= self._pre_roll_until:
            self.state = RUNNING
            self.clock.start()
            self._log("counting", count=self.clock.count)
        if self.cfg.slate.mode != "off" and self.state != RUNNING:
            self._refresh_slate()
        self._shown = self.clock.count
        ops = compose(self.cfg, self.layout, self._shown, self._slate_visible())
        return ops, hex_colour(self.cfg.clock.bg)

    def presented(self, t_ns: int) -> None:
        """Call right after the vsynced present returns."""
        self.clock.frame_displayed()
        for ev in self.monitor.observe(self._frame, t_ns):
            self._record(ev)
        if self.monitor.mismatched != self._mismatch:
            self._mismatch = self.monitor.mismatched
            self._log_rate()
        self._frame += 1
        self._publish()

    def close(self) -> None:
        self._drain()
        if self.state != IDLE:
            self._stop()
        for ev in self.monitor.flush():
            self._record(ev)

    # ---- commands ---------------------------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            self._run(cmd)

    def _run(self, cmd: _Command) -> None:
        handler = getattr(self, f"_cmd_{cmd.name}", None)
        try:
            if handler is None:
                raise CommandError(f"unknown command {cmd.name!r}")
            cmd.result = handler(**cmd.args)
        except CommandError as e:
            cmd.error = e
        except Exception as e:  # never let a bad request stop the render loop
            cmd.error = CommandError(str(e))
        self._publish()
        cmd.done.set()

    def _cmd_start(self) -> None:
        if self.state != IDLE:
            raise CommandError("a run is already in progress")
        self.run_id = new_run_id(self._now())
        self.run_log = RunLog(self.log_dir, self.run_id)
        self._refresh_slate(force=True)
        self.run_log.write("run_start", run_id=self.run_id, run_number=self.run_number,
                           config=self.cfg.to_dict(), start_count=self.clock.count,
                           refresh_hz=self.cfg.refresh_hz, tc_rate=self.cfg.tc_rate,
                           display={"width": self.width, "height": self.height},
                           # Logged even when not shown, so the analysis can name the run.
                           slate=list(slate_lines(self.cfg, self.run_number, self._now())),
                           slate_shown=self.cfg.slate.mode)
        if self._mismatch:
            self._log_rate()
        pre = self.cfg.slate.pre_roll_s if self.cfg.slate.mode == "pre_roll" else 0
        if pre > 0:
            self.state = PRE_ROLL
            self._pre_roll_until = self._mono() + pre
        else:
            self.state = RUNNING
            self.clock.start()

    def _cmd_stop(self) -> None:
        if self.state == IDLE:
            raise CommandError("no run in progress")
        self._stop()

    def _cmd_reset(self) -> None:
        self.clock.reset()
        self._log("reset", count=self.clock.count)

    def _cmd_set_start_value(self, value: str) -> None:
        parse_timecode(value, self.cfg.refresh_hz, self.cfg.tc_rate)
        data = self.cfg.to_dict() | {"start_mode": "value", "start_value": value}
        self._cmd_set_config(config=data)
        if self.state == IDLE:
            self.clock.reset()

    def _cmd_set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        cfg = self._fit(GeneratorConfig.from_dict(config))
        if cfg.rate_set != self.cfg.rate_set and self.state != IDLE:
            raise CommandError("stop the run before changing the rate set")
        rate_changed = (cfg.rate_set, cfg.refresh_hz) != (self.cfg.rate_set, self.cfg.refresh_hz)
        start_changed = (cfg.start_mode, cfg.start_value) != (self.cfg.start_mode,
                                                              self.cfg.start_value)
        self.cfg = cfg
        self.clock.cfg = cfg
        if rate_changed:
            self.monitor = FrameMonitor(cfg.refresh_hz)
        if self.state == IDLE and (rate_changed or start_changed):
            self.clock.reset()
        self._config_rev += 1
        self._log("config", config=cfg.to_dict())
        self._refresh_slate(force=True)
        self._relayout()
        return cfg.to_dict()

    def _cmd_ack_warnings(self) -> None:
        self._unacked = 0

    def _cmd_set_display_mode(self, mode: str) -> dict[str, Any]:
        if self.display is None or not self.display.can_set:
            raise CommandError(self.display.note if self.display
                               else "no display: the display mode cannot be set")
        if self.state != IDLE:
            raise CommandError("stop the run before changing the display mode")
        try:
            want = DisplayMode.parse(mode)
        except ValueError as e:
            raise CommandError(str(e)) from None
        self._set_mode(want)
        self._save_state()
        return self.display_info()

    # ---- display ----------------------------------------------------------------------

    def display_info(self) -> dict[str, Any]:
        """The display's usable modes, for the web UI. Any thread."""
        with self._lock:
            return json.loads(json.dumps(self._display_info))

    def display_warning(self) -> str | None:
        """Why the display's refresh does not suit the rate set, if it does not."""
        if self._mode is None:
            return None
        rs = get_rate_set(self.cfg.rate_set)
        if self._mode.hz in rs.refresh_rates:
            return None
        rates = " or ".join(map(str, rs.refresh_rates))
        return (f"The display runs at {self._mode.hz} Hz; the {rs.label} rate set needs "
                f"{rates} Hz. Choose a display mode or the other rate set.")

    def _fit(self, cfg: GeneratorConfig) -> GeneratorConfig:
        """Count at the display's refresh when the rate set allows it."""
        rs = get_rate_set(cfg.rate_set)
        hz = rs.refresh_for(self._mode.hz if self._mode else None)
        fitted = replace(cfg, display_hz=hz, tc_rate=rs.fit_tc_rate(cfg.tc_rate, hz))
        fitted.validate()
        return fitted

    def _set_mode(self, want: DisplayMode) -> None:
        if want not in self._modes:
            raise CommandError(f"{want} is not a mode this display offers")
        try:
            # TODO(verify): on the Pi 5 under KMS, that the output and window follow.
            self._mode = self.display.set_mode(want)
        except Exception as e:
            raise CommandError(f"could not set {want}: {e}") from None
        self.width, self.height = self._mode.width, self._mode.height
        self.cfg = self._fit(self.cfg)
        self.clock.cfg = self.cfg
        self.clock.reset()  # a tick is a different length now
        self.monitor = FrameMonitor(self.cfg.refresh_hz)
        self._mismatch = False
        self._config_rev += 1
        self._refresh_slate(force=True)
        self._relayout()
        self._publish_display()

    def _restore_mode(self, request: str | None) -> None:
        if self.display is None or not self.display.can_set:
            if request:
                why = self.display.note if self.display else "no display"
                self.notes.append(f"--mode ignored: {why}")
            return
        text = request or self._saved_mode
        if not text:
            return
        try:
            want = DisplayMode.parse(text)
            if want != self._mode:
                self._set_mode(want)
        except (ValueError, CommandError) as e:
            source = "requested" if request else "saved"
            self.notes.append(f"{source} display mode {text} not used: {e}")
            return
        if request:
            self._save_state()

    def _publish_display(self) -> None:
        info = {
            "current": str(self._mode) if self._mode else None,
            "can_set": bool(self.display and self.display.can_set),
            "note": self.display.note if self.display else "No display (headless).",
            "modes": [{"mode": str(m), "width": m.width, "height": m.height, "hz": m.hz,
                       "rate_sets": m.rate_sets()} for m in self._modes],
        }
        with self._lock:
            self._display_info = info

    def _log_rate(self) -> None:
        mon = self.monitor
        if self._mismatch:
            self._log("rate_mismatch", measured_hz=mon.measured_hz,
                      expected_hz=self.cfg.refresh_hz)
        else:
            self._log("rate_ok", measured_hz=mon.measured_hz)

    # ---- internals --------------------------------------------------------------------

    def _stop(self) -> None:
        self.clock.stop()
        if self.run_log:
            self.run_log.write("run_stop", count=self.clock.count, frames=self.monitor.frames,
                               repeated_refreshes=self.monitor.repeats,
                               early_presents=self.monitor.early,
                               measured_refresh_hz=self.monitor.measured_hz)
            self.run_log.close()
        self.run_log = None
        self.state = IDLE
        self.run_number += 1
        self._save_state()
        self._refresh_slate(force=True)

    def _slate_visible(self) -> bool:
        mode = self.cfg.slate.mode
        return mode == "always" or (mode == "pre_roll" and self.state != RUNNING)

    def _refresh_slate(self, force: bool = False) -> None:
        if self.cfg.slate.mode == "off":
            lines: tuple[str, ...] = ()
        else:
            lines = slate_lines(self.cfg, self.run_number, self._now())
        if force or lines != self._slate:
            self._slate = lines
            self._relayout()

    def _relayout(self) -> None:
        self.layout = layout_mod.compute(self.cfg, self.width, self.height, self._slate)
        self._warnings = list(self.layout.warnings)

    def _record(self, ev: FrameEvent) -> None:
        tc = to_timecode(self._shown, self.cfg.refresh_hz, self.cfg.tc_rate)
        entry = {"kind": ev.kind, "frame": ev.frame, "refreshes": ev.refreshes,
                 "gap_ms": round(ev.gap_ms, 3), "count": self._shown,
                 "timecode": tc.hmsf() + tc.ms(), "t": self._now().isoformat()}
        self._events.append(entry)
        self._unacked += max(1, ev.refreshes)
        self._log("frame", **{k: v for k, v in entry.items() if k != "t"})

    def _log(self, event: str, **fields: Any) -> None:
        if self.run_log:
            self.run_log.write(event, **fields)

    def _publish(self) -> None:
        tc = to_timecode(self._shown, self.cfg.refresh_hz, self.cfg.tc_rate)
        mon = self.monitor
        status = {
            "state": self.state,
            "count": self._shown,
            "timecode": tc.hmsf() + tc.ms(),
            "rate_set": self.cfg.rate_set,
            "tc_rate": self.cfg.tc_rate,
            "refresh_hz": self.cfg.refresh_hz,
            "measured_hz": round(mon.measured_hz, 3) if mon.measured_hz else None,
            "rate_mismatch": mon.mismatched,
            "display_warning": self.display_warning(),
            "tc_rates": list(get_rate_set(self.cfg.rate_set).timecode_rates_at(
                self.cfg.refresh_hz)),
            "frames": mon.frames,
            "repeated_refreshes": mon.repeats,
            "early_presents": mon.early,
            "unacked_frame_warnings": self._unacked,
            "recent_events": list(self._events),
            "layout_warnings": self._warnings,
            "run_number": self.run_number,
            "run_id": self.run_id,
            "pre_roll_remaining": (max(0.0, round(self._pre_roll_until - self._mono(), 1))
                                   if self.state == PRE_ROLL else None),
            "display": {"width": self.width, "height": self.height,
                        "mode": str(self._mode) if self._mode else None},
            "config_rev": self._config_rev,
            "simulated_timing": self.simulated,
        }
        with self._lock:
            self._status = status
            self._preview_src = (self.cfg, self.layout, self._shown, self._slate_visible())

    def _state_path(self) -> Path:
        return self.log_dir / "state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save_state(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {"next_run_number": self.run_number}
        if self.display and self.display.can_set and self._mode:
            state["display_mode"] = str(self._mode)
        self._state_path().write_text(json.dumps(state))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _op_json(op: Op) -> dict[str, Any]:
    r = op.rect
    colour = "#" + "".join(f"{c:02x}" for c in op.colour)
    base = {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "colour": colour}
    if isinstance(op, Fill):
        return {"type": "fill", **base}
    return {"type": "text", **base, "px": op.px, "text": op.text}
