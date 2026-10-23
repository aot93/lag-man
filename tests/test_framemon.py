from lagman.core.framemon import EARLY, REPEAT, FrameMonitor

P = 10_000_000  # 100 Hz period in ns


def feed(mon, gaps):
    t, events = 0, []
    events += mon.observe(0, t)
    for i, g in enumerate(gaps, start=1):
        t += g
        events += mon.observe(i, t)
    return events + mon.flush()


def test_steady_one_hour_no_events():
    mon = FrameMonitor(100)
    assert feed(mon, [P] * 360_000) == []
    assert mon.frames == 360_001 and mon.repeats == 0
    assert abs(mon.measured_hz - 100) < 1e-6


def test_missed_vblank_reported_once():
    mon = FrameMonitor(100)
    events = feed(mon, [P] * 50 + [2 * P] + [P] * 50)
    assert [(e.kind, e.frame, e.refreshes) for e in events] == [(REPEAT, 51, 1)]
    assert mon.repeats == 1


def test_long_stall_counts_every_repeat():
    mon = FrameMonitor(100)
    events = feed(mon, [P] * 20 + [5 * P + 300_000] + [P] * 20)
    assert [(e.kind, e.refreshes) for e in events] == [(REPEAT, 4)]


def test_timestamp_jitter_pair_ignored():
    mon = FrameMonitor(100)
    # one timestamp taken a period late, the next on time
    assert feed(mon, [P] * 20 + [int(1.9 * P), int(0.1 * P)] + [P] * 20) == []


def test_miss_at_end_flushed():
    mon = FrameMonitor(100)
    assert [e.kind for e in feed(mon, [P] * 20 + [2 * P])] == [REPEAT]


def test_early_present_reported():
    mon = FrameMonitor(100)
    events = feed(mon, [P] * 20 + [P // 10] + [P] * 20)
    assert [e.kind for e in events] == [EARLY]


def test_tracks_off_nominal_display_without_false_events():
    mon = FrameMonitor(100)
    gap = int(P * 1.0004)  # 99.96 Hz panel
    assert feed(mon, [gap] * 360_000) == []
    assert not mon.rate_mismatch()


def test_wrong_refresh_detected():
    mon = FrameMonitor(100)
    feed(mon, [16_666_667] * 300)  # running at 60 Hz
    assert mon.rate_mismatch()
    assert abs(mon.measured_hz - 60) < 0.01
