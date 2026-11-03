from datetime import datetime, timedelta

from lagman.core.wallclock import WallClock


def make(start=datetime(2026, 1, 1, 0, 0, 0)):
    """A system clock that is wrong (no RTC) and a monotonic clock, both moved by hand."""
    t = {"sys": start, "mono": 100.0}
    clock = WallClock(now=lambda: t["sys"], monotonic=lambda: t["mono"])
    return clock, t


def advance(t, seconds):
    t["sys"] += timedelta(seconds=seconds)
    t["mono"] += seconds


def test_system_clock_until_synced():
    clock, t = make()
    assert clock.now() == t["sys"] and clock.source == "system"


def test_sync_takes_the_browser_time_and_keeps_counting():
    clock, t = make()
    clock.sync(datetime(2026, 11, 3, 14, 30, 0))
    assert clock.source == "browser"
    advance(t, 61.5)
    assert clock.now() == datetime(2026, 11, 3, 14, 31, 1, 500000)


def test_a_later_system_clock_step_does_not_move_a_synced_clock():
    clock, t = make()
    clock.sync(datetime(2026, 11, 3, 14, 30, 0))
    t["sys"] = datetime(2026, 11, 3, 14, 30, 0)  # NTP steps the system clock
    advance(t, 10)
    assert clock.now() == datetime(2026, 11, 3, 14, 30, 10)


def test_clear_goes_back_to_the_system_clock():
    clock, t = make()
    clock.sync(datetime(2026, 11, 3, 14, 30, 0))
    clock.clear()
    assert clock.now() == t["sys"] and clock.source == "system"


def test_offset_from_the_system_clock():
    clock, t = make(datetime(2026, 11, 3, 14, 29, 57))
    clock.sync(datetime(2026, 11, 3, 14, 30, 0))
    assert round(clock.offset_s(), 3) == 3.0
