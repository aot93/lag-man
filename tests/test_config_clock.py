import json
from datetime import datetime

import pytest

from lagman.core.clock import Clock
from lagman.core.config import GeneratorConfig
from lagman.core.runlog import RunLog, read_run_log
from lagman.core.slate import slate_lines


def test_config_json_round_trip():
    cfg = GeneratorConfig(rate_set="60", tc_rate=60)
    cfg.clock.cx = 0.3
    cfg.slate.mode = "always"
    cfg.slate.fields["camera_name"].value = "Cam A"
    again = GeneratorConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert again == cfg


def test_partial_config_fills_defaults():
    cfg = GeneratorConfig.from_dict({"clock": {"size": 0.2}, "slate": {"fields": {
        "notes": {"value": "hi"}}}})
    assert cfg.clock.size == 0.2 and cfg.clock.cx == 0.5
    assert cfg.slate.fields["notes"].value == "hi" and cfg.slate.fields["notes"].enabled
    assert "operator" in cfg.slate.fields


@pytest.mark.parametrize("bad", [
    {"rate_set": "48"},
    {"tc_rate": 30},
    {"rate_set": "60", "tc_rate": 25},
    {"clock": {"cx": 2}},
    {"clock": {"fg": "white"}},
    {"clock": {"size": "big"}},
    {"clock": {"strip": 1}},
    {"bogus": 1},
    {"slate": {"position": "middle"}},
    {"slate": {"fields": {"shoe_size": {}}}},
])
def test_config_rejects(bad):
    with pytest.raises(ValueError):
        GeneratorConfig.from_dict(bad)


def test_clock_counts_only_displayed_frames_while_running():
    clock = Clock(GeneratorConfig())
    clock.frame_displayed()
    assert clock.count == 0
    clock.start()
    for _ in range(360_000):
        clock.frame_displayed()
    assert clock.count == 360_000
    clock.stop()
    clock.frame_displayed()
    assert clock.count == 360_000
    clock.reset()
    assert clock.count == 0


def test_clock_start_modes():
    cfg = GeneratorConfig(start_mode="value", start_value="01:00:00:10")
    assert Clock(cfg).count == 360_000 + 40
    cfg = GeneratorConfig(start_mode="time_of_day")
    clock = Clock(cfg, now=lambda: datetime(2026, 10, 22, 13, 30, 15, 250_000))
    assert clock.count == (13 * 3600 + 30 * 60 + 15) * 100 + 25


def test_slate_lines():
    cfg = GeneratorConfig()
    cfg.slate.fields["camera_name"].value = "Cam\nA"
    cfg.slate.fields["operator"].enabled = False
    lines = slate_lines(cfg, 7, datetime(2026, 10, 22, 9, 5, 0))
    text = "\n".join(lines)
    assert "CAMERA    Cam A" in text
    assert "RUN       7" in text
    assert "2026-10-22 09:05:00" in text
    assert "OPERATOR" not in text and "TEST" not in text  # disabled / blank
    assert lines[-1].endswith("25 fps TC / 100 Hz")


def test_run_log(tmp_path):
    log = RunLog(tmp_path / "runs", "r1")
    log.write("run_start", start_count=0)
    log.write("frame", kind="repeat", frame=5)
    log.close()
    records = read_run_log(log.path)
    assert [r["event"] for r in records] == ["run_start", "frame"]
    assert records[1]["frame"] == 5 and "t" in records[0]
