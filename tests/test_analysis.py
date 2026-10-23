"""Analysis extension, tested end to end on synthetic control camera recordings."""

import csv
import json

import pytest

pytest.importorskip("cv2")

from lagman.analysis import report, synth  # noqa: E402
from lagman.analysis.analyse import OK, SUSPECT, TRANSITION, analyse, summarise  # noqa: E402
from lagman.analysis.cli import main as cli  # noqa: E402
from lagman.analysis.roi import Quad, refine  # noqa: E402
from lagman.analysis.setup import RunInfo, Setup  # noqa: E402
from lagman.analysis.video import Video  # noqa: E402

SIZE = (960, 540)


def delay(f):
    return 7 + (f // 20) % 3


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("clip") / "clip.mp4"
    setup, truths, log = synth.make(out, seconds=0.6, latency_ticks=delay, size=SIZE,
                                    transition_rate=0.2)
    return out, Setup.load(out.with_suffix(".setup.json")), truths, log


def run(path, setup, info):
    with Video(path) as v:
        return analyse(v.frames(), setup, info)


def test_latency_matches_truth(clip):
    path, setup, truths, log = clip
    info = RunInfo.from_run_log(log)
    assert (info.refresh_hz, info.capture_fps, info.tick_ms) == (100, 100, 10.0)
    samples = run(path, setup, info)
    assert len(samples) == len(truths) == 60
    for s, t in zip(samples, truths, strict=True):
        clean = t.source_blend == 0 and t.wall_blend == 0
        if clean:
            assert s.status == OK, s.note
            assert (s.source.count, s.wall.count) == (t.source, t.wall)
            assert s.latency_ms == pytest.approx(delay(t.frame) * 10)
        elif s.status == OK:
            # a blend mostly showing the next count reads as that count: within one tick
            assert s.source.count - t.source in (0, 1) and s.wall.count - t.wall in (0, 1)
        else:
            assert s.status == TRANSITION
    summary = summarise(samples, info)
    assert summary.counts["unreadable"] == 0 and summary.counts["suspect"] == 0
    assert summary.source_breaks == 0
    assert 65 <= summary.mean_ms <= 95


def test_capture_rate_sets_real_time(clip):
    path, setup, _, log = clip
    samples = run(path, setup, RunInfo.from_run_log(log))
    assert samples[10].time_s == pytest.approx(0.10)  # 10ms per frame, not the file's 20ms


def test_swapped_regions_flagged(clip):
    path, setup, _, log = clip
    swapped = Setup(setup.wall, setup.source)
    samples = run(path, swapped, RunInfo.from_run_log(log))
    assert all(s.status in (SUSPECT, TRANSITION) for s in samples)
    assert any("swapped" in s.note for s in samples)


def test_wrong_run_log_flagged(clip):
    path, setup, _, _ = clip
    info = RunInfo.for_rate_set("50")
    info = RunInfo(**{**info.__dict__, "start_count": 0, "stop_count": 1000})
    samples = run(path, setup, info)
    assert any(s.status == SUSPECT and "run" in s.note for s in samples)


def test_mirrored_wall_reads_correctly(tmp_path):
    out = tmp_path / "mirror.mp4"
    _, truths, log = synth.make(out, seconds=0.2, latency_ticks=9, size=SIZE,
                                transition_rate=0, mirror_wall=True)
    samples = run(out, Setup.load(out.with_suffix(".setup.json")), RunInfo.from_run_log(log))
    assert all(s.status == OK and s.latency_ticks == 9 for s in samples)
    assert all(s.wall.reversed and not s.source.reversed for s in samples)


def test_60hz_rate_set(tmp_path):
    out = tmp_path / "us.mp4"
    _, truths, log = synth.make(out, seconds=0.2, rate_set="60", latency_ticks=10, size=SIZE,
                                transition_rate=0)
    info = RunInfo.from_run_log(log)
    assert (info.refresh_hz, info.capture_fps) == (120, 120)
    samples = run(out, Setup.load(out.with_suffix(".setup.json")), info)
    assert all(s.latency_ms == pytest.approx(83.333, abs=0.01) for s in samples)


def test_refine_snaps_misplaced_regions(clip):
    path, setup, _, _ = clip
    with Video(path) as v:
        frames = [g for _, g in v.frames(0, 12)]
    for q in (setup.source, setup.wall):
        cell = q.length / 30
        for s, e in ((0.4, 0.4), (-0.35, 0.3), (0.45, -0.45)):
            r, clean = refine(frames, q.adjusted(s, e))
            for a, b in zip(r.points, q.points, strict=True):
                assert abs(a[0] - b[0]) < 0.2 * cell and abs(a[1] - b[1]) < 0.2 * cell + 1
            assert clean >= 8


def test_quad_from_points_any_order():
    pts = [(100, 50), (400, 60), (398, 80), (102, 70)]
    base = Quad.from_points(pts)
    for order in ([2, 0, 3, 1], [3, 2, 1, 0], [1, 3, 0, 2]):
        assert Quad.from_points([pts[i] for i in order]).points == base.points
    assert base.length > base.height
    with pytest.raises(ValueError):
        Quad.from_points([(0, 0), (1, 0), (1, 1), (0, 1)])
    with pytest.raises(ValueError):
        Quad.from_points(pts[:3])


def test_setup_round_trip(tmp_path, clip):
    _, setup, _, _ = clip
    setup.save(tmp_path / "s.json")
    assert Setup.load(tmp_path / "s.json") == setup
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(ValueError):
        Setup.load(tmp_path / "bad.json")


def test_run_info_from_generator_log(tmp_path):
    from lagman.core.config import GeneratorConfig
    from lagman.generator.engine import Engine

    cfg = GeneratorConfig(rate_set="60", tc_rate=60)
    cfg.slate.fields["camera_name"].value = "Cam B"
    eng = Engine(cfg, 1920, 1080, tmp_path)
    eng.apply("start")
    for i in range(5):
        eng.frame()
        eng.presented(i * 8_333_333)
    eng.apply("stop")
    info = RunInfo.from_run_log(next(tmp_path.glob("*.jsonl")))
    assert (info.refresh_hz, info.capture_fps, info.tc_rate) == (120, 120, 60)
    assert info.slate_fields()["CAMERA"] == "Cam B"
    assert info.stop_count == info.start_count + 5


def test_outputs_and_combine(tmp_path, clip):
    path, setup, _, log = clip
    info = RunInfo.from_run_log(log)
    samples = run(path, setup, info)
    summary = summarise(samples, info)
    paths = report.write_outputs(tmp_path, samples, summary, info, path.name, [])
    rows = list(csv.DictReader(paths["csv"].open()))
    assert len(rows) == 60 and rows[0]["status"] in (OK, TRANSITION)
    data = json.loads(paths["summary"].read_text())
    assert data["camera"] == "Synthetic cam" and data["n"] == summary.n
    html = paths["report"].read_text()
    assert "Synthetic cam" in html and "<svg" in html and "http" not in html.split("<body>")[0]
    combined = report.combine([data, {**data, "n": 10, "mean_ms": 100.0}])
    assert combined[0]["runs"] == 2 and combined[0]["samples"] == data["n"] + 10


def test_cli_analyse(tmp_path, clip, capsys):
    path, _, _, log = clip
    out = tmp_path / "res"
    code = cli(["analyse", str(path), "--setup", str(path.with_suffix(".setup.json")),
                "--run-log", str(log), "--out", str(out)])
    assert code == 0
    assert "good samples" in capsys.readouterr().out
    assert (out / "report.html").stat().st_size > 10_000
    assert cli(["review", str(path), "--setup", str(path.with_suffix(".setup.json")),
                "--frame", "3", "--out", str(tmp_path / "f.png")]) == 0
    assert (tmp_path / "f.png").is_file()
