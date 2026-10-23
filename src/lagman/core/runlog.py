"""Run log: one JSON Lines file per run, read by the analysis and photodiode extensions.

Every line is an object with ``event``, ``t`` (ISO 8601 wall time) and event fields.
Events: ``run_start`` (settings, slate, start count), ``frame`` (dropped or repeated frame),
``run_stop`` (final count and frame statistics).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import IO, Any


class RunLog:
    def __init__(self, directory: Path, run_id: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{run_id}.jsonl"
        self._fh: IO[str] | None = self.path.open("a", encoding="utf-8")

    def write(self, event: str, **fields: Any) -> None:
        if self._fh is None:
            raise ValueError("run log is closed")
        record = {"event": event, "t": datetime.now().astimezone().isoformat(), **fields}
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def read_run_log(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
