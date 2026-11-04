import itertools
import random

import pytest

from lagman.core import ltc

SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]


def field(bits, start, n):
    return sum(bits[start + i] << i for i in range(n))


def read_bits(bits):
    """The time code in an 80-bit LTC word (SMPTE 12M), read independently of the encoder."""
    return (field(bits, 48, 4) + 10 * field(bits, 56, 2),
            field(bits, 32, 4) + 10 * field(bits, 40, 3),
            field(bits, 16, 4) + 10 * field(bits, 24, 3),
            field(bits, 0, 4) + 10 * field(bits, 8, 2))


@pytest.mark.parametrize("fps, polarity_bit", [(25, 59), (30, 27)])
def test_frame_bits(fps, polarity_bit):
    for h, m, s, f in ((0, 0, 0, 0), (12, 34, 56, 12), (23, 59, 59, fps - 1), (7, 8, 9, 3)):
        bits = ltc.frame_bits(h, m, s, f, fps)
        assert len(bits) == 80
        assert read_bits(bits) == (h, m, s, f)
        assert bits[64:] == SYNC
        assert sum(bits) % 2 == 0  # polarity correction: same phase at every frame start
        assert bits[10] == 0  # not drop frame
    flags = {27, 43, 59} - {polarity_bit}  # binary group flags: unused, always zero
    assert all(ltc.frame_bits(h, 1, 2, 3, fps)[i] == 0 for h in range(24) for i in flags)
    assert any(ltc.frame_bits(h, 1, 2, 3, fps)[polarity_bit] for h in range(24))


def test_ltc_rate_halves_fast_time_code():
    assert [ltc.ltc_fps(r) for r in (25, 30, 50, 60, 100, 120)] == [25, 30, 25, 30, 25, 30]


def test_frame_time_code_wraps_at_midnight():
    assert ltc.frame_timecode(0, 25) == (0, 0, 0, 0)
    assert ltc.frame_timecode(25 * 3661 + 7, 25) == (1, 1, 1, 7)
    assert ltc.frame_timecode(25 * 86400 + 1, 25) == (0, 0, 0, 1)


def decode(samples, spb):
    """Biphase mark back to bits: a level change mid-bit is a one."""
    sign = [1 if x > 0 else -1 if x < 0 else 0 for x in samples]
    bits = []
    for start in range(0, len(sign) - spb + 1, spb):
        first = sign[start + spb // 4]
        second = sign[start + 3 * spb // 4]
        bits.append(int(first != second))
    return bits


@pytest.mark.parametrize("fps", [25, 30])
def test_waveform_decodes_and_starts_each_frame_in_phase(fps):
    stream = ltc.LtcStream(48_000, fps, level_db=-6)
    spf = 48_000 // fps
    frame = fps * (12 * 3600 + 34 * 60 + 56) + 12
    samples = stream.fill(3 * spf, frame)
    assert len(samples) == 3 * spf
    spb = spf // 80
    for i in range(3):
        bits = decode(samples[i * spf:(i + 1) * spf], spb)
        assert read_bits(bits) == ltc.frame_timecode(frame + i, fps)
        # Each bit starts with a transition; the frame starts at the same level each time.
        assert samples[i * spf + 2] < 0
    peak = max(samples)
    assert abs(peak - round(32767 * 10 ** (-6 / 20))) <= 1


def test_stream_is_continuous_across_buffers():
    whole = ltc.LtcStream(48_000, 25).fill(5 * 1920, 1000)
    parts = ltc.LtcStream(48_000, 25)
    out, pos = [], 1000 * 1920
    rnd = random.Random(1)
    while len(out) < len(whole):
        n = min(rnd.randint(1, 700), len(whole) - len(out))
        # The target drifts a little (audio and display clocks), within half a frame.
        jitter = rnd.randint(-40, 40) if out else 0
        out += parts.fill(n, (pos + jitter) / 1920)
        pos += n
    assert out == whole


def test_stream_slews_back_to_a_target_a_little_off():
    s = ltc.LtcStream(48_000, 25)
    s.fill(480, 1000)
    pos = 1000 * 1920 + 480
    out = []
    for i in range(1400):  # the target is 300 samples later, then 300 earlier
        before = s._pos
        out += s.fill(480, (pos + (300 if i < 700 else -300)) / 1920)
        assert s._pos - before in (479, 480, 481)  # one sample at a time, never a jump
        pos += 480
    assert abs(s._pos - (pos - 300)) <= ltc.SLEW_SAMPLES
    # A skipped or repeated sample is never an edge's, so every edge stays where a reader
    # looks for it: half a bit (12 samples) or a whole bit apart, give or take one.
    edges = [i for i, v in enumerate(out) if v == 0]
    assert {b - a for a, b in itertools.pairwise(edges)} <= {11, 12, 13, 23, 24, 25}


def test_stream_jumps_to_a_target_far_away_and_is_silent_when_stopped():
    s = ltc.LtcStream(48_000, 25)
    s.fill(1920, 1000)
    jumped = s.fill(1920, 5000)
    assert read_bits(decode(jumped, 24)) == ltc.frame_timecode(5000, 25)
    assert s.fill(480, None) == [0] * 480


def test_target_looks_forward_from_the_last_displayed_frame():
    # Count 400 at 100Hz is 25fps frame 100; 80ms later the screen will show count 408.
    assert ltc.target_frames(400, 10.0, 100, 10.08, 25) == pytest.approx(102.0)
    assert ltc.target_frames(480, 0.0, 120, 0.0, 30) == pytest.approx(120.0)
