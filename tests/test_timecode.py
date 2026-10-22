import pytest

from camlag.core.rates import RATE_SETS, get_rate_set
from camlag.core.timecode import (
    count_from_seconds,
    count_to_ms,
    parse_timecode,
    ticks_per_day,
    to_timecode,
)


def test_rate_sets_match_spec():
    eu, us = get_rate_set("50"), get_rate_set("60")
    assert (eu.refresh_hz, eu.timecode_rates, eu.tick_ms) == (100, (25, 50, 100), 10.0)
    assert (us.refresh_hz, us.timecode_rates) == (120, (30, 60, 120))
    with pytest.raises(ValueError):
        get_rate_set("48")


@pytest.mark.parametrize("count,tc_rate,expected", [
    (0, 25, "00:00:00:00.000"),
    (1, 25, "00:00:00:00.010"),
    (4, 25, "00:00:00:01.040"),
    (99, 25, "00:00:00:24.990"),
    (100, 25, "00:00:01:00.000"),
    (99, 100, "00:00:00:99.990"),
    (360_000 + 6_000 + 100 + 37, 50, "01:01:01:18.370"),
])
def test_to_timecode_100hz(count, tc_rate, expected):
    tc = to_timecode(count, 100, tc_rate)
    assert tc.hmsf() + tc.ms() == expected


def test_to_timecode_120hz_millis_floor():
    assert [to_timecode(n, 120, 120).millis for n in range(4)] == [0, 8, 16, 25]
    assert to_timecode(119, 120, 30).frames == 29


def test_hours_wrap_at_24():
    assert to_timecode(ticks_per_day(100) + 5, 100, 100).hmsf() == "00:00:00:05"


def test_tc_rate_must_divide_refresh():
    with pytest.raises(ValueError):
        to_timecode(0, 100, 30)


@pytest.mark.parametrize("rs", RATE_SETS.values())
def test_parse_round_trip(rs):
    for tc_rate in rs.timecode_rates:
        for count in (0, 1234, 4_000_000, ticks_per_day(rs.refresh_hz) - 1):
            step = rs.refresh_hz // tc_rate
            aligned = count - count % step
            text = to_timecode(aligned, rs.refresh_hz, tc_rate).hmsf()
            assert parse_timecode(text, rs.refresh_hz, tc_rate) == aligned


@pytest.mark.parametrize("bad", ["", "1:2:3", "24:00:00:00", "00:60:00:00", "00:00:00:25"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_timecode(bad, 100, 25)


def test_time_of_day_and_ms():
    assert count_from_seconds(3600.5, 100) == 360_050
    assert count_to_ms(3, 120) == pytest.approx(25.0)
