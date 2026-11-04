import array

import pytest

from lagman.core import ltc
from lagman.core.config import LtcConfig
from lagman.generator.ltc_audio import CHUNK, LtcOutput, parse_devices


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

    o = LtcOutput(spawn=spawn, clock=lambda: now[0], threaded=False,
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
    assert procs[0].args[:3] == ["aplay", "-q", "-D"] and "hw:1" in procs[0].args
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
    o = LtcOutput(spawn=None, threaded=False, have_aplay=lambda: False)
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
