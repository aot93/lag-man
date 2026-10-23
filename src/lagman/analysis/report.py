"""Analysis outputs: results CSV, summary JSON and a standalone, printable HTML report.

The HTML report embeds its fonts, logo and review images, so it is one file that opens
offline and can be emailed or archived with the recording. Styling follows docs/brand.md.
"""

from __future__ import annotations

import base64
import csv
import json
import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from .analyse import OK, SUSPECT, TRANSITION, UNREADABLE, Sample, Summary
from .setup import RunInfo

STATIC = Path(__file__).parents[1] / "web" / "static"
REPORT_CSS = Path(__file__).with_name("report.css")
FONTS = (("Raleway", 700, "raleway-latin-700-normal"),
         ("Raleway", 800, "raleway-latin-800-normal"),
         ("Roboto", 400, "roboto-latin-400-normal"),
         ("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal"))
CSV_FIELDS = ("frame", "time_s", "source_count", "source_tc", "wall_count", "wall_tc",
              "latency_ticks", "latency_ms", "status", "note")


def write_csv(path: Path, samples: Sequence[Sample], info: RunInfo) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for s in samples:
            w.writerow(s.row(info))


def summary_json(summary: Summary, info: RunInfo, video: str) -> dict[str, Any]:
    slate = info.slate_fields()
    return {
        "video": video,
        "run_id": info.run_id,
        "run_number": info.run_number,
        "camera": slate.get("CAMERA"),
        "camera_fps": slate.get("FPS"),
        "test": slate.get("TEST"),
        "rate_set": info.rate_set,
        "refresh_hz": info.refresh_hz,
        "capture_fps": info.capture_fps,
        "tick_ms": round(info.tick_ms, 4),
        "slate": list(info.slate),
        "analysed": datetime.now().astimezone().isoformat(timespec="seconds"),
        **summary.to_json(),
    }


def pick_review(samples: Sequence[Sample], limit_flagged: int = 8) -> list[tuple[str, Sample]]:
    """Frames worth a look: the extremes and median of the good samples, then flagged ones."""
    good = sorted((s for s in samples if s.status == OK), key=lambda s: s.latency_ms)
    picks: list[tuple[str, Sample]] = []
    if good:
        picks += [("Lowest latency", good[0]), ("Median latency", good[len(good) // 2]),
                  ("Highest latency", good[-1])]
    order = {SUSPECT: 0, UNREADABLE: 1, TRANSITION: 2}
    flagged = sorted((s for s in samples if s.status in order), key=lambda s: order[s.status])
    seen = set()
    for s in flagged:
        if len([p for p in picks if p[1].status != OK]) >= limit_flagged:
            break
        if (s.status, s.note) in seen and s.status == TRANSITION:
            continue
        seen.add((s.status, s.note))
        picks.append((s.status.capitalize(), s))
    return picks


# ---- HTML ------------------------------------------------------------------------------

def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _fmt(v: float | None, unit: str = " ms", digits: int = 1) -> str:
    return "–" if v is None else f"{v:.{digits}f}{unit}"


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    v = start
    while v <= hi + step * 0.001:
        ticks.append(round(v, 6))
        v += step
    return ticks


def _scatter_svg(samples: Sequence[Sample], info: RunInfo) -> str:
    """Latency over time: one dot per good sample, flagged frames as ticks in a lane below."""
    good = [s for s in samples if s.status == OK]
    flagged = [s for s in samples if s.status != OK]
    if not samples:
        return ""
    W, H = 900, 300
    left, right, top, plot_bottom, lane = 56, 16, 30, 236, 22
    t_max = max(s.time_s for s in samples) or 1
    xt = _nice_ticks(0, t_max)
    x_hi = max(xt[-1], t_max)
    lat = [s.latency_ms for s in good] or [0.0]
    y_lo = max(0.0, min(lat) - info.tick_ms * 2)
    yt = _nice_ticks(y_lo, max(lat) + info.tick_ms * 2)
    y_lo, y_hi = yt[0], yt[-1]

    def x(t: float) -> float:
        return left + (W - left - right) * t / x_hi

    def y(v: float) -> float:
        return plot_bottom - (plot_bottom - top) * (v - y_lo) / ((y_hi - y_lo) or 1)

    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
             f'aria-labelledby="scatter-title"><title id="scatter-title">Latency of each '
             f'good sample over the recording, in milliseconds</title>']
    for v in yt:
        parts.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y(v):.1f}" '
                     f'y2="{y(v):.1f}"/><text class="ax" x="{left - 8}" y="{y(v) + 4:.1f}" '
                     f'text-anchor="end">{v:g}</text>')
    for t in xt:
        parts.append(f'<text class="ax" x="{x(t):.1f}" y="{H - 6}" text-anchor="middle">'
                     f'{t:g}s</text>')
    parts.append(f'<line class="axis" x1="{left}" x2="{W - right}" y1="{plot_bottom}" '
                 f'y2="{plot_bottom}"/>')
    parts.append(f'<text class="ax" x="{left - 8}" y="12" text-anchor="end">ms</text>')
    radius = 4 if len(good) <= 400 else 3
    for s in good:
        parts.append(f'<circle class="dot" data-frame="{s.frame}" cx="{x(s.time_s):.1f}" '
                     f'cy="{y(s.latency_ms):.1f}" '
                     f'r="{radius}"><title>Frame {s.frame} · {s.time_s:.2f} s · '
                     f'{s.latency_ms:.1f} ms ({s.latency_ticks} ticks)</title></circle>')
    lane_y = plot_bottom + 12
    parts.append(f'<text class="lane" x="{left - 8}" y="{lane_y + lane / 2 + 4:.1f}" '
                 f'text-anchor="end">flagged</text>')
    for s in flagged:
        parts.append(f'<line class="flag {s.status}" data-frame="{s.frame}" x1="{x(s.time_s):.1f}" '
                     f'x2="{x(s.time_s):.1f}" y1="{lane_y}" y2="{lane_y + lane}">'
                     f'<title>Frame {s.frame} · {s.time_s:.2f} s · {escape(s.status)}: '
                     f'{escape(s.note)}</title></line>')
    parts.append("</svg>")
    return "".join(parts)


