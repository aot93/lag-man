"""pygame-ce backends that execute scene operations.

``WindowRenderer`` draws to the display through an SDL renderer with vsync, using cached
glyph textures so a frame costs a handful of copies. ``SurfaceRenderer`` draws the same
operations into an off-screen surface, for previews and tests.
"""

from __future__ import annotations

from pathlib import Path

import pygame
from pygame._sdl2 import video

from ..core.layout import CHAR_W
from .scene import Colour, Fill, Op, Text

FONT_PATH = Path(__file__).parent / "fonts" / "IBMPlexMono-SemiBold.ttf"


class GlyphCache:
    """Rendered glyph surfaces per (pixel size, colour, character)."""

    def __init__(self) -> None:
        if not pygame.font.get_init():
            pygame.font.init()
        self._fonts: dict[int, pygame.font.Font] = {}
        self._glyphs: dict[tuple[int, Colour, str], pygame.Surface] = {}

    def glyph(self, px: int, colour: Colour, ch: str) -> pygame.Surface:
        key = (px, colour, ch)
        surf = self._glyphs.get(key)
        if surf is None:
            font = self._fonts.get(px)
            if font is None:
                font = self._fonts[px] = pygame.font.Font(FONT_PATH, px)
            surf = self._glyphs[key] = font.render(ch, True, colour)
        return surf


def _glyph_positions(op: Text) -> list[tuple[int, str]]:
    return [(op.rect.x + round(i * CHAR_W * op.px), ch)
            for i, ch in enumerate(op.text) if ch != " "]


class SurfaceRenderer:
    def __init__(self, width: int, height: int):
        self.surface = pygame.Surface((width, height))
        self.glyphs = GlyphCache()

    def draw(self, ops: list[Op], bg: Colour) -> pygame.Surface:
        self.surface.fill(bg)
        for op in ops:
            if isinstance(op, Fill):
                self.surface.fill(op.colour, (op.rect.x, op.rect.y, op.rect.w, op.rect.h))
            else:
                for x, ch in _glyph_positions(op):
                    self.surface.blit(self.glyphs.glyph(op.px, op.colour, ch), (x, op.rect.y))
        return self.surface


class WindowRenderer:
    def __init__(self, window: video.Window):
        try:
            self.renderer = video.Renderer(window, accelerated=1, vsync=True)
            self.accelerated = True
        except pygame.error:
            # No GPU renderer (headless testing): software rendering, timing not trustworthy.
            self.renderer = video.Renderer(window, accelerated=-1, vsync=True)
            self.accelerated = False
        self.glyphs = GlyphCache()
        self._textures: dict[tuple[int, Colour, str], video.Texture] = {}

    def _texture(self, px: int, colour: Colour, ch: str) -> video.Texture:
        key = (px, colour, ch)
        tex = self._textures.get(key)
        if tex is None:
            surf = self.glyphs.glyph(px, colour, ch)
            tex = self._textures[key] = video.Texture.from_surface(self.renderer, surf)
        return tex

    def draw(self, ops: list[Op], bg: Colour) -> None:
        r = self.renderer
        r.draw_color = (*bg, 255)
        r.clear()
        for op in ops:
            if isinstance(op, Fill):
                r.draw_color = (*op.colour, 255)
                r.fill_rect((op.rect.x, op.rect.y, op.rect.w, op.rect.h))
            else:
                for x, ch in _glyph_positions(op):
                    tex = self._texture(op.px, op.colour, ch)
                    tex.draw(dstrect=(x, op.rect.y, tex.width, tex.height))

    def present(self) -> None:
        """Blocks until the frame is queued for the next vertical blank (vsync)."""
        self.renderer.present()
