"""LTC out of an ALSA audio output, through ``aplay``.

The samples go to an ``aplay`` process rather than through SDL: pygame-ce runs SDL's audio
callback in Python, and pausing or closing that device while the render loop holds the GIL
deadlocks. A pipe also paces the writer for free: a write blocks until ALSA has room.

The engine reports each present (which count went on screen, and when); the writer works out
where the time code will be when the samples it is about to write reach the output, and the
stream follows that without breaks (``core.ltc``).
"""

from __future__ import annotations

import array
import fcntl
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from ..core.config import LtcConfig
from ..core.ltc import SAMPLE_RATE, LtcStream, target_frames

CHUNK = 480  # samples per write: 10ms
BUFFER_US, PERIOD_US = 40_000, 10_000  # ALSA buffer and period asked of aplay
PIPE_BYTES = 4096  # keep the pipe short, so little audio waits between us and ALSA
RETRY_S = 3.0
TRACK = 0.02  # how fast the play-time estimate follows what blocked writes say
_F_SETPIPE_SZ = 1031  # Linux
_SKIP = ("null", "dmix", "dsnoop", "surround", "front", "iec958", "sysdefault", "usbstream",
         "lavrate", "samplerate", "speexrate", "upmix", "vdownmix")


class AplayProcess:
    """``aplay`` reading raw 48kHz 16-bit stereo from a pipe."""

    def __init__(self, args: list[str]):
        self.args = args
        self._p = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.stdin = self._p.stdin
        try:
            fcntl.fcntl(self.stdin.fileno(), _F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            pass

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
        if self._p.poll() is None or self._p.stderr is None:
            return ""
        return self._p.stderr.read().decode(errors="replace").strip()


def _have_aplay() -> bool:
    return shutil.which("aplay") is not None


def aplay_args(device: str) -> list[str]:
    return ["aplay", "-q", "-D", device or "default", "-t", "raw", "-f", "S16_LE",
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


class LtcOutput:
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
        with self._lock:
            return {**self._state, "fps": self._fps if self._cfg.enabled else None,
                    "device": self._cfg.device or "default"}

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
        if self._thread is not None:
            self._thread.join(1)
            self._thread = None

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = {"state": state, "detail": detail}
