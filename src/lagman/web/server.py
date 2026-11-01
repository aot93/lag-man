"""HTTP control server: the web UI and a JSON API over the same routes.

Runs in a background thread inside the generator. Everything that changes the generator goes
through ``Engine.submit``, so the render loop applies it between frames.

API (all JSON):

    GET    /api/status                    live state, count, refresh and frame warnings
    GET    /api/config                    current configuration
    PUT    /api/config                    replace or patch the configuration
    POST   /api/start | /api/stop | /api/reset | /api/warnings/ack
    POST   /api/start-value               {"value": "HH:MM:SS:FF"}
    GET    /api/preview                   current frame as draw operations
    GET    /api/display                   display modes the generator can count at
    PUT    /api/display                   {"mode": "1920x1080@50"}; only while idle
    GET    /api/presets                   preset names
    GET    /api/presets/<name>            preset config (?download=1 to save as a file)
    PUT    /api/presets/<name>            save; body is a config, or empty for the current one
    POST   /api/presets/<name>/load       apply a preset
    DELETE /api/presets/<name>
    GET    /api/runs                      run logs, newest first
    GET    /api/runs/<id>                 one run log (JSON Lines)
"""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..generator.engine import CommandError, Engine
from .presets import PresetStore

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 256 * 1024
_RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ControlServer:
    def __init__(self, engine: Engine, presets: PresetStore, host: str = "0.0.0.0",
                 port: int = 8080, password: str | None = None):
        self.engine = engine
        self.presets = presets
        self.password = password
        handler = type("Handler", (_Handler,), {"app": self})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="lagman-web",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- routes -----------------------------------------------------------------------

    def route(self, method: str, path: str, query: dict[str, list[str]],
              body: Any) -> tuple[Any, dict[str, str]]:
        eng = self.engine
        parts = [unquote(p) for p in path.strip("/").split("/")][1:]  # drop "api"
        match method, parts:
            case "GET", ["status"]:
                return eng.status(), {}
            case "GET", ["config"]:
                return eng.cfg.to_dict(), {}
            case "PUT", ["config"]:
                return self._apply(deep_merge(eng.cfg.to_dict(), _obj(body))), {}
            case "POST", ["start" | "stop" | "reset" as cmd]:
                self._submit(cmd)
                return eng.status(), {}
            case "POST", ["warnings", "ack"]:
                self._submit("ack_warnings")
                return eng.status(), {}
            case "POST", ["start-value"]:
                value = _obj(body).get("value")
                if not isinstance(value, str):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "value must be HH:MM:SS:FF")
                self._submit("set_start_value", value=value)
                return eng.status(), {}
            case "GET", ["preview"]:
                return eng.preview(), {}
            case "GET", ["display"]:
                return eng.display_info(), {}
            case "PUT", ["display"]:
                mode = _obj(body).get("mode")
                if not isinstance(mode, str):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be WIDTHxHEIGHT@HZ")
                # A mode change can take the display a few seconds.
                return self._submit("set_display_mode", timeout=15.0, mode=mode), {}
            case "GET", ["presets"]:
                return {"presets": self.presets.list()}, {}
            case "GET", ["presets", name]:
                data = self._preset(lambda: self.presets.load(name))
                headers = {}
                if query.get("download"):
                    headers["Content-Disposition"] = f'attachment; filename="{name}.json"'
                return data, headers
            case "PUT", ["presets", name]:
                config = _obj(body) if body else eng.cfg.to_dict()
                return self._preset(lambda: self.presets.save(name, config)), {}
            case "POST", ["presets", name, "load"]:
                return self._apply(self._preset(lambda: self.presets.load(name))), {}
            case "DELETE", ["presets", name]:
                self._preset(lambda: self.presets.delete(name))
                return {"presets": self.presets.list()}, {}
            case "GET", ["runs"]:
                return {"runs": self._runs()}, {}
            case "GET", ["runs", run_id]:
                return self._run_file(run_id), {}
        raise ApiError(HTTPStatus.NOT_FOUND, f"no route for {method} {path}")

    def _submit(self, name: str, timeout: float = 2.0, **args: Any) -> Any:
        try:
            return self.engine.submit(name, timeout, **args)
        except CommandError as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e)) from None
        except TimeoutError as e:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(e)) from None

    def _apply(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._submit("set_config", config=config)

    def _preset(self, fn):
        try:
            return fn()
        except KeyError:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such preset") from None
        except ValueError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None

    def _runs(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.engine.log_dir.glob("*.jsonl"), reverse=True)[:100]:
            try:
                with path.open(encoding="utf-8") as fh:
                    start = json.loads(fh.readline() or "{}")
            except (OSError, ValueError):
                start = {}
            slate = start.get("slate") or []
            out.append({"id": path.stem, "started": start.get("t"),
                        "run_number": start.get("run_number"),
                        "refresh_hz": start.get("refresh_hz"), "tc_rate": start.get("tc_rate"),
                        "slate": slate, "bytes": path.stat().st_size})
        return out

    def _run_file(self, run_id: str) -> _Raw:
        if not _RUN_ID_RE.match(run_id):
            raise ApiError(HTTPStatus.NOT_FOUND, "no such run")
        path = self.engine.log_dir / f"{run_id}.jsonl"
        if not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "no such run")
        return _Raw(path.read_bytes(), "application/x-ndjson",
                    {"Content-Disposition": f'attachment; filename="{run_id}.jsonl"'})


class _Raw:
    def __init__(self, data: bytes, content_type: str, headers: dict[str, str]):
        self.data, self.content_type, self.headers = data, content_type, headers


def _obj(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
    return body


class _Handler(BaseHTTPRequestHandler):
    app: ControlServer
    server_version = "lag-man"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep the generator console for frame warnings

    def do_GET(self) -> None:
        self._handle("GET")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            body = self._read_body()
            if not self._authorised():
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("WWW-Authenticate", 'Basic realm="lag-man", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            url = urlsplit(self.path)
            if url.path.startswith("/api/"):
                result, headers = self.app.route(method, url.path, parse_qs(url.query), body)
                if isinstance(result, _Raw):
                    self._send(HTTPStatus.OK, result.data, result.content_type, result.headers)
                else:
                    self._json(HTTPStatus.OK, result, headers)
            elif method == "GET":
                self._static(url.path)
            else:
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:  # report, never crash the server thread
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"internal error: {e}"})

    def _authorised(self) -> bool:
        password = self.app.password
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(given.encode(), password.encode())

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body is not valid JSON") from None

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".woff2":
            ctype = "font/woff2"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype,
                   {"Cache-Control": "no-cache"})

    def _json(self, status: HTTPStatus, data: Any, headers: dict[str, str] | None = None):
        self._send(status, json.dumps(data).encode(), "application/json", headers or {})

    def _send(self, status: HTTPStatus, data: bytes, ctype: str, headers: dict[str, str]):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
