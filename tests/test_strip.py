import random

import pytest

from lagman.core import strip
from lagman.core.strip import DecodeStatus, count_difference, decode, encode, gray, ungray


def levels_for(count, white=230.0, black=12.0):
    return [white if b else black for b in encode(count)]


def test_gray_round_trip_and_single_bit_steps():
    for n in list(range(5000)) + [strip.COUNT_MODULUS - 2, strip.COUNT_MODULUS - 1]:
        assert ungray(gray(n)) == n
        nxt = (n + 1) % strip.COUNT_MODULUS
        assert bin(gray(n) ^ gray(nxt)).count("1") == 1


def test_encode_layout():
    cells = encode(0)
    assert len(cells) == strip.CELLS == 30
    assert cells[:2] == [1, 0] and cells[-3:] == [0, 1, 1]
    assert sum(cells[2:-3]) % 2 == 0


def test_reversed_strip_decodes_the_right_way_round():
    # a region marked right to left, or seen in a mirror
    for n in (0, 1, 777, 5_000_000):
        d = decode(levels_for(n)[::-1])
        assert d.status is DecodeStatus.OK and d.count == n and d.reversed
    assert not decode(levels_for(777)).reversed


def test_reversed_mid_change_flagged():
    a, b = levels_for(1023), levels_for(1024)
    d = decode([(x + y) / 2 for x, y in zip(a, b, strict=True)][::-1])
    assert d.status is DecodeStatus.TRANSITION and sorted(d.candidates) == [1023, 1024]


def test_one_tick_changes_two_cells_at_most():
    # one Gray bit plus the parity cell
    for n in range(2000):
        diff = sum(a != b for a, b in zip(encode(n), encode(n + 1), strict=True))
        assert diff == 2


def test_decode_round_trip_with_noise():
    rng = random.Random(1)
    for _ in range(500):
        n = rng.randrange(strip.COUNT_MODULUS)
        noisy = [v + rng.uniform(-20, 20) for v in levels_for(n)]
        d = decode(noisy)
        assert d.status is DecodeStatus.OK and d.count == n


def test_decode_inverted_offset_levels():
    # dim capture: white 90, black 40 still decodes
    assert decode(levels_for(777, 90, 40)).count == 777


def test_mid_change_is_flagged_not_guessed():
    n = 1023  # 1023 -> 1024 flips a high Gray bit
    a, b = levels_for(n), levels_for(n + 1)
    blended = [(x + y) / 2 for x, y in zip(a, b, strict=True)]
    d = decode(blended)
    assert d.status is DecodeStatus.TRANSITION
    assert d.count is None
    assert sorted(d.candidates) == [n, n + 1]


def test_uneven_mid_change_with_only_parity_ambiguous():
    # An exposure 70/30 across a change: the changing data cell lands just over the white
    # threshold while the parity cell is clearly in between. Still mid-change, not a read.
    n = 838
    a, b = levels_for(n), levels_for(n + 1)
    lv = [0.3 * x + 0.7 * y for x, y in zip(a, b, strict=True)]
    changing = next(i for i in range(2, 26) if a[i] != b[i])
    lv[changing] = a[changing] + 0.75 * (b[changing] - a[changing])
    d = decode(lv)
    assert d.status is DecodeStatus.TRANSITION and sorted(d.candidates) == [n, n + 1]


def test_parity_alone_ambiguous_without_a_near_mid_data_cell_is_unreadable():
    lv = levels_for(5000)
    lv[26] = 128  # nothing else near the middle: not a change in progress
    assert decode(lv).status is DecodeStatus.UNREADABLE


def test_two_ambiguous_cells_unreadable():
    lv = levels_for(5000)
    lv[5] = lv[9] = 120
    assert decode(lv).status is DecodeStatus.UNREADABLE


def test_parity_error_unreadable():
    lv = levels_for(5000)
    lv[10] = 230 if lv[10] < 100 else 12
    d = decode(lv)
    assert d.status is DecodeStatus.UNREADABLE and "parity" in d.reason


def test_low_contrast_unreadable():
    assert decode(levels_for(5, 60, 40)).status is DecodeStatus.UNREADABLE


def test_wrong_length():
    with pytest.raises(ValueError):
        decode([0.0] * 10)


def test_count_difference_wraps():
    assert count_difference(5, 2) == 3
    assert count_difference(1, strip.COUNT_MODULUS - 2) == 3
    assert count_difference(2, 5) == -3


def test_blurred_reference_cells_still_decode():
    """Small cells on a soft picture: a black reference picks up light from the whites
    beside it, one far more than the other, and the lead white fades into the screen
    around it."""
    for count in (130, 2744, 5_000_000):
        lv = levels_for(count, white=200.0, black=10.0)
        lv[1], lv[-3] = 85.0, 30.0
        lv[0] = 150.0  # the lead white, at the strip's end, dimmed by the dark around it
        d = decode(lv)
        assert d.status is DecodeStatus.OK and d.count == count
        assert decode(lv[::-1]).count == count  # still tells which way round it is


def blended(counts, clip=False, white=200.0, black=10.0):
    """Levels for an exposure that saw ``counts`` for equal times; ``clip`` saturates any
    cell lit for part of it (an over-exposed LED wall)."""
    cells = [sum(encode(c)[i] for c in counts) / len(counts) for i in range(strip.CELLS)]
    if clip:
        cells = [1.0 if v > 0 else 0.0 for v in cells]
    return [black + (white - black) * v for v in cells]


def test_blend_of_several_counts_reads_as_its_span():
    for counts in ([130, 131, 132], [2744, 2745], [999, 1000, 1001, 1002], [5_000_000]):
        d = strip.decode_blend(blended(counts), counts[0] - 150, counts[-1] + 5)
        assert d is not None and d.status is DecodeStatus.BLEND
        assert (d.count, d.count + d.span - 1) == (counts[0], counts[-1])
        assert d.centre == sum(counts) / len(counts)


def test_clipped_blend_reads_within_its_run():
    """Over-exposed, every cell lit for part of the exposure reads white: the OR of the
    codes, which can itself be another count's code (130 to 132 OR to 133's). So a clipped
    blend is read to within its own length, not exactly."""
    for counts in ([130, 131, 132], [2744, 2745, 2746], [999, 1000, 1001, 1002]):
        d = strip.decode_blend(blended(counts, clip=True), counts[0] - 150, counts[-1] + 5)
        assert d is not None
        assert abs(d.centre - sum(counts) / len(counts)) <= len(counts)


def test_blend_needs_the_counts_in_range_and_reads_either_way_round():
    lv = blended([130, 131, 132])
    assert strip.decode_blend(lv, 500, 700) is None
    d = strip.decode_blend(lv[::-1], 0, 200)
    assert d is not None and d.reversed and (d.count, d.span) == (130, 3)


def test_blend_longer_than_the_limit_is_not_read():
    lv = blended(list(range(100, 140)))
    assert strip.decode_blend(lv, 0, 200) is None
