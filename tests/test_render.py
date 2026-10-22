"""Render frames off-screen and read the strip back, as the analysis extension will."""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
pygame = pytest.importorskip("pygame")

from camlag.core import layout, strip  # noqa: E402
from camlag.core.config import GeneratorConfig  # noqa: E402
from camlag.generator.render import FONT_PATH, SurfaceRenderer  # noqa: E402
from camlag.generator.scene import Text, compose, hex_colour  # noqa: E402


def cell_levels(surface, lay):
    levels = []
    for c in lay.strip_cells:
        # sample the inner half of each cell
        x0, y0 = c.x + c.w // 4, c.y + c.h // 4
        px = [sum(surface.get_at((x, y))[:3]) / 3
              for x in range(x0, c.x + 3 * c.w // 4) for y in range(y0, c.y + 3 * c.h // 4)]
        levels.append(sum(px) / len(px))
    return levels


@pytest.mark.parametrize("rate_set,tc", [("50", 25), ("60", 120)])
def test_rendered_strip_decodes(rate_set, tc):
    cfg = GeneratorConfig(rate_set=rate_set, tc_rate=tc)
    cfg.clock.fg, cfg.clock.bg = "#ffcc00", "#202020"
    lay = layout.compute(cfg, 1920, 1080)
    r = SurfaceRenderer(1920, 1080)
    for count in (0, 1, 99, 12_345, 8_640_000 - 1, strip.COUNT_MODULUS + 3):
        surf = r.draw(compose(cfg, lay, count), hex_colour(cfg.clock.bg))
        d = strip.decode(cell_levels(surf, lay))
        assert d.status is strip.DecodeStatus.OK
        assert d.count == count % strip.COUNT_MODULUS


def test_font_metrics_match_layout():
    pygame.font.init()
    font = pygame.font.Font(FONT_PATH, 200)
    adv = font.metrics("0")[0][4]
    assert abs(adv / 200 - layout.CHAR_W) < 0.01
    assert abs(font.get_height() / 200 - layout.LINE_H) < 0.03


def test_digits_drawn_inside_clock_rect():
    cfg = GeneratorConfig()
    lay = layout.compute(cfg, 1920, 1080)
    surf = SurfaceRenderer(1920, 1080).draw(compose(cfg, lay, 888_888), (0, 0, 0))
    main = lay.clock_main
    lit = [(x, y) for x in range(0, 1920, 3) for y in range(0, 1080, 3)
           if surf.get_at((x, y))[0] > 128]
    in_clock = [p for p in lit if main.x <= p[0] < main.right and main.y <= p[1] < main.bottom]
    assert len(in_clock) > 500
    ops = compose(cfg, lay, 888_888)
    assert any(isinstance(o, Text) and o.text == "02:28:08:22" for o in ops)
