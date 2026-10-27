"""HTTP-level tests for Client. Uses `responses` to mock the server."""

from __future__ import annotations

import hashlib
import json

import pytest
import requests
import responses
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, ProtocolError

from agenticml.client import Client


BASE = "https://api.test.local"


@pytest.fixture
def client():
    return Client(base_url=BASE, timeout=2, max_retries=2, api_key="kkk")


@responses.activate
def test_init_run_returns_id(client):
    responses.add(
        responses.POST, f"{BASE}/init",
        json={"run_id": "abc123"}, status=200,
    )
    rid = client.init_run({"project": "p", "name": "n"})
    assert rid == "abc123"

    sent = responses.calls[0].request
    assert sent.headers["x-api-key"] == "kkk"


@responses.activate
def test_log_batch_skips_empty(client):
    # Should NOT make any HTTP call when items is empty.
    client.log_batch([])
    assert len(responses.calls) == 0


@responses.activate
def test_log_batch_posts_items(client):
    responses.add(responses.POST, f"{BASE}/log/batch", status=200)
    client.log_batch([{"run_id": "r1", "step": 1, "metrics": {"loss": 0.1}}])
    assert len(responses.calls) == 1
    assert b'"items"' in responses.calls[0].request.body


@responses.activate
def test_artifacts_exists_short_circuits_on_empty(client):
    assert client.artifacts_exists([]) == []
    assert len(responses.calls) == 0


@responses.activate
def test_artifacts_exists_returns_list(client):
    responses.add(
        responses.POST, f"{BASE}/artifacts/exists",
        json={"existing": ["sha1", "sha2"]}, status=200,
    )
    out = client.artifacts_exists(["sha1", "sha2", "sha3"])
    assert out == ["sha1", "sha2"]


@responses.activate
def test_finalize_posts_summary(client):
    responses.add(responses.POST, f"{BASE}/runs/r1/finalize", status=200)
    client.finalize("r1", {"final": 1.0}, status="finished")
    body = json.loads(responses.calls[0].request.body)
    assert body == {"summary": {"final": 1.0}, "status": "finished"}


@responses.activate
def test_idempotent_post_retries_status(client):
    seen = []

    def fail(request):
        seen.append(request.url)
        return 503, {}, "{}"

    responses.add_callback(responses.POST, f"{BASE}/log/batch", callback=fail)
    with pytest.raises(requests.HTTPError):
        client.log_batch([{"run_id": "r", "step": 1, "metrics": {"loss": 1.0}}])
    # max_retries=2 => 1 original + 2 retries.
    assert len(seen) == 3


@responses.activate
def test_5xx_then_success(client):
    responses.add(responses.POST, f"{BASE}/log/batch", status=503)
    responses.add(responses.POST, f"{BASE}/log/batch", status=200)
    client.log_batch([{"run_id": "r", "step": 1, "metrics": {"loss": 0.1}}])
    assert len(responses.calls) == 2


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", "https://override.example")
    monkeypatch.setenv("AGENTICML_API_KEY", "envkey")
    c = Client()
    assert c.base_url == "https://override.example"
    assert c.api_key == "envkey"


def test_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", "https://env.example")
    c = Client(base_url="https://explicit.example")
    assert c.base_url == "https://explicit.example"


@responses.activate
def test_upload_code_empty_skips_requests(client):
    assert client.upload_code("r1", []) == {"status": "ok", "registered": []}
    assert not responses.calls


@pytest.mark.parametrize("existing", [[], ["sha1"], ["sha1", "sha2"]])
@responses.activate
def test_upload_code_presigns_only_missing_and_registers_all(client, existing):
    files = [
        {"path": "train.py", "sha256": "sha1", "content": b"print(1)", "content_type": "text/x-python"},
        {"path": "config.yaml", "sha256": "sha2", "content": b"lr: 0.1", "content_type": "text/yaml", "source": "extra"},
    ]
    missing = [item for item in files if item["sha256"] not in existing]
    responses.add(responses.POST, f"{BASE}/artifacts/exists", json={"existing": existing})
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload",
        json={"uploads": [
            {"path": item["path"], "url": f"https://bucket.test/{item['path']}",
             "headers": {"Content-Type": item["content_type"]}}
            for item in missing
        ]},
    )
    for item in missing:
        responses.add(responses.PUT, f"https://bucket.test/{item['path']}", status=200)
    responses.add(responses.POST, f"{BASE}/runs/r1/artifacts/register", json={"status": "ok"})

    assert client.upload_code("r1", files) == {"status": "ok"}
    presigned = json.loads(responses.calls[1].request.body)["files"]
    assert {item["path"] for item in presigned} == {item["path"] for item in missing}
    registered = json.loads(responses.calls[-1].request.body)["files"]
    assert {item["path"] for item in registered} == {item["path"] for item in files}
    assert registered[1]["source"] == "extra"
    for call, item in zip(responses.calls[2:-1], missing):
        assert call.request.body == item["content"]
        assert call.request.headers["Content-Type"] == item["content_type"]
        assert "x-api-key" not in call.request.headers
    assert all("stored" not in item for item in files)


