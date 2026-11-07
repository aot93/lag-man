"""LTC out of an ALSA audio output, through ``aplay``, from a worker process.

The writer runs in its own process (``LtcProcess`` starts it; ``python -m`` this module). In
the generator's process it would need the GIL for every buffer, and the render loop holds the
GIL for most of each frame (a vsynced present does not release it), so the audio ran dry.
The generator sends the worker each present (which count went on screen, and when) down a
pipe that never blocks it.

The samples go to ``aplay`` rather than through SDL: pygame-ce runs SDL's audio callback in
Python, and pausing or closing that device while another thread holds the GIL deadlocks. A
pipe also paces the writer for free: a write blocks until ALSA has room.

The writer works out where the time code will be when the samples it is about to write reach
the output, and the stream follows that without breaks (``core.ltc``).
"""

from __future__ import annotations

import array
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..core.config import LtcConfig
from ..core.ltc import SAMPLE_RATE, LtcStream, target_frames

CHUNK = 480  # samples per write: 10ms
# ALSA buffer and period asked of aplay. A long buffer rides out a busy moment; the timing
# allows for it, so the LTC still lines up with the picture.
BUFFER_US, PERIOD_US = 100_000, 20_000
PIPE_BYTES = 4096  # keep the pipe short, so little audio waits between us and ALSA
RETRY_S = 3.0
TRACK = 0.02  # how fast the play-time estimate follows what blocked writes say
_F_SETPIPE_SZ = 1031  # Linux
_SKIP = ("null", "dmix", "dsnoop", "surround", "front", "iec958", "sysdefault", "usbstream",
         "lavrate", "samplerate", "speexrate", "upmix", "vdownmix")


def underrun(line: str) -> bool:
    """Whether a line of aplay's messages reports that the output ran dry."""
    return line.startswith("underrun!!!")


