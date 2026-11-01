"""Display modes through SDL, for the generator window.

pygame-ce cannot choose a refresh rate, so this calls the SDL library pygame-ce already
loaded, through ctypes. Modes are set only under KMS (the Pi console), where SDL drives the
output directly. Under a desktop the compositor owns the mode, so modes are listed but
not set (use wlr-randr or the desktop's screen settings).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import sys
from pathlib import Path

from ..core.display import DisplayMode

SDL_WINDOW_FULLSCREEN = 0x00000001
SDL_WINDOW_FULLSCREEN_DESKTOP = SDL_WINDOW_FULLSCREEN | 0x00001000


class _SDLDisplayMode(ctypes.Structure):
    _fields_ = [("format", ctypes.c_uint32), ("w", ctypes.c_int), ("h", ctypes.c_int),
                ("refresh_rate", ctypes.c_int), ("driverdata", ctypes.c_void_p)]


class SDLDisplay:
    def __init__(self, window, fullscreen: bool):
        self.can_set = False
        self.driver = ""
        self.note = "Display modes are unavailable: SDL could not be reached."
        self._sdl = _load_sdl()
        self._win = None
        if self._sdl is None:
            return
        _declare(self._sdl)
        self._win = self._sdl.SDL_GetWindowFromID(window.id)
        if not self._win:
            # A different copy of SDL from the one pygame-ce initialised.
            self._sdl = None
            return
        driver = (self._sdl.SDL_GetCurrentVideoDriver() or b"").decode()
        self.driver = driver
        if driver.lower() != "kmsdrm":
            self.note = (f"The {driver} video driver does not let the generator set modes. "
                         "Set the mode in the desktop's screen settings, or run from the "
                         "console with SDL_VIDEODRIVER=kmsdrm.")
        elif not fullscreen:
            self.note = "Modes are set only in full screen."
        else:
            self.can_set = True
            self.note = "Modes are set through KMS. The display may go blank for a few seconds."

    def _index(self) -> int:
        return max(0, self._sdl.SDL_GetWindowDisplayIndex(self._win))

    def _sdl_modes(self) -> list[_SDLDisplayMode]:
        if self._sdl is None:
            return []
        out = []
        index = self._index()
        for i in range(self._sdl.SDL_GetNumDisplayModes(index)):
            m = _SDLDisplayMode()
            if self._sdl.SDL_GetDisplayMode(index, i, ctypes.byref(m)) == 0:
                out.append(m)
        return out

    def modes(self) -> list[DisplayMode]:
        # SDL2 rounds refresh to whole hertz, so 59.94 and 60 both read as 60. The display
        # lists the exact rate first; the frame monitor measures what actually arrives.
        return [DisplayMode(m.w, m.h, m.refresh_rate) for m in self._sdl_modes()]

    def current(self) -> DisplayMode | None:
        if self._sdl is None:
            return None
        m = _SDLDisplayMode()
        if self._sdl.SDL_GetCurrentDisplayMode(self._index(), ctypes.byref(m)) != 0:
            return None
        return DisplayMode(m.w, m.h, m.refresh_rate)

    def set_mode(self, mode: DisplayMode) -> DisplayMode:
        if not self.can_set:
            raise RuntimeError(self.note)
        match = next((m for m in self._sdl_modes()
                      if (m.w, m.h, m.refresh_rate) == (mode.width, mode.height, mode.hz)), None)
        if match is None:
            raise RuntimeError(f"{mode} is not a mode this display offers")
        sdl, win = self._sdl, self._win
        # TODO(verify): on the Pi 5 under KMS, that this changes the HDMI output mode, that
        # the renderer follows the new size, and that present still blocks on vsync.
        if sdl.SDL_SetWindowDisplayMode(win, ctypes.byref(match)) != 0:
            raise RuntimeError(_error(sdl))
        flags = sdl.SDL_GetWindowFlags(win)
        if flags & SDL_WINDOW_FULLSCREEN_DESKTOP == SDL_WINDOW_FULLSCREEN_DESKTOP:
            # Desktop full screen keeps the current mode; exclusive full screen applies the
            # window's display mode. Later changes apply directly in exclusive full screen.
            if sdl.SDL_SetWindowFullscreen(win, SDL_WINDOW_FULLSCREEN) != 0:
                raise RuntimeError(_error(sdl))
        return self.current() or mode


def _error(sdl) -> str:
    return (sdl.SDL_GetError() or b"unknown SDL error").decode(errors="replace")


def _declare(sdl) -> None:
    p, i, u32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
    mode_p = ctypes.POINTER(_SDLDisplayMode)
    for name, res, args in (
        ("SDL_GetWindowFromID", p, [u32]),
        ("SDL_GetWindowDisplayIndex", i, [p]),
        ("SDL_GetNumDisplayModes", i, [i]),
        ("SDL_GetDisplayMode", i, [i, i, mode_p]),
        ("SDL_GetCurrentDisplayMode", i, [i, mode_p]),
        ("SDL_SetWindowDisplayMode", i, [p, mode_p]),
        ("SDL_SetWindowFullscreen", i, [p, u32]),
        ("SDL_GetWindowFlags", u32, [p]),
        ("SDL_GetCurrentVideoDriver", ctypes.c_char_p, []),
        ("SDL_GetError", ctypes.c_char_p, []),
    ):
        fn = getattr(sdl, name)
        fn.restype, fn.argtypes = res, args


def _load_sdl():
    """The SDL2 library pygame-ce loaded: its bundled copy or the system one."""
    candidates = []
    if sys.platform.startswith("linux"):
        try:
            maps = Path("/proc/self/maps").read_text()
        except OSError:
            maps = ""
        candidates += sorted({line.split()[-1] for line in maps.splitlines()
                              if re.search(r"/libSDL2-2[.-][^/]*$", line)})
    else:
        import pygame

        base = Path(pygame.__file__).parent
        candidates += [str(p) for p in (base / "SDL2.dll", *base.glob(".dylibs/libSDL2*"))
                       if p.exists()]
    found = ctypes.util.find_library("SDL2")
    if found:
        candidates.append(found)
    for path in candidates:
        try:
            return ctypes.CDLL(path, mode=getattr(os, "RTLD_NOLOAD", 0) | ctypes.RTLD_GLOBAL)
        except OSError:
            continue
    return None
