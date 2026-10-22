"""Generator configuration. Serialises to plain JSON so it can be saved as a preset."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

from .rates import get_rate_set

START_MODES = ("zero", "value", "time_of_day")
SLATE_POSITIONS = ("top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right")
SLATE_MODES = ("off", "pre_roll", "always")
PATCH_MODES = ("off", "white", "black")
SLATE_FIELDS = (
    "test_name", "camera_name", "camera_fps", "run_number", "date_time",
    "operator", "location", "notes",
)
_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class ClockConfig:
    # Centre of the clock block, as fractions of the screen. Default mid-screen so
    # scan-out skew matches the LED wall (SPEC.md, "Layout").
    cx: float = 0.5
    cy: float = 0.5
    # Height of the main digits as a fraction of screen height.
    size: float = 0.16
    fg: str = "#ffffff"
    bg: str = "#000000"
    show_ms: bool = True
    strip: bool = True


@dataclass
class SlateField:
    enabled: bool = True
    value: str = ""


@dataclass
class SlateConfig:
    mode: str = "off"
    position: str = "top"
    # Text height as a fraction of screen height. Shrunk automatically to avoid the clock.
    size: float = 0.035
    pre_roll_s: float = 5.0
    fields: dict[str, SlateField] = field(
        default_factory=lambda: {name: SlateField() for name in SLATE_FIELDS})


@dataclass
class PatchConfig:
    mode: str = "off"
    # Patch rectangle as fractions of the screen.
    x: float = 0.02
    y: float = 0.4
    w: float = 0.1
    h: float = 0.2


@dataclass
class GeneratorConfig:
    rate_set: str = "50"
    tc_rate: int = 25
    start_mode: str = "zero"
    start_value: str = "00:00:00:00"
    clock: ClockConfig = field(default_factory=ClockConfig)
    slate: SlateConfig = field(default_factory=SlateConfig)
    patch: PatchConfig = field(default_factory=PatchConfig)

    @property
    def refresh_hz(self) -> int:
        return get_rate_set(self.rate_set).refresh_hz

    def validate(self) -> None:
        rs = get_rate_set(self.rate_set)
        if self.tc_rate not in rs.timecode_rates:
            raise ValueError(f"time code rate {self.tc_rate} not in rate set {rs.key} "
                             f"({', '.join(map(str, rs.timecode_rates))})")
        _one_of("start_mode", self.start_mode, START_MODES)
        c = self.clock
        _between("clock.cx", c.cx, 0, 1)
        _between("clock.cy", c.cy, 0, 1)
        _between("clock.size", c.size, 0.02, 0.5)
        for name in ("fg", "bg"):
            if not _COLOUR_RE.match(getattr(c, name)):
                raise ValueError(f"clock.{name} must be #rrggbb")
        s = self.slate
        _one_of("slate.mode", s.mode, SLATE_MODES)
        _one_of("slate.position", s.position, SLATE_POSITIONS)
        _between("slate.size", s.size, 0.01, 0.2)
        _between("slate.pre_roll_s", s.pre_roll_s, 0, 600)
        unknown = set(s.fields) - set(SLATE_FIELDS)
        if unknown:
            raise ValueError(f"unknown slate fields: {sorted(unknown)}")
        p = self.patch
        _one_of("patch.mode", p.mode, PATCH_MODES)
        for name in ("x", "y", "w", "h"):
            _between(f"patch.{name}", getattr(p, name), 0, 1)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeneratorConfig:
        """Build from JSON, filling missing keys with defaults and rejecting unknown ones."""
        base = cls()
        top = _pick(cls, data, nested=("clock", "slate", "patch"))
        cfg = replace(
            base, **top,
            clock=replace(base.clock, **_pick(ClockConfig, data.get("clock", {}))),
            patch=replace(base.patch, **_pick(PatchConfig, data.get("patch", {}))),
            slate=_slate_from_dict(data.get("slate", {})),
        )
        cfg.validate()
        return cfg


def _slate_from_dict(data: dict[str, Any]) -> SlateConfig:
    base = SlateConfig()
    raw_fields = data.get("fields", {})
    if not isinstance(raw_fields, dict):
        raise ValueError("slate.fields must be an object")
    merged = dict(base.fields)
    for name, value in raw_fields.items():
        if name not in SLATE_FIELDS:
            raise ValueError(f"unknown slate field {name!r}")
        merged[name] = replace(SlateField(), **_pick(SlateField, value))
    return replace(base, **_pick(SlateConfig, data, nested=("fields",)), fields=merged)


def _pick(cls: type, data: Any, nested: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__} must be an object")
    names = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(names)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    out = {}
    for key, value in data.items():
        if key in nested:
            continue
        expected = names[key].type
        out[key] = _coerce(f"{cls.__name__}.{key}", value, expected)
    return out


def _coerce(name: str, value: Any, expected: Any) -> Any:
    kind = expected if isinstance(expected, str) else getattr(expected, "__name__", "")
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        value = float(value)
    elif kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    return value


def _one_of(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")


def _between(name: str, value: float, lo: float, hi: float) -> None:
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}; got {value}")