class AplayProcess:
    """``aplay`` reading raw 48kHz 16-bit stereo from a pipe. Its messages are read as they
    come, counting underruns (the output ran dry) and keeping the last few for errors."""

    def __init__(self, args: list[str]):
        self.args = args
        self._p = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.stdin = self._p.stdin
        self.underruns = 0
        self._tail: deque[str] = deque(maxlen=3)
        try:
            fcntl.fcntl(self.stdin.fileno(), _F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            pass
        self._reader = threading.Thread(target=self._read_messages, name="aplay-messages",
                                        daemon=True)
        self._reader.start()

    def _read_messages(self) -> None:
        for raw in self._p.stderr:
            line = raw.decode(errors="replace").strip()
            if underrun(line):
                self.underruns += 1
            elif line and not line.startswith("Playing "):
                self._tail.append(line)

    @property
    def returncode(self) -> int | None:
        return self._p.returncode

    def poll(self) -> int | None:
        return self._p.poll()

    def terminate(self) -> None:
        try:
            self.stdin.close()
        except OSError:
            pass  # a write was cut off: nothing left to flush
        if self._p.poll() is None:
            self._p.terminate()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._p.wait(timeout)
        except subprocess.TimeoutExpired:
            self._p.kill()
            return self._p.wait()

    def stderr_text(self) -> str:
        self._reader.join(1)
        return " ".join(self._tail)


def _have_aplay() -> bool:
    return shutil.which("aplay") is not None


def aplay_args(device: str) -> list[str]:
    # Not -q: that would hide the underrun messages.
    return ["aplay", "-D", device or "default", "-t", "raw", "-f", "S16_LE",
            "-r", str(SAMPLE_RATE), "-c", "2", f"--buffer-time={BUFFER_US}",
            f"--period-time={PERIOD_US}", "-"]


def parse_devices(text: str) -> list[dict[str, str]]:
    """Outputs from ``aplay -L``: a name, then its description indented under it."""
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        if line and not line[0].isspace():
            out.append({"name": line.strip(), "description": ""})
        elif out and not out[-1]["description"]:
            out[-1]["description"] = line.strip()
    return [d for d in out if not d["name"].split(":")[0].startswith(_SKIP)]


def list_devices() -> list[dict[str, str]]:
    if not _have_aplay():
        return []
    try:
        p = subprocess.run(["aplay", "-L"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_devices(p.stdout)


class LtcWriter:
    """Writes LTC to ``aplay``. Runs in the worker process (``worker_main``)."""

    def __init__(self, spawn: Callable[[list[str]], Any] | None = AplayProcess,
                 clock: Callable[[], float] = time.perf_counter, threaded: bool = True,
                 have_aplay: Callable[[], bool] = _have_aplay):
        self._spawn, self._clock, self._threaded = spawn, clock, threaded
        self._have_aplay = have_aplay
        self._lock = threading.Lock()
        self._anchor: tuple[int, float, bool, int] | None = None
        self._cfg = LtcConfig()
        self._fps: int | None = None
        self._stream: LtcStream | None = None
        self._proc = None
        self._state = {"state": "off", "detail": ""}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t_next: float | None = None  # when the next sample written will be heard
        self._underruns = 0  # from aplay processes already finished
        # From writing a sample to hearing it: the ALSA buffer, a period and the pipe.
        # TODO(verify) on the Pi 5 (HDMI and USB audio); ltc.offset_ms trims what is left.
        self.latency_s = (BUFFER_US + PERIOD_US) / 1e6 + PIPE_BYTES / 4 / SAMPLE_RATE

    # ---- render loop ------------------------------------------------------------------

    def configure(self, cfg: LtcConfig, fps: int) -> None:
        """Apply settings. A new output, rate or on/off restarts ``aplay``; level, channel
        and offset apply as they are."""
        with self._lock:
            restart = (cfg.enabled, cfg.device, fps) != (self._cfg.enabled, self._cfg.device,
                                                        self._fps)
            self._cfg, self._fps = cfg, fps
            if self._stream is not None and not restart:
                self._stream.set_level(cfg.level_db)
        if not restart:
            return
        self._halt()
        if not cfg.enabled:
            self._set_state("off")
            return
        if not self._have_aplay():
            self._set_state("error", "LTC needs aplay: install alsa-utils "
                                     "('sudo apt install alsa-utils').")
            return
        with self._lock:
            self._stream = LtcStream(SAMPLE_RATE, fps, cfg.level_db)
        self._set_state("starting")
        self._stop = threading.Event()
        if self._threaded:
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name="lagman-ltc", daemon=True)
            self._thread.start()
        else:
            self._open()

    def anchor(self, count: int, t_shown: float, running: bool, refresh_hz: int) -> None:
        """The count that went on screen at ``t_shown`` (perf_counter seconds)."""
        self._anchor = (count, t_shown, running, refresh_hz)

    def status(self) -> dict[str, Any]:
        proc = self._proc
        with self._lock:
            return {**self._state, "fps": self._fps if self._cfg.enabled else None,
                    "device": self._cfg.device or "default",
                    "underruns": self._underruns + getattr(proc, "underruns", 0)}

    def close(self) -> None:
        self._halt()

    # ---- writer -----------------------------------------------------------------------

    def target(self, t_play: float | None = None) -> float | None:
        """Where the time code is when a sample heard at ``t_play`` is heard."""
        a, fps = self._anchor, self._fps
        if a is None or not a[2] or fps is None:
            return None  # the clock is stopped: silence
        count, t_shown, _, hz = a
        if t_play is None:
            t_play = self._clock() + self.latency_s
        return target_frames(count, t_shown, hz, t_play + self._cfg.offset_ms / 1000, fps)

    def pump(self) -> bool:
        """Write one chunk. False when the output failed (``aplay`` exited)."""
        proc = self._proc
        if proc is None and not self._open():
            return False
        proc = self._proc
        if proc.poll() is not None:
            return self._failed(proc)
        with self._lock:
            stream, channel = self._stream, self._cfg.channel
        # Samples are heard one after another, so the next one's play time moves on by
        # exactly what was written. With nothing queued (at the start, or if the output ran
        # dry) it is about a period from now.
        now = self._clock()
        t_next = self._t_next
        if t_next is None or t_next < now:
            t_next = now + PERIOD_US / 1e6
        mono = stream.fill(CHUNK, self.target(t_next))
        frames = array.array("h", bytes(4 * CHUNK))
        if channel in ("both", "left"):
            frames[0::2] = array.array("h", mono)
        if channel in ("both", "right"):
            frames[1::2] = array.array("h", mono)
        try:
            proc.stdin.write(frames.tobytes())
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return self._failed(proc)
        after = self._clock()
        t_next += CHUNK / SAMPLE_RATE
        if after - now > 0.002:
            # The write waited, so the queue is full: the next sample is about a full
            # queue away. Follow that slowly, to track the audio clock without jitter.
            t_next += TRACK * (after + self.latency_s - t_next)
        self._t_next = t_next
        if self._state["state"] != "playing":
            self._set_state("playing")
        return True

    def _open(self) -> bool:
        with self._lock:
            device = self._cfg.device
        try:
            self._proc = self._spawn(aplay_args(device))
            return True
        except OSError as e:
            self._set_state("error", f"Could not start aplay: {e}.")
            return False

    def _failed(self, proc) -> bool:
        proc.wait(1)
        self._underruns += getattr(proc, "underruns", 0)
        if self._stop.is_set():
            return False  # stopped on purpose (new settings or exit), not a failure
        why = proc.stderr_text() or f"aplay exited with code {proc.returncode}"
        with self._lock:
            device = self._cfg.device or "the default output"
        self._set_state("error", f"Could not play to {device}: {why}")
        print(f"ltc: {self._state['detail']}", file=sys.stderr)
        self._proc = None
        self._t_next = None
        return False

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.pump():
                stop.wait(RETRY_S)  # a device unplugged or busy: try again

    def _halt(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        self._t_next = None
        if proc is not None:
            proc.terminate()  # unblocks a write in progress
            proc.wait(1)
            self._underruns += getattr(proc, "underruns", 0)
        if self._thread is not None:
            self._thread.join(1)
            self._thread = None

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = {"state": state, "detail": detail}


# ---- worker process ---------------------------------------------------------------------
#
# Lines from the generator: "c {json}" with {"cfg": LtcConfig fields, "fps": n}, and
# "a COUNT T_SHOWN RUNNING HZ" for each present. Lines back: the writer's status as JSON,
# when it changes and every couple of seconds.


def worker_main(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    writer = LtcWriter()
    done = threading.Event()

    def report() -> None:
        last, sent_at = None, 0.0
        while not done.wait(0.25):
            st = writer.status()
            if st != last or time.monotonic() - sent_at > 2:
                try:
                    stdout.write(json.dumps(st) + "\n")
                    stdout.flush()
                except (BrokenPipeError, OSError, ValueError):
                    return
                last, sent_at = st, time.monotonic()

    threading.Thread(target=report, name="ltc-status", daemon=True).start()
    try:
        for raw in stdin:
            line = raw.decode(errors="replace").strip()
            if line.startswith("a "):
                count, t_shown, running, hz = line[2:].split()
                writer.anchor(int(count), float(t_shown), running == "1", int(hz))
            elif line.startswith("c "):
                msg = json.loads(line[2:])
                writer.configure(LtcConfig(**msg["cfg"]), int(msg["fps"]))
    finally:  # the generator closed the pipe or exited
        done.set()
        writer.close()
    return 0


class LtcProcess:
    """The generator's side: starts the worker while LTC is on, and passes it settings and
    presents. Nothing here waits on the worker, so it never holds up the render loop."""

    def __init__(self, command: list[str] | None = None):
        self._command = command or [sys.executable, "-m", __name__]
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._status: dict[str, Any] = {"state": "off", "detail": ""}

    def configure(self, cfg: LtcConfig, fps: int) -> None:
        if not cfg.enabled:
            self._stop()
            self._set({"state": "off", "detail": ""})
            return
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        self._send("c " + json.dumps({"cfg": cfg.__dict__, "fps": fps}))

    def anchor(self, count: int, t_shown: float, running: bool, refresh_hz: int) -> None:
        if self._proc is not None:
            self._send(f"a {count} {t_shown:.6f} {int(running)} {refresh_hz}")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def close(self) -> None:
        self._stop()
        self._set({"state": "off", "detail": ""})

    def _start(self) -> None:
        self._set({"state": "starting", "detail": ""})
        try:
            proc = subprocess.Popen(self._command, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)
        except OSError as e:
            self._set({"state": "error", "detail": f"Could not start the LTC worker: {e}."})
            return
        os.set_blocking(proc.stdin.fileno(), False)
        self._proc = proc
        threading.Thread(target=self._read, args=(proc,), name="ltc-worker-status",
                         daemon=True).start()

    def _send(self, line: str) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            os.write(proc.stdin.fileno(), (line + "\n").encode())
        except BlockingIOError:
            pass  # the worker is behind; it only needs the latest present anyway
        except (BrokenPipeError, OSError, ValueError):
            pass  # it exited; the status reader reports it

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            try:
                status = json.loads(raw)
            except ValueError:
                continue
            if proc is self._proc:
                self._set(status)
        if proc.wait() != 0 and proc is self._proc:
            self._set({"state": "error", "detail": "The LTC worker stopped; it restarts "
                       "when the LTC settings change."})

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # the worker stops its output and exits
        except OSError:
            pass
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _set(self, status: dict[str, Any]) -> None:
        with self._lock:
            self._status = status


if __name__ == "__main__":
    sys.exit(worker_main())
