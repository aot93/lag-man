"""Screen layout for the clock, code strip, slate and sync patch, and for the slate screen.

Pure geometry in integer pixels. Text is measured with fixed monospace metrics so layout
does not depend on a font library; the renderer uses a font with the same metrics.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import strip
from .config import GeneratorConfig

# IBM Plex Mono: advance 600/1000 em, ascent + descent 1300/1000 em.
CHAR_W = 0.6
LINE_H = 1.3

MAIN_CHARS = len("00:00:00:00")
MS_CHARS = len(".000")
MS_SCALE = 0.55
STRIP_GAP = 0.25  # gap above the strip, in main-digit ems
STRIP_MIN_H = 0.4  # minimum strip height, in main-digit ems
MARGIN = 0.02  # screen margin, as a fraction of screen height
SLATE_MIN_PX = 12
SLATE_PAD = 0.4  # padding around slate text, in slate ems


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def intersects(self, other: Rect) -> bool:
        return (self.x < other.right and other.x < self.right
                and self.y < other.bottom and other.y < self.bottom)

    def inflate(self, d: int) -> Rect:
        return Rect(self.x - d, self.y - d, self.w + 2 * d, self.h + 2 * d)

    def inside(self, w: int, h: int) -> bool:
        return self.x >= 0 and self.y >= 0 and self.right <= w and self.bottom <= h


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    clock_px: int
    clock_main: Rect
    ms_px: int
    clock_ms: Rect | None
    strip: Rect | None
    strip_cells: tuple[Rect, ...]
    clock_block: Rect
    slate_px: int = 0
    slate: Rect | None = None
    slate_lines: tuple[str, ...] = ()
    patch: Rect | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Label:
    rect: Rect
    px: int
    text: str


@dataclass(frozen=True)
class SlateScreen:
    """The slate on its own: a time-of-day clock, time code under it, then the slate."""

    width: int
    height: int
    clock_px: int
    clock: Rect
    timecode_px: int
    timecode: Rect | None
    labels: tuple[Label, ...]
    slate_px: int = 0
    slate: Rect | None = None
    slate_lines: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


TOD_CHARS = len("00:00:00")
LABEL_SCALE = 0.3  # caption height, in the time code's ems
STACK_GAP = 0.3  # gap between the blocks, in time-of-day ems


def text_size(chars: int, px: int, lines: int = 1) -> tuple[int, int]:
    return round(chars * CHAR_W * px), round(lines * LINE_H * px)


def compute(cfg: GeneratorConfig, width: int, height: int,
            slate_lines: tuple[str, ...] = ()) -> Layout:
    c = cfg.clock
    margin = round(MARGIN * height)
    px = max(8, round(c.size * height))
    main_w, main_h = text_size(MAIN_CHARS, px)
    # Keep the block on screen: shrink the digits if the screen is too narrow.
    if main_w > width - 2 * margin:
        px = int((width - 2 * margin) / (MAIN_CHARS * CHAR_W))
        main_w, main_h = text_size(MAIN_CHARS, px)

    ms_px = round(px * MS_SCALE)
    ms_w, ms_h = text_size(MS_CHARS, ms_px) if c.show_ms else (0, 0)

    cell = main_w // strip.CELLS if c.strip else 0
    strip_w = cell * strip.CELLS
    strip_h = max(cell, round(STRIP_MIN_H * px)) if c.strip else 0
    strip_gap = round(STRIP_GAP * px) if c.strip else 0

    block_w = main_w
    block_h = main_h + ms_h + strip_gap + strip_h
    bx = _clamp(round(c.cx * width - block_w / 2), margin, width - margin - block_w)
    by = _clamp(round(c.cy * height - block_h / 2), margin, height - margin - block_h)

    clock_main = Rect(bx, by, main_w, main_h)
    clock_ms = Rect(bx + main_w - ms_w, by + main_h, ms_w, ms_h) if c.show_ms else None
    strip_rect = None
    cells: tuple[Rect, ...] = ()
    if c.strip:
        sx = bx + (main_w - strip_w) // 2
        sy = by + main_h + ms_h + strip_gap
        strip_rect = Rect(sx, sy, strip_w, strip_h)
        cells = tuple(Rect(sx + i * cell, sy, cell, strip_h) for i in range(strip.CELLS))
    block = Rect(bx, by, block_w, block_h)

    warnings: list[str] = []
    slate_px, slate_rect = 0, None
    if slate_lines:
        slate_px, slate_rect = _place_slate(cfg, width, height, margin, block, slate_lines)
        if slate_rect is None:
            warnings.append("slate does not fit beside the clock at this position and size; "
                            "hidden")

    p = cfg.patch
    patch = None
    if p.mode != "off":
        patch = Rect(round(p.x * width), round(p.y * height),
                     max(1, round(p.w * width)), max(1, round(p.h * height)))

    return Layout(width, height, px, clock_main, ms_px, clock_ms, strip_rect, cells, block,
                  slate_px, slate_rect, slate_lines if slate_rect else (), patch,
                  tuple(warnings))


def _place_slate(cfg: GeneratorConfig, width: int, height: int, margin: int, block: Rect,
                 lines: tuple[str, ...]) -> tuple[int, Rect | None]:
    """Largest slate text size, up to the configured one, that clears the clock block."""
    pos = cfg.slate.position
    keep_out = block.inflate(margin)
    chars = max(len(line) for line in lines)
    px = max(SLATE_MIN_PX, round(cfg.slate.size * height))
    while px >= SLATE_MIN_PX:
        tw, th = text_size(chars, px, len(lines))
        pad = round(SLATE_PAD * px)
        w, h = tw + 2 * pad, th + 2 * pad
        if pos.startswith("top"):
            y = margin
        else:
            y = height - margin - h
        if pos.endswith("left"):
            x = margin
        elif pos.endswith("right"):
            x = width - margin - w
        else:
            x = (width - w) // 2
        rect = Rect(x, y, w, h)
        if rect.inside(width, height) and not rect.intersects(keep_out):
            return px, rect
        if px == SLATE_MIN_PX:
            break
        px = max(SLATE_MIN_PX, int(px * 0.9))
    return 0, None


def compute_slate_screen(cfg: GeneratorConfig, width: int, height: int,
                         slate_lines: tuple[str, ...] = ()) -> SlateScreen:
    """Centre the blocks in a column, shrinking them together until the column fits."""
    s = cfg.slate_screen
    margin = round(MARGIN * height)
    avail_w, avail_h = width - 2 * margin, height - 2 * margin
    labels = ["TIME OF DAY"] + ([f"TIME CODE {cfg.tc_rate} FPS"] if s.timecode else [])
    lines = slate_lines
    warnings: list[str] = []
    scale = 1.0
    while True:
        # Each block is first held to the screen's width, then all shrink for its height.
        tod_px = max(8, min(round(s.clock_size * height * scale), _fit_px(TOD_CHARS, avail_w)))
        tc_px = (max(8, min(round(s.tc_size * height * scale), _fit_px(MAIN_CHARS, avail_w)))
                 if s.timecode else 0)
        slate_px = 0
        if lines:
            chars = max(len(line) for line in lines)
            slate_px = max(SLATE_MIN_PX, min(round(cfg.slate.size * height * scale),
                                             _fit_px(chars + 2 * SLATE_PAD / CHAR_W, avail_w)))
        label_px = max(SLATE_MIN_PX, round(LABEL_SCALE * (tc_px or tod_px * 0.5)))
        blocks = [(TOD_CHARS, tod_px, 1), (len(labels[0]), label_px, 1)]
        if s.timecode:
            blocks += [(MAIN_CHARS, tc_px, 1), (len(labels[1]), label_px, 1)]
        if lines:
            blocks.append((max(len(line) for line in lines), slate_px, len(lines)))
        sizes = [text_size(chars, px, n) for chars, px, n in blocks]
        if lines:
            pad = round(SLATE_PAD * slate_px)
            sizes[-1] = (sizes[-1][0] + 2 * pad, sizes[-1][1] + 2 * pad)
        # Each caption sits right under its digits; the gap goes before the next block.
        gap = round(STACK_GAP * tod_px)
        gaps = [gap if i and i % 2 == 0 else 0 for i in range(len(sizes))]
        total_h = sum(h for _, h in sizes) + sum(gaps)
        if total_h <= avail_h and max(w for w, _ in sizes) <= avail_w:
            break
        if scale < 0.1 or (lines and slate_px == SLATE_MIN_PX and scale < 0.5):
            if lines:
                # The slate cannot fit at a readable size: show the clocks alone.
                warnings.append("slate does not fit under the clock at this size; hidden")
                lines, scale = (), 1.0
                continue
            break
        scale *= 0.9

    rects = []
    y = margin + max(0, (avail_h - total_h) // 2)
    for i, (w, h) in enumerate(sizes):
        y += gaps[i]
        rects.append(Rect((width - w) // 2, y, w, h))
        y += h
    out_labels = [Label(rects[1], label_px, labels[0])]
    if s.timecode:
        out_labels.append(Label(rects[3], label_px, labels[1]))
    return SlateScreen(width, height, tod_px, rects[0], tc_px,
                       rects[2] if s.timecode else None, tuple(out_labels),
                       slate_px if lines else 0, rects[-1] if lines else None, lines,
                       tuple(warnings))


def _fit_px(chars: float, avail_w: int) -> int:
    return int(avail_w / (chars * CHAR_W))


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi)) if hi >= lo else lo
