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

Keys: Space start/stop a run (on the slate screen, the time code), R reset (on the slate
screen, back to the start value), Esc or Q quit. On the Pi, `deploy/install-pi.sh`
starts it at boot and lets the web UI change the network (below). Settings can also come from a JSON
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

## Slate only

The Screen card switches between the latency test and **Slate only**: a large time-of-day
clock, time code under it, and the slate fields below, for labelling recordings without a
test. Start it that way with `--screen slate` (on the Pi, in `LAGMAN_ARGS`).

- **Time of day** comes from the generator's clock, and changes on the frame where the
  second changes. The Pi sets its clock from a time server once it has internet; with none
  (hotspot mode) and no RTC battery, it is wrong after a boot. The Screen card says how far
  the generator's time is from your device's. **Set from this device** takes the browser's
  time (and time zone) instead, allowing for the network delay, and keeps it until the
  generator restarts, even if the Pi's clock is set later. **Use the generator's clock**
  goes back. With a time-of-day start, the time code jams to the new time unless a run is
  in progress (a run logs the change instead). Run names and log times use the same time.
- **Time code** runs at the time code rate (25 or 50fps, or 30 or 60fps for the 60Hz set),
  counting displayed frames like the test clock, from the start value. It runs as soon as
  the slate screen is chosen; the Run card's **Start time code** and **Stop time code** run
  and hold it (record run), and Reset clock goes back to the start value. Choose **Time of
  day** as the start to jam it to the clock; Reset clock jams it again. Over a long day it
  can drift from the clock by the display's crystal error (a few seconds a day at most);
  jam it again before a take that has to match. Turn it off for the clock and slate alone.
- **Slate fields** always show on this screen; untick a field to hide it. A blank run
  number stays blank and the date shows without the time.
- **Control page address:** shown under the fields unless turned off.
- There are no runs or run logs on this screen. The screen chosen last comes back at the
  next start; `--screen`, or a `--config` file that sets `screen`, overrides it. The slate
  fields are not remembered between starts: save them as a preset and start with
  `--config presets/<name>.json` to bring them back at boot.

## LTC output

The LTC output card sends SMPTE linear time code as audio, following the time code on
screen (on the slate screen, or the test clock during a run), so cameras and recorders can
jam to it. It is silent while the clock is stopped.

- **Rate:** 25 or 30fps. 50 and 60fps time code, and fast mode, go out at half the rate,
  as LTC tops out at 30fps.
- **Output:** any ALSA output, through `aplay` (`deploy/install-pi.sh` installs
  `alsa-utils`). The Pi 5 has no headphone socket: use HDMI audio (it comes out of the
  display's audio output) or a USB audio interface. Choose the channel (left, right or
  both) and the level (-18 dBFS by default).
- **Timing:** the generator writes the audio a few tens of milliseconds ahead, working out
  where the time code on screen will be when each sample is heard, and nudges the audio a
  sample at a time to stay within about a millisecond of that. It jumps only if it is more
  than half a frame out (after an audio underrun, say). The output's own delay is
  estimated; **Offset** trims what is left (positive sends LTC earlier). To set it, film the
  screen and record the LTC on the same camera, and compare.
