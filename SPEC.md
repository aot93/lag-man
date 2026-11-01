# Latency Test Generator: Scope for Claude Code

Oct 22, 2026 · @Bez

A generator app and two extensions that run the glass-to-glass camera latency test: a precise on-screen clock, a web control front end, photodiode timing, and automated analysis of control camera recordings.

## Overview and goals

The app replaces a hand-built clock source with a purpose-built test generator, so the glass-to-glass latency test can be run repeatably and the results calculated automatically.

It supports the test procedure already written: a 100Hz clock on a Dell gaming monitor, a test camera at 25fps and 50fps, and a 100fps control camera that sees the source clock and the LED wall in one frame. Latency is the source clock minus the wall clock. The same procedure runs in 60Hz regions with a 120Hz source and 30fps and 60fps test cameras.

Goals:

- Display a frame-accurate clock that the control camera can read unambiguously at frame-by-frame review.
- Label every recording with a slate so each clip identifies the camera, frame rate and run.
- Configure everything from a web page, on the generator or from another device.
- Add photodiode timing as an independent cross-check.
- Turn control camera recordings into latency figures without reading digits by eye.

## At a glance

&#91;embedded content: architecture · generator, web UI, two extensions\]

The web control UI configures the generator, which writes its settings and slates to a run log. Both extensions read from or write to that log, so every result can be matched to the run that produced it.

## Generator core

The generator is a full-screen app that renders a time code clock, locked to the display's refresh, on a Raspberry Pi 5 (4GB).

**Rate sets**

The generator ships with two rate sets, selected in the web UI, so the same test works in 50Hz and 60Hz regions. Fractional rates (29.97, 59.94 and 119.88) are out of scope for now.

| Rate set | Test camera (fps) | Source refresh | Control camera (fps) | Clock tick |
| --- | --- | --- | --- | --- |
| 50Hz regions (Europe) | 25, 50 | 100Hz | 100 | 10ms |
| 60Hz regions (non-European) | 30, 60 | 120Hz | 120 | about 8.3ms |

A display that cannot reach 100 or 120Hz (most TVs) can run each rate set at a fallback of half the refresh: 50Hz in the 50Hz rate set, 60Hz in the 60Hz rate set. The clock then ticks every 20ms or 16.7ms, so latency resolution halves, and fast mode becomes 50 or 60fps. The count is always ticks of the refresh the display actually runs at, and the run log records it.

**Clock content**

- Hours, minutes, seconds, frames and milliseconds in large digits, readable from a camera at a distance.
- A binary or Gray-code strip carrying the same count, designed to be decoded by software (this is what the analysis extension reads, not the digits).
- Selectable time code rate from the active rate set, including the fast mode that updates at the source refresh rate.
- Start from zero, from a set value, or free-run from time of day.

**Timing requirements**

- Output at the monitor's refresh rate: 100Hz in the 50Hz rate set, 120Hz in the 60Hz rate set, or the 50 or 60Hz fallback. Each displayed frame increments the count by exactly one tick, driven by the frame count rather than the system clock, so the clock cannot drift or skip silently.
- Detect and log dropped or repeated frames, and show a warning flag in the config UI when one occurs.
- Vsync on, no tearing, no scaling or interpolation.

**Display mode**

- The web UI lists the display's modes at a refresh a rate set can count at (from its EDID, as the display driver reports them) and switches between them while no run is in progress. The chosen mode is remembered for the next start, and can also be given on the command line.
- Modes are set directly only from the Pi console (KMS). Under a desktop the compositor owns the mode, so the UI lists modes and says how to set them there.
- If the display runs at a rate the rate set cannot use, the UI shows one warning naming the rate and the fix, rather than a repeated frame warning on every frame.
- Later: forcing a mode the display does not list, and installing an EDID override. Both need root and a reboot, and a mode the display cannot show leaves it blank, so they need a safe way back.

**Layout**

