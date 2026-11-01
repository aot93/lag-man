# lag-man

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
uv run lagman-generator --rate-set 50 --tc-rate 25 --run      # full screen, 100Hz set
uv run lagman-generator --rate-set 60 --tc-rate 120 --run     # 120Hz, fast mode
uv run lagman-generator --mode 1920x1080@50 --run             # a TV at the 50Hz fallback
uv run lagman-generator --windowed 1280x720 --run             # window, for development
uv run lagman-generator --snapshot frame.png --count 123456   # render one frame, no display
```

Keys: Space start/stop a run, R reset, Esc or Q quit. Settings can also come from a JSON
file (`--config`), in the same format as the presets the web UI saves.

Each run (Start to Stop) writes a JSON Lines log to `runs/` with the settings, slate, start
count, every configuration change and every dropped or repeated frame. Run numbers persist
in `runs/state.json`.

## Web control

The generator serves its control page on port 8080: open `http://<generator>:8080/` from any
device on the network. It works with no internet (fonts are bundled). Set a shared password
with `--password` or `LAGMAN_PASSWORD`; the browser asks for it, and any user name works.

From the page: start, stop and reset; rate set and time code rate; start value; clock size,
position and colours; strip and milliseconds; slate fields, position, size and mode (off,
before each run, or always); the sync patch; presets (save, load, export, import); run logs;
live status with a dropped-frame warning to acknowledge; and a live preview drawn from the
same frame data the generator renders.

To work on the UI without a display, run headless (frames are paced by a timer, so timing
is simulated and the frame monitor is not exercised):

```sh
uv run lagman-generator --headless 1920x1080 --port 8080
```

Every control is also an HTTP JSON API, so runs can be scripted:

```sh
curl -X PUT  localhost:8080/api/config -d '{"tc_rate": 50, "slate": {"mode": "pre_roll"}}'
curl -X POST localhost:8080/api/start
curl         localhost:8080/api/status
curl -X POST localhost:8080/api/stop
```

The full route list is in `src/lagman/web/server.py`. The API is plain HTTP with polling
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

Runs on a PC (Windows, macOS or Linux). Install with `uv sync --extra analysis`, or on
Windows without cloning:

```powershell
uv tool install "lagman[analysis] @ git+https://github.com/aot93/lag-man"
```

To update later, run `uv tool upgrade lagman`. If you installed it under its old name, run
`uv tool uninstall camlag` first; the app moves an old `Documents\cam-lag` workspace to
`Documents\lag-man` on its first start.

Then run `lagman-analyse` with no arguments. It opens the analysis app in your browser, and
the whole workflow happens there:

1. **Add a recording** with the file browser. Videos are read where they are, never copied.
2. **Attach its run log.** Fetch it from the generator over the network, or pick the `.jsonl`
   file. Logs in the workspace and next to the video are listed, closest in time first.
   Without a run log, choose the rate set by hand.
3. **Mark the regions** on a frame: four corners per strip, checked live, then saved as a
   named setup that later recordings from the same camera position can reuse.
4. **Analyse** in the background, with a progress bar and a cancel button.
5. **Review the results:** figures, charts, checks and every sample. Click a dot, a flag or
   a row to see that frame with the regions drawn on, and step frame by frame. Open the
   report, or download the CSV or a summary per camera and frame rate.

A **Make a demo recording** button creates a synthetic recording to try it without
footage. The workspace (default `Documents/lag-man`, or `--workspace DIR`) keeps
recordings, setups, fetched run logs and results between sessions. The app only listens on
this PC (127.0.0.1), and its API needs the page's session token.

The same steps are available on the command line, for scripting:

```sh
uv run lagman-analyse analyse A001.mp4 --setup A001.setup.json --run-log runs/20261022-140512.jsonl
uv run lagman-analyse review A001.mp4 --setup A001.setup.json --frame 250
uv run lagman-analyse combine A001-latency A002-latency --out summary.csv
uv run lagman-analyse synth demo.mp4 --latency-ticks 8
uv run lagman-analyse mark A001.mp4   # opens the app on this video's regions step
```

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
- **Theme:** the generator page, the analysis app and the report each have an Auto / Light
  / Dark switch. Auto follows the system. Printing always uses the light print style.
- **Report:** one self-contained HTML file with the slate, figures, a latency-over-time
  chart, the distribution, checks, review frames with the regions drawn on, and every
  sample. It prints cleanly.
- **Strip direction:** a strip read right to left (marked backwards, or seen in a mirror)
  is detected and decoded correctly.

Still to check against real footage: the decode rate on the LED wall (PWM, scan banding,
moiré), and exact frame seeking in the S5's long-GOP H.264 (used only for review images).

## Display modes

The clock counts at the display's refresh: 100 or 120Hz, or on a display that cannot reach
that (most TVs), a fallback of 50 or 60Hz with half the resolution (20ms or 16.7ms ticks).
The web UI's Display card lists the modes the display offers at those rates and switches
between them while no run is in progress; `--mode 1920x1080@50` does the same at start. The
last mode chosen is remembered in `runs/state.json`.

Modes are set only from the Pi console (KMS). Under a desktop, set the mode in the desktop's
screen settings or with `wlr-randr`; the UI still lists them. If the display runs at a rate
the rate set cannot use, the UI shows one warning instead of a repeated-frame warning on
every frame.

## On the Pi 5 (to verify)

Run from the console with `SDL_VIDEODRIVER=kmsdrm`, or under the desktop (Wayland) with the
display set to the mode you want. The pygame-ce wheel's SDL has no KMS driver, so on the Pi
build pygame-ce against the system SDL (`libsdl2-dev` and friends, `python3-dev`, then
`UV_PYTHON=/usr/bin/python3 UV_NO_BINARY_PACKAGE=pygame-ce uv sync --extra generator`).
The generator warns if the display reports a different rate or the measured rate is off. Still to be checked on hardware: 120Hz output, that present
blocks on vsync under KMS, switching display modes through SDL under KMS, and a clean
one-hour run (SPEC.md, "Proposed acceptance checks").

## Development

```sh
uv run pytest
uv run ruff check .
```

The code strip format is in [docs/strip-format.md](docs/strip-format.md). UI styling follows
[docs/brand.md](docs/brand.md) and [design/tokens.css](design/tokens.css).

IBM Plex Mono, Raleway and Roboto are included under the SIL Open Font License
(`src/lagman/generator/fonts/OFL.txt`, `src/lagman/web/static/fonts/LICENSE-*.txt`).