@responses.activate
def test_upload_code_recovers_missing_deduplicated_content(client):
    files = [
        {"path": "train.py", "sha256": "sha1", "content": b"print(1)", "content_type": "text/x-python"},
        {"path": "config.yaml", "sha256": "sha2", "content": b"lr: 0.1", "content_type": "text/yaml"},
    ]
    responses.add(responses.POST, f"{BASE}/artifacts/exists", json={"existing": ["sha1", "sha2"]})
    responses.add(responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload", json={"uploads": []})
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/register",
        json={"detail": {"missing": ["train.py"]}}, status=409,
    )
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload",
        json={"uploads": [{"path": "train.py", "url": "https://bucket.test/recover"}]},
    )
    responses.add(responses.PUT, "https://bucket.test/recover", status=200)
    responses.add(responses.POST, f"{BASE}/runs/r1/artifacts/register", json={"status": "ok"})

    assert client.upload_code("r1", files) == {"status": "ok"}
    assert json.loads(responses.calls[1].request.body) == {"files": []}
    recovery = json.loads(responses.calls[3].request.body)["files"]
    assert [item["path"] for item in recovery] == ["train.py"]
    assert responses.calls[4].request.body == b"print(1)"
    assert "x-api-key" not in responses.calls[4].request.headers
    assert responses.calls[2].request.body == responses.calls[-1].request.body


@responses.activate
def test_upload_code_continues_when_dedup_query_fails(client):
    responses.add(responses.POST, f"{BASE}/artifacts/exists", status=400)
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload",
        json={"uploads": [{"path": "train.py", "url": "https://bucket.test/train.py"}]},
    )
    responses.add(responses.PUT, "https://bucket.test/train.py", status=200)
    responses.add(responses.POST, f"{BASE}/runs/r1/artifacts/register", json={"status": "ok"})
    client.upload_code("r1", [
        {"path": "train.py", "sha256": "sha1", "content": b"print(1)", "content_type": "text/x-python", "stored": True},
    ])
    assert json.loads(responses.calls[1].request.body)["files"][0]["path"] == "train.py"
    assert responses.calls[2].request.body == b"print(1)"


@pytest.mark.parametrize("dedup", [False, True])
@responses.activate
def test_log_artifact_presigns_and_registers_version(client, tmp_path, dedup):
    artifact = tmp_path / "model.bin"
    content = b"model weights"
    artifact.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload",
        json={"uploads": [] if dedup else [{"path": artifact.name, "url": "https://bucket.test/model"}]},
    )
    if not dedup:
        responses.add(responses.PUT, "https://bucket.test/model", status=200)
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/register-version",
        json={"id": "a1", "version": 2},
    )
    result = client.log_artifact("r1", "model", "checkpoint", {"epoch": 3}, artifact, "application/octet-stream")
    assert result == {"id": "a1", "version": 2}
    descriptor = json.loads(responses.calls[0].request.body)["files"][0]
    assert descriptor == {
        "path": "model.bin", "sha256": sha, "size_bytes": len(content),
        "content_type": "application/octet-stream", "source": "user",
    }
    registered = json.loads(responses.calls[-1].request.body)
    assert registered == {
        "name": "model", "type": "checkpoint", "metadata": {"epoch": 3},
        "sha256": sha, "size_bytes": len(content), "content_type": "application/octet-stream",
        "filename": "model.bin",
    }
    if not dedup:
        uploaded = responses.calls[1].request
        assert uploaded.body == content
        assert uploaded.headers["Content-Length"] == str(len(content))
        assert "x-api-key" not in uploaded.headers


