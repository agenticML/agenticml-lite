"""Tests for _OfflineClient — the journaling fallback used in offline mode."""

from __future__ import annotations

import json
from pathlib import Path

from agenticml.client import _OfflineClient


def _read_journal(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "journal.jsonl").read_text().splitlines()
        if line.strip()
    ]


def test_init_run_creates_run_dir_and_journal(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p", "name": "n"})
    assert rid.startswith("offline-")
    run_dir = tmp_path / rid
    assert run_dir.is_dir()
    assert (run_dir / "journal.jsonl").exists()

    entries = _read_journal(run_dir)
    assert entries[0]["kind"] == "init"
    assert entries[0]["payload"] == {"project": "p", "name": "n"}


def test_init_run_uses_run_id_hint(tmp_path):
    c = _OfflineClient(tmp_path, run_id_hint="my-custom-id")
    rid = c.init_run({"project": "p"})
    assert rid == "my-custom-id"
    assert (tmp_path / "my-custom-id" / "journal.jsonl").exists()


def test_log_batch_journals_under_init(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p"})
    c.log_batch([
        {"run_id": rid, "step": 0, "metrics": {"loss": 0.5}},
        {"run_id": rid, "step": 1, "metrics": {"loss": 0.4}},
    ])
    entries = _read_journal(tmp_path / rid)
    kinds = [e["kind"] for e in entries]
    assert kinds == ["init", "log_batch"]
    assert len(entries[1]["payload"]["items"]) == 2


def test_log_batch_with_empty_is_noop(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p"})
    c.log_batch([])
    entries = _read_journal(tmp_path / rid)
    # Only "init", no "log_batch" entry for empty list.
    assert [e["kind"] for e in entries] == ["init"]


def test_finalize_journals_status_and_summary(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p"})
    c.finalize(rid, {"final": 9.0}, status="crashed", snapshot_hash="snapshot-1")
    entries = _read_journal(tmp_path / rid)
    fin = entries[-1]
    assert fin["kind"] == "finalize"
    assert fin["payload"]["status"] == "crashed"
    assert fin["payload"]["summary"] == {"final": 9.0}
    assert fin["payload"]["snapshot_hash"] == "snapshot-1"


def test_log_artifact_copies_file_and_journals(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p"})

    artifact_src = tmp_path / "model.bin"
    artifact_src.write_bytes(b"fake weights")

    result = c.log_artifact(
        rid, "my_model", "model", {"epoch": 5},
        str(artifact_src), "application/octet-stream",
    )
    assert result["status"] == "offline"

    run_dir = tmp_path / rid
    artifacts = list((run_dir / "artifacts").iterdir())
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == b"fake weights"

    entries = _read_journal(run_dir)
    art_entry = [e for e in entries if e["kind"] == "log_artifact"][0]
    assert art_entry["payload"]["name"] == "my_model"
    assert art_entry["payload"]["metadata"] == {"epoch": 5}


def test_upload_code_writes_files_under_code_dir(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p"})

    files = [
        {"path": "src/main.py", "content": b"print(1)\n", "content_type": "text/x-python"},
        {"path": "README.md", "content": b"# hi\n", "content_type": "text/markdown"},
    ]
    result = c.upload_code(rid, json.dumps({"files": ["x"]}), files)
    assert result == {"status": "offline", "files": 2}

    run_dir = tmp_path / rid
    assert (run_dir / "code" / "src" / "main.py").read_bytes() == b"print(1)\n"
    assert (run_dir / "code" / "README.md").read_bytes() == b"# hi\n"

    entries = _read_journal(run_dir)
    upload = [e for e in entries if e["kind"] == "upload_code"][0]
    paths = {f["path"] for f in upload["payload"]["files"]}
    assert paths == {"src/main.py", "README.md"}


def test_log_media_copies_file_with_step_in_name(tmp_path):
    c = _OfflineClient(tmp_path)
    rid = c.init_run({"project": "p"})

    img_src = tmp_path / "img.png"
    img_src.write_bytes(b"\x89PNG\r\n\x1a\n")

    c.log_media(rid, "preview", 7, "image", "first", str(img_src), "image/png")

    media_dir = tmp_path / rid / "media"
    files = list(media_dir.iterdir())
    assert len(files) == 1
    assert "step_7" in files[0].name


def test_artifacts_exists_returns_empty(tmp_path):
    c = _OfflineClient(tmp_path)
    assert c.artifacts_exists(["sha1", "sha2"]) == []


def test_get_run_returns_none(tmp_path):
    c = _OfflineClient(tmp_path)
    assert c.get_run("anything") is None


def test_concurrent_runs_have_isolated_dirs(tmp_path):
    a = _OfflineClient(tmp_path)
    a.init_run({"project": "p"})
    b = _OfflineClient(tmp_path)
    b.init_run({"project": "q"})
    dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(dirs) == 2
