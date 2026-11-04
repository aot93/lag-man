"""Linear time code (SMPTE 12M LTC): the time code as an audio signal, for cameras and
recorders to jam to.

Each frame is an 80-bit word sent as biphase mark: the level changes at the start of every
bit, and a one also changes it half way through. LTC runs at 30fps at most, so 50 and 60fps
time code (and fast mode) is sent at half the rate, as most generators do. No drop frame:
fractional rates are out of scope.
"""

from __future__ import annotations

SAMPLE_RATE = 48_000
SECONDS_PER_DAY = 86_400
SYNC_BITS = (0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1)
# Within this many samples of the target the stream runs on untouched; beyond it, it skips or
# repeats one sample per buffer (a rate change of a fraction of a percent, which LTC readers
# take in their stride) until it is back.
SLEW_SAMPLES = 48
# The bit that keeps every word's count of ones even, so each frame starts at the same level.
_POLARITY_BIT = {25: 59, 24: 27, 30: 27}


def ltc_fps(tc_rate: int) -> int:
    """The LTC frame rate for a time code rate: halved until it is 30fps or less."""
    fps = tc_rate
    while fps > 30:
        fps //= 2
    return fps


def frame_timecode(frame: int, fps: int) -> tuple[int, int, int, int]:
    """Hours, minutes, seconds and frames for a frame number. Wraps at midnight."""
    seconds, f = divmod(frame % (SECONDS_PER_DAY * fps), fps)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return h, m, s, f


def frame_bits(hours: int, minutes: int, seconds: int, frames: int, fps: int) -> list[int]:
    """The 80 bits of one LTC word, first sent first. User bits and flags are zero."""
    bits = [0] * 80

    def put(start: int, width: int, value: int) -> None:
        for i in range(width):
            bits[start + i] = (value >> i) & 1

    put(0, 4, frames % 10)
    put(8, 2, frames // 10)
    put(16, 4, seconds % 10)
    put(24, 3, seconds // 10)
    put(32, 4, minutes % 10)
    put(40, 3, minutes // 10)
    put(48, 4, hours % 10)
    put(56, 2, hours // 10)
    bits[64:] = SYNC_BITS
    if sum(bits) % 2:
        bits[_POLARITY_BIT[fps]] = 1
    return bits


class LtcStream:
    """Audio samples of LTC that follow a target position, without breaks.

    ``fill`` is given where the time code should be at its first sample, in frames. Within
    half a frame of that the stream carries on unbroken, slewing back towards it (the audio
    and display clocks drift apart only slowly); further off, it jumps there.
    """

    def __init__(self, sample_rate: int, fps: int, level_db: float = -18.0):
        if fps not in _POLARITY_BIT or sample_rate % (fps * 80):
            raise ValueError(f"LTC at {fps} fps needs a sample rate that is a multiple of "
                             f"{fps * 80}; got {sample_rate}")
        self.fps = fps
        self.spf = sample_rate // fps  # samples per frame
        self.spb = self.spf // 80  # samples per bit
        self._pos: int | None = None  # samples since midnight-equivalent zero
        self._wave: tuple[int, list[int]] | None = None
        self.set_level(level_db)

    def set_level(self, level_db: float) -> None:
        """Peak level in dB below full scale. Takes effect at the next frame."""
        self.amplitude = round(32767 * 10 ** (level_db / 20))
        self._wave = None

    def fill(self, n: int, target_frames: float | None) -> list[int]:
        """``n`` mono samples. None for the target means stopped: silence."""
        if target_frames is None:
            self._pos = None
            return [0] * n
        want = round(target_frames * self.spf)
        if self._pos is None or abs(self._pos - want) > self.spf // 2:
            self._pos = want
        if abs(want - self._pos) <= SLEW_SAMPLES:
            return self._emit(n)
        # Skip (or repeat) one sample, at the first place in this buffer clear of an edge.
        step = 1 if want > self._pos else -1
        at = next((k for k in range(1, n) if self._steady(self._pos + k)), None)
        if at is None:
            return self._emit(n)
        out = self._emit(at)
        self._pos += step
        return out + self._emit(n - at)

    def _emit(self, n: int) -> list[int]:
        out: list[int] = []
        while len(out) < n:
            frame, within = divmod(self._pos, self.spf)
            take = min(n - len(out), self.spf - within)
            out += self._frame_wave(frame)[within:within + take]
            self._pos += take
        return out

    def _steady(self, pos: int) -> bool:
        """Whether the samples either side of ``pos`` are clear of an edge, so one can be
        skipped or repeated there without moving an edge."""
        return all(self._frame_wave(p // self.spf)[p % self.spf] != 0
                   for p in (pos - 1, pos, pos + 1))

    def _frame_wave(self, frame: int) -> list[int]:
        if self._wave is not None and self._wave[0] == frame:
            return self._wave[1]
        half = self.spb // 2
        level = self.amplitude
        wave: list[int] = []
        for bit in frame_bits(*frame_timecode(frame, self.fps), self.fps):
            # A zero-level sample at each change softens the edge (LTC wants ~40us rises).
            level = -level
            wave += [0] + [level] * (half - 1)
            if bit:
                level = -level
                wave += [0] + [level] * (half - 1)
            else:
                wave += [level] * half
        self._wave = (frame, wave)
        return wave


def target_frames(count: int, t_shown: float, refresh_hz: int, t: float, fps: int) -> float:
    """Where the LTC should be at time ``t``, in LTC frames, given that ``count`` went on
    screen at ``t_shown``. Audio is generated ahead of the screen, so this looks forward
    from the last displayed frame at the refresh rate; the count itself is untouched."""
    return (count + (t - t_shown) * refresh_hz) * fps / refresh_hz