- Adjustable clock position and size, with a default of mid-screen so scan-out skew matches the LED wall.
- Fixed contrast and background, with no animation other than the count, to avoid motion blur and moiré problems.
- Optional test patterns (a white sync patch, for the photodiode extension).

## Slate

A slate is a block of text shown on screen so that any recording identifies itself, and so the person reviewing it knows what they are looking at.

- Fields, all editable from the web UI: test name, camera name, camera frame rate, run number, date and time, operator, location, free-text notes. Each field has its own on/off toggle, so unused fields are hidden and the slate stays compact.
- Auto-filled where possible: date and time, run number (increments per run), and the generator's own settings (time code rate, refresh rate).
- Position and size are configurable (top, bottom or corner), and it never overlaps the clock or the binary strip.
- Can be toggled on and off, and can be shown for a set time before a run starts (for example five seconds) or throughout.
- Slate text is plain and high contrast so it is readable on the control camera recording and by OCR.
- Each slate is also written to the run's log file, so the recording and the log can be matched later.

## Web control front end

The generator runs a small web server. Any device on the network opens a page to configure and control it, so the operator does not need to touch the generator's keyboard.

- **Controls:** start, stop, reset the clock, set the start value, and switch time code rate.
- **Configuration:** clock size and position, colours, binary strip on or off, slate fields and position, test patterns.
- **Presets:** save, load and name configurations (for example "Camera A 25fps"), stored as JSON files that can be exported and imported.
- **Status:** current refresh rate, current count, dropped-frame warnings, and a live preview of what the generator is displaying.
- **Run log:** each run records its start time, settings and slate to a file the analysis extension can read.
- **API:** every control is also available over a simple HTTP or WebSocket API, so runs can be scripted and the extensions can talk to the generator.
- **Access:** works on the local network without internet. Basic access protection, such as a shared password, is enough.
- **Network:** on the Pi, set the wired port (DHCP or static), join a Wi-Fi network (name, password, DHCP or static) or run a hotspot with a fixed address and DHCP for the devices that join. Because a change can cut off the browser that made it, each change waits for the user to confirm it from the new address and reverts after two minutes otherwise. The generator starts at boot as a service.

## Extension 1: photodiode testing

This extension measures latency with light sensors rather than a control camera, as an independent cross-check on the camera-based result and as a way to measure individual stages.

**How it works**

- The generator shows a sync patch that flips between black and white at a known frame.
- Dual mode: one photodiode on the source monitor and one on the LED wall. Each signals when the patch changes.
- Each photodiode drives a Raspberry Pi 5 GPIO input. A high-to-low interrupt timestamps the edge against the Pi's system clock.
- The time between the two edges is the glass-to-glass delay for the camera under test. No control camera or video review is involved.
- Higher precision option (separate extension, below): a Raspberry Pi Pico script timestamps the edges in hardware and reports them to the Pi over USB. Pi GPIO timing is the default and is good enough for now.

**Requirements**

- The generator places the sync patch at a configurable position and size, and triggers it from the web UI or the API.
- Single mode: one photodiode on one screen (the source monitor or the LED wall). The Pi logs the time of the patch draw command, and the photodiode edge stops the timer. The result is the delay from draw command to light at that screen.
- Repeat runs: set N in the web UI. The patch flips N times at a set interval, and the app reports each measured delay plus the mean, median, max and min.
- Results are saved to the run log and shown in the web UI.
- The timing code sits behind an interface, so the Pi GPIO timer and the Pico timer can be swapped without other changes.

**Using it**

- With the monitor sitting before the camera and the wall at the end, the photodiodes can also time a stage on its own, for example server capture or LED processor delay, by moving the sensor position.

**Circuit (proposed, to be confirmed)**

This is a proposed design from research. It has not been built or measured. One channel per screen:

