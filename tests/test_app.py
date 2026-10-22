"""The analysis app's HTTP API, driven as the browser drives it."""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("cv2")

from camlag.analysis import app as app_mod  # noqa: E402
from camlag.analysis import synth  # noqa: E402
from camlag.analysis.setup import Setup  # noqa: E402

SIZE = (960, 540)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("clip") / "A001.mp4"
    synth.make(out, seconds=0.5, latency_ticks=8, size=SIZE, transition_rate=0.1)
    return out


@pytest.fixture
def server(tmp_path):
    httpd, app = app_mod.make_server(tmp_path / "ws", 0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    client = Client(f"http://127.0.0.1:{httpd.server_address[1]}", app.token)
    yield client, app
    httpd.shutdown()
    httpd.server_close()


class Client:
    def __init__(self, base, token):
        self.base, self.token = base, token

    def call(self, method, path, body=None, token=True, host=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Camlag-Token", self.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = resp.read()
                return resp.status, payload if raw else json.loads(payload), resp.headers
        except urllib.error.HTTPError as e:
            payload = e.read()
            return e.code, payload if raw else json.loads(payload or b"{}"), e.headers

    def ok(self, method, path, body=None):
        code, data, _ = self.call(method, path, body)
        assert code == 200, data
        return data


def wait_job(c, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = next(j for j in c.ok("GET", "/api/state")["jobs"] if j["id"] == job_id)
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_page_and_security(server):
    c, app = server
    code, html, headers = c.call("GET", "/", token=False, raw=True)
    assert code == 200 and app.token.encode() in html
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert c.call("GET", "/app/app.js", token=False, raw=True)[0] == 200
    assert c.call("GET", "/static/theme.js", token=False, raw=True)[0] == 200
    # no token, wrong token, wrong host (DNS rebinding)
    assert c.call("GET", "/api/state", token=False)[0] == 403
    c2 = Client(c.base, "nope")
    assert c2.call("GET", "/api/state")[0] == 403
    assert c.call("GET", "/api/state", host="evil.example:80")[0] == 403
    assert c.call("GET", "/", token=False, host="evil.example", raw=True)[0] == 403
    # GETs may carry the token in the query, for images and links
    code, _, _ = c.call("GET", f"/api/state?t={app.token}", token=False)
    assert code == 200
    assert c.call("GET", "/app/../app.py", token=False, raw=True)[0] == 404


def test_file_browser(server, clip):
    c, _ = server
    out = c.ok("GET", f"/api/fs?path={clip.parent}")
    assert out["path"] == str(clip.parent.resolve())
    kinds = {f["name"]: f["kind"] for f in out["files"]}
    assert kinds["A001.mp4"] == "video" and kinds["A001.run.jsonl"] == "runlog"
    assert "A001.setup.json" not in kinds  # only videos and run logs are listed
    assert c.call("GET", "/api/fs?path=/no/such/dir")[0] == 400


def test_whole_workflow(server, clip):
    c, app = server
    assert c.call("POST", "/api/recordings", {"video": "/no/such.mp4"})[0] == 400
    rec = c.ok("POST", "/api/recordings", {"video": str(clip)})
    rid = rec["id"]
    assert rid == "A001"
    assert c.ok("POST", "/api/recordings", {"video": str(clip)})["id"] == "A001-2"
    c.ok("DELETE", "/api/recordings/A001-2")

    # run log: suggested, then attached
    logs = c.ok("GET", f"/api/runlogs?dir={clip.parent}&recording={rid}")["runlogs"]
    assert logs[0]["name"] == "A001.run.jsonl" and logs[0]["camera"] == "Synthetic cam"
    assert c.call("PATCH", f"/api/recordings/{rid}", {"run_log": str(clip)})[0] == 400
    c.ok("PATCH", f"/api/recordings/{rid}", {"run_log": logs[0]["path"]})
    info = c.ok("GET", f"/api/recordings/{rid}/info")
    assert info["run"]["refresh_hz"] == 100 and info["run"]["slate"]["CAMERA"] == "Synthetic cam"
    assert info["video"]["frames"] == 50 and info["setup"] is None

    code, jpg, _ = c.call("GET", f"/api/recordings/{rid}/frame?n=3", raw=True)
    assert code == 200 and jpg[:2] == b"\xff\xd8"

    # analysing before marking is refused
    assert c.call("POST", f"/api/recordings/{rid}/analyse", {})[0] == 400

    # regions: hand-marked a little off, checked live, snapped on save
    truth = Setup.load(clip.with_suffix(".setup.json"))
    body = {"frame": 0, "source": truth.source.adjusted(0.3, -0.3).to_json(),
            "wall": truth.wall.adjusted(-0.2, 0.3).to_json()}
    check = c.ok("POST", f"/api/recordings/{rid}/check", body)
    assert check["source"]["ok_frames"] > 0 and check["latency_ticks"]
    assert c.call("POST", f"/api/recordings/{rid}/regions", {**body, "name": "../x"})[0] == 400
    saved = c.ok("POST", f"/api/recordings/{rid}/regions", {**body, "name": "Cam pos 1"})
    assert saved["clean"]["source"] >= 8 and saved["clean"]["wall"] >= 8
    state = c.ok("GET", "/api/state")
    assert state["setups"] == ["Cam pos 1"] and state["recordings"][0]["setup"] == "Cam pos 1"

    # analyse in the background
    job = c.ok("POST", f"/api/recordings/{rid}/analyse", {})
    assert c.call("POST", f"/api/recordings/{rid}/analyse", {})[0] in (200, 409)
    job = wait_job(c, job["id"])
    assert job["state"] == "done", job["message"]
    assert job["done"] == job["total"] == 50

    res = c.ok("GET", f"/api/recordings/{rid}/results")
    assert res["summary"]["n"] > 30 and res["summary"]["mean_ms"] == pytest.approx(80, abs=10)
    assert len(res["rows"]) == 50 and 'data-frame="' in res["charts"]["scatter"]
    sample = c.ok("GET", f"/api/recordings/{rid}/sample?n=5")
    assert sample["status"] in ("ok", "transition")
    code, overlay, _ = c.call("GET", f"/api/recordings/{rid}/frame?n=5&overlay=1", raw=True)
    assert code == 200 and overlay[:2] == b"\xff\xd8"
    code, html, _ = c.call("GET", f"/api/recordings/{rid}/files/report.html", raw=True)
    assert code == 200 and b"Synthetic cam" in html
    code, _, headers = c.call("GET", f"/api/recordings/{rid}/files/results.csv?download=1",
                              raw=True)
    assert "attachment" in headers["Content-Disposition"]
    assert c.call("GET", f"/api/recordings/{rid}/files/workspace.json", raw=True)[0] == 404

    rows = c.ok("GET", "/api/combined")["rows"]
    assert rows[0]["camera"] == "Synthetic cam" and rows[0]["runs"] == 1
    code, csv_data, _ = c.call("GET", "/api/combined.csv", raw=True)
    assert csv_data.startswith(b"camera,camera_fps")

    # the workspace survives a restart
    again = app_mod.Workspace(app.ws.root)
    assert again.get(rid).setup == "Cam pos 1" and again.get(rid).analysed

    c.ok("DELETE", f"/api/recordings/{rid}")
    assert not app.ws.results_dir(rid).exists() and clip.exists()


def test_cancel(server, clip):
    c, app = server
    rid = c.ok("POST", "/api/recordings", {"video": str(clip), "rate_set": "50"})["id"]
    app.ws.save_setup("s", Setup.load(clip.with_suffix(".setup.json")))
    c.ok("PATCH", f"/api/recordings/{rid}", {"setup": "s"})
    gate = threading.Event()
    real = app_mod.run

    def slow_run(*args, **kwargs):
        gate.wait(5)
        return real(*args, **kwargs)

    app_mod.run = slow_run
    try:
        job = c.ok("POST", f"/api/recordings/{rid}/analyse", {})
        c.ok("POST", f"/api/jobs/{job['id']}/cancel")
        gate.set()
        assert wait_job(c, job["id"])["state"] == "cancelled"
    finally:
        app_mod.run = real


def test_demo_job(server, monkeypatch):
    c, app = server
    real = synth.make
    monkeypatch.setattr(synth, "make", lambda out, **kw: real(
        out, **{**kw, "seconds": 0.2, "size": SIZE}))
    job = wait_job(c, c.ok("POST", "/api/demo")["id"])
    assert job["state"] == "done", job["message"]
    rec = c.ok("GET", "/api/state")["recordings"][0]
    assert rec["setup"] == "demo-1" and rec["run_log"]


def test_fetch_run_logs_from_generator(server, tmp_path):
    from camlag.core.config import GeneratorConfig
    from camlag.generator.engine import Engine
    from camlag.web.presets import PresetStore
    from camlag.web.server import ControlServer

    c, app = server
    eng = Engine(GeneratorConfig(), 1920, 1080, tmp_path / "gen-runs")
    stop = threading.Event()

    def loop():
        t = 0
        while not stop.is_set():
            eng.frame()
            t += 10_000_000
            eng.presented(t)
            time.sleep(0.001)

    threading.Thread(target=loop, daemon=True).start()
    gen = ControlServer(eng, PresetStore(tmp_path / "p"), "127.0.0.1", 0, password="pw")
    gen.start()
    try:
        eng.submit("start")
        time.sleep(0.05)
        eng.submit("stop")
        url = f"127.0.0.1:{gen.port}"
        code, err, _ = c.call("POST", "/api/generator/runs", {"url": url})
        assert code == 400 and "password" in err["error"]
        runs = c.ok("POST", "/api/generator/runs", {"url": url, "password": "pw"})["runs"]
        assert len(runs) == 1 and not runs[0]["fetched"]
        got = c.ok("POST", "/api/generator/fetch",
                   {"url": url, "password": "pw", "run_id": runs[0]["id"]})
        assert Path(got["path"]).parent == app.ws.runlog_dir()
        assert c.ok("POST", "/api/generator/runs", {"url": url, "password": "pw"})["runs"][0][
            "fetched"]
        assert c.call("POST", "/api/generator/fetch",
                      {"url": url, "password": "pw", "run_id": "../../etc"})[0] == 400
        assert c.ok("GET", "/api/state")["generator_url"] == f"http://{url}"
        unreachable = c.call("POST", "/api/generator/runs", {"url": "127.0.0.1:1"})
        assert unreachable[0] == 400 and "cannot reach" in unreachable[1]["error"]
    finally:
        gen.stop()
        stop.set()