- **Dropouts:** the audio is written by a separate worker process, so a busy render loop
  cannot starve it, with a 100ms output buffer. If the output still runs dry, the card
  counts each time (from `aplay`'s underrun reports), and the service log has the details.
- Not available headless (timing there is simulated).

Still to check on the Pi 5: HDMI and USB audio through `aplay`, the output delay, and that
the audio never runs dry while the generator is busy.

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
3. **Mark the regions** on a frame: click near the four corners of each strip. Each region
   is then fitted automatically, using frames from across the recording. The ends snap to
   the white reference cells, which takes out a few pixels of error, and the cell grid
   bows to follow lens distortion. Ticks show where each cell edge is expected. Drag a
   corner, or click it and nudge it with the arrow keys (Shift for bigger steps), and
   **Fit to cells** fits again. Save the regions as a named setup that later recordings
   from the same camera position can reuse.
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
  it with `--capture-fps`, or in the app's recording details. The analysis also measures it
  from how fast the source strip advances, and warns if it differs. The first S5 test was
  shot at 150fps. Latency figures do not depend on it; times do.
- **Reading the strips:** cells are measured in linear light, so a cell lit for part of an
  exposure reads as in between rather than as white. A shutter much faster than the
  frame interval (1/500 s, say) catches fewer frames mid-change.
- **Latency:** source count minus wall count, times the tick (10ms at 100Hz, 8.333ms at
  120Hz). The resolution is one tick.
- **Equivalents:** the median and mean are also given in frames at the rate set's test
  camera rates (25 and 50fps, or 30 and 60fps), and as the distance sound travels in that
  time at 343 m/s: audio from that far away arrives in step with the picture.
- **Flagged frames:** these are left out of the figures and listed in the report:
  - a strip caught mid-change
  - a strip that cannot be read (glare, moiré)
  - a reading that looks wrong: negative latency (regions swapped), or a count outside the
    run log's range (wrong log)
  - frames from before Start or after Stop, when the clock was not counting (listed as
    idle, not as problems)
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

## Start at boot (Pi)

```sh
cd ~/lag-man            # wherever the repo is
uv sync --extra generator
deploy/install-pi.sh
```

This installs a systemd service, `lagman-generator`, that starts the generator on the
console at boot (KMS, no desktop) with network control on, and restarts it if it crashes.
It runs as the user who ran the script, from the repo folder, so run logs and presets stay
in `runs/` and `presets/` there. Set a web UI password, or extra options such as
`--mode 1920x1080@50`, in `/etc/default/lagman-generator`, then
`sudo systemctl restart lagman-generator`.

- Logs: `journalctl -u lagman-generator -f`
- Esc or Q on the Pi's keyboard quits it until the next boot or restart.
- Stop it before running the generator by hand (`sudo systemctl stop lagman-generator`),
  since only one program can drive the display.
- After `git pull`: `uv sync --extra generator && sudo systemctl restart lagman-generator`.

## Network

With `--network` (the service turns it on), the web UI's Network card changes the Pi's
network through NetworkManager:

- **Wired:** DHCP, or a static address, gateway and DNS.
- **Join a network:** a Wi-Fi network's name and password (WPA2 or WPA2/WPA3 Personal),
  with DHCP or a static address. Scan lists the networks in range.
- **Hotspot:** the Pi makes its own Wi-Fi network, keeps a fixed address (192.168.4.1 by
  default) and gives devices that join an address by DHCP. Join it, then open
  `http://192.168.4.1:8080/`.

The generator's screen shows where to find the control page (each port's address, the
Wi-Fi or hotspot name, and `<hostname>.local`) from start until the first run starts, even
with the slate off, and again whenever an address changes. It is never shown during a run
or written to the run log.

A change is applied a moment after you press Apply, and may cut off the page you pressed it
from. Reconnect at the new address (the hostname, `http://<hostname>.local:8080/`, often
works too) and press Keep within two minutes; otherwise the previous settings come back.
The Wi-Fi mode chosen last is the one used at boot.

**Hotspot failover:** with this on, if neither port is on a network a set time after the
generator starts (30 seconds by default), it starts the saved hotspot, so you can still
reach the control page. Save a hotspot first. It lasts until the next start, which tries the
Wi-Fi network again; the setting is kept in `runs/network.json`. Rebooting during the two minutes keeps
the new settings, so if you lose the Pi, reach it over the other port, or log in at its
keyboard (Q quits the generator) and run `nmtui`.

The generator only edits its own NetworkManager profiles (`lagman-wired`, `lagman-wifi`,
`lagman-hotspot`); the ones Raspberry Pi Imager made are left alone. The hotspot needs the
Wi-Fi country set (`sudo raspi-config`, Localisation). Anyone who can open the web UI can
change the network, so set a password. Headless runs simulate the network, so the card
can be tried anywhere.

## Development

```sh
uv run pytest
uv run ruff check .
```

The code strip format is in [docs/strip-format.md](docs/strip-format.md). UI styling follows
[docs/brand.md](docs/brand.md) and [design/tokens.css](design/tokens.css).

IBM Plex Mono, Raleway and Roboto are included under the SIL Open Font License
(`src/lagman/generator/fonts/OFL.txt`, `src/lagman/web/static/fonts/LICENSE-*.txt`).