def _histogram_svg(samples: Sequence[Sample], info: RunInfo) -> str:
    good = [s for s in samples if s.status == OK]
    if not good:
        return ""
    counts = Counter(s.latency_ticks for s in good)
    lo, hi = min(counts), max(counts)
    bins = list(range(lo, hi + 1))
    W, H = 900, 220
    left, right, top, bottom = 56, 16, 22, 186
    peak = max(counts.values())
    slot = (W - left - right) / len(bins)
    bar = max(6.0, min(56.0, slot - 2))
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-labelledby="hist-title">'
             f'<title id="hist-title">Number of good samples at each latency</title>']
    parts.append(f'<line class="axis" x1="{left}" x2="{W - right}" y1="{bottom}" y2="{bottom}"/>')
    for i, b in enumerate(bins):
        c = counts.get(b, 0)
        cx = left + slot * (i + 0.5)
        ms = b * info.tick_ms
        if c:
            h = (bottom - top) * c / peak
            r = min(4.0, bar / 2, h)
            x0, x1, y0 = cx - bar / 2, cx + bar / 2, bottom - h
            # rounded on the value end only, anchored square on the baseline
            parts.append(f'<path class="bar" d="M{x0:.1f},{bottom} V{y0 + r:.1f} '
                         f'Q{x0:.1f},{y0:.1f} {x0 + r:.1f},{y0:.1f} H{x1 - r:.1f} '
                         f'Q{x1:.1f},{y0:.1f} {x1:.1f},{y0 + r:.1f} V{bottom} Z">'
                         f'<title>{ms:g} ms: {c} sample{"s" if c != 1 else ""}</title></path>')
            parts.append(f'<text class="val" x="{cx:.1f}" y="{y0 - 5:.1f}" '
                         f'text-anchor="middle">{c}</text>')
        if len(bins) <= 24 or i % math.ceil(len(bins) / 24) == 0:
            parts.append(f'<text class="ax" x="{cx:.1f}" y="{bottom + 16}" '
                         f'text-anchor="middle">{ms:g}</text>')
    parts.append(f'<text class="ax" x="{W - right}" y="{H - 2}" text-anchor="end">'
                 f'latency, ms</text></svg>')
    return "".join(parts)


