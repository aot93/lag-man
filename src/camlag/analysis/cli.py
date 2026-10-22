"""camlag-analyse: latency from control camera recordings.

  mark      open a page to mark the source and wall strips on a video (once per setup)
  analyse   decode every frame and write results.csv, summary.json and report.html
  review    write one frame with both regions and their decoded values drawn on
  combine   summarise several analysed runs per camera and frame rate
  synth     make a synthetic recording with a known latency, for testing
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from ..core.rates import RATE_SETS


def _info(args: argparse.Namespace):
    from .setup import RunInfo

    if args.run_log:
        return RunInfo.from_run_log(args.run_log, args.capture_fps)
    if not args.rate_set:
        raise SystemExit("give --run-log (preferred) or --rate-set")
    return RunInfo.for_rate_set(args.rate_set, args.capture_fps)


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-log", type=Path, help="generator run log (.jsonl) for this recording")
    p.add_argument("--rate-set", choices=sorted(RATE_SETS),
                   help="rate set, when there is no run log")
    p.add_argument("--capture-fps", type=int,
                   help="real capture rate; default is the rate set's control camera rate "
                        "(100 or 120), not the file's playback rate")


def cmd_analyse(args: argparse.Namespace) -> int:
    from .analyse import analyse, summarise
    from .overlay import draw, jpeg
    from .report import pick_review, write_outputs
    from .setup import Setup
    from .video import Video

    info = _info(args)
    setup = Setup.load(args.setup)
    out = args.out or args.video.with_name(args.video.stem + "-latency")
    with Video(args.video) as video:
        if setup.video_size and setup.video_size != (video.width, video.height):
            print(f"warning: regions were marked on a {setup.video_size[0]}x"
                  f"{setup.video_size[1]} video; this one is {video.width}x{video.height}",
                  file=sys.stderr)
        start = round(args.start * info.capture_fps) if args.start else 0
        end = round(args.end * info.capture_fps) if args.end else None
        print(f"analysing {args.video.name}: {video.frame_count} frames, "
              f"{info.frame_ms:.3f} ms each at {info.capture_fps} fps capture", file=sys.stderr)
        samples = analyse(video.frames(start, end), setup, info)
        summary = summarise(samples, info)
        review = [(title, s, jpeg(draw(video.read(s.frame), setup, s, info, 960)))
                  for title, s in pick_review(samples)]
    paths = write_outputs(out, samples, summary, info, args.video.name, review)
    c = summary.counts
    mean = f"{summary.mean_ms:.1f} ms" if summary.mean_ms is not None else "–"
    print(f"{summary.n} good samples of {summary.frames}: mean {mean}, "
          f"min {summary.min_ms} ms, max {summary.max_ms} ms; flagged {c['transition']} "
          f"mid-change, {c['unreadable']} unreadable, {c['suspect']} suspect")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0 if summary.n else 1


def cmd_review(args: argparse.Namespace) -> int:
    import cv2

    from .analyse import sample_frame
    from .overlay import draw
    from .setup import Setup
    from .video import Video, to_gray

    info = _info(args) if (args.run_log or args.rate_set) else None
    setup = Setup.load(args.setup)
    with Video(args.video) as video:
        frame = video.read(args.frame)
    if info is None:
        from .setup import RunInfo
        info = RunInfo.for_rate_set("50")
    s = sample_frame(args.frame, 0.0, to_gray(frame), setup, info)
    out = args.out or Path(f"frame-{args.frame}.png")
    cv2.imwrite(str(out), draw(frame, setup, s, info))
    print(f"frame {args.frame}: {s.status}"
          + (f", {s.latency_ms:.1f} ms" if s.latency_ms is not None else "")
          + (f" ({s.note})" if s.note else ""))
    print(out)
    return 0


def cmd_combine(args: argparse.Namespace) -> int:
    from .report import combine

    summaries = []
    for p in args.results:
        path = p / "summary.json" if p.is_dir() else p
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    rows = combine(summaries)
    fields = ["camera", "camera_fps", "runs", "samples", "min_ms", "max_ms", "mean_ms"]
    if args.out:
        with args.out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    w = csv.DictWriter(sys.stdout, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    from . import synth

    ticks = args.latency_ticks
    setup, truths, log = synth.make(args.out, seconds=args.seconds, rate_set=args.rate_set,
                                    latency_ticks=lambda f: ticks + (f // 40) % 3,
                                    mirror_wall=args.mirror_wall)
    print(f"{args.out}: {len(truths)} frames; regions {args.out.with_suffix('.setup.json')}; "
          f"run log {log}")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    from .mark import serve

    out = args.setup or args.video.with_suffix(".setup.json")
    return serve(args.video, out, args.port)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="camlag-analyse", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyse", help="decode a recording and report latency")
    a.add_argument("video", type=Path)
    a.add_argument("--setup", type=Path, required=True, help="regions file from 'mark'")
    _add_run_args(a)
    a.add_argument("--out", type=Path, help="output folder (default <video>-latency)")
    a.add_argument("--start", type=float, help="start this many seconds (real time) in")
    a.add_argument("--end", type=float, help="stop this many seconds (real time) in")
    a.set_defaults(fn=cmd_analyse)

    r = sub.add_parser("review", help="draw the regions and decoded values on one frame")
    r.add_argument("video", type=Path)
    r.add_argument("--setup", type=Path, required=True)
    r.add_argument("--frame", type=int, required=True, help="video frame index")
    _add_run_args(r)
    r.add_argument("--out", type=Path, help="PNG to write (default frame-N.png)")
    r.set_defaults(fn=cmd_review)

    c = sub.add_parser("combine", help="summarise analysed runs per camera and frame rate")
    c.add_argument("results", type=Path, nargs="+", help="result folders or summary.json files")
    c.add_argument("--out", type=Path, help="also write the table to this CSV")
    c.set_defaults(fn=cmd_combine)

    m = sub.add_parser("mark", help="mark the strip regions on a recording in the browser")
    m.add_argument("video", type=Path)
    m.add_argument("--setup", type=Path, help="regions file to write (default <video>.setup.json)")
    m.add_argument("--port", type=int, default=8090)
    m.set_defaults(fn=cmd_mark)

    s = sub.add_parser("synth", help="make a synthetic recording with a known latency")
    s.add_argument("out", type=Path, help="video file to write (.mp4)")
    s.add_argument("--seconds", type=float, default=4.0)
    s.add_argument("--rate-set", choices=sorted(RATE_SETS), default="50")
    s.add_argument("--latency-ticks", type=int, default=8,
                   help="base delay in source ticks; varies by up to 2 more over the clip")
    s.add_argument("--mirror-wall", action="store_true", help="show the wall mirrored")
    s.set_defaults(fn=cmd_synth)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, OSError) as e:
        print(f"camlag-analyse: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
