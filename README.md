# cam-lag

A test generator for the glass-to-glass camera latency test. It shows a frame-locked clock on
a 100Hz (or 120Hz) monitor, with a Gray-code strip that software can decode, so a control
camera that sees both the source monitor and the LED wall can be turned into latency figures.

See [SPEC.md](SPEC.md) for the full scope and build phases.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Generator core: clock, code strip, frame-locked timing, dropped-frame logging | Done, awaiting Pi 5 hardware checks |
| 2 | Web control front end, presets, slate | Next |
| 3 | Image analysis extension | Planned |
| 4 | Photodiode extension (Pi GPIO), then Pico timer and Lumix control | Planned |

## Running

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra generator
uv run camlag-generator --rate-set 50 --tc-rate 25 --run      # full screen, 100Hz set
uv run camlag-generator --rate-set 60 --tc-rate 120 --run     # 120Hz, fast mode
uv run camlag-generator --windowed 1280x720 --run             # window, for development
uv run camlag-generator --snapshot frame.png --count 123456   # render one frame, no display
```

Keys: Space start/stop, R reset, Esc or Q quit. Settings can also come from a JSON file
(`--config`), in the same format as the presets the web UI will save.

Each run writes a JSON Lines log to `runs/` with the settings, slate, start count and every
dropped or repeated frame. On exit the generator prints a frame summary.

## How the timing works

- The count advances by exactly one tick per displayed frame, so it cannot drift from the
  display. Each present waits for vsync.
- After each present the generator timestamps the return. A gap of N refresh periods means
  the previous image was held for N - 1 extra refreshes: logged as a repeat. A gap far
  shorter than one period means vsync is not holding: logged as an early present. A single
  late timestamp followed by an early one is treated as scheduling jitter and ignored.
- The measured refresh rate is checked against the rate set, and a mismatch is logged.

## On the Pi 5 (to verify)

Run under the desktop (Wayland) with the display set to 100Hz or 120Hz, or from the console
with `SDL_VIDEODRIVER=kmsdrm`. The generator warns if the display reports a different rate
or the measured rate is off. Still to be checked on hardware: 120Hz output, that present
blocks on vsync under KMS, and a clean one-hour run (SPEC.md, "Proposed acceptance checks").

## Development

```sh
uv run pytest
uv run ruff check .
```

The code strip format is in [docs/strip-format.md](docs/strip-format.md). UI styling follows
[docs/brand.md](docs/brand.md) and [design/tokens.css](design/tokens.css).

IBM Plex Mono is included under the SIL Open Font License (`src/camlag/generator/fonts/OFL.txt`).
