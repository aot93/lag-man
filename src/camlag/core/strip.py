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


@dataclass(frozen=True)
class Decoded:
    status: DecodeStatus
    count: int | None = None
    # For TRANSITION: the two counts the frame lies between, in no particular order.
    candidates: tuple[int, ...] = ()
    reason: str = ""
    ambiguous: tuple[int, ...] = field(default=())
    reversed: bool = False  # the levels were given right to left


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
    for i, b in _REFS:
        if (norm[i] <= HIGH) if b else (norm[i] >= LOW):
            return Decoded(DecodeStatus.UNREADABLE, reason=_NO_REFS)

    data = norm[_DATA]
    ambiguous = tuple(i for i, v in enumerate(data) if LOW <= v <= HIGH)
    bits = [1 if v > 0.5 else 0 for v in data]

    if not ambiguous:
        if sum(bits) & 1:
            return Decoded(DecodeStatus.UNREADABLE, reason="parity error")
        return Decoded(DecodeStatus.OK, count=ungray(_bits_to_int(bits[:COUNT_BITS])))

    # One changing Gray bit also flips the parity cell, so a clean mid-change frame shows
    # one ambiguous data bit and possibly an ambiguous parity cell.
    data_amb = [i for i in ambiguous if i < COUNT_BITS]
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


def count_difference(a: int, b: int) -> int:
    """Signed ``a - b`` for two strip counts, allowing for wrap at 2**24."""
    d = (a - b) % COUNT_MODULUS
    return d - COUNT_MODULUS if d >= COUNT_MODULUS // 2 else d


def _bits_to_int(bits: Sequence[int]) -> int:
    n = 0
    for b in bits:
        n = (n << 1) | b
    return n
