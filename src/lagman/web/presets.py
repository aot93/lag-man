"""Named presets, one JSON file each, in the same format as ``GeneratorConfig.to_dict``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.config import GeneratorConfig

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")


def check_name(name: str) -> str:
    name = name.strip()
    if not _NAME_RE.match(name) or ".." in name:
        raise ValueError("preset names use letters, digits, spaces, '.', '_' or '-' "
                         "(up to 64 characters)")
    return name


class PresetStore:
    def __init__(self, directory: Path):
        self.dir = directory

    def _path(self, name: str) -> Path:
        return self.dir / f"{check_name(name)}.json"

    def list(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted((p.stem for p in self.dir.glob("*.json")), key=str.lower)

    def load(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.is_file():
            raise KeyError(name)
        # Validate on the way out too, so a hand-edited file cannot reach the generator.
        return GeneratorConfig.from_dict(json.loads(path.read_text(encoding="utf-8"))).to_dict()

    def save(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        data = GeneratorConfig.from_dict(config).to_dict()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return data

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.is_file():
            raise KeyError(name)
        path.unlink()