def _review_html(review: Sequence[tuple[str, Sample, bytes]], info: RunInfo) -> str:
    cards = []
    for title, s, img in review:
        row = s.row(info)
        value = f"{s.latency_ms:.1f} ms" if s.latency_ms is not None else "no figure"
        cards.append(
            f'<figure class="card review"><img alt="Frame {s.frame} with both regions marked" '
            f'src="data:image/jpeg;base64,{base64.b64encode(img).decode()}">'
            f'<figcaption><span class="tag {"ok" if s.status == OK else ""}">'
            f'{escape(title)}</span> <strong>{value}</strong> · frame {s.frame} · '
            f'{s.time_s:.2f} s<br><span class="mono">source {escape(row["source_tc"] or "–")}'
            f' · wall {escape(row["wall_tc"] or "–")}</span>'
            + (f'<br><span class="note">{escape(s.note)}</span>' if s.note else "")
            + "</figcaption></figure>")
    return "".join(cards)


def html_report(samples: Sequence[Sample], summary: Summary, info: RunInfo, video: str,
                review: Sequence[tuple[str, Sample, bytes]]) -> str:
    slate = info.slate_fields()
    fonts = "".join(
        f'@font-face{{font-family:"{fam}";font-weight:{wt};font-display:swap;'
        f'src:url(data:font/woff2;base64,{_b64(STATIC / "fonts" / (name + ".woff2"))}) '
        f'format("woff2")}}' for fam, wt, name in FONTS)
    tokens = (STATIC / "tokens.css").read_text(encoding="utf-8")
    css = REPORT_CSS.read_text(encoding="utf-8")
    logo = _b64(STATIC / "vyv-logo.png")
    title_bits = [f"Run {info.run_number}" if info.run_number else None,
                  slate.get("CAMERA"), f"{slate['FPS']} fps" if slate.get("FPS") else None]
    title = " · ".join(b for b in title_bits if b) or "Latency report"
    c = summary.counts
    checks = [
        (c[OK], "good samples used for the figures"),
        (c[TRANSITION], "frames with a strip caught mid-change (flagged, not used)"),
        (c[UNREADABLE], "frames where a strip could not be read (glare, moiré, obstruction)"),
        (c[SUSPECT], "frames that decoded but look wrong (negative latency, or outside the run)"),
        (summary.source_breaks, "places where the source strip did not advance as expected"),
        (summary.generator_events,
         "dropped or repeated generator frames logged during this window"),
    ]
    rows = "".join(
        "<tr>" + "".join(
            f'<td class="{"r mono" if k not in ("status", "note") else ""}">'
            f'{escape(str(v)) if v is not None else ""}</td>'
            for k, v in s.row(info).items()) + "</tr>" for s in samples)
    slate_rows = "".join(f"<tr><th>{escape(k.title())}</th><td>{escape(v)}</td></tr>"
                         for k, v in slate.items()) or '<tr><td>No slate in the run log.</td></tr>'
    meta = [("Video", video), ("Run log", info.run_id or "none (rate set from command line)"),
            ("Rate set", f"{info.refresh_hz} Hz source, {info.tick_ms:.3f} ms per tick"),
            ("Capture", f"{info.capture_fps} fps, {info.frame_ms:.3f} ms per video frame"),
            ("Time code", f"{summary.first_tc or '–'} to {summary.last_tc or '–'}"),
            ("Analysed", datetime.now().strftime("%Y-%m-%d %H:%M"))]
    meta_rows = "".join(f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>"
                        for k, v in meta)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · latency</title>
