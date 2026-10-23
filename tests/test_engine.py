import threading
from datetime import datetime

import pytest

from lagman.core.runlog import read_run_log
from lagman.generator.engine import IDLE, PRE_ROLL, RUNNING, CommandError, Engine

P = 10_000_000


class Loop:
    """Drives an engine like the render loop, with simulated present timestamps."""

    def __init__(self, engine):
        self.engine, self.t, self.mono = engine, 0, 0.0

    def frames(self, n, gap=P):
        for _ in range(n):
            self.engine.frame()
            self.t += gap
            self.mono += gap / 1e9
            self.engine.presented(self.t)


def make(tmp_path, cfg=None):
    from lagman.core.config import GeneratorConfig
    holder = {}
    eng = Engine(cfg or GeneratorConfig(), 1920, 1080, tmp_path / "runs",
                 now=lambda: datetime(2026, 10, 22, 12, 0, 0),
                 monotonic=lambda: holder["loop"].mono)
    holder["loop"] = Loop(eng)
    return eng, holder["loop"]


def call(engine, loop, name, **args):
    """Submit from another thread while this thread runs frames, as in production."""
    out = {}

    def worker():
        try:
            out["result"] = engine.submit(name, **args)
        except Exception as e:
            out["error"] = e

    th = threading.Thread(target=worker)
    th.start()
    while th.is_alive():
        loop.frames(1)
        th.join(0.001)
    if "error" in out:
        raise out["error"]
    return out.get("result")


def test_run_counts_frames_and_logs(tmp_path):
    eng, loop = make(tmp_path)
    loop.frames(5)
    assert eng.status()["count"] == 0
    call(eng, loop, "start")
    assert eng.state == RUNNING
    before = eng.clock.count
    loop.frames(100)
    assert eng.clock.count - before == 100
    call(eng, loop, "stop")
    assert eng.state == IDLE and eng.run_number == 2
    records = read_run_log(tmp_path / "runs" / f"{eng.status()['run_id']}.jsonl")
    assert records[0]["event"] == "run_start" and records[0]["refresh_hz"] == 100
    assert records[-1]["event"] == "run_stop"
    # run number persists across restarts
    eng2, _ = make(tmp_path)
    assert eng2.run_number == 2


def test_repeat_is_logged_and_flagged(tmp_path):
    eng, loop = make(tmp_path)
    call(eng, loop, "start")
    loop.frames(30)
    loop.frames(1, gap=2 * P)
    loop.frames(10)
    st = eng.status()
    assert st["repeated_refreshes"] == 1 and st["unacked_frame_warnings"] == 1
    assert st["recent_events"][0]["kind"] == "repeat"
    call(eng, loop, "ack_warnings")
    assert eng.status()["unacked_frame_warnings"] == 0
    call(eng, loop, "stop")
    records = read_run_log(tmp_path / "runs" / f"{eng.status()['run_id']}.jsonl")
    frames = [r for r in records if r["event"] == "frame"]
    assert len(frames) == 1 and frames[0]["kind"] == "repeat"


def test_pre_roll_shows_slate_then_counts(tmp_path):
    eng, loop = make(tmp_path)
    cfg = eng.cfg.to_dict()
    cfg["slate"].update(mode="pre_roll", pre_roll_s=0.5)
    call(eng, loop, "set_config", config=cfg)
    call(eng, loop, "start")
    assert eng.state == PRE_ROLL
    loop.frames(20)
    assert eng.clock.count == 0
    assert any(op["type"] == "text" and op["text"].startswith("RUN")
               for op in eng.preview()["ops"])
    loop.frames(40)
    assert eng.state == RUNNING and eng.clock.count > 0
    assert not any(op["type"] == "text" and op["text"].startswith("RUN")
                   for op in eng.preview()["ops"])


def test_config_rules(tmp_path):
    eng, loop = make(tmp_path)
    with pytest.raises(CommandError):
        call(eng, loop, "set_config", config={"tc_rate": 7})
    call(eng, loop, "start")
    with pytest.raises(CommandError):
        call(eng, loop, "set_config", config={"rate_set": "60", "tc_rate": 30})
    # the time code rate can switch mid-run
    call(eng, loop, "set_config", config={"tc_rate": 50})
    assert eng.status()["tc_rate"] == 50
    with pytest.raises(CommandError):
        call(eng, loop, "start")
    call(eng, loop, "stop")
    call(eng, loop, "set_config", config={"rate_set": "60", "tc_rate": 120})
    assert eng.monitor.nominal_hz == 120


def test_set_start_value(tmp_path):
    eng, loop = make(tmp_path)
    call(eng, loop, "set_start_value", value="10:00:00:00")
    loop.frames(1)
    assert eng.status()["timecode"] == "10:00:00:00.000"
    with pytest.raises(CommandError):
        call(eng, loop, "set_start_value", value="nonsense")


def test_unknown_command(tmp_path):
    eng, loop = make(tmp_path)
    with pytest.raises(CommandError):
        call(eng, loop, "explode")


def test_preview_ops_serialisable(tmp_path):
    eng, loop = make(tmp_path)
    loop.frames(1)
    pv = eng.preview()
    assert pv["width"] == 1920 and pv["bg"] == "#000000"
    kinds = {op["type"] for op in pv["ops"]}
    assert kinds == {"fill", "text"}
