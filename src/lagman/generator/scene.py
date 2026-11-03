"""Frame composition: turns a layout and a count into draw operations. No graphics library."""

from __future__ import annotations

from dataclasses import dataclass

from ..core import strip
from ..core.config import GeneratorConfig
from ..core.layout import Layout, Rect, SlateScreen
from ..core.timecode import to_timecode

Colour = tuple[int, int, int]
WHITE: Colour = (255, 255, 255)
BLACK: Colour = (0, 0, 0)


@dataclass(frozen=True)
class Fill:
    rect: Rect
    colour: Colour


@dataclass(frozen=True)
class Text:
    rect: Rect
    px: int
    text: str
    colour: Colour


Op = Fill | Text


def hex_colour(value: str) -> Colour:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def compose(cfg: GeneratorConfig, layout: Layout, count: int, show_slate: bool = True,
            patch_white: bool | None = None) -> list[Op]:
    """Draw operations for one frame, after clearing to the background colour.

    ``patch_white`` overrides the configured sync patch state, for flip sequences.
    """
    fg = hex_colour(cfg.clock.fg)
    tc = to_timecode(count, cfg.refresh_hz, cfg.tc_rate)
    ops: list[Op] = [Text(layout.clock_main, layout.clock_px, tc.hmsf(), fg)]
    if layout.clock_ms is not None:
        ops.append(Text(layout.clock_ms, layout.ms_px, tc.ms(), fg))
    # The strip is always pure white on black, whatever the clock colours.
    if layout.strip is not None:
        for cell, bit in zip(layout.strip_cells, strip.encode(count), strict=True):
            ops.append(Fill(cell, WHITE if bit else BLACK))
    if show_slate and layout.slate is not None:
        ops += _slate_ops(layout.slate, layout.slate_px, layout.slate_lines)
    if layout.patch is not None:
        white = cfg.patch.mode == "white" if patch_white is None else patch_white
        ops.append(Fill(layout.patch, WHITE if white else BLACK))
    return ops


def compose_slate_screen(cfg: GeneratorConfig, layout: SlateScreen, count: int,
                         time_of_day: str) -> list[Op]:
    """Draw operations for the slate screen. ``time_of_day`` is HH:MM:SS from the system
    clock, for people to read; the time code comes from the count, like the test clock."""
    fg = hex_colour(cfg.clock.fg)
    ops: list[Op] = [Text(layout.clock, layout.clock_px, time_of_day, fg)]
    ops += [Text(lb.rect, lb.px, lb.text, fg) for lb in layout.labels[:1]]
    if layout.timecode is not None:
        tc = to_timecode(count, cfg.refresh_hz, cfg.tc_rate)
        ops.append(Text(layout.timecode, layout.timecode_px, tc.hmsf(), fg))
        ops += [Text(lb.rect, lb.px, lb.text, fg) for lb in layout.labels[1:]]
    if layout.slate is not None:
        ops += _slate_ops(layout.slate, layout.slate_px, layout.slate_lines)
    return ops


def _slate_ops(rect: Rect, px: int, lines: tuple[str, ...]) -> list[Op]:
    """Plain white text on a black box, so it stays readable to the camera and to OCR."""
    ops: list[Op] = [Fill(rect, BLACK)]
    pad = (rect.h - round(len(lines) * 1.3 * px)) // 2
    line_h = round(1.3 * px)
    for i, line in enumerate(lines):
        r = Rect(rect.x + pad, rect.y + pad + i * line_h, rect.w - 2 * pad, line_h)
        ops.append(Text(r, px, line, WHITE))
    return ops