- **Photodiode:** Vishay BPW34, in a black tube or foam cup pressed against the patch, which is the main rejection of room light.
- **Amplifier:** a transimpedance stage using a 3.3V rail-to-rail op-amp (part not yet chosen), with the photodiode reverse biased, 100kΩ to 1MΩ feedback with a trimmer, and a small capacitor for stability.
- **Filter:** a low-pass with a corner of about 3 to 10kHz, to suppress backlight and LED wall PWM ripple.
- **Comparator:** TI TLV3201 with external hysteresis and an adjustable threshold (10kΩ pot) set midway between black and white.
- **GPIO protection:** comparator powered from the Pi's 3.3V, with a 330Ω series resistor to the GPIO. Output polarity is chosen to match the high-to-low edge the Pi timestamps.

**What it can resolve**

The circuit's edge is estimated at 10 to 100µs, far below the frame periods (8.3ms at 120Hz up to 33.3ms at 30Hz). The error will be dominated by LCD pixel response, where on the panel the sensor sits (scan-out can shift a result by up to one frame period), and Pi interrupt jitter.

**Software implications**

- A calibration screen with steady black and white patches, for setting the threshold.
- Optional random delay between patch flips, so the test does not lock to the frame rate.

**To confirm on hardware**

- Scope the amplifier and comparator outputs on each screen, checking for oscillation or ringing.
- Threshold margin on the LCD at every refresh rate, and on the LED wall with its PWM and scan banding.
- Double triggering on LED wall PWM.
- The actual edge delay of the circuit, for example with a known LED strobe.
- Pi 5 input thresholds and GPIO interrupt jitter under load.
- Enough BPW34 signal at the screen's brightness.

