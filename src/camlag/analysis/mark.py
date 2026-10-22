"""Local page for marking the two strip regions on a recording.

Serves on localhost only. The page shows a frame, takes four corner clicks per strip, decodes
them live, and on save nudges each region onto the cells (``roi.refine``) and writes the
setup file.
"""

from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..core.strip import DecodeStatus, count_difference
from .overlay import jpeg
from .roi import Quad, read, refine
from .setup import Setup
from .video import Video

PAGE = Path(__file__).parent / "mark.html"
STATIC = Path(__file__).parents[1] / "web" / "static"
CHECK_FRAMES = 10  # frames decoded from the shown one onward, for the live check
REFINE_FRAMES = 12


class Marker:
    def __init__(self, video_path: Path, setup_path: Path):
        self.video = Video(video_path)
        self.video_path = video_path
        self.setup_path = setup_path
        self.lock = threading.Lock()  # one OpenCV capture: serialise access

    def info(self) -> dict[str, Any]:
        existing = None
        if self.setup_path.is_file():
            try:
                existing = Setup.load(self.setup_path).to_json()
            except ValueError:
                existing = None
        v = self.video
        return {"video": self.video_path.name, "frames": v.frame_count, "width": v.width,
                "height": v.height, "file_fps": v.file_fps, "setup": existing,
                "setup_path": str(self.setup_path)}

    def frame_jpeg(self, n: int) -> bytes:
        with self.lock:
            return jpeg(self.video.read(self._clamp(n)), 90)

    def _clamp(self, n: int) -> int:
        return max(0, min(n, max(0, self.video.frame_count - 1)))

    def _grays(self, start: int, count: int) -> list:
        with self.lock:
            return [g for _, g in self.video.frames(self._clamp(start), self._clamp(start) + count)]

    def check(self, body: dict[str, Any]) -> dict[str, Any]:
        frames = self._grays(int(body.get("frame", 0)), CHECK_FRAMES)
        out: dict[str, Any] = {}
        counts: dict[str, list[int | None]] = {}
        for name in ("source", "wall"):
            pts = body.get(name)
            if not pts or len(pts) != 4:
                continue
            try:
                quad = Quad.from_points(pts)
            except ValueError as e:
                out[name] = {"error": str(e)}
                continue
            reads = [read(g, quad) for g in frames]
            first = reads[0]
            counts[name] = [r.count for r in reads]
            out[name] = {
                "status": first.status.value, "count": first.count, "reason": first.reason,
                "reversed": first.reversed, "candidates": list(first.candidates),
                "ok_frames": sum(r.status is DecodeStatus.OK for r in reads),
                "checked": len(reads),
            }
        if "source" in counts and "wall" in counts:
            lat = [count_difference(s, w) for s, w in zip(counts["source"], counts["wall"],
                                                          strict=True)
                   if s is not None and w is not None]
            out["latency_ticks"] = lat
        return out

    def save(self, body: dict[str, Any]) -> dict[str, Any]:
        n = int(body.get("frame", 0))
        frames = self._grays(n, REFINE_FRAMES)
        quads, clean = {}, {}
        for name in ("source", "wall"):
            quad = Quad.from_points(body[name])
            quads[name], clean[name] = refine(frames, quad)
        setup = Setup(quads["source"], quads["wall"], (self.video.width, self.video.height), n)
        setup.save(self.setup_path)
        return {"setup": setup.to_json(), "clean": clean, "checked": len(frames),
                "path": str(self.setup_path)}


def serve(video_path: Path, setup_path: Path, port: int = 8090, open_browser: bool = True,
          ready: threading.Event | None = None) -> int:
    marker = Marker(video_path, setup_path)
    handler = type("Handler", (_Handler,), {"marker": marker})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"marking {video_path.name}: open {url} (Ctrl+C to finish)", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    if ready:
        ready.port = httpd.server_address[1]  # type: ignore[attr-defined]
        ready.server = httpd  # type: ignore[attr-defined]
        ready.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        marker.video.close()
    return 0


class _Handler(BaseHTTPRequestHandler):
    marker: Marker
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        try:
            if url.path == "/":
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/info":
                self._json(self.marker.info())
            elif url.path == "/api/frame":
                n = int(parse_qs(url.query).get("n", ["0"])[0])
                self._send(self.marker.frame_jpeg(n), "image/jpeg")
            elif url.path.startswith("/static/"):
                self._static(url.path.removeprefix("/static/"))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, IndexError) as e:
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            path = urlsplit(self.path).path
            if path == "/api/check":
                self._json(self.marker.check(body))
            elif path == "/api/save":
                self._json(self.marker.save(body))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, TypeError) as e:
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def _static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        types = {".css": "text/css", ".png": "image/png", ".woff2": "font/woff2"}
        self._send(target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(data).encode(), "application/json", status)

    def _send(self, data: bytes, ctype: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)
