"""Dashboard HTTP contract using the same server future browser tests will use."""
from contextlib import closing, contextmanager
import gzip
import hashlib
import http.client
import json
import os
import time
from urllib.parse import urlsplit

import agenticml
from agenticml.store import RunReader, RunWriter, list_runs
import pytest
import requests

pytestmark = pytest.mark.ui


@contextmanager
def http_api(server):
    with requests.Session() as session:
        session.trust_env = False
        session.headers["Accept-Encoding"] = "identity"

        def request(path, method="GET", **kwargs):
            return session.request(method, server.url + path, timeout=5, **kwargs)
        yield request


@pytest.fixture
def api(ui_server):
    with http_api(ui_server) as request:
        yield request


@pytest.fixture
def private_ui(tmp_path, ui_server_factory):
    """Deterministic large data and abandoned heartbeat in a disposable store."""
    from agenticml.ui.server import _SERIES_LIMIT
    root = tmp_path / "private-ui-store"
    dense = RunWriter.create(root, meta={"project": "private", "name": "dense"})
    dense.write_scalars([(step, {"dense": float(step)}) for step in range(5105)])
    dense.finish("finished")
    wide = RunWriter.create(root, meta={"project": "private", "name": "wide"})
    wide.write_scalars([(0, {f"metric/{i:03}": float(i) for i in range(_SERIES_LIMIT + 1)})])
    wide.finish("finished")
    dead = RunWriter.create(root, meta={"project": "private", "name": "dead"})
    dead.close()
    os.utime(root / "runs" / dead.run_id / "run.lock", (0, 0))
    with ui_server_factory(root) as server:
        server.runs = {"dense": dense.run_id, "wide": wide.run_id, "dead": dead.run_id}
        yield server


@pytest.fixture
def private_api(private_ui):
    with http_api(private_ui) as request:
        yield request


@pytest.mark.parametrize("path,content_type", [
    ("/", "text/html"), ("/static/app.js", "text/javascript"),
    ("/static/app.css", "text/css"), ("/static/charts.js", "text/javascript"),
    ("/static/code.js", "text/javascript"),
])
def test_static_files_and_etags(api, path, content_type):
    response = api(path)
    assert response.status_code == 200 and response.content
    assert response.headers["Content-Type"].startswith(content_type)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["ETag"] == '"' + hashlib.sha256(response.content).hexdigest() + '"'
    cached = api(path, headers={"If-None-Match": response.headers["ETag"]})
    assert cached.status_code == 304 and cached.content == b""
    assert cached.headers["ETag"] == response.headers["ETag"]


def test_run_list_and_metadata(api, ui_server):
    response = api("/api/runs")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    rows = response.json()
    assert len(rows) == 5
    assert {r["run_id"] for r in rows} == set(ui_server.runs.values())
    fields = {"run_id", "project", "name", "status", "created_at", "finished_at", "tags", "group", "summary"}
    for row in rows:
        assert fields <= row.keys()
        assert row["status"] == ("crashed" if row["name"] == "crashed" else "finished")
        assert row["created_at"] and row["finished_at"]
    baseline = next(r for r in rows if r["name"] == "explicit-baseline")
    assert baseline["project"] == "testbed" and baseline["tags"] == ["fixture", "baseline"]
    assert baseline["group"] == "synthetic" and baseline["summary"]["final_loss"] < 0.2
    details = api("/api/runs/" + baseline["run_id"]).json()
    rr = RunReader(ui_server.root, baseline["run_id"])
    assert details["config"]["lr"] == 0.15
    assert details["keys"] == rr.keys() and {"train/loss", "train/lr", "val/loss", "val/acc"} <= set(details["keys"])
    assert details["artifacts"] == rr.artifacts() and len(details["artifacts"]) == 2
    assert details["code_manifest_length"] == len(rr.code_manifest()) > 0
    high = api("/api/runs/" + ui_server.runs["explicit-highlr"]).json()
    assert high["config"]["lr"] == 0.4 and high["tags"] == ["fixture", "highlr"]
    zero = api("/api/runs/" + ui_server.runs["zero-code"]).json()
    assert not any(k.startswith("train/") for k in zero["keys"])