@responses.activate
def test_log_media_posts_file_and_fields(client, tmp_path):
    media = tmp_path / "plot.png"
    media.write_bytes(b"image bytes")
    responses.add(responses.POST, f"{BASE}/runs/r1/log_media", json={"id": "m1"})
    assert client.log_media("r1", "preview", 7, "image", "caption", media, "image/png") == {"id": "m1"}
    sent = responses.calls[0].request
    assert sent.headers["Content-Type"].startswith("multipart/form-data;")
    for field in (b"preview", b"7", b"image", b"caption", b"plot.png", b"image bytes"):
        assert field in sent.body
    assert sent.headers["x-api-key"] == "kkk"


def test_host_is_normalised_and_remote_http_needs_insecure():
    assert Client(base_url="HTTPS://API.test.local:443/").base_url == "https://api.test.local"
    assert Client(base_url="http://127.0.0.1:80/").base_url == "http://127.0.0.1"
    assert Client(base_url="http://example.com", insecure=True).base_url == "http://example.com"
    with pytest.raises(ValueError, match="insecure"):
        Client(base_url="http://example.com")
    with pytest.raises(ValueError, match="scheme"):
        Client(base_url="ftp://example.com")


def test_connect_retry_ignores_read_and_status(client):
    retry = client._once.get_adapter("https://").max_retries
    assert (retry.connect, retry.read, retry.status, retry.other) == (2, 0, 0, 0)
    full = client.session.get_adapter("https://").max_retries
    assert full.read == 2 and 503 in full.status_forcelist
    nxt = retry.increment("POST", f"{BASE}/init", error=ConnectTimeoutError("timeout"))
    assert nxt.connect == 1
    with pytest.raises(MaxRetryError):
        retry.increment("POST", f"{BASE}/init", error=ProtocolError("reset"))
    with pytest.raises(MaxRetryError):
        retry.increment("POST", f"{BASE}/init", error=RuntimeError("other"))


