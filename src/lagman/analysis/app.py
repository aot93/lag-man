"""The analysis app: the whole workflow in a browser, served from this PC only.

Add recordings, attach run logs (picked from disk or fetched from the generator), mark the
strip regions, analyse in the background, then review results frame by frame.

The server listens on 127.0.0.1. Every API call must carry the session token embedded in the
page, and the Host header must be this server, so other web pages open in the same browser
cannot drive it or read files through it.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import secrets
import string
import sys
import threading
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ..core.strip import DecodeStatus, count_difference
from . import report, synth
from .analyse import sample_frame
from .overlay import draw, jpeg
from .pipeline import Cancelled, run
from .roi import Quad, read, refine
from .setup import RunInfo, Setup
from .video import Video, to_gray
from .workspace import VIDEO_SUFFIXES, Recording, Workspace, check_name

HERE = Path(__file__).parent
APP_STATIC = HERE / "static"
WEB_STATIC = HERE.parent / "web" / "static"
CHECK_FRAMES = 10
REFINE_FRAMES = 12
MAX_BODY = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def bad(message: str) -> ApiError:
    return ApiError(HTTPStatus.BAD_REQUEST, message)


# ---- background jobs --------------------------------------------------------------------

@dataclass
class Job:
    id: str
    kind: str
    recording: str | None
    label: str
    state: str = "queued"  # queued, running, done, failed, cancelled
    done: int = 0
    total: int = 0
    message: str = ""
    started: str | None = None
    finished: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_json(self) -> dict[str, Any]:
        keys = ("id", "kind", "recording", "label", "state", "done", "total", "message",
                "started", "finished")
        return {k: getattr(self, k) for k in keys}


class Jobs:
    """One worker: analyses are CPU and disk heavy, so they run one at a time."""

    def __init__(self) -> None:
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        threading.Thread(target=self._worker, name="lagman-jobs", daemon=True).start()

    def submit(self, kind: str, recording: str | None, label: str, fn) -> Job:
        job = Job(uuid.uuid4().hex[:10], kind, recording, label)
        job.fn = fn  # type: ignore[attr-defined]
        with self._lock:
            active = [j for j in self._jobs if j.recording == recording and recording
                      and j.state in ("queued", "running")]
            if active:
                raise ApiError(HTTPStatus.CONFLICT, "this recording is already being analysed")
            self._jobs.append(job)
            self._jobs = self._jobs[-50:]
            self._wake.notify()
        return job

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_json() for j in self._jobs]

    def cancel(self, job_id: str) -> None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id and j.state in ("queued", "running"):
                    j.cancel.set()
                    if j.state == "queued":
                        j.state = "cancelled"
                    return
        raise ApiError(HTTPStatus.NOT_FOUND, "no such active job")

    def _worker(self) -> None:
        while True:
            with self._lock:
                job = next((j for j in self._jobs if j.state == "queued"), None)
                if job is None:
                    self._wake.wait()
                    continue
                job.state = "running"
                job.started = _now()

            def progress(done: int, total: int, job: Job = job) -> None:
                job.done, job.total = done, total

            try:
                job.message = job.fn(progress, job.cancel) or ""  # type: ignore[attr-defined]
                job.state = "done"
            except Cancelled:
                job.state, job.message = "cancelled", "cancelled"
            except Exception as e:  # report every failure in the UI
                job.state, job.message = "failed", str(e) or type(e).__name__
                traceback.print_exc()
            job.finished = _now()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---- the app ----------------------------------------------------------------------------

class App:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.jobs = Jobs()
        self.token = secrets.token_urlsafe(24)
        self._videos: dict[str, tuple[Video, threading.Lock]] = {}
        self._vlock = threading.Lock()

    # videos are opened once per recording and shared, one reader at a time
    def _video(self, rec: Recording) -> tuple[Video, threading.Lock]:
        with self._vlock:
            entry = self._videos.get(rec.id)
            if entry is None:
                try:
                    entry = self._videos[rec.id] = (Video(rec.video_path), threading.Lock())
                except ValueError as e:
                    raise bad(str(e)) from None
            return entry

    def _forget_video(self, rec_id: str) -> None:
        with self._vlock:
            entry = self._videos.pop(rec_id, None)
        if entry:
            entry[0].close()

    def _rec(self, rec_id: str) -> Recording:
        try:
            return self.ws.get(rec_id)
        except KeyError:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such recording") from None

    # ---- state ----

    def state(self) -> dict[str, Any]:
        jobs = self.jobs.list()
        recs = []
        for r in self.ws.state.recordings:
            s = self.ws.summary(r.id)
            active = next((j for j in reversed(jobs) if j["recording"] == r.id
                           and j["state"] in ("queued", "running")), None)
            recs.append({**asdict(r), "name": r.video_path.name,
                         "exists": r.video_path.is_file(),
                         "summary": _brief(s), "job": active})
        return {"workspace": str(self.ws.root), "recordings": recs,
                "setups": self.ws.setup_names(), "jobs": jobs,
                "generator_url": self.ws.state.generator_url, "last_dir": self.ws.state.last_dir,
                "home": str(Path.home())}

    def info(self, rec: Recording) -> dict[str, Any]:
        video, lock = self._video(rec)
        try:
            run_info = rec.run_info()
            run = {"rate_set": run_info.rate_set, "refresh_hz": run_info.refresh_hz,
                   "capture_fps": run_info.capture_fps, "tick_ms": run_info.tick_ms,
                   "run_id": run_info.run_id, "run_number": run_info.run_number,
                   "slate": run_info.slate_fields(), "error": None}
        except (OSError, ValueError) as e:
            run = {"error": str(e)}
        setup = None
        if rec.setup:
            try:
                setup = self.ws.load_setup(rec.setup).to_json()
            except ValueError:
                setup = None
        st = rec.video_path.stat()
        return {"recording": asdict(rec), "video": {
                    "name": rec.video_path.name, "frames": video.frame_count,
                    "width": video.width, "height": video.height, "file_fps": video.file_fps,
                    "bytes": st.st_size, "modified": _iso(st.st_mtime)},
                "run": run, "setup": setup, "summary": self.ws.summary(rec.id)}

    # ---- frames and regions ----

    def frame(self, rec: Recording, n: int, overlay: bool) -> bytes:
        video, lock = self._video(rec)
        n = max(0, min(n, max(0, video.frame_count - 1)))
        with lock:
            try:
                img = video.read(n)
            except IndexError as e:
                raise ApiError(HTTPStatus.NOT_FOUND, str(e)) from None
        if overlay and rec.setup:
            setup = self.ws.load_setup(rec.setup)
            info = self._run_info(rec)
            s = sample_frame(n, n * info.frame_ms / 1000, to_gray(img), setup, info)
            img = draw(img, setup, s, info, 1600)
        return jpeg(img, 88)

    def sample(self, rec: Recording, n: int) -> dict[str, Any]:
        if not rec.setup:
            raise bad("mark the regions first")
        video, lock = self._video(rec)
        n = max(0, min(n, max(0, video.frame_count - 1)))
        with lock:
            img = video.read(n)
        info = self._run_info(rec)
        s = sample_frame(n, n * info.frame_ms / 1000, to_gray(img), self.ws.load_setup(rec.setup),
                         info)
        return {**s.row(info), "source_reason": s.source.reason, "wall_reason": s.wall.reason,
                "source_reversed": s.source.reversed, "wall_reversed": s.wall.reversed}

    def _run_info(self, rec: Recording) -> RunInfo:
        try:
            return rec.run_info()
        except (OSError, ValueError) as e:
            raise bad(f"run log: {e}") from None

    def _grays(self, rec: Recording, start: int, count: int) -> list:
        video, lock = self._video(rec)
        start = max(0, min(start, max(0, video.frame_count - 1)))
        with lock:
            return [g for _, g in video.frames(start, start + count)]

    def check(self, rec: Recording, body: dict[str, Any]) -> dict[str, Any]:
        frames = self._grays(rec, int(body.get("frame", 0)), CHECK_FRAMES)
        out: dict[str, Any] = {}
        counts: dict[str, list[int | None]] = {}
        for name in ("source", "wall"):
            pts = body.get(name)
            if not pts or len(pts) != 4:
                continue
            try:
                quad = Quad.from_points(pts)
            except (ValueError, TypeError) as e:
                out[name] = {"error": str(e)}
                continue
            reads = [read(g, quad) for g in frames]
            first = reads[0]
            counts[name] = [r.count for r in reads]
            out[name] = {"status": first.status.value, "count": first.count,
                         "reason": first.reason, "reversed": first.reversed,
                         "ok_frames": sum(r.status is DecodeStatus.OK for r in reads),
                         "checked": len(reads)}
        if "source" in counts and "wall" in counts:
            out["latency_ticks"] = [count_difference(s, w) for s, w in
                                    zip(counts["source"], counts["wall"], strict=True)
                                    if s is not None and w is not None]
        return out

    def save_regions(self, rec: Recording, body: dict[str, Any]) -> dict[str, Any]:
        name = check_name(str(body.get("name") or ""))
        n = int(body.get("frame", 0))
        frames = self._grays(rec, n, REFINE_FRAMES)
        video, _ = self._video(rec)
        quads, clean = {}, {}
        for which in ("source", "wall"):
            try:
                quads[which], clean[which] = refine(frames, Quad.from_points(body[which]))
            except (KeyError, TypeError, ValueError) as e:
                raise bad(f"{which} region: {e}") from None
        setup = Setup(quads["source"], quads["wall"], (video.width, video.height), n)
        self.ws.save_setup(name, setup)
        self.ws.update(rec.id, {"setup": name})
        return {"setup": setup.to_json(), "name": name, "clean": clean, "checked": len(frames)}

    # ---- jobs ----

    def analyse(self, rec: Recording, body: dict[str, Any]) -> Job:
        if not rec.setup:
            raise bad("mark the regions first")
        setup = self.ws.load_setup(rec.setup)
        info = self._run_info(rec)
        out = self.ws.results_dir(rec.id)
        start, end = body.get("start_s"), body.get("end_s")

        def work(progress, cancel) -> str:
            result = run(rec.video_path, setup, info, out, start, end, progress, cancel)
            rec.analysed, rec.error = _now(), None
            self.ws.save()
            s = result.summary
            return (f"{s.n} good samples of {s.frames}"
                    + (f", mean {s.mean_ms:.1f} ms" if s.mean_ms is not None else "")
                    + "".join(f"; {w}" for w in result.warnings))

        return self.jobs.submit("analyse", rec.id, f"Analyse {rec.video_path.name}", work)

    def demo(self) -> Job:
        folder = self.ws.root / "demo"

        def work(progress, cancel) -> str:
            folder.mkdir(exist_ok=True)
            n = 1
            while (folder / f"demo-{n}.mp4").exists():
                n += 1
            out = folder / f"demo-{n}.mp4"
            setup, _, log = synth.make(out, seconds=4, latency_ticks=lambda f: 7 + (f // 40) % 3,
                                       progress=progress)
            name = f"demo-{n}"
            self.ws.save_setup(name, setup)
            rec = self.ws.add(out, log, setup=name)
            return f"added {rec.id}"

        return self.jobs.submit("demo", None, "Make a demo recording", work)

    # ---- results ----

    def results(self, rec: Recording) -> dict[str, Any]:
        out = self.ws.results_dir(rec.id)
        summary = self.ws.summary(rec.id)
        if summary is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "not analysed yet")
        with (out / "results.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        info = self._run_info(rec)
        samples = [_row_to_sample(r) for r in rows]
        return {"summary": summary, "rows": rows,
                "charts": {"scatter": report._scatter_svg(samples, info),
                           "histogram": report._histogram_svg(samples, info)}}

    def combined(self) -> list[dict[str, Any]]:
        sums = [s for r in self.ws.state.recordings if (s := self.ws.summary(r.id))]
        return report.combine(sums)

    # ---- files and run logs ----

    def listdir(self, path: str | None) -> dict[str, Any]:
        if not path:
            roots = _roots()
            if len(roots) > 1:
                return {"path": "", "parent": None, "dirs": roots, "files": []}
            path = self.ws.state.last_dir or str(Path.home())
        p = Path(path).expanduser()
        try:
            p = p.resolve()
            entries = list(os.scandir(p))
        except OSError as e:
            raise bad(f"cannot open {p}: {e.strerror or e}") from None
        dirs, files = [], []
        for e in entries:
            if e.name.startswith((".", "$")):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file():
                    suffix = Path(e.name).suffix.lower()
                    kind = ("video" if suffix in VIDEO_SUFFIXES
                            else "runlog" if suffix == ".jsonl" else None)
                    if kind:
                        st = e.stat()
                        files.append({"name": e.name, "path": str(Path(e.path)), "kind": kind,
                                      "bytes": st.st_size, "modified": _iso(st.st_mtime)})
            except OSError:
                continue
        parent = str(p.parent) if p.parent != p else ""
        return {"path": str(p), "parent": parent, "dirs": sorted(dirs, key=str.lower),
                "files": sorted(files, key=lambda f: f["name"].lower())}

    def runlogs(self, folder: str | None, rec_id: str | None) -> list[dict[str, Any]]:
        """Run logs in ``folder``, or else in the workspace and next to the recording."""
        dirs = [Path(folder).expanduser()] if folder else [self.ws.runlog_dir()]
        target = None
        if rec_id:
            rec = self._rec(rec_id)
            if rec.video_path.is_file():
                target = rec.video_path.stat().st_mtime
            if not folder:
                dirs += [rec.video_path.parent, rec.video_path.parent / "runs"]
        seen: set[Path] = set()
        paths = []
        for d in dirs:
            for p in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
                if p.resolve() not in seen:
                    seen.add(p.resolve())
                    paths.append(p)
        out = []
        for p in paths:
            meta = _runlog_meta(p)
            if meta is None:
                continue
            if target is not None and meta["start_ts"] is not None:
                meta["delta_s"] = round(abs(target - meta["start_ts"]))
            out.append(meta)
        if target is not None:  # closest in time to the recording first
            out.sort(key=lambda m: (m.get("delta_s") is None, m.get("delta_s") or 0))
        else:  # newest first
            out.sort(key=lambda m: m["started"] or "", reverse=True)
        return out

    # ---- generator ----

    def _gen_request(self, url: str, password: str | None, path: str) -> bytes:
        base = url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "http://" + base
        parts = urlsplit(base)
        if not parts.hostname:
            raise bad("enter the generator's address, for example 192.168.1.50:8080")
        req = urllib.request.Request(base + path)
        if password:
            token = base64.b64encode(f"lagman:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read(20 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise bad("the generator wants a password (or this one is wrong)") from None
            raise bad(f"generator replied {e.code} {e.reason}") from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise bad(f"cannot reach the generator at {base}: {reason}") from None
        self.ws.state.generator_url = base
        self.ws.save()
        return data

    def generator_runs(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        data = json.loads(self._gen_request(body.get("url", ""), body.get("password"),
                                            "/api/runs"))
        have = {p.stem for p in self.ws.runlog_dir().glob("*.jsonl")}
        return [{**r, "fetched": r.get("id") in have} for r in data.get("runs", [])]

    def generator_fetch(self, body: dict[str, Any]) -> dict[str, Any]:
        run_id = str(body.get("run_id", ""))
        if not run_id or any(c not in string.digits + "-" for c in run_id):
            raise bad("bad run id")
        data = self._gen_request(body.get("url", ""), body.get("password"),
                                 f"/api/runs/{quote(run_id)}")
        path = self.ws.runlog_dir() / f"{run_id}.jsonl"
        path.write_bytes(data)
        if _runlog_meta(path) is None:
            path.unlink()
            raise bad("the generator sent something that is not a run log")
        return {"path": str(path)}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _brief(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not summary:
        return None
    keys = ("n", "frames", "mean_ms", "median_ms", "min_ms", "max_ms", "camera", "camera_fps",
            "counts", "analysed")
    return {k: summary.get(k) for k in keys}


def _row_to_sample(row: dict[str, str]):
    """Enough of a Sample, from a results.csv row, to draw the charts."""
    from ..core.strip import Decoded
    from .analyse import Sample

    def num(v: str, kind=float):
        return kind(v) if v not in ("", None) else None

    dummy = Decoded(DecodeStatus.OK)
    return Sample(int(row["frame"]), float(row["time_s"]), dummy, dummy, row["status"],
                  num(row["latency_ticks"], int), num(row["latency_ms"]), row["note"])


def _runlog_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            first = json.loads(fh.readline() or "{}")
    except (OSError, ValueError):
        return None
    if first.get("event") != "run_start":
        return None
    slate = {}
    for line in first.get("slate") or []:
        label, _, value = str(line).partition("  ")
        slate[label.strip()] = value.strip()
    ts = None
    try:
        ts = datetime.fromisoformat(first["t"]).timestamp()
    except (KeyError, ValueError, TypeError):
        pass
    return {"path": str(path), "name": path.name, "run_id": first.get("run_id", path.stem),
            "run_number": first.get("run_number"), "started": first.get("t"), "start_ts": ts,
            "refresh_hz": first.get("refresh_hz"), "tc_rate": first.get("tc_rate"),
            "camera": slate.get("CAMERA"), "fps": slate.get("FPS"), "test": slate.get("TEST")}


def _roots() -> list[str]:
    if os.name != "nt":
        return ["/"]
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


# ---- HTTP -------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    app: App
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            if not self._host_ok():
                raise ApiError(HTTPStatus.FORBIDDEN, "wrong host")
            url = urlsplit(self.path)
            if url.path.startswith("/api/"):
                # Images and links cannot send headers, so GETs may carry it as ?t=.
                given = self.headers.get("X-Lagman-Token") or (
                    parse_qs(url.query).get("t", [""])[0] if method == "GET" else "")
                if not secrets.compare_digest(given, self.app.token):
                    raise ApiError(HTTPStatus.FORBIDDEN, "missing or wrong session token; "
                                                         "reload the page")
                body = self._body() if method in ("POST", "PATCH") else None
                self._route(method, [unquote(p) for p in url.path.split("/")[2:]],
                            parse_qs(url.query), body)
            elif method == "GET":
                self._static(url.path)
            else:
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"internal error: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise bad("request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise bad("request body is not JSON") from None
        if not isinstance(body, dict):
            raise bad("request body must be a JSON object")
        return body

    def _route(self, method: str, parts: list[str], q: dict[str, list[str]],
               body: dict[str, Any] | None) -> None:
        app = self.app
        one = lambda k, d=None: q.get(k, [d])[0]  # noqa: E731
        match method, parts:
            case "GET", ["state"]:
                return self._json(app.state())
            case "GET", ["fs"]:
                return self._json(app.listdir(one("path")))
            case "GET", ["runlogs"]:
                return self._json({"runlogs": app.runlogs(one("dir"), one("recording"))})
            case "POST", ["recordings"]:
                try:
                    rec = app.ws.add(Path(str(body.get("video", ""))),
                                     Path(body["run_log"]) if body.get("run_log") else None,
                                     body.get("rate_set"), body.get("setup"))
                except ValueError as e:
                    raise bad(str(e)) from None
                return self._json(asdict(rec))
            case "PATCH", ["recordings", rid]:
                app._rec(rid)
                try:
                    return self._json(asdict(app.ws.update(rid, body)))
                except ValueError as e:
                    raise bad(str(e)) from None
            case "DELETE", ["recordings", rid]:
                app._rec(rid)
                app._forget_video(rid)
                app.ws.remove(rid)
                return self._json({"ok": True})
            case "GET", ["recordings", rid, "info"]:
                return self._json(app.info(app._rec(rid)))
            case "GET", ["recordings", rid, "frame"]:
                data = app.frame(app._rec(rid), int(one("n", "0")), one("overlay") == "1")
                return self._send(data, "image/jpeg")
            case "GET", ["recordings", rid, "sample"]:
                return self._json(app.sample(app._rec(rid), int(one("n", "0"))))
            case "POST", ["recordings", rid, "check"]:
                return self._json(app.check(app._rec(rid), body))
            case "POST", ["recordings", rid, "regions"]:
                try:
                    return self._json(app.save_regions(app._rec(rid), body))
                except ValueError as e:
                    raise bad(str(e)) from None
            case "POST", ["recordings", rid, "analyse"]:
                return self._json(app.analyse(app._rec(rid), body).to_json())
            case "GET", ["recordings", rid, "results"]:
                return self._json(app.results(app._rec(rid)))
            case "GET", ["recordings", rid, "files", name]:
                return self._file(app, rid, name, one("download") == "1")
            case "GET", ["combined"]:
                return self._json({"rows": app.combined()})
            case "GET", ["combined.csv"]:
                return self._combined_csv(app)
            case "POST", ["demo"]:
                return self._json(app.demo().to_json())
            case "POST", ["jobs", job_id, "cancel"]:
                app.jobs.cancel(job_id)
                return self._json({"ok": True})
            case "POST", ["generator", "runs"]:
                return self._json({"runs": app.generator_runs(body)})
            case "POST", ["generator", "fetch"]:
                return self._json(app.generator_fetch(body))
        raise ApiError(HTTPStatus.NOT_FOUND, f"no route for {method} /api/{'/'.join(parts)}")

    def _file(self, app: App, rid: str, name: str, download: bool) -> None:
        types = {"report.html": "text/html; charset=utf-8", "results.csv": "text/csv",
                 "summary.json": "application/json"}
        if name not in types:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such file")
        path = app.ws.results_dir(app._rec(rid).id) / name
        if not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not analysed yet")
        headers = {}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{rid}-{name}"'
        self._send(path.read_bytes(), types[name], headers)

    def _combined_csv(self, app: App) -> None:
        rows = app.combined()
        fields = ["camera", "camera_fps", "runs", "samples", "min_ms", "max_ms", "mean_ms"]
        lines = [",".join(fields)] + [",".join(_csv_cell(r.get(f)) for f in fields)
                                      for r in rows]
        self._send(("\r\n".join(lines) + "\r\n").encode(), "text/csv",
                   {"Content-Disposition": 'attachment; filename="latency-summary.csv"'})

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            html = (APP_STATIC / "index.html").read_text(encoding="utf-8")
            data = html.replace("__LAGMAN_TOKEN__", self.app.token).encode()
            return self._send(data, "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; img-src 'self' "
                               "data: blob:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'"})
        for prefix, root in (("/app/", APP_STATIC), ("/static/", WEB_STATIC)):
            if path.startswith(prefix):
                target = (root / path.removeprefix(prefix)).resolve()
                if target.is_relative_to(root.resolve()) and target.is_file():
                    types = {".css": "text/css", ".js": "text/javascript", ".png": "image/png",
                             ".woff2": "font/woff2", ".html": "text/html"}
                    return self._send(target.read_bytes(),
                                      types.get(target.suffix, "application/octet-stream"))
        raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(data).encode(), "application/json", status=status)

    def _send(self, data: bytes, ctype: str, headers: dict[str, str] | None = None,
              status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


def _csv_cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return f'"{s.replace(chr(34), chr(34) * 2)}"' if any(c in s for c in ',"\n') else s


def make_server(workspace: Path, port: int = 8090) -> tuple[ThreadingHTTPServer, App]:
    app = App(Workspace(workspace))
    handler = type("Handler", (_Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd, app


def default_workspace() -> Path:
    docs = Path.home() / "Documents"
    base = docs if docs.is_dir() else Path.home()
    new, old = base / "lag-man", base / "cam-lag"
    # The project was called cam-lag; carry an old workspace over (same format).
    if not new.exists() and (old / "workspace.json").is_file():
        try:
            old.rename(new)
        except OSError:
            return old
        print(f"moved the workspace from {old} to {new}", file=sys.stderr)
    return new


def serve(workspace: Path | None = None, port: int = 8090, open_browser: bool = True,
          add: Path | None = None) -> int:
    try:
        httpd, app = make_server(workspace or default_workspace(), port)
    except OSError as e:
        print(f"lagman-analyse: cannot listen on port {port}: {e.strerror or e}. Is the app "
              f"already open? Try --port {port + 1}.", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if add is not None:
        try:
            target = add.expanduser().resolve()
            rec = next((r for r in app.ws.state.recordings if Path(r.video) == target), None)
            rec = rec or app.ws.add(add)
        except ValueError as e:
            print(f"lagman-analyse: {e}", file=sys.stderr)
            httpd.server_close()
            return 2
        url += f"#/recording/{quote(rec.id)}/regions"
    print(f"lag-man analysis: {url}\nworkspace: {app.ws.root}\nPress Ctrl+C to stop.",
          file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