def test_scalars_filters_ranges_and_minimum_limit(api, ui_server):
    base = "/api/runs/" + ui_server.runs["explicit-baseline"] + "/scalars"
    rr = RunReader(ui_server.root, ui_server.runs["explicit-baseline"])
    full = api(base).json()
    assert set(full["series"]) == set(rr.keys()) and full["truncated"] is False
    assert full["series"]["train/loss"] == rr.series("train/loss")
    filtered = api(base, params=[("key", "train/loss"), ("key", "val/acc"),
                                ("key", "train/loss"), ("key", "unknown")]).json()
    assert set(filtered["series"]) == {"train/loss", "val/acc"}
    assert api(base, params={"key": "unknown"}).json()["series"] == {}
    selected = api(base, params={"key": "train/loss", "start": 4, "end": 8}).json()["series"]["train/loss"]
    assert selected == rr.series("train/loss", start=4, end=8)
    assert [p[0] for p in selected["points"]] == [4, 5, 6, 7, 8]
    for bounds in ({"start": 30}, {"end": 2}, {"start": 8, "end": 4}):
        result = api(base, params={"key": "train/loss", **bounds}).json()["series"]["train/loss"]
        assert result == rr.series("train/loss", **bounds)
    minimum = api(base, params={"key": "train/loss", "max_points": 10}).json()
    assert minimum["series"]["train/loss"]["mode"] == "overview"
    assert len(minimum["series"]["train/loss"]["points"]) <= 10
    for limit in (-100, 0, 1):
        assert api(base, params={"key": "train/loss", "max_points": limit}).json() == minimum
    assert api(base, params={"max_points": "bad"}).status_code == 400
    assert api(base, params={"start": "bad"}).status_code == 400
    assert api(base, params=[("key", str(i)) for i in range(201)]).status_code == 400


def test_time_media_and_code(api, ui_server):
    run_id = ui_server.runs["explicit-baseline"]
    base = "/api/runs/" + run_id
    rr = RunReader(ui_server.root, run_id)
    times = api(base + "/time").json()
    assert times == [list(p) for p in rr.time_index()] and times
    assert all(step >= 0 and seconds >= 0 for step, seconds in times)
    media = api(base + "/media").json()
    assert media == rr.media() and len(media["fit"]) == 1
    assert api(base + "/media", params={"key": "fit"}).json() == media["fit"]
    assert api(base + "/media", params={"key": "missing"}).json() == []
    manifest = api(base + "/code").json()
    assert manifest == rr.code_manifest()
    assert any(e["path"].endswith("dummytrain/model.py") for e in manifest)


@pytest.mark.parametrize("hint,expected", [("text/x-python", "text/plain"), ("text/html", "text/plain"),
                                          ("application/x-unknown", "application/octet-stream"),
                                          ("application/json", "application/json")])
def test_code_objects_type_hints(api, ui_server, hint, expected):
    manifest = api("/api/runs/" + ui_server.runs["explicit-baseline"] + "/code").json()
    entry = next(e for e in manifest if e["path"].endswith("dummytrain/model.py"))
    response = api("/api/objects/" + entry["sha256"], params={"type": hint})
    assert response.status_code == 200 and response.headers["Content-Type"] == expected
    assert hashlib.sha256(response.content).hexdigest() == entry["sha256"]
    assert b"class LogisticRegression" in response.content
    assert response.headers["Cache-Control"] == "immutable, max-age=31536000"


def test_image_object(api, ui_server):
    row = api("/api/runs/" + ui_server.runs["explicit-baseline"] + "/media").json()["fit"][0]
    response = api("/api/objects/" + row["sha256"], params={"type": row["content_type"]})
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    assert hashlib.sha256(response.content).hexdigest() == row["sha256"]
    assert "immutable" in response.headers["Cache-Control"]
    assert api("/api/objects/" + row["sha256"]).headers["Content-Type"] == "application/octet-stream"


@pytest.mark.parametrize("path,status", [
    ("/api/runs/.hidden", 400), ("/api/runs/bad%5Cid", 400),
    ("/api/runs/unknown-valid-id", 404), ("/api/runs/unknown-valid-id/scalars", 404),
    ("/unknown", 404), ("/static/missing.js", 404),
    ("/static/uPlot.iife.min.js", 404), ("/static/uPlot.min.css", 404),
    ("/static/uPlot.LICENSE", 404),
    ("/api/objects/bad", 400), ("/api/objects/" + "a" * 64, 404),
])
def test_errors(api, path, status):
    response = api(path)
    assert response.status_code == status
    assert response.headers["Content-Type"] == "application/json" and response.json()["error"]


def test_unknown_run_route(api, ui_server):
    base = "/api/runs/" + ui_server.runs["explicit-baseline"]
    assert api(base + "/unknown").status_code == 404
    assert api(base + "/scalars/extra").status_code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"])
def test_read_only_methods(api, method):
    response = api("/api/runs", method=method)
    assert response.status_code == 405 and response.headers["Allow"] == "GET"
    if method == "HEAD":
        assert response.content == b""
    else:
        assert response.json()["error"] == "method not allowed"


