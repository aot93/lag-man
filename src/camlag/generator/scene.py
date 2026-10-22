"""Frame composition: turns a layout and a count into draw operations. No graphics library."""

from __future__ import annotations

from dataclasses import dataclass

from ..core import strip
from ..core.config import GeneratorConfig
from ..core.layout import Layout, Rect
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
        ops.append(Fill(layout.slate, BLACK))
        pad = (layout.slate.h - round(len(layout.slate_lines) * 1.3 * layout.slate_px)) // 2
        line_h = round(1.3 * layout.slate_px)
        for i, line in enumerate(layout.slate_lines):
            r = Rect(layout.slate.x + pad, layout.slate.y + pad + i * line_h,
                     layout.slate.w - 2 * pad, line_h)
            ops.append(Text(r, layout.slate_px, line, WHITE))
    if layout.patch is not None:
        white = cfg.patch.mode == "white" if patch_white is None else patch_white
        ops.append(Fill(layout.patch, WHITE if white else BLACK))
    return ops
