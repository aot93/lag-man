import pytest

from lagman.core.config import GeneratorConfig
from lagman.core.display import DisplayMode, SimulatedDisplay, usable_modes
from lagman.core.rates import get_rate_set


def test_rate_sets_have_a_fallback_refresh():
    eu, us = get_rate_set("50"), get_rate_set("60")
    assert eu.refresh_rates == (100, 50) and us.refresh_rates == (120, 60)
    assert eu.timecode_rates_at(50) == (25, 50)
    assert us.timecode_rates_at(60) == (30, 60)
    assert eu.timecode_rates_at(100) == eu.timecode_rates == (25, 50, 100)


def test_refresh_follows_the_display_when_the_rate_set_allows_it():
    eu = get_rate_set("50")
    assert eu.refresh_for(50) == 50
    assert eu.refresh_for(100) == 100
    assert eu.refresh_for(60) == 100  # not usable: stays at the standard rate, and warns
    assert eu.refresh_for(None) == 100


def test_fast_mode_follows_the_display():
    eu = get_rate_set("50")
    assert eu.fit_tc_rate(100, 50) == 50
    assert eu.fit_tc_rate(25, 50) == 25
    assert eu.fit_tc_rate(50, 100) == 50


def test_display_refresh_is_runtime_state_not_a_setting():
    cfg = GeneratorConfig.from_dict({"tc_rate": 50}, display_hz=50)
    assert cfg.refresh_hz == 50
    assert "display_hz" not in cfg.to_dict()
    with pytest.raises(ValueError, match="unknown"):
        GeneratorConfig.from_dict({"display_hz": 50})
    with pytest.raises(ValueError, match="100"):
        GeneratorConfig.from_dict({"tc_rate": 100}, display_hz=50)
    with pytest.raises(ValueError, match="60 Hz"):
        GeneratorConfig.from_dict({}, display_hz=60)


def test_mode_text_round_trip():
    m = DisplayMode.parse("1920x1080@50")
    assert (m.width, m.height, m.hz) == (1920, 1080, 50)
    assert str(m) == "1920x1080@50"
    for bad in ("1920x1080", "1920x1080@", "big@50", "0x0@50"):
        with pytest.raises(ValueError):
            DisplayMode.parse(bad)


def test_usable_modes_keep_countable_rates_in_preference_order():
    # The LG TV in the first Pi test: no 100 or 120Hz, 4K and 1080p at 50 and 60.
    edid = [DisplayMode(1920, 1080, 60), DisplayMode(1920, 1080, 50),
            DisplayMode(1920, 1080, 60), DisplayMode(1920, 1080, 24),
            DisplayMode(3840, 2160, 60), DisplayMode(3840, 2160, 50),
            DisplayMode(1280, 720, 50), DisplayMode(720, 400, 70)]
    assert [str(m) for m in usable_modes(edid)] == [
        "3840x2160@60", "1920x1080@60", "3840x2160@50", "1920x1080@50", "1280x720@50"]


def test_simulated_display_switches_modes():
    d = SimulatedDisplay(DisplayMode(1920, 1080, 100))
    assert d.can_set and DisplayMode(1920, 1080, 50) in d.modes()
    assert d.set_mode(DisplayMode(1920, 1080, 50)) == d.current() == DisplayMode(1920, 1080, 50)


def test_sdl_display_reads_modes_through_pygames_sdl(monkeypatch):
    pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    import pygame
    from pygame._sdl2 import video

    from lagman.generator.display import SDLDisplay

    pygame.display.init()
    try:
        window = video.Window("test", size=(320, 240))
        d = SDLDisplay(window, fullscreen=True)
        assert d.driver == "dummy" and not d.can_set and "dummy" in d.note
        current = d.current()
        assert current is not None and current.width > 0 and current in d.modes()
        with pytest.raises(RuntimeError):
            d.set_mode(current)
        window.destroy()
    finally:
        pygame.display.quit()