def test_forbidden_host(api):
    for method in ("GET", "POST"):
        response = api("/api/runs", method=method, headers={"Host": "evil.example"})
        assert response.status_code == 403 and response.json()["error"] == "forbidden host"


@pytest.mark.parametrize("hosts", [[], ["127.0.0.1", "localhost"]])
def test_missing_or_duplicate_host(ui_server, hosts):
    url = urlsplit(ui_server.url)
    with closing(http.client.HTTPConnection(url.hostname, url.port, timeout=5)) as connection:
        connection.putrequest("GET", "/api/runs", skip_host=True)
        for host in hosts:
            connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"] == "forbidden host"


def test_gzip_negotiation(private_api, private_ui):
    api = private_api
    path = "/api/runs/" + private_ui.runs["dense"] + "/scalars?key=dense&max_points=5000"
    expected = api(path)
    assert len(expected.content) > 1024
    with api(path, headers={"Accept-Encoding": "gzip"}, stream=True) as response:
        assert response.status_code == 200 and response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        raw = response.raw.read(decode_content=False)
        assert raw.startswith(b"\x1f\x8b")
        assert json.loads(gzip.decompress(raw)) == expected.json()
    plain = api(path, headers={"Accept-Encoding": "gzip;q=0, *;q=1"})
    assert "Content-Encoding" not in plain.headers
    assert plain.content == expected.content
    tiny = api("/api/runs/" + private_ui.runs["dense"] + "/media", headers={"Accept-Encoding": "gzip"})
    assert tiny.status_code == 200 and tiny.json() == {} and len(tiny.content) < 1024
    assert "Content-Encoding" not in tiny.headers
    # Static files and tiny error JSON do not use the JSON gzip path.
    assert "Content-Encoding" not in api("/static/app.js", headers={"Accept-Encoding": "gzip"}).headers
    assert "Content-Encoding" not in api("/missing", headers={"Accept-Encoding": "gzip"}).headers


def test_live_run_updates(private_api, private_ui, trained_store):
    api = private_api
    assert len(list_runs(trained_store.root)) == 5
    run = agenticml.init("live-project", "live", dir=private_ui.root, flush_interval=0.01,
                         system_metrics=False, auto_track=False, track_source=False, capture_env=False)
    base = "/api/runs/" + run.run_id

    def wait_for_points(key, count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = api(base + "/scalars", params={"key": key}).json()["series"].get(key)
            if result and result["total"] == count:
                return result
            time.sleep(0.01)
        pytest.fail(f"live series {key} did not reach {count} points")

    try:
        row = next(r for r in api("/api/runs").json() if r["run_id"] == run.run_id)
        assert row["status"] == "running" and row["finished_at"] is None
        agenticml.log({"live/loss": 2})
        assert wait_for_points("live/loss", 1)["points"] == [[0, 2.0]]
        agenticml.log({"live/loss": 1})
        assert wait_for_points("live/loss", 2)["points"] == [[0, 2.0], [1, 1.0]]
        agenticml.finish()
        row = next(r for r in api("/api/runs").json() if r["run_id"] == run.run_id)
        assert row["status"] == "finished" and row["finished_at"]
        assert api(base).json()["status"] == "finished"
    finally:
        agenticml.finish()
        assert len(list_runs(trained_store.root)) == 5


def test_maximum_limit_on_prebuilt_series(private_api, private_ui):
    path = "/api/runs/" + private_ui.runs["dense"] + "/scalars"
    capped = private_api(path, params={"key": "dense", "max_points": 5000}).json()
    series = capped["series"]["dense"]
    assert series["total"] == 5105 and series["mode"] == "overview"
    assert 0 < len(series["points"]) <= 5000
    assert private_api(path, params={"key": "dense", "max_points": 999999}).json() == capped


def test_series_count_truncation(private_api, private_ui):
    from agenticml.ui.server import _SERIES_LIMIT
    run_id = private_ui.runs["wide"]
    assert len(RunReader(private_ui.root, run_id).keys()) == _SERIES_LIMIT + 1
    result = private_api("/api/runs/" + run_id + "/scalars").json()
    assert result["truncated"] is True and len(result["series"]) == _SERIES_LIMIT
    assert all(series["points"] for series in result["series"].values())


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX lifetime run locks")
def test_lock_free_old_heartbeat_looks_crashed(private_api, private_ui):
    run_id = private_ui.runs["dead"]
    assert RunReader(private_ui.root, run_id).meta()["status"] == "running"
    row = next(r for r in private_api("/api/runs").json() if r["run_id"] == run_id)
    assert row["status"] == "crashed"
