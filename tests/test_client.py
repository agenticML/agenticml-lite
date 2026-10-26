"""HTTP-level tests for Client. Uses `responses` to mock the server."""

from __future__ import annotations

import hashlib
import json

import pytest
import responses
from requests.exceptions import RetryError

from agenticml.client import Client, _OfflineClient, make_client, resolve_dir


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
    client.finalize("r1", {"final": 1.0}, status="finished", snapshot_hash="abc123")
    body = responses.calls[0].request.body
    assert b"finished" in body
    assert b"final" in body
    assert b"abc123" in body


@responses.activate
def test_5xx_triggers_retry_then_fails(client):
    # Retry adapter retries on 500/502/503/504.
    for _ in range(5):
        responses.add(responses.POST, f"{BASE}/init", status=503)

    with pytest.raises((RetryError, Exception)):
        client.init_run({"project": "p", "name": "n"})

    # max_retries=2 => 1 original + 2 retries = 3 calls.
    assert len(responses.calls) >= 3


@responses.activate
def test_5xx_then_success(client):
    responses.add(responses.POST, f"{BASE}/init", status=503)
    responses.add(responses.POST, f"{BASE}/init", json={"run_id": "ok"}, status=200)
    rid = client.init_run({"project": "p", "name": "n"})
    assert rid == "ok"


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


@pytest.mark.parametrize("mode", ["local", "offline"])
def test_make_client_returns_journal_for_local_and_alias(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path))
    c = make_client(mode)
    assert isinstance(c, _OfflineClient)


def test_make_client_dir_arg_beats_env(tmp_path, monkeypatch):
    env_dir = tmp_path / "from-env"
    arg_dir = tmp_path / "from-arg"
    monkeypatch.setenv("AGENTICML_DIR", str(env_dir))
    c = make_client("local", dir=arg_dir)
    assert isinstance(c, _OfflineClient)
    assert c.root == arg_dir.resolve()
    assert c.root != env_dir.resolve()


def test_make_client_dir_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    c = make_client("local", dir="~/offline-here")
    assert c.root == (tmp_path / "offline-here").resolve()


def test_resolve_dir_precedence(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    monkeypatch.setenv("AGENTICML_DIR", str(env_dir))
    assert resolve_dir(tmp_path / "arg") == (tmp_path / "arg").resolve()
    assert resolve_dir() == env_dir.resolve()


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


def test_resolve_dir_defaults_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    assert resolve_dir() == tmp_path / ".agenticml"
