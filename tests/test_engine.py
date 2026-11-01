import threading
from datetime import datetime

import pytest

from lagman.core.display import DisplayMode, SimulatedDisplay
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


def make(tmp_path, cfg=None, display=None, mode=None):
    from lagman.core.config import GeneratorConfig
    holder = {}
    eng = Engine(cfg or GeneratorConfig(), 1920, 1080, tmp_path / "runs",
                 now=lambda: datetime(2026, 10, 22, 12, 0, 0),
                 monotonic=lambda: holder["loop"].mono, display=display, mode_request=mode)
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


# ---- display modes ---------------------------------------------------------------------

P50 = 20_000_000
TV = [DisplayMode(1920, 1080, 60), DisplayMode(1920, 1080, 50), DisplayMode(3840, 2160, 50)]


def tv(current="1920x1080@60"):
    return SimulatedDisplay(DisplayMode.parse(current), TV)


def test_refresh_follows_a_50hz_display(tmp_path):
    eng, loop = make(tmp_path, display=tv("1920x1080@50"))
    st = eng.status()
    assert st["refresh_hz"] == 50 and st["tc_rates"] == [25, 50]
    assert st["display"]["mode"] == "1920x1080@50" and st["display_warning"] is None
    call(eng, loop, "start")
    before = eng.clock.count
    loop.frames(100, gap=P50)
    assert eng.clock.count - before == 100
    assert not eng.monitor.mismatched and eng.status()["repeated_refreshes"] == 0
    call(eng, loop, "stop")
    records = read_run_log(tmp_path / "runs" / f"{eng.status()['run_id']}.jsonl")
    assert records[0]["refresh_hz"] == 50
    assert "CLOCK     25 fps TC / 50 Hz" in records[0]["slate"]


def test_unusable_display_rate_warns_once(tmp_path):
    eng, loop = make(tmp_path, display=tv("1920x1080@60"))
    assert eng.status()["refresh_hz"] == 100
    assert "60 Hz" in eng.status()["display_warning"]
    call(eng, loop, "start")
    loop.frames(300, gap=16_666_667)
    st = eng.status()
    assert st["rate_mismatch"] and st["repeated_refreshes"] <= 10
    call(eng, loop, "stop")
    records = read_run_log(tmp_path / "runs" / f"{eng.status()['run_id']}.jsonl")
    assert [r["event"] for r in records].count("rate_mismatch") == 1
    # The 60Hz rate set can count at 60Hz.
    call(eng, loop, "set_config", config={"rate_set": "60", "tc_rate": 30})
    assert eng.status()["refresh_hz"] == 60 and eng.status()["display_warning"] is None


def test_set_display_mode(tmp_path):
    eng, loop = make(tmp_path, display=tv())
    call(eng, loop, "set_config", config={"tc_rate": 100})
    out = call(eng, loop, "set_display_mode", mode="3840x2160@50")
    assert out["current"] == "3840x2160@50"
    st = eng.status()
    assert (st["display"]["width"], st["display"]["height"]) == (3840, 2160)
    assert st["refresh_hz"] == 50 and st["tc_rate"] == 50  # fast mode follows the display
    assert eng.monitor.nominal_hz == 50 and eng.preview()["width"] == 3840
    # Remembered for the next start.
    eng2, _ = make(tmp_path, display=tv())
    assert eng2.status()["display"]["mode"] == "3840x2160@50"
    # An explicit request wins over the saved mode.
    eng3, _ = make(tmp_path, display=tv(), mode="1920x1080@50")
    assert eng3.status()["display"]["mode"] == "1920x1080@50"


def test_display_mode_rules(tmp_path):
    eng, loop = make(tmp_path, display=tv())
    with pytest.raises(CommandError, match="not a mode"):
        call(eng, loop, "set_display_mode", mode="1920x1080@100")
    with pytest.raises(CommandError):
        call(eng, loop, "set_display_mode", mode="nonsense")
    call(eng, loop, "start")
    with pytest.raises(CommandError, match="stop the run"):
        call(eng, loop, "set_display_mode", mode="1920x1080@50")
    call(eng, loop, "stop")
    no_display, loop2 = make(tmp_path / "b")
    with pytest.raises(CommandError):
        call(no_display, loop2, "set_display_mode", mode="1920x1080@50")
    # A saved mode the display no longer offers is ignored with a note.
    (tmp_path / "c" / "runs").mkdir(parents=True)
    (tmp_path / "c" / "runs" / "state.json").write_text(
        '{"next_run_number": 4, "display_mode": "2560x1440@100"}')
    eng4, _ = make(tmp_path / "c", display=tv())
    assert eng4.run_number == 4 and eng4.status()["display"]["mode"] == "1920x1080@60"
    assert any("2560x1440@100" in n for n in eng4.notes)


def test_display_info_lists_usable_modes(tmp_path):
    eng, _ = make(tmp_path, display=tv())
    info = eng.display_info()
    assert info["can_set"] and info["current"] == "1920x1080@60"
    assert [(m["mode"], m["rate_sets"]) for m in info["modes"]] == [
        ("1920x1080@60", ["60"]), ("3840x2160@50", ["50"]), ("1920x1080@50", ["50"])]
