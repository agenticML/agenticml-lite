"""Replay local journals from the shared root through the HTTP client."""

import json

import pytest
import responses

from agenticml.cli import main
from agenticml.client import _OfflineClient


@pytest.mark.parametrize("explicit_dir", [False, True])
@responses.activate
def test_sync_reads_runs_under_root(tmp_path, monkeypatch, capsys, explicit_dir):
    root = tmp_path / "local"
    monkeypatch.setenv("AGENTICML_DIR", str(root))
    local = _OfflineClient(root, run_id_hint="local-id")
    run_id = local.init_run({"project": "p", "name": "n"})
    local.log_batch([{"run_id": run_id, "step": 2, "metrics": {"loss": 0.5}}])
    local.upload_code(run_id, "{}", [
        {"path": "train.py", "content": b"print(1)", "content_type": "text/x-python"},
    ])
    local.finalize(run_id, {"done": True}, snapshot_hash="snapshot")

    base = "https://sync.test"
    responses.add(responses.POST, f"{base}/init", json={"run_id": "server-id"})
    responses.add(responses.POST, f"{base}/log/batch", status=200)
    responses.add(responses.POST, f"{base}/artifacts/exists", json={"existing": []})
    responses.add(
        responses.POST, f"{base}/runs/server-id/artifacts/presign-upload",
        json={"uploads": [{"path": "train.py", "url": "https://bucket.test/train.py"}]},
    )
    responses.add(responses.PUT, "https://bucket.test/train.py", status=200)
    responses.add(responses.POST, f"{base}/runs/server-id/artifacts/register", json={"status": "ok"})
    responses.add(responses.POST, f"{base}/runs/server-id/finalize", status=200)

    args = ["sync", "--host", base, "--api-key", "sync-key"]
    if explicit_dir:
        args.extend(["--dir", str(root)])
        monkeypatch.setenv("AGENTICML_DIR", str(tmp_path / "unused"))
    assert main(args) == 0
    assert "synced=1 failed=0" in capsys.readouterr().out
    assert (root / "runs" / "local-id.synced" / "journal.jsonl").is_file()
    assert not (root / "runs" / "local-id").exists()
    assert responses.calls[0].request.headers["x-api-key"] == "sync-key"
    batch = json.loads(responses.calls[1].request.body)
    assert batch["items"][0]["run_id"] == "server-id"
    assert responses.calls[4].request.body == b"print(1)"
    assert json.loads(responses.calls[-1].request.body)["snapshot_hash"] == "snapshot"
    calls_before = len(responses.calls)
    assert main(args) == 0
    assert "synced=0 failed=0" in capsys.readouterr().out
    assert len(responses.calls) == calls_before
