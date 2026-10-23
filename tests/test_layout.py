import itertools

import pytest

from lagman.core import layout, strip
from lagman.core.config import SLATE_POSITIONS, GeneratorConfig

SLATE = ("TEST      latency A", "CAMERA    Venice 2", "FPS       25", "RUN       12",
         "CLOCK     25 fps TC / 100 Hz")


def cfg_with(**clock):
    cfg = GeneratorConfig()
    for k, v in clock.items():
        setattr(cfg.clock, k, v)
    return cfg


def test_default_is_mid_screen():
    lay = layout.compute(GeneratorConfig(), 1920, 1080)
    b = lay.clock_block
    assert abs((b.x + b.w / 2) - 960) <= 1
    assert abs((b.y + b.h / 2) - 540) <= 1


def test_strip_cells_are_equal_and_adjacent():
    lay = layout.compute(GeneratorConfig(), 1920, 1080)
    cells = lay.strip_cells
    assert len(cells) == strip.CELLS
    assert len({(c.w, c.h) for c in cells}) == 1
    assert all(a.right == b.x for a, b in itertools.pairwise(cells))
    assert cells[0].w >= 10


@pytest.mark.parametrize("size", [(1920, 1080), (2560, 1440), (1280, 720), (800, 1280)])
def test_slate_never_covers_clock_or_strip(size):
    w, h = size
    checked = 0
    for pos, cx, cy, csize, ssize in itertools.product(
            SLATE_POSITIONS, (0.0, 0.25, 0.5, 0.75, 1.0), (0.0, 0.1, 0.5, 0.9, 1.0),
            (0.05, 0.16, 0.3, 0.5), (0.01, 0.035, 0.1, 0.2)):
        cfg = cfg_with(cx=cx, cy=cy, size=csize)
        cfg.slate.position, cfg.slate.size = pos, ssize
        lay = layout.compute(cfg, w, h, SLATE)
        for r in (lay.clock_main, lay.clock_ms, lay.strip, lay.clock_block):
            assert r.inside(w, h)
        if lay.slate is None:
            assert lay.warnings
            continue
        checked += 1
        assert lay.slate.inside(w, h)
        for r in (lay.clock_main, lay.clock_ms, lay.strip):
            assert not lay.slate.intersects(r)
    assert checked > 0


def test_slate_shrinks_rather_than_overlapping():
    cfg = cfg_with(cy=0.1)
    cfg.slate.position, cfg.slate.size = "top", 0.1
    lay = layout.compute(cfg, 1920, 1080, SLATE)
    if lay.slate is not None:
        assert lay.slate_px < round(0.1 * 1080)


def test_options_off():
    lay = layout.compute(cfg_with(show_ms=False, strip=False), 1920, 1080)
    assert lay.clock_ms is None and lay.strip is None and lay.strip_cells == ()


def test_patch_rect():
    cfg = GeneratorConfig()
    cfg.patch.mode = "white"
    lay = layout.compute(cfg, 1000, 1000)
    assert lay.patch == layout.Rect(20, 400, 100, 200)
