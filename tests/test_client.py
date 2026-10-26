"""HTTP-level tests for Client. Uses `responses` to mock the server."""

from __future__ import annotations

import pytest
import responses
from requests.exceptions import RetryError

from agenticml.client import Client, _OfflineClient, make_client, resolve_offline_dir


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
def test_get_run_returns_none_on_404(client):
    responses.add(responses.GET, f"{BASE}/runs/missing", status=404)
    assert client.get_run("missing") is None


@responses.activate
def test_get_run_returns_body_on_200(client):
    responses.add(
        responses.GET, f"{BASE}/runs/abc",
        json={"run_id": "abc", "status": "running"}, status=200,
    )
    assert client.get_run("abc") == {"run_id": "abc", "status": "running"}


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


def test_make_client_returns_offline_when_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_OFFLINE_DIR", str(tmp_path))
    c = make_client("offline")
    assert isinstance(c, _OfflineClient)


def test_make_client_offline_dir_arg_beats_env(tmp_path, monkeypatch):
    env_dir = tmp_path / "from-env"
    arg_dir = tmp_path / "from-arg"
    monkeypatch.setenv("AGENTICML_OFFLINE_DIR", str(env_dir))
    c = make_client("offline", offline_dir=arg_dir)
    assert isinstance(c, _OfflineClient)
    assert c.root == arg_dir.resolve()
    assert c.root != env_dir.resolve()


def test_make_client_offline_dir_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTICML_OFFLINE_DIR", raising=False)
    c = make_client("offline", offline_dir="~/offline-here")
    assert c.root == (tmp_path / "offline-here").resolve()


def test_resolve_offline_dir_precedence(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    monkeypatch.setenv("AGENTICML_OFFLINE_DIR", str(env_dir))
    assert resolve_offline_dir(tmp_path / "arg") == (tmp_path / "arg").resolve()
    assert resolve_offline_dir() == env_dir.resolve()


def test_make_client_returns_real_when_online():
    c = make_client("online", base_url=BASE, api_key="x")
    assert isinstance(c, Client)
