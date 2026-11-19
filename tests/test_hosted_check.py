"""24.12: scripts/hosted_check.py against a local two-workspace server."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from agenticml import sync
from agenticml.client import Client
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.keys import KeyStore
from agenticml.server.workspaces import prepare_store, store_root
from agenticml.store import RunReader, RunWriter
from test_server_ingest import running

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hosted_check.py"
spec = importlib.util.spec_from_file_location("hosted_check", SCRIPT)
hosted_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted_check)

SLUG_A, SLUG_B = "customer-one", "customer-two"


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as server:
        server.users.add("alice", "admin")
        server.users.add("bob", "admin")
        a = WorkspaceRepo(server.db).create(SLUG_A, owner="alice")
        b = WorkspaceRepo(server.db).create(SLUG_B, owner="bob")
        for ws in (a, b):
            prepare_store(server.data_dir, ws["id"])
        _, key_a = KeyStore(server.data_dir / "server", db=server.db, workspace_id=a["id"]).create(
            "laptop", user="alice")
        _, key_b = KeyStore(server.data_dir / "server", db=server.db, workspace_id=b["id"]).create(
            "laptop", user="bob")
        server.ws = {SLUG_A: a, SLUG_B: b}
        server.url = "http://127.0.0.1:%d" % server.server_address[1]
        monkeypatch.setenv("AGENTICML_HOST", server.url)
        monkeypatch.setenv("AGENTICML_API_KEY", key_a)
        server.key_a, server.key_b = key_a, key_b
        yield server


def local_run(marker, size=300):
    root = hosted_check.resolve_root()
    writer = RunWriter.create(root, meta={"project": "launch-check", "name": marker})
    writer.write_scalars([(0, {"loss": 1.0}), (1, {"loss": 0.5})])
    blob = root / f"{marker}.bin"
    blob.write_bytes(os.urandom(size))
    writer.add_artifact(blob, "checkpoint", "model", {}, copy=True)
    writer.finish("finished")
    writer.close()
    return writer.run_id, root


def push(server, key, run_id, root):
    result = sync.SyncResult(run_id)
    with Client(server.url, key, timeout=30, max_retries=0) as client:
        sync.sync_run(RunReader(root, run_id), client.base_url, client, result)
    assert not result.failed, result.warnings


def invoke(capsys, monkeypatch, *argv, key=None):
    if key is not None:
        monkeypatch.setenv("AGENTICML_API_KEY", key)
    code = hosted_check.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def statuses(out):
    return [line.split()[0] for line in out.splitlines() if line.split()[0] in
            ("PASS", "FAIL", "WARN", "INFO", "SKIP", "TODO")]


def test_verify_passes_after_sync(two, capsys, monkeypatch):
    run_id, root = local_run("one")
    push(two, two.key_a, run_id, root)
    code, out, err = invoke(capsys, monkeypatch, "verify", run_id)
    assert code == 0, out
    assert "FAIL" not in statuses(out)
    assert "RESULT: PASS" in out
    for check in ("files", "objects", "remote status", "fingerprint"):
        assert any(line.startswith("PASS") and check in line for line in out.splitlines()), (check, out)


def test_verify_json(two, capsys, monkeypatch):
    run_id, root = local_run("one")
    push(two, two.key_a, run_id, root)
    code, out, _ = invoke(capsys, monkeypatch, "verify", "--json", run_id)
    doc = json.loads(out)
    assert code == 0 and doc["result"] == "PASS" and doc["command"] == "verify"
    assert {row["check"] for row in doc["checks"]} >= {"files", "objects", "fingerprint"}


def test_verify_fails_when_server_file_changed(two, capsys, monkeypatch):
    run_id, root = local_run("one")
    push(two, two.key_a, run_id, root)
    events = store_root(two.data_dir, two.ws[SLUG_A]["id"]) / "runs" / run_id / "events.jsonl"
    data = bytearray(events.read_bytes())
    data[-3] ^= 0x01
    events.write_bytes(bytes(data))
    code, out, _ = invoke(capsys, monkeypatch, "verify", run_id)
    assert code == 1
    assert any(line.startswith("FAIL") and "events.jsonl" in line for line in out.splitlines()), out
    assert "RESULT: FAIL" in out


def test_verify_fails_when_object_missing(two, capsys, monkeypatch):
    run_id, root = local_run("one")
    push(two, two.key_a, run_id, root)
    sha = sorted(RunReader(root, run_id).referenced_shas())[0]
    two.objects.delete(two.ws[SLUG_A]["id"], sha)
    code, out, _ = invoke(capsys, monkeypatch, "verify", run_id)
    assert code == 1
    assert any(line.startswith("FAIL") and "objects" in line and sha in line for line in out.splitlines()), out


def test_verify_fails_for_unsynced_run(two, capsys, monkeypatch):
    run_id, _ = local_run("one")
    code, out, _ = invoke(capsys, monkeypatch, "verify", run_id)
    assert code == 1
    assert "does not know this run" in out


def test_isolation_passes_across_workspaces(two, capsys, monkeypatch):
    run_b, root = local_run("two")
    push(two, two.key_b, run_b, root)
    code, out, _ = invoke(capsys, monkeypatch, "isolation", "--run", run_b, "--slug-b", SLUG_B,
                          key=two.key_a)
    assert code == 0, out
    assert "FAIL" not in statuses(out)
    assert "RESULT: PASS" in out
    assert any(line.startswith("PASS") and "/v2/objects/missing" in line for line in out.splitlines())
    assert any(line.startswith("TODO") and "browser step" in line for line in out.splitlines())
    assert statuses(out).count("PASS") >= 8
    # Read-only towards workspace B: its copy of the run is untouched and still verifies.
    code, out, _ = invoke(capsys, monkeypatch, "verify", run_b, key=two.key_b)
    assert code == 0, out


def test_isolation_object_flag_and_skip(two, capsys, monkeypatch):
    run_b, root = local_run("two")
    push(two, two.key_b, run_b, root)
    sha = sorted(RunReader(root, run_b).referenced_shas())[0]
    code, out, _ = invoke(capsys, monkeypatch, "isolation", "--run", "no-such-local-run",
                          "--object", sha, "--slug-b", SLUG_B, key=two.key_a)
    assert code == 0, out
    assert any(line.startswith("PASS") and "/v2/objects/missing" in line for line in out.splitlines())
    code, out, _ = invoke(capsys, monkeypatch, "isolation", "--run", "no-such-local-run",
                          "--slug-b", SLUG_B, key=two.key_a)
    assert code == 0, out
    assert any(line.startswith("SKIP") and "/v2/objects/missing" in line for line in out.splitlines())


def test_isolation_fails_when_probe_gets_data(two, capsys, monkeypatch):
    # Point the probe at the key's own workspace: every probe now reaches real data.
    run_a, root = local_run("one")
    push(two, two.key_a, run_a, root)
    before = (store_root(two.data_dir, two.ws[SLUG_A]["id"]) / "runs" / run_a / "meta.json").read_bytes()
    code, out, _ = invoke(capsys, monkeypatch, "isolation", "--run", run_a, "--slug-b", SLUG_B)
    assert code == 1
    for probe in (f"GET /v2/runs/{run_a}/state", f"GET /v2/runs/{run_a}/lineage", "/v2/objects/missing"):
        assert any(line.startswith("FAIL") and probe in line for line in out.splitlines()), (probe, out)
    assert any(line.startswith("FAIL") and "/commit" in line for line in out.splitlines()), out
    # The commit probe sends an empty body, which the server rejects before changing anything.
    after = (store_root(two.data_dir, two.ws[SLUG_A]["id"]) / "runs" / run_a / "meta.json").read_bytes()
    assert before == after
    code, out, _ = invoke(capsys, monkeypatch, "verify", run_a)
    assert code == 0, out


def test_isolation_refuses_own_workspace(two, capsys, monkeypatch):
    run_b, _ = local_run("two")
    code, out, err = invoke(capsys, monkeypatch, "isolation", "--run", run_b, "--slug-b", SLUG_B,
                            key=two.key_b)
    assert code == 2
    assert "belongs to workspace" in err


def test_key_never_printed(two, capsys, monkeypatch):
    run_a, root = local_run("one")
    push(two, two.key_a, run_a, root)
    run_b, root_b = local_run("two")
    push(two, two.key_b, run_b, root_b)
    seen = ""
    for argv in (("verify", run_a), ("verify", "--json", run_a), ("verify", run_b),
                 ("isolation", "--run", run_b, "--slug-b", SLUG_B),
                 ("isolation", "--json", "--run", run_a, "--slug-b", SLUG_B)):
        _, out, err = invoke(capsys, monkeypatch, *argv)
        seen += out + err
    assert two.key_a not in seen
    assert two.key_a[:8] in seen


def test_key_prompt_when_env_missing(two, capsys, monkeypatch):
    run_a, root = local_run("one")
    push(two, two.key_a, run_a, root)
    monkeypatch.delenv("AGENTICML_API_KEY")
    monkeypatch.setattr(hosted_check.getpass, "getpass", lambda prompt="": two.key_a)
    code, out, _ = invoke(capsys, monkeypatch, "verify", run_a)
    assert code == 0, out


def test_keys_not_accepted_as_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        hosted_check.main(["verify", "--key", "x", "run-1"])
    assert exc.value.code == 2


def test_make_run_creates_finished_run_of_requested_size(tmp_path, capsys, monkeypatch):
    cwd = os.getcwd()
    code, out, err = invoke(capsys, monkeypatch, "make-run", "--mb", "0.03", "--files", "3",
                            "--steps", "20", "--project", "tiny")
    assert code == 0, err
    assert os.getcwd() == cwd
    run_id = out.split()[0]
    root = hosted_check.resolve_root()
    reader = RunReader(root, run_id)
    meta = reader.meta()
    assert meta["status"] == "finished" and meta["project"] == "tiny"
    assert meta["summary"]["final_loss"] == pytest.approx(1 / 20)
    events = [json.loads(line) for line in (reader.run_dir / "events.jsonl").read_text().splitlines()]
    artifacts = [event for event in events if event.get("kind") == "artifact"]
    assert [event["name"] for event in artifacts] == ["checkpoint", "shard-001", "shard-002"]
    assert all(event["copy"] and event["size"] == int(0.03 * 1024 * 1024) // 3 for event in artifacts)
    assert len(reader.referenced_shas()) >= 3
    assert "checkpoint sha256" in err
    assert artifacts[0]["sha256"] in err  # the printed digest is the stored object digest
    assert len(reader.keys()) == 2


def test_make_run_refuses_over_limit(capsys, monkeypatch):
    code, out, err = invoke(capsys, monkeypatch, "make-run", "--mb", "1501")
    assert code == 2 and "at most 1500" in err and out == ""


def test_help(capsys):
    for argv in (["--help"], ["make-run", "--help"], ["verify", "--help"], ["isolation", "--help"]):
        with pytest.raises(SystemExit) as exc:
            hosted_check.main(argv)
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out