def test_init_timeout_is_at_least_30s(client, monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"run_id": "abc"}

        def raise_for_status(self):
            return None

    def fake_request(_method, _url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(client._once, "request", fake_request)
    assert client.init_run({"project": "p", "name": "n"}) == "abc"
    assert captured["timeout"] == 30
    assert captured["allow_redirects"] is False


@responses.activate
def test_redirect_with_api_key_is_refused(client):
    responses.add(
        responses.POST, f"{BASE}/init", status=302,
        headers={"Location": "https://evil.example/steal"},
    )
    with pytest.raises(ValueError, match="server redirected; refusing to send the API key") as exc:
        client.init_run({"project": "p", "name": "n"})
    assert "evil.example" not in str(exc.value)
    assert len(responses.calls) == 1

    responses.add(
        responses.GET, f"{BASE}/runs", status=302,
        headers={"Location": "https://evil.example/runs"},
    )
    with pytest.raises(ValueError, match="server redirected; refusing to send the API key"):
        client.check_auth()
    assert len(responses.calls) == 2


@responses.activate
@pytest.mark.parametrize("kind", ["init", "finalize", "media", "version"])
def test_non_idempotent_post_does_not_retry_status(client, tmp_path, kind):
    seen = []

    def fail(request):
        seen.append(request.url)
        return 503, {}, "{}"

    if kind == "init":
        responses.add_callback(responses.POST, f"{BASE}/init", callback=fail)
        with pytest.raises(requests.HTTPError):
            client.init_run({"project": "p", "name": "n"})
    elif kind == "finalize":
        responses.add_callback(responses.POST, f"{BASE}/runs/r/finalize", callback=fail)
        with pytest.raises(requests.HTTPError):
            client.finalize("r", {"a": 1})
    elif kind == "media":
        media = tmp_path / "plot.png"
        media.write_bytes(b"image")
        responses.add_callback(responses.POST, f"{BASE}/runs/r/log_media", callback=fail)
        with pytest.raises(requests.HTTPError):
            client.log_media("r", "preview", 1, "image", "", media, "image/png")
    else:
        artifact = tmp_path / "model.bin"
        artifact.write_bytes(b"weights")
        responses.add(responses.POST, f"{BASE}/runs/r/artifacts/presign-upload", json={"uploads": []})
        responses.add_callback(
            responses.POST, f"{BASE}/runs/r/artifacts/register-version", callback=fail,
        )
        with pytest.raises(requests.HTTPError):
            client.log_artifact("r", "model", "file", {}, artifact, "application/octet-stream")
    assert len(seen) == 1


@responses.activate
def test_code_presign_and_register_retry_status(client):
    seen = {"exists": 0, "presign": 0, "register": 0}

    def count(key, succeed_on):
        def callback(request):
            seen[key] += 1
            if seen[key] < succeed_on:
                return 503, {}, "{}"
            if key == "exists":
                return 200, {}, json.dumps({"existing": []})
            if key == "presign":
                return 200, {}, json.dumps({"uploads": []})
            return 200, {}, json.dumps({"status": "ok"})
        return callback

    responses.add_callback(responses.POST, f"{BASE}/artifacts/exists", callback=count("exists", 3))
    responses.add_callback(
        responses.POST, f"{BASE}/runs/r/artifacts/presign-upload", callback=count("presign", 3),
    )
    responses.add_callback(
        responses.POST, f"{BASE}/runs/r/artifacts/register", callback=count("register", 3),
    )
    assert client.upload_code("r", [{
        "path": "train.py", "sha256": "sha1", "content": b"print(1)", "content_type": "text/x-python",
    }]) == {"status": "ok"}
    assert seen == {"exists": 3, "presign": 3, "register": 3}


def test_retry_after_ignored_and_bounded_backoff(client):
    once_retry = client._once.get_adapter("https://").max_retries
    session_retry = client.session.get_adapter("https://").max_retries
    for r in (once_retry, session_retry):
        assert r.respect_retry_after_header is False
        assert r.backoff_max == 30.0
        assert r.backoff_factor == 0.5


@responses.activate
def test_log_media_accepts_file_object_and_bytes(client, tmp_path):
    media_file = tmp_path / "plot.png"
    content = b"png binary data"
    media_file.write_bytes(content)

    responses.add(responses.POST, f"{BASE}/runs/r1/log_media", json={"id": "m1"})
    with open(media_file, "rb") as fh:
        res = client.log_media("r1", "preview", 5, "image", "caption1", fh, "image/png", filename="plot.png")
    assert res == {"id": "m1"}
    assert content in responses.calls[0].request.body

    responses.add(responses.POST, f"{BASE}/runs/r1/log_media", json={"id": "m2"})
    res2 = client.log_media("r1", "preview", 6, "image", "caption2", content, "image/png")
    assert res2 == {"id": "m2"}
    assert content in responses.calls[1].request.body


@responses.activate
def test_log_artifact_accepts_file_object_and_bytes(client, tmp_path):
    content = b"artifact weights binary"
    sha = hashlib.sha256(content).hexdigest()
    file_path = tmp_path / "model.bin"
    file_path.write_bytes(content)

    # 1. Binary file object
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload",
        json={"uploads": [{"path": "model.bin", "url": "https://bucket.test/model"}]},
    )
    responses.add(responses.PUT, "https://bucket.test/model", status=200)
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/register-version",
        json={"id": "a1", "version": 1},
    )

    with open(file_path, "rb") as fh:
        res1 = client.log_artifact("r1", "model", "checkpoint", {}, fh, "application/octet-stream", filename="model.bin")
    assert res1 == {"id": "a1", "version": 1}
    uploaded = responses.calls[1].request
    assert uploaded.body == content
    registered = json.loads(responses.calls[2].request.body)
    assert registered["sha256"] == sha
    assert registered["size_bytes"] == len(content)

    # 2. Bytes directly
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/presign-upload",
        json={"uploads": [{"path": "model_bytes", "url": "https://bucket.test/bytes"}]},
    )
    responses.add(responses.PUT, "https://bucket.test/bytes", status=200)
    responses.add(
        responses.POST, f"{BASE}/runs/r1/artifacts/register-version",
        json={"id": "a2", "version": 2},
    )

    res2 = client.log_artifact("r1", "model_bytes", "checkpoint", {}, content, "application/octet-stream")
    assert res2 == {"id": "a2", "version": 2}
    uploaded2 = responses.calls[4].request
    assert uploaded2.body == content
    registered2 = json.loads(responses.calls[5].request.body)
    assert registered2["sha256"] == sha
    assert registered2["size_bytes"] == len(content)

