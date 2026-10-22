# cam-lag

A test generator for the glass-to-glass camera latency test. It shows a frame-locked clock on
a 100Hz (or 120Hz) monitor, with a Gray-code strip that software can decode, so a control
camera that sees both the source monitor and the LED wall can be turned into latency figures.

See [SPEC.md](SPEC.md) for the full scope and build phases.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Generator core: clock, code strip, frame-locked timing, dropped-frame logging | Done, awaiting Pi 5 hardware checks |
| 2 | Web control front end, presets, slate | Done |
| 3 | Image analysis extension | Started: works on synthetic recordings, awaiting real S5 footage |
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

Keys: Space start/stop a run, R reset, Esc or Q quit. Settings can also come from a JSON
file (`--config`), in the same format as the presets the web UI saves.

Each run (Start to Stop) writes a JSON Lines log to `runs/` with the settings, slate, start
count, every configuration change and every dropped or repeated frame. Run numbers persist
in `runs/state.json`.

## Web control

The generator serves its control page on port 8080: open `http://<generator>:8080/` from any
device on the network. It works with no internet (fonts are bundled). Set a shared password
with `--password` or `CAMLAG_PASSWORD`; the browser asks for it, and any user name works.

From the page: start, stop and reset; rate set and time code rate; start value; clock size,
position and colours; strip and milliseconds; slate fields, position, size and mode (off,
before each run, or always); the sync patch; presets (save, load, export, import); run logs;
live status with a dropped-frame warning to acknowledge; and a live preview drawn from the
same frame data the generator renders.

To work on the UI without a display, run headless (frames are paced by a timer, so timing
is simulated and the frame monitor is not exercised):

```sh
uv run camlag-generator --headless 1920x1080 --port 8080
```

Every control is also an HTTP JSON API, so runs can be scripted:

```sh
curl -X PUT  localhost:8080/api/config -d '{"tc_rate": 50, "slate": {"mode": "pre_roll"}}'
curl -X POST localhost:8080/api/start
curl         localhost:8080/api/status
curl -X POST localhost:8080/api/stop
```

The full route list is in `src/camlag/web/server.py`. The API is plain HTTP with polling
rather than WebSocket, which keeps the server in the standard library.

## How the timing works

- The count advances by exactly one tick per displayed frame, so it cannot drift from the
  display. Each present waits for vsync.
- After each present the generator timestamps the return. A gap of N refresh periods means
  the previous image was held for N - 1 extra refreshes: logged as a repeat. A gap far
  shorter than one period means vsync is not holding: logged as an early present. A single
  late timestamp followed by an early one is treated as scheduling jitter and ignored.
- The measured refresh rate is checked against the rate set, and a mismatch is logged.

## Analysing recordings

Runs on a PC. Install with `uv sync --extra analysis` (OpenCV and NumPy).

```sh
# once per camera setup: mark both strips on a frame of the recording (opens a local page)
uv run camlag-analyse mark A001.mp4

# decode every frame; writes A001-latency/results.csv, summary.json and report.html
uv run camlag-analyse analyse A001.mp4 --setup A001.setup.json --run-log runs/20261022-140512.jsonl

# one frame with both regions and their decoded values drawn on, for checking by eye
uv run camlag-analyse review A001.mp4 --setup A001.setup.json --frame 250

# one row per camera and frame rate across several runs
uv run camlag-analyse combine A001-latency A002-latency --out summary.csv

# a synthetic recording with a known latency, to try the tools without footage
uv run camlag-analyse synth demo.mp4 --latency-ticks 8
```

- **Marking:** click the four corners of each strip, in any order. The page decodes them
  live and shows the latency over the next few frames. On save it nudges each end onto
  the cells. Regions are saved per setup and reused for every recording from that setup.
- **Timing:** the real capture rate comes from the rate set (100fps or 120fps), not from
  the file, because the S5 writes high-frame-rate footage into a 50p or 60p file. Override
  it with `--capture-fps` if needed.
- **Latency:** source count minus wall count, times the tick (10ms at 100Hz, 8.333ms at
  120Hz). The resolution is one tick.
- **Flagged frames:** these are left out of the figures and listed in the report:
  - a strip caught mid-change
  - a strip that cannot be read (glare, moiré)
  - a reading that looks wrong: negative latency (regions swapped), or a count outside the
    run log's range (wrong log)
- **Report:** one self-contained HTML file with the slate, figures, a latency-over-time
  chart, the distribution, checks, review frames with the regions drawn on, and every
  sample. It prints cleanly.
- **Strip direction:** a strip read right to left (marked backwards, or seen in a mirror)
  is detected and decoded correctly.

Still to check against real footage: the decode rate on the LED wall (PWM, scan banding,
moiré), and exact frame seeking in the S5's long-GOP H.264 (used only for review images).

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

IBM Plex Mono, Raleway and Roboto are included under the SIL Open Font License
(`src/camlag/generator/fonts/OFL.txt`, `src/camlag/web/static/fonts/LICENSE-*.txt`).
