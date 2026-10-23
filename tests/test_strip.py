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
