import array

import pytest

from lagman.core import ltc
from lagman.core.config import LtcConfig
from lagman.generator.ltc_audio import CHUNK, LtcWriter, parse_devices, underrun


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.written = bytearray()
        self.stdin = self
        self.returncode = None

    def write(self, data):
        if self.returncode is not None:
            raise BrokenPipeError
        self.written += data

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def stderr_text(self):
        return "aplay: main:850: audio open error: No such file or directory"


@pytest.fixture
def out():
    procs = []
    now = [100.0]

    def spawn(args):
        procs.append(FakeProc(args))
        return procs[-1]

    o = LtcWriter(spawn=spawn, clock=lambda: now[0], threaded=False,
                  have_aplay=lambda: True)
    return o, procs, now


def samples(proc):
    return array.array("h", bytes(proc.written))


def test_off_until_enabled(out):
    o, procs, _ = out
    o.configure(LtcConfig(), 25)
    assert not procs and o.status()["state"] == "off"


def test_sends_ltc_on_the_chosen_channel(out):
    o, procs, now = out
    o.configure(LtcConfig(enabled=True, device="hw:1", channel="left", level_db=-12), 25)
    assert procs[0].args[:2] == ["aplay", "-D"] and "hw:1" in procs[0].args
    o.anchor(400 * 12, now[0], True, 100)  # 00:00:48:00
    for _ in range(int(3 * 1920 / CHUNK) + 2):
        o.pump()
    s = samples(procs[0])
    left, right = s[0::2], s[1::2]
    assert set(right) == {0} and max(left) == round(32767 * 10 ** (-12 / 20))
    assert o.status()["state"] == "playing" and o.status()["fps"] == 25


def test_silent_while_the_clock_is_stopped(out):
    o, procs, now = out
    o.configure(LtcConfig(enabled=True), 25)
    o.anchor(1000, now[0], False, 100)
    o.pump()
    assert set(samples(procs[0])) == {0}


def test_level_and_offset_change_without_a_restart(out):
    o, procs, now = out
    o.configure(LtcConfig(enabled=True), 25)
    o.configure(LtcConfig(enabled=True, level_db=-6, offset_ms=20), 25)
    assert len(procs) == 1
    o.configure(LtcConfig(enabled=True), 30)  # a new rate restarts the output
    assert len(procs) == 2 and procs[0].returncode is not None


def test_target_allows_for_the_output_delay_and_offset(out):
    o, _, now = out
    o.configure(LtcConfig(enabled=True, offset_ms=10), 25)
    o.anchor(400, now[0], True, 100)
    expected = ltc.target_frames(400, now[0], 100, now[0] + o.latency_s + 0.010, 25)
    assert o.target() == pytest.approx(expected)


def test_a_failed_device_is_reported(out):
    o, procs, now = out
    o.configure(LtcConfig(enabled=True, device="hw:9"), 25)
    procs[0].returncode = 1
    o.pump()
    st = o.status()
    assert st["state"] == "error" and "hw:9" in st["detail"]


def test_no_aplay():
    o = LtcWriter(spawn=None, threaded=False, have_aplay=lambda: False)
    o.configure(LtcConfig(enabled=True), 25)
    assert o.status()["state"] == "error" and "alsa-utils" in o.status()["detail"]


def test_parse_devices():
    text = ("null\n    Discard all samples (playback) or generate zero samples (capture)\n"
            "default\n    Default ALSA output\n"
            "hdmi:CARD=vc4hdmi0,DEV=0\n    vc4-hdmi-0, MAI PCM i2s-hifi-0\n"
            "    HDMI Audio Output\n"
            "plughw:CARD=Device,DEV=0\n    USB Audio Device, USB Audio\n")
    assert parse_devices(text) == [
        {"name": "default", "description": "Default ALSA output"},
        {"name": "hdmi:CARD=vc4hdmi0,DEV=0", "description": "vc4-hdmi-0, MAI PCM i2s-hifi-0"},
        {"name": "plughw:CARD=Device,DEV=0", "description": "USB Audio Device, USB Audio"},
    ]


def test_underruns_are_counted_from_aplay():
    assert underrun("underrun!!! (at least 12.345 ms long)")
    assert not underrun("Playing raw data 'stdin' : Signed 16 bit Little Endian, Rate 48000 Hz")


def test_worker_process_sends_ltc(tmp_path, monkeypatch):
    """The real worker process, writing to a stand-in aplay that keeps what it is sent."""
    import os
    import sys
    import time

    from lagman.generator.ltc_audio import LtcProcess

    fake = tmp_path / "aplay"
    fake.write_text(f"""#!{sys.executable}
import sys, time
out = open({str(tmp_path / "out.raw")!r}, "wb")
start, got = time.perf_counter(), 0
print("underrun!!! (at least 1.000 ms long)", file=sys.stderr, flush=True)
while data := sys.stdin.buffer.read(1920):
    out.write(data); out.flush(); got += len(data) // 4
    time.sleep(max(0, start + got / 48000 - 0.1 - time.perf_counter()))
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    proc = LtcProcess()
    try:
        proc.configure(LtcConfig(enabled=True), 25)
        t0 = time.perf_counter()
        n = 0
        while time.perf_counter() < t0 + 1.5:
            proc.anchor(n, time.perf_counter(), True, 100)
            n += 1
            time.sleep(0.01)
        st = proc.status()
        assert st["state"] == "playing", st
        assert st["fps"] == 25 and st["underruns"] == 1
    finally:
        proc.close()
    data = array.array("h", (tmp_path / "out.raw").read_bytes())
    assert len(data) > 2 * 48000 // 2  # at least half a second of stereo
    assert max(data[0::2]) > 1000  # LTC, not silence
    st = proc.status()
    assert st["state"] == "off"
