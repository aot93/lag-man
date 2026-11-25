"""The machine-readable code strip (format v2, documented in docs/strip-format.md).

One row of equal cells, left to right:

    W B | g23 g22 ... g0 | P | B W W

W and B are white and black reference cells, g23..g0 is the count (mod 2**24) in Gray code,
most significant bit first, and P makes the number of ones in g23..g0 plus P even. Gray code
means one tick changes exactly one cell, so a frame caught mid-change shows at most one
intermediate cell, which the decoder flags instead of guessing. The references are
asymmetric, so a strip read right to left (a region marked backwards, or a mirror) is
recognised and decoded the right way round rather than misread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

FORMAT_VERSION = 2
COUNT_BITS = 24
COUNT_MODULUS = 1 << COUNT_BITS
LEAD = (1, 0)
TRAIL = (0, 1, 1)
CELLS = len(LEAD) + COUNT_BITS + 1 + len(TRAIL)
_DATA = slice(len(LEAD), len(LEAD) + COUNT_BITS + 1)  # Gray bits then parity
_REFS = [*((i, b) for i, b in enumerate(LEAD)),
         *((CELLS - len(TRAIL) + i, b) for i, b in enumerate(TRAIL))]

# Normalised level bands: below LOW is black, above HIGH is white, between is ambiguous.
LOW = 0.3
HIGH = 0.7
NEAR_MID = 0.35  # a data cell this close to 0.5 can be the changing bit (see _decode)
# Minimum white-minus-black reference contrast, as a fraction of full scale.
MIN_CONTRAST = 0.15


def gray(n: int) -> int:
    return n ^ (n >> 1)


def ungray(g: int) -> int:
    n = g
    shift = 1
    while (g >> shift) and shift < 64:
        n ^= g >> shift
        shift += 1
    return n


def encode(count: int) -> list[int]:
    """Cell values (1 white, 0 black) for a count."""
    g = gray(count % COUNT_MODULUS)
    bits = [(g >> i) & 1 for i in range(COUNT_BITS - 1, -1, -1)]
    parity = sum(bits) & 1
    return [*LEAD, *bits, parity, *TRAIL]


class DecodeStatus(StrEnum):
    OK = "ok"
    TRANSITION = "transition"  # exactly one data cell between levels: caught mid-change
    UNREADABLE = "unreadable"  # low contrast, several ambiguous cells, or a parity failure
    BLEND = "blend"  # from decode_blend: an exposure that saw ``span`` counts in a row


@dataclass(frozen=True)
class Decoded:
    status: DecodeStatus
    count: int | None = None
    # For TRANSITION: the two counts the frame lies between, in no particular order.
    candidates: tuple[int, ...] = ()
    reason: str = ""
    ambiguous: tuple[int, ...] = field(default=())
    reversed: bool = False  # the levels were given right to left
    span: int = 1  # for BLEND: the frame saw counts ``count`` to ``count + span - 1``

    @property
    def centre(self) -> float | None:
        """The middle of what the frame saw: the count, or the middle of a blend."""
        return None if self.count is None else self.count + (self.span - 1) / 2


def decode(levels: Sequence[float], full_scale: float = 255.0) -> Decoded:
    """Decode measured cell levels (for example mean luma per cell), in either direction."""
    if len(levels) != CELLS:
        raise ValueError(f"expected {CELLS} cell levels, got {len(levels)}")
    result = _decode(levels, full_scale)
    if result.reason in _REF_FAILURES:
        flipped = _decode(levels[::-1], full_scale)
        if flipped.reason not in _REF_FAILURES:
            return Decoded(flipped.status, flipped.count, flipped.candidates, flipped.reason,
                           flipped.ambiguous, reversed=True)
    return result


_NO_CONTRAST = "reference contrast too low"
_NO_REFS = "reference cells not found"
_REF_FAILURES = (_NO_CONTRAST, _NO_REFS)


def _decode(levels: Sequence[float], full_scale: float) -> Decoded:
    whites = [levels[i] for i, b in _REFS if b]
    blacks = [levels[i] for i, b in _REFS if not b]
    white, black = sum(whites) / len(whites), sum(blacks) / len(blacks)
    span = white - black
    if span < MIN_CONTRAST * full_scale:
        return Decoded(DecodeStatus.UNREADABLE, reason=_NO_CONTRAST)

    norm = [(v - black) / span for v in levels]
    # The references only have to be on their side of the middle: enough to show the strip
    # is there and which way round it is. On small cells blur lifts one black reference
    # (whites on both sides) far more than the other, taking it past the threshold.
    for i, b in _REFS:
        if (norm[i] <= 0.5) if b else (norm[i] >= 0.5):
            return Decoded(DecodeStatus.UNREADABLE, reason=_NO_REFS)

    data = norm[_DATA]
    ambiguous = tuple(i for i, v in enumerate(data) if LOW <= v <= HIGH)
    bits = [1 if v > 0.5 else 0 for v in data]

    if not ambiguous:
        if sum(bits) & 1:
            return Decoded(DecodeStatus.UNREADABLE, reason="parity error")
        return Decoded(DecodeStatus.OK, count=ungray(_bits_to_int(bits[:COUNT_BITS])))

    # One changing Gray bit also flips the parity cell, so a clean mid-change frame shows
    # one ambiguous data bit and possibly an ambiguous parity cell. An exposure split
    # unevenly across the change can push the data bit just past a threshold while the
    # parity cell stays in between: then the changing bit is the data cell nearest the
    # middle, if one is reasonably near it.
    data_amb = [i for i in ambiguous if i < COUNT_BITS]
    if not data_amb and ambiguous == (COUNT_BITS,):
        nearest = min(range(COUNT_BITS), key=lambda j: abs(data[j] - 0.5))
        if abs(data[nearest] - 0.5) <= NEAR_MID:
            data_amb = [nearest]
    if len(data_amb) == 1:
        i = data_amb[0]
        gray_bits = bits[:COUNT_BITS]
        lo = [*gray_bits[:i], 0, *gray_bits[i + 1:]]
        hi = [*gray_bits[:i], 1, *gray_bits[i + 1:]]
        a, b = ungray(_bits_to_int(lo)), ungray(_bits_to_int(hi))
        if (a - b) % COUNT_MODULUS in (1, COUNT_MODULUS - 1):
            return Decoded(DecodeStatus.TRANSITION, candidates=(a, b),
                           reason="code caught mid-change", ambiguous=ambiguous)
    return Decoded(DecodeStatus.UNREADABLE, reason="ambiguous cells", ambiguous=ambiguous)


BLEND_SPAN = 8  # widest blend read, in counts


def decode_blend(levels: Sequence[float], lo: int, hi: int,
                 max_span: int = BLEND_SPAN) -> Decoded | None:
    """Read a frame that saw several counts in a row, starting between ``lo`` and ``hi``.

    A camera exposure longer than a tick, or a wall that blends frames, shows a mix of
    counts, and an over-exposed wall shows any cell lit for part of the exposure as white.
    Either way the cells that did not change during it still read as they were. This
    finds the shortest run of counts whose unchanging cells all read on their own side of
    the middle, with every in-between cell among the ones that changed. None if no run of
    up to ``max_span`` counts fits, or the runs that fit are too far apart to tell.
    """
    if len(levels) != CELLS:
        raise ValueError(f"expected {CELLS} cell levels, got {len(levels)}")
    norm, reversed_ = None, False
    for flipped in (False, True):
        lv = list(levels[::-1]) if flipped else list(levels)
        n = _normalise(lv)
        if n is not None:
            norm, reversed_ = n, flipped
            break
    if norm is None:
        return None
    width = COUNT_BITS + 1
    full = (1 << width) - 1
    ones = zeros = 0
    for v in norm[_DATA]:
        ones, zeros = (ones << 1) | (v > HIGH), (zeros << 1) | (v < LOW)
    pats = [_pattern(c) for c in range(lo, hi + max_span)]
    all_one, all_zero = list(pats), [full ^ p for p in pats]
    fits: list[tuple[float, int, int]] = []  # (fraction error, run length, first count)
    for k in range(1, max_span + 1):
        if k > 1:
            all_one = [all_one[i] & pats[i + k - 1] for i in range(len(all_one) - 1)]
            all_zero = [all_zero[i] & (full ^ pats[i + k - 1]) for i in range(len(all_zero) - 1)]
        # A cell that stays 1 (or 0) through the run must read clearly 1 (or 0).
        for i in range(min(len(all_one), hi - lo + 1)):
            if not all_one[i] & ~ones and not all_zero[i] & ~zeros:
                fits.append((_fraction_error(norm[_DATA], lo + i, k), k, lo + i))
    if not fits:
        return None
    # Several runs can fit side for side; how far each cell is lit tells them apart (a
    # third of the way for one count in three). Of those that fit about as well, the
    # shortest; where even that leaves a choice (a clipped picture), all of them.
    best = min(f[0] for f in fits)
    close = [f for f in fits if f[0] <= best + FRACTION_TOLERANCE]
    k = min(f[1] for f in close)
    starts = [f[2] for f in close if f[1] == k]
    first, last = min(starts), max(starts) + k - 1
    if last - first + 1 > max_span:
        return None
    return Decoded(DecodeStatus.BLEND, count=first % COUNT_MODULUS, span=last - first + 1,
                   reversed=reversed_, reason=f"blend of {last - first + 1} counts")


FRACTION_TOLERANCE = 0.2  # summed over the data cells: runs this close are equally likely


def _fraction_error(data: Sequence[float], first: int, k: int) -> float:
    """How far the data cells are from lit for the share of ``first``..``first+k-1`` that
    each is on in."""
    pats = [_pattern(c) for c in range(first, first + k)]
    n = len(data)
    return sum(abs(min(1.0, max(0.0, v)) - sum((p >> (n - 1 - j)) & 1 for p in pats) / k)
               for j, v in enumerate(data))


def rough_count(levels: Sequence[float]) -> int | None:
    """The count the data cells give each cut at the middle, parity unchecked: near a
    blend's counts (where to look with ``decode_blend``), not a reading. None if the
    reference cells are not there either way round."""
    for lv in (list(levels), list(levels[::-1])):
        norm = _normalise(lv)
        if norm is not None:
            return ungray(_bits_to_int([int(v > 0.5) for v in norm[_DATA][:COUNT_BITS]]))
    return None


def _normalise(levels: Sequence[float]) -> list[float] | None:
    """Levels on a 0 (black) to 1 (white) scale from the reference cells, or None if the
    references are not there this way round."""
    whites = [levels[i] for i, b in _REFS if b]
    blacks = [levels[i] for i, b in _REFS if not b]
    white, black = sum(whites) / len(whites), sum(blacks) / len(blacks)
    span = white - black
    if span <= 0:
        return None
    norm = [(v - black) / span for v in levels]
    if any((norm[i] <= 0.5) if b else (norm[i] >= 0.5) for i, b in _REFS):
        return None
    return norm


def _pattern(count: int) -> int:
    """The data cells (Gray bits, then parity) for a count, as an int, first cell highest."""
    g = gray(count % COUNT_MODULUS)
    return (g << 1) | (bin(g).count("1") & 1)


def count_difference(a: int, b: int) -> int:
    """Signed ``a - b`` for two strip counts, allowing for wrap at 2**24."""
    d = (a - b) % COUNT_MODULUS
    return d - COUNT_MODULUS if d >= COUNT_MODULUS // 2 else d


def _bits_to_int(bits: Sequence[int]) -> int:
    n = 0
    for b in bits:
        n = (n << 1) | b
    return n