Sources: [BPW34 datasheet](https://www.vishay.com/docs/81521/bpw34.pdf), [TLV3201 datasheet](https://www.ti.com/lit/ds/symlink/tlv3201.pdf), [Blur Busters photodiode thread](https://forums.blurbusters.com/viewtopic.php?p=35747).

## Extension 2: image analysis

This application reads a control camera recording and calculates latency for every sample, replacing the manual frame-by-frame review.

**Input**

- A video file from the control camera (Panasonic Lumix S5, 100fps), plus the run log from the generator. The S5 writes 100fps and 120fps captures into a 50fps or 60fps file, so the footage plays back slowed. Each file frame is really 10ms (100fps capture) or 8.333ms (120fps capture) of real time. The app takes the capture rate from the selected rate set, not from the file.
- The user marks two regions of interest once per setup: the source clock and the wall clock. The regions are saved with the run.

**Processing**

- For each video frame, decode the binary or Gray-code strip in both regions. Decoding the strip is preferred over reading digits, since it is more reliable. Reading the digits is an optional fallback.
- Latency for a frame = source count minus wall count, converted to milliseconds using the time code rate.
- Handle partly transitioned codes (a code caught mid-change) by flagging or discarding them rather than guessing.
- Flag frames where either region cannot be decoded, for example from glare or moiré.

**Output**

- A results table per run matching the test procedure: sample, source reading, wall reading and latency, in milliseconds.
- Summary of samples, min, max and mean, per camera and per frame rate.
- A review view that shows any chosen sample frame with its two regions highlighted and the decoded values, so a person can verify it.
- CSV export, and a printable summary that includes the slate details.

**Cross-checks**

- If a photodiode run exists for the same setup, show both figures side by side.

## Optional extension: control camera recording

The control camera is a Panasonic Lumix S5. This extension lets the generator start and stop its recording, so a run and its video are always captured together.

- Start recording from the web UI or automatically with a run, and stop it when the run ends.
- The run log records when recording started and stopped, so the video can be matched to the run.
- Control route is still to be proven on the S5. First choice is the Wi-Fi API through liblumix. Fallback is a wired remote cable on the camera's 2.5mm jack.
- Built last. The generator, analysis and photodiode work do not depend on it.

**S5 recording details**

- Full-frame FHD recording is confirmed at 1920x1080 50.00p (28Mbps) and 25.00p (20Mbps), 4:2:0 8-bit LongGOP H.264 with AAC audio.
- Variable frame rates available: 1, 2, 5, 10, 15, 30, 60, 100, 120, 150 and 180fps. 100fps suits the 50Hz rate set and 120fps the 60Hz rate set. At 150fps and 180fps the angle of view narrows, and autofocus switches to manual focus from 150fps, so those rates are not used.
- Recording settings (frame rate, shutter, focus) are set on the camera. The generator only starts and stops recording.

**Control routes, both unproven for video**

- [liblumix](https://github.com/njfdev/liblumix) is a reverse engineered C++ driver that uses the Lumix Sync Wi-Fi API over a LAN. Its page documents still photos only, and the author tested it on an S5IIX rather than the S5. Video start and stop is not mentioned.
- The [Robotroom remote shutter cable](https://www.robotroom.com/Macro-Photography-2.html) uses the camera's 2.5mm four-conductor jack with a resistor network that the camera reads as a button press. The page does not cover video recording, so its behaviour in video mode needs testing on the S5.

## Optional extension: Pico precision timer

Pi GPIO timing is the default and is good enough for now. This extension adds a Raspberry Pi Pico script for higher precision.

- The Pico timestamps both photodiode edges in hardware against its own clock and reports them to the Pi over USB serial.
- It sits behind the same interface as the Pi GPIO timer, so the web UI, the run modes (single, dual, N runs) and the results are unchanged. Results record which timer produced them.
- Dual mode needs only the difference between two Pico timestamps. Single mode also needs the draw command time, so one option is for the Pi to pulse a GPIO line to the Pico at that moment and have both events timestamped on the Pico clock.
- Built after the Pi GPIO version works.

Suggested approach, unverified: start with a GPIO interrupt that reads the Pico's 1µs timer, then move to a PIO program that timestamps each edge with a free-running counter (about 8ns resolution at 125MHz). Report over USB serial, and use the Pico clock for the delay between the two edges rather than timestamping on arrival at the Pi.

## Platform, constraints and build phases

**Platform**

- Generator and photodiode timing: Raspberry Pi 5 (4GB), running full screen on a 100Hz display (120Hz in the 60Hz rate set). 100Hz output on the Pi 5 has been confirmed by research, and 120Hz is still to be checked.
- Rendering: native, for frame-accurate output (for example SDL2 or OpenGL; the stack is Code's call). The clock is not rendered in a browser.
- Web control front end: served by the generator, used from any browser on the network.
- Analysis extension: runs on a PC, written in Python (OpenCV for the video), as a desktop app or command line tool.

**Build phases**

1. Generator core: clock, binary strip, frame-locked timing, dropped-frame logging.
2. Web control front end, presets and slate.
3. Image analysis extension, using test recordings from the real setup.
4. Photodiode extension, starting with Pi GPIO timing, once the circuit is confirmed. Then, as optional extensions, the Pico precision timer and Lumix recording control.

**Proposed acceptance checks**

- The count advances by exactly one tick per refresh over a one-hour run, with every dropped or repeated frame reported.
- Analysis of a recording gives the same latency as a manual review, within one control camera frame (10ms at 100fps).
- A saved preset reproduces the same display when reloaded.
- The slate never covers the clock or strip, at any position setting.

## Open questions

- Does the Pi 5 also output 120Hz (and 60Hz) cleanly on the chosen monitor? 100Hz is confirmed by research.
- Does switching the display mode through SDL under KMS change the HDMI output, keep the renderer at the new size and keep present blocking on vsync? Untested on the Pi 5.
- Does the hotspot work on the Pi 5's Wi-Fi with the settings chosen (WPA2, CCMP), and does switching between hotspot and a Wi-Fi network, and reverting, behave under NetworkManager? Untested on the Pi 5.
- Does the generator, started at boot as a service with no login, get the display and keyboard under KMS? Untested on the Pi 5.
- The proposed photodiode circuit is unbuilt. Confirm it on hardware using the checklist in the circuit section.
- Can the S5 be started and stopped over Wi-Fi with liblumix, or does the wired remote cable start video recording? Neither is confirmed for the S5.
- How should the Pico and Pi clocks be related for single mode timing?
