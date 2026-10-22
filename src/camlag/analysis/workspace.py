"""The analysis app's workspace: recordings, named region setups, run logs and results.

    <workspace>/
      workspace.json        recordings and preferences
      setups/<name>.json    regions, reused by every recording from that camera position
      runlogs/<id>.jsonl    run logs fetched from the generator
      results/<recording>/  results.csv, summary.json, report.html

Videos are referenced where they are, never copied: control camera files are large.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.rates import RATE_SETS
from .setup import RunInfo, Setup

VIDEO_SUFFIXES = {".mp4", ".mov", ".mts", ".m2ts", ".mxf", ".avi", ".mkv"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")


def check_name(name: str) -> str:
    name = name.strip()
    if not _NAME_RE.match(name) or ".." in name:
        raise ValueError("names use letters, digits, spaces, '.', '_' or '-' (up to 64)")
    return name


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-.")
    return s[:48] or "recording"


@dataclass
class Recording:
    id: str
    video: str
    run_log: str | None = None
    rate_set: str | None = None  # used when there is no run log
    capture_fps: int | None = None  # override; default from the rate set
    setup: str | None = None
    added: str = ""
    analysed: str | None = None
    error: str | None = None

    @property
    def video_path(self) -> Path:
        return Path(self.video)

    def run_info(self) -> RunInfo:
        if self.run_log:
            return RunInfo.from_run_log(Path(self.run_log), self.capture_fps)
        return RunInfo.for_rate_set(self.rate_set or "50", self.capture_fps)


@dataclass
class State:
    recordings: list[Recording] = field(default_factory=list)
    last_dir: str | None = None
    generator_url: str | None = None


class Workspace:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in ("setups", "runlogs", "results"):
            (self.root / sub).mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self.state = self._load()

    # ---- persistence ----

    @property
    def _file(self) -> Path:
        return self.root / "workspace.json"

    def _load(self) -> State:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()
        known = {f.name for f in fields(Recording)}
        recs = [Recording(**{k: v for k, v in r.items() if k in known})
                for r in data.get("recordings", []) if isinstance(r, dict) and "id" in r]
        return State(recs, data.get("last_dir"), data.get("generator_url"))

    def save(self) -> None:
        with self._lock:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.state), indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._file)

    # ---- recordings ----

    def get(self, rec_id: str) -> Recording:
        for r in self.state.recordings:
            if r.id == rec_id:
                return r
        raise KeyError(rec_id)

    def add(self, video: Path, run_log: Path | None = None, rate_set: str | None = None,
            setup: str | None = None) -> Recording:
        video = video.expanduser().resolve()
        if not video.is_file():
            raise ValueError(f"no such video: {video}")
        if run_log is not None and not run_log.is_file():
            raise ValueError(f"no such run log: {run_log}")
        if rate_set is not None and rate_set not in RATE_SETS:
            raise ValueError(f"unknown rate set {rate_set!r}")
        with self._lock:
            taken = {r.id for r in self.state.recordings}
            base = slug(video.stem)
            rec_id, n = base, 2
            while rec_id in taken:
                rec_id, n = f"{base}-{n}", n + 1
            rec = Recording(rec_id, str(video), str(run_log.resolve()) if run_log else None,
                            rate_set, None, setup,
                            datetime.now().astimezone().isoformat(timespec="seconds"))
            self.state.recordings.append(rec)
            self.state.last_dir = str(video.parent)
            self.save()
            return rec

    def update(self, rec_id: str, changes: dict[str, Any]) -> Recording:
        allowed = {"run_log", "rate_set", "capture_fps", "setup"}
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"cannot change {sorted(bad)}")
        with self._lock:
            rec = self.get(rec_id)
            if "run_log" in changes and changes["run_log"]:
                path = Path(changes["run_log"]).expanduser()
                if not path.is_file():
                    raise ValueError(f"no such run log: {path}")
                RunInfo.from_run_log(path)  # reject files that are not run logs
                changes["run_log"] = str(path.resolve())
            if changes.get("rate_set") not in (None, *RATE_SETS):
                raise ValueError("unknown rate set")
            if changes.get("capture_fps") is not None:
                fps = changes["capture_fps"]
                if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 1000:
                    raise ValueError("capture fps must be a whole number")
            if changes.get("setup"):
                self.load_setup(changes["setup"])
            for k, v in changes.items():
                setattr(rec, k, v or None)
            self.save()
            return rec

    def remove(self, rec_id: str) -> None:
        with self._lock:
            rec = self.get(rec_id)
            self.state.recordings.remove(rec)
            out = self.results_dir(rec.id)
            if out.is_dir():
                for p in out.iterdir():
                    p.unlink()
                out.rmdir()
            self.save()

    def results_dir(self, rec_id: str) -> Path:
        return self.root / "results" / slug(rec_id)

    def summary(self, rec_id: str) -> dict[str, Any] | None:
        path = self.results_dir(rec_id) / "summary.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- setups ----

    def setup_names(self) -> list[str]:
        return sorted((p.stem for p in (self.root / "setups").glob("*.json")), key=str.lower)

    def load_setup(self, name: str) -> Setup:
        path = self.root / "setups" / f"{check_name(name)}.json"
        if not path.is_file():
            raise ValueError(f"no setup named {name!r}")
        return Setup.load(path)

    def save_setup(self, name: str, setup: Setup) -> None:
        setup.save(self.root / "setups" / f"{check_name(name)}.json")

    # ---- run logs ----

    def runlog_dir(self) -> Path:
        return self.root / "runlogs"