<style>{fonts}{tokens}{css}</style></head>
<body><div class="wrap">
<header class="mast"><img class="logo" src="data:image/png;base64,{logo}" alt="VYV">
<span class="product">lag-man</span><span class="tag-eyebrow">Latency report</span>
<div class="theme" role="radiogroup" aria-label="Theme"><button value="system">Auto</button><button
value="light">Light</button><button value="dark">Dark</button></div></header>
<p class="eyebrow">Glass-to-glass latency</p>
<h1>{escape(title)}</h1>
<p class="lede">Source clock minus wall clock for every control camera frame, decoded from the
code strips. Frames caught mid-change or unreadable are flagged and left out of the figures.</p>
<section class="keyfacts">
<div class="kf"><div class="n">{_fmt(summary.mean_ms)}</div><div class="l">Mean latency</div></div>
<div class="kf"><div class="n">{_fmt(summary.median_ms)}</div><div class="l">Median</div></div>
<div class="kf"><div class="n">{_fmt(summary.min_ms, "", 0)}–{_fmt(summary.max_ms)}</div>
<div class="l">Min to max</div></div>
<div class="kf"><div class="n">{summary.n}</div><div class="l">Good samples of {summary.frames}
frames · σ {_fmt(summary.stdev_ms)}</div></div>
</section>
<div class="two">
<section><h2>Slate</h2><table class="kv">{slate_rows}</table></section>
<section><h2>Recording</h2><table class="kv">{meta_rows}</table></section>
</div>
<section><h2>Latency over time</h2><div class="card">{_scatter_svg(samples, info)}</div>
<p class="hint">Each dot is one good control camera frame. Ticks below mark flagged frames.
Resolution is one source tick ({info.tick_ms:.3f} ms).</p></section>
<section><h2>Distribution</h2><div class="card">{_histogram_svg(samples, info)}</div></section>
<section><h2>Checks</h2><ul class="checks">{"".join(
        f'<li class="{"warn" if n and i > 0 else ""}"><strong>{n}</strong> {escape(text)}</li>'
        for i, (n, text) in enumerate(checks))}</ul></section>
<section><h2>Review frames</h2><p class="hint">Blue outlines the source monitor strip, red the
LED wall strip, with the values decoded from each.</p>
<div class="reviews">{_review_html(review, info)}</div></section>
<section class="samples"><h2>All samples</h2>
<details><summary>{len(samples)} rows (also in results.csv)</summary>
<div class="tablewrap"><table class="data"><thead><tr>{"".join(
        f"<th>{escape(f.replace('_', ' '))}</th>" for f in CSV_FIELDS)}</tr></thead>
<tbody>{rows}</tbody></table></div></details></section>
<script>{THEME_SCRIPT}</script>
<footer class="foot">lag-man analysis · strip format v2 · latency = (source count − wall count)
× {info.tick_ms:.3f} ms</footer>
</div></body></html>
"""




# Auto / Light / Dark, remembered per browser like the apps (web/static/theme.js).
THEME_SCRIPT = """
(function(){var K="lag-man.theme",r=document.documentElement,
bs=document.querySelectorAll(".theme button");
function get(){try{return localStorage.getItem(K)||"system"}catch(e){return "system"}}
function set(t){if(t==="system")r.removeAttribute("data-theme");else r.setAttribute("data-theme",t);
try{t==="system"?localStorage.removeItem(K):localStorage.setItem(K,t)}catch(e){}
bs.forEach(function(b){var on=b.value===t;b.classList.toggle("on",on);
b.setAttribute("aria-checked",on)})}
bs.forEach(function(b){b.type="button";b.setAttribute("role","radio");
b.onclick=function(){set(b.value)}});set(get())})();
"""


def write_outputs(out_dir: Path, samples: Sequence[Sample], summary: Summary, info: RunInfo,
                  video: str, review: Sequence[tuple[str, Sample, bytes]]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"csv": out_dir / "results.csv", "summary": out_dir / "summary.json",
             "report": out_dir / "report.html"}
    write_csv(paths["csv"], samples, info)
    paths["summary"].write_text(json.dumps(summary_json(summary, info, video), indent=2) + "\n",
                                encoding="utf-8")
    paths["report"].write_text(html_report(samples, summary, info, video, review),
                               encoding="utf-8")
    return paths


def combine(summaries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per camera and frame rate across several analysed runs (SPEC.md, "Output")."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in summaries:
        key = (s.get("camera") or "unknown", str(s.get("camera_fps") or "?"))
        groups.setdefault(key, []).append(s)
    rows = []
    for (camera, fps), runs in sorted(groups.items()):
        n = sum(r["n"] for r in runs)
        means = [(r["mean_ms"], r["n"]) for r in runs if r.get("mean_ms") is not None]
        rows.append({
            "camera": camera, "camera_fps": fps, "runs": len(runs), "samples": n,
            "min_ms": min((r["min_ms"] for r in runs if r.get("min_ms") is not None),
                          default=None),
            "max_ms": max((r["max_ms"] for r in runs if r.get("max_ms") is not None),
                          default=None),
            "mean_ms": round(sum(m * k for m, k in means) / n, 3) if n else None,
        })
    return rows
