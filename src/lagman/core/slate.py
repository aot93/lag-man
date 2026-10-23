"""Slate text: identifies the camera, frame rate and run on every recording."""

from __future__ import annotations

from datetime import datetime

from .config import GeneratorConfig

LABELS = {
    "test_name": "TEST",
    "camera_name": "CAMERA",
    "camera_fps": "FPS",
    "run_number": "RUN",
    "date_time": "DATE",
    "operator": "OPERATOR",
    "location": "LOCATION",
    "notes": "NOTES",
}
MAX_VALUE_CHARS = 60


def slate_lines(cfg: GeneratorConfig, run_number: int, now: datetime) -> tuple[str, ...]:
    """Enabled slate fields as plain ``LABEL  value`` lines, plus the generator settings.

    Run number and date default to the live values when left blank (SPEC.md, "Slate").
    """
    auto = {"run_number": str(run_number), "date_time": now.strftime("%Y-%m-%d %H:%M:%S")}
    width = max(len(label) for label in LABELS.values())
    lines = []
    for name, label in LABELS.items():
        f = cfg.slate.fields.get(name)
        if f is None or not f.enabled:
            continue
        value = _clean(f.value) or auto.get(name, "")
        if value:
            lines.append(f"{label:<{width}}  {value}")
    lines.append(f"{'CLOCK':<{width}}  {cfg.tc_rate} fps TC / {cfg.refresh_hz} Hz")
    return tuple(lines)


def _clean(value: str) -> str:
    """Printable single-line text only, so slate text stays OCR-friendly."""
    text = "".join(ch if ch.isprintable() else " " for ch in value).strip()
    return text[:MAX_VALUE_CHARS]
