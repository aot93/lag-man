"""Generator state machine, shared by the render loop and the web server.

The render loop owns all state. Other threads call ``submit`` to queue a command, which the
render loop applies between frames, so a frame never sees a half-applied change. ``status``
and ``preview`` read snapshots that the render loop publishes once per frame.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core import layout as layout_mod
from ..core.clock import Clock
from ..core.config import GeneratorConfig
from ..core.framemon import FrameEvent, FrameMonitor
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
                 monotonic: Callable[[], float] = time.monotonic, simulated: bool = False):
        self.width, self.height = width, height
        self.simulated = simulated
        self.log_dir = log_dir
        self._now, self._mono = now, monotonic
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._lock = threading.Lock()
        self.cfg = cfg
        self.clock = Clock(cfg, now=now)
        self.monitor = FrameMonitor(cfg.refresh_hz)
        self.state = IDLE
        self.run_number = self._load_run_number()
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
        cfg = GeneratorConfig.from_dict(config)
        if cfg.rate_set != self.cfg.rate_set and self.state != IDLE:
            raise CommandError("stop the run before changing the rate set")
        rate_changed = cfg.rate_set != self.cfg.rate_set
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
        self._save_run_number()
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
            "rate_mismatch": mon.rate_mismatch(),
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
            "display": {"width": self.width, "height": self.height},
            "config_rev": self._config_rev,
            "simulated_timing": self.simulated,
        }
        with self._lock:
            self._status = status
            self._preview_src = (self.cfg, self.layout, self._shown, self._slate_visible())

    def _state_path(self) -> Path:
        return self.log_dir / "state.json"

    def _load_run_number(self) -> int:
        try:
            return int(json.loads(self._state_path().read_text())["next_run_number"])
        except (OSError, ValueError, KeyError, TypeError):
            return 1

    def _save_run_number(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(json.dumps({"next_run_number": self.run_number}))


def _op_json(op: Op) -> dict[str, Any]:
    r = op.rect
    colour = "#" + "".join(f"{c:02x}" for c in op.colour)
    base = {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "colour": colour}
    if isinstance(op, Fill):
        return {"type": "fill", **base}
    return {"type": "text", **base, "px": op.px, "text": op.text}
