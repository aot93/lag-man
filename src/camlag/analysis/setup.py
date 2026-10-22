"""Per-setup analysis inputs: the two strip regions and the run they belong to."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.rates import get_rate_set
from .roi import Quad

SETUP_VERSION = 1


@dataclass(frozen=True)
class Setup:
    source: Quad
    wall: Quad
    video_size: tuple[int, int] | None = None
    marked_on_frame: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"version": SETUP_VERSION, "source": self.source.to_json(),
                "wall": self.wall.to_json(),
                "video_size": list(self.video_size) if self.video_size else None,
                "marked_on_frame": self.marked_on_frame}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Setup:
        try:
            size = data.get("video_size")
            return cls(Quad.from_points(data["source"]), Quad.from_points(data["wall"]),
                       tuple(size) if size else None, data.get("marked_on_frame"))
        except (KeyError, TypeError) as e:
            raise ValueError(f"setup file is missing or has a malformed field: {e}") from None

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Setup:
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class RunInfo:
    """What the analysis needs from the generator's run log, or from the command line."""

    rate_set: str
    refresh_hz: int
    capture_fps: int
    tc_rate: int | None = None
    run_id: str | None = None
    run_number: int | None = None
    slate: tuple[str, ...] = ()
    start_count: int | None = None
    stop_count: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tick_ms(self) -> float:
        return 1000.0 / self.refresh_hz

    @property
    def frame_ms(self) -> float:
        """Real time between control camera frames: the capture rate, not the file rate."""
        return 1000.0 / self.capture_fps

    def slate_fields(self) -> dict[str, str]:
        out = {}
        for line in self.slate:
            label, _, value = line.partition("  ")
            out[label.strip()] = value.strip()
        return out

    @classmethod
    def for_rate_set(cls, key: str, capture_fps: int | None = None) -> RunInfo:
        rs = get_rate_set(key)
        return cls(rs.key, rs.refresh_hz, capture_fps or rs.control_camera_fps)

    @classmethod
    def from_run_log(cls, path: Path, capture_fps: int | None = None) -> RunInfo:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        start = next((r for r in records if r.get("event") == "run_start"), None)
        if start is None:
            raise ValueError(f"{path.name} has no run_start record; is it a cam-lag run log?")
        stop = next((r for r in reversed(records) if r.get("event") == "run_stop"), None)
        config = start.get("config", {})
        rs = get_rate_set(config.get("rate_set", "50"))
        return cls(
            rate_set=rs.key,
            refresh_hz=int(start.get("refresh_hz", rs.refresh_hz)),
            capture_fps=capture_fps or rs.control_camera_fps,
            tc_rate=start.get("tc_rate", config.get("tc_rate")),
            run_id=start.get("run_id", path.stem),
            run_number=start.get("run_number"),
            slate=tuple(start.get("slate") or ()),
            start_count=start.get("start_count"),
            stop_count=stop.get("count") if stop else None,
            events=[r for r in records if r.get("event") == "frame"],
        )
