import base64
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from camlag.core.config import GeneratorConfig
from camlag.generator.engine import Engine
from camlag.web.presets import PresetStore
from camlag.web.server import ControlServer, deep_merge


@pytest.fixture
def srv(tmp_path):
    yield from _server(tmp_path)


@pytest.fixture
def locked(tmp_path):
    yield from _server(tmp_path, password="hunter2")


def _server(tmp_path, password=None):
    eng = Engine(GeneratorConfig(), 1920, 1080, tmp_path / "runs")
    stop = threading.Event()
    t = [0]

    def loop():
        while not stop.is_set():
            eng.frame()
            t[0] += 10_000_000
            eng.presented(t[0])
            time.sleep(0.001)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    server = ControlServer(eng, PresetStore(tmp_path / "presets"), "127.0.0.1", 0, password)
    server.start()
    yield f"http://127.0.0.1:{server.port}", eng
    server.stop()
    stop.set()
    th.join()


def req(base, method, path, body=None, auth=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if auth:
        r.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload)), resp.headers
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, (payload if raw else json.loads(payload or b"{}")), e.headers


def test_status_and_run_cycle(srv):
    base, eng = srv
    code, st, _ = req(base, "GET", "/api/status")
    assert code == 200 and st["state"] == "idle" and st["refresh_hz"] == 100
    code, st, _ = req(base, "POST", "/api/start")
    assert code == 200 and st["state"] == "running"
    assert req(base, "POST", "/api/start")[0] == 409
    time.sleep(0.05)
    code, st, _ = req(base, "POST", "/api/stop")
    assert st["state"] == "idle"
    code, runs, _ = req(base, "GET", "/api/runs")
    assert len(runs["runs"]) == 1
    run_id = runs["runs"][0]["id"]
    code, log, headers = req(base, "GET", f"/api/runs/{run_id}", raw=True)
    assert code == 200 and b"run_start" in log and "attachment" in headers["Content-Disposition"]
    assert req(base, "GET", "/api/runs/..%2Fstate", raw=True)[0] == 404


def test_config_patch_and_errors(srv):
    base, _ = srv
    code, cfg, _ = req(base, "PUT", "/api/config", {"clock": {"size": 0.2}})
    assert code == 200 and cfg["clock"]["size"] == 0.2 and cfg["clock"]["cx"] == 0.5
    code, err, _ = req(base, "PUT", "/api/config", {"tc_rate": 7})
    assert code == 409 and "time code rate" in err["error"]
    code, err, _ = req(base, "PUT", "/api/config", [1, 2])
    assert code == 400
    code, st, _ = req(base, "POST", "/api/start-value", {"value": "01:02:03:04"})
    assert code == 200
    time.sleep(0.02)
    assert req(base, "GET", "/api/status")[1]["timecode"].startswith("01:02:03:04")
    assert req(base, "POST", "/api/start-value", {"value": "xx"})[0] == 409


def test_presets(srv):
    base, _ = srv
    req(base, "PUT", "/api/config", {"clock": {"cx": 0.3}})
    assert req(base, "PUT", "/api/presets/Camera%20A%2025fps")[0] == 200
    assert req(base, "GET", "/api/presets")[1]["presets"] == ["Camera A 25fps"]
    req(base, "PUT", "/api/config", {"clock": {"cx": 0.7}})
    code, cfg, _ = req(base, "POST", "/api/presets/Camera%20A%2025fps/load")
    assert code == 200 and cfg["clock"]["cx"] == 0.3
    # a reloaded preset reproduces the same display
    assert req(base, "GET", "/api/config")[1] == cfg
    code, data, headers = req(base, "GET", "/api/presets/Camera%20A%2025fps?download=1")
    assert "attachment" in headers["Content-Disposition"] and data == cfg
    assert req(base, "PUT", "/api/presets/..%2Fevil")[0] == 400
    assert req(base, "PUT", "/api/presets/bad", {"clock": {"cx": 9}})[0] == 400
    assert req(base, "DELETE", "/api/presets/Camera%20A%2025fps")[1]["presets"] == []
    assert req(base, "GET", "/api/presets/nope")[0] == 404


def test_preview(srv):
    base, _ = srv
    code, pv, _ = req(base, "GET", "/api/preview")
    assert code == 200 and pv["width"] == 1920 and pv["ops"]


def test_static_and_traversal(srv):
    base, _ = srv
    code, html, headers = req(base, "GET", "/", raw=True)
    assert code == 200 and b"cam-lag" in html and headers["Content-Type"] == "text/html"
    assert req(base, "GET", "/tokens.css", raw=True)[0] == 200
    assert req(base, "GET", "/../server.py", raw=True)[0] == 404
    assert req(base, "GET", "/%2e%2e/server.py", raw=True)[0] == 404
    assert req(base, "GET", "/api/nope")[0] == 404


def test_password(locked):
    base, _ = locked
    code, _, headers = req(base, "GET", "/api/status", raw=True)
    assert code == 401 and "Basic" in headers["WWW-Authenticate"]
    assert req(base, "GET", "/api/status", auth="x:wrong", raw=True)[0] == 401
    assert req(base, "GET", "/api/status", auth="anyone:hunter2")[0] == 200


def test_deep_merge():
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}


def test_static_tokens_match_design():
    from pathlib import Path

    root = Path(__file__).parents[1]
    static = root / "src/camlag/web/static"
    assert (static / "tokens.css").read_bytes() == (root / "design/tokens.css").read_bytes()
