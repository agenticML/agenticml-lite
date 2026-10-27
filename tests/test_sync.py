"""Resumable HTTP sync of real RunWriter stores; no journal replay."""
import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest
import responses

from agenticml.cli import main
from agenticml.store.gc import collect, run_fingerprint, usage
from agenticml.store.overview import OVERVIEW_MIN_POINTS, Overview
from agenticml.store.reader import RunReader
from agenticml.store.writer import RunWriter

BASE = "https://sync.test"


class Server:
    def __init__(self, mock):
        self.mock = mock
        self.init_count = 0
        self.log_count = 0
        self.register_count = 0
        self.fail_batch = None
        self.fail_register = None
        self.interrupt_batch = None
        self.existing = []
        self.register_missing = None
        self.add_host(BASE)

    @property
    def calls(self):
        return self.mock.calls

    def add_host(self, host):
        def init(request):
            self.init_count += 1
            return 200, {}, json.dumps({"run_id": f"remote-{self.init_count}"})

        def batch(request):
            self.log_count += 1
            if self.log_count == self.interrupt_batch:
                raise KeyboardInterrupt()
            return (400, {}, "{}") if self.log_count == self.fail_batch else (200, {}, "{}")

        def exists(request):
            return 200, {}, json.dumps({"existing": self.existing})

        def presign(request):
            files = json.loads(request.body)["files"]
            uploads = [{"path": item["path"], "url": f"https://bucket.test/{item['sha256']}"}
                       for item in files]
            return 200, {}, json.dumps({"uploads": uploads})

        def register(request):
            self.register_count += 1
            if self.fail_register and self.register_count == self.fail_register:
                return 400, {}, json.dumps({"detail": "register failed"})
            if self.register_missing:
                missing, self.register_missing = self.register_missing, None
                return 409, {}, json.dumps({"detail": {"missing": missing}})
            return 200, {}, "{}"

        self.mock.add_callback(responses.POST, host + "/init", callback=init)
        self.mock.add_callback(responses.POST, host + "/log/batch", callback=batch)
        self.mock.add_callback(responses.POST, host + "/artifacts/exists", callback=exists)
        prefix = re.escape(host) + r"/runs/[^/]+"
        self.mock.add_callback(responses.POST, re.compile(prefix + r"/artifacts/presign-upload$"), callback=presign)
        self.mock.add_callback(responses.POST, re.compile(prefix + r"/artifacts/register$"), callback=register)
        for path in ("finalize", "log_media", "artifacts/register-version"):
            self.mock.add(responses.POST, re.compile(prefix + "/" + path + "$"), json={})
        self.mock.add(responses.PATCH, re.compile(prefix + r"/config$"), json={})
        self.mock.add(responses.PUT, re.compile(r"https://bucket.test/.*"), status=200)

    def matching(self, suffix, method=None):
        return [call for call in self.calls if call.request.url.endswith(suffix)
                and (method is None or call.request.method == method)]

    def bodies(self, suffix, method=None):
        return [json.loads(call.request.body) for call in self.matching(suffix, method)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTICML_HOST", raising=False)
    monkeypatch.delenv("AGENTICML_API_KEY", raising=False)


@pytest.fixture
def server():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield Server(mock)


def make_run(root, run_id="local-id", *, finished=True, points=3, **meta):
    writer = RunWriter.create(root, run_id=run_id, meta={"project": "demo", "name": "train", **meta})
    writer.write_scalars([(step, {"loss": float(step), "acc": 0.5}) for step in range(points)])
    if finished:
        writer.finish("finished", {"best": 0.5})
    return writer


def sync(root, *ids, host=BASE, extra=()):
    return main(["sync", *ids, "--dir", str(root), "--host", host, "--api-key", "sync-key", *extra])


def state(writer, host=BASE):
    data = json.loads((writer.run_dir / "sync.json").read_text())
    assert data["version"] == 1
    return data["hosts"][host]


def test_payload_grouping_offsets_and_finalize(tmp_path, server, capsys):
    env = {"python_version": "3.12", "platform": "linux", "packages": {"torch": "2.0"}}
    git = {"repo_root": "/repo", "git_commit": "abc", "git_branch": "main", "is_dirty": True}
    writer = make_run(tmp_path, finished=False, config={"lr": 0.1}, tags=["x"], notes="note",
                      group="g", job_type="train", env=env, git=git)
    writer.write_scalars([(3, {"bad": float("nan"), "loss": 3.0}),
                          (4, {"bad": float("inf")})])
    writer.finish("crashed", {"best": 0.5})
    assert sync(tmp_path, writer.run_id) == 0
    payload, = server.bodies("/init")
    assert payload == {
        "project": "demo", "name": "train", "config": {"lr": 0.1}, "tags": ["x"],
        "notes": "note", "group": "g", "job_type": "train",
        "hostname": RunReader(tmp_path, writer.run_id).meta()["hostname"], **env, **git,
    }
    items = server.bodies("/log/batch")[0]["items"]
    assert items == [{"run_id": "remote-1", "step": step,
                      "metrics": {"loss": float(step), "acc": 0.5}} for step in range(3)] + [
        {"run_id": "remote-1", "step": 3, "metrics": {"loss": 3.0}},
    ]
    saved = state(writer)
    assert saved["offsets"] == {"loss": 32, "acc": 24, "bad": 16}
    assert saved["skipped_nonfinite"] == 2
    assert saved["complete"] and saved["finalized"]
    finalized, = server.bodies("/finalize")
    assert finalized == {"summary": {"best": 0.5}, "status": "crashed"}
    assert "skipped=2" in capsys.readouterr().out
    assert server.calls[0].request.headers["x-api-key"] == "sync-key"
    before = len(server.calls)
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.calls) == before
    assert "up to date" in capsys.readouterr().out


def test_second_batch_failure_resumes_without_reinit_or_lost_points(tmp_path, server, capsys):
    writer = make_run(tmp_path, points=4501)
    server.fail_batch = 2
    assert sync(tmp_path, writer.run_id) == 1
    assert state(writer)["remote_run_id"] == "remote-1"
    # The first 2000 steps were accepted and must not be replayed.
    assert state(writer)["offsets"] == {"loss": 2000 * 8, "acc": 2000 * 8}
    assert not state(writer)["complete"]
    assert "failed: HTTP 400" in capsys.readouterr().out
    server.fail_batch = None
    assert sync(tmp_path, writer.run_id) == 0
    assert server.init_count == 1
    accepted = [json.loads(call.request.body)["items"] for call in server.matching("/log/batch")
                if call.response.status_code == 200]
    assert all(len(batch) <= 2000 for batch in accepted)
    for key in ("loss", "acc"):
        steps = [item["step"] for batch in accepted for item in batch if key in item["metrics"]]
        assert steps == list(range(4501))
    assert state(writer)["offsets"] == {"loss": 4501 * 8, "acc": 4501 * 8}
    assert len(server.matching("/finalize")) == 1


def test_round_checkpoints_and_new_keys(tmp_path, server):
    writer = make_run(tmp_path, finished=False, points=20005)
    server.fail_batch = 11  # The first batch of the second 20k-record round.
    assert sync(tmp_path, writer.run_id) == 1
    assert state(writer)["offsets"] == {"loss": 20000 * 8, "acc": 20000 * 8}
    writer.write_scalars([(20005, {"new": 7.0})])
    writer.finish("finished", {})
    server.fail_batch = None
    before = len(server.matching("/log/batch"))
    assert sync(tmp_path, writer.run_id) == 0
    new_batches = server.bodies("/log/batch")[before:]
    assert len(new_batches) == 1
    assert [item["step"] for item in new_batches[0]["items"]] == list(range(20000, 20006))
    assert state(writer)["offsets"]["new"] == 8


def test_repeated_steps_are_not_dropped(tmp_path, server):
    writer = make_run(tmp_path, finished=False, points=0)
    writer.write_scalars([(1, {"a": 1.0}), (1, {"a": 2.0, "b": 4.0})])
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    items = server.bodies("/log/batch")[0]["items"]
    assert [item["metrics"]["a"] for item in items] == [1.0, 2.0]
    assert sum("b" in item["metrics"] for item in items) == 1


def test_config_patch_after_finished_and_once(tmp_path, server):
    writer = make_run(tmp_path, config={"lr": 0.1})
    assert sync(tmp_path, writer.run_id) == 0
    # meta is a documented atomic JSON file; emulate a later metadata correction.
    path = writer.run_dir / "meta.json"
    meta = json.loads(path.read_text())
    meta.update(config={"lr": 0.2}, tags=["updated"], notes="new", group="g", job_type="eval")
    path.write_text(json.dumps(meta))
    assert sync(tmp_path, extra=("--all",)) == 0
    payload, = server.bodies("/config", "PATCH")
    assert payload == {"config": {"lr": 0.2}, "tags": ["updated"], "notes": "new", "group": "g", "job_type": "eval"}
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.matching("/config", "PATCH")) == 1
    assert len(server.matching("/finalize")) == 1
    assert server.init_count == 1


def add_code(writer, count=1):
    paths = [f"code/{index}.py" for index in range(count)]
    writer.add_code([{"path": path, "content_type": "text/x-python", "source": "code"} for path in paths],
                    [(path, f"print({index})".encode()) for index, path in enumerate(paths)])
    return RunReader(writer.root, writer.run_id).code_manifest()


def test_code_dedup_media_and_artifacts_once(tmp_path, server, capsys):
    writer = make_run(tmp_path, finished=False)
    manifest = add_code(writer, 2)
    server.existing = [manifest[0]["sha256"]]
    writer.add_media("preview", 2, b"PNG-image", "image/png", "caption")
    ref = tmp_path / "reference.bin"
    ref.write_bytes(b"reference")
    copy = tmp_path / "copy.bin"
    copy.write_bytes(b"snapshot")
    ref_event = writer.add_artifact(ref, "ref", "model", {"a": 1}, copy=False)
    copied = writer.add_artifact(copy, "copy", "model", {}, copy=True)
    copy.unlink()  # Copied artifacts are independent of the source file.
    writer.finish("finished", {})
    assert sync(tmp_path, extra=("--all",)) == 0
    output = capsys.readouterr().out
    assert "local-id  demo/train  synced  points=6 files=2 media=1 artifacts=2 skipped=0 replaced=0" in output
    assert ref_event["path"] in output
    presigns = server.bodies("/artifacts/presign-upload")
    assert [item["path"] for item in presigns[0]["files"]] == [manifest[1]["path"]]
    registered, = server.bodies("/artifacts/register")
    assert len(registered["files"]) == 2
    media, = server.matching("/log_media")
    assert b"PNG-image" in media.request.body and b"caption" in media.request.body
    versions = server.bodies("/artifacts/register-version")
    assert [item["name"] for item in versions] == ["ref", "copy"]
    assert versions[1]["sha256"] == copied["sha256"]
    assert "snapshot_hash" not in server.bodies("/finalize")[0]
    saved = state(writer)
    assert len(saved["code"]) == 2 and len(saved["media"]) == 1 and len(saved["artifacts"]) == 2
    before = len(server.calls)
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.calls) == before


def test_code_batches_and_missing_content_recovery(tmp_path, server):
    writer = make_run(tmp_path, finished=False, points=0)
    manifest = add_code(writer, 201)
    server.existing = [item["sha256"] for item in manifest]
    server.register_missing = [manifest[0]["path"]]
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    # One failed registration, its recovery, then the final one-file batch.
    registrations = server.bodies("/artifacts/register")
    assert [len(item["files"]) for item in registrations] == [200, 200, 1]
    assert len(server.matching("", "PUT")) == 1
    assert len(state(writer)["code"]) == 201


@pytest.mark.parametrize("change", ["missing", "resized", "mtime", "symlink"])
def test_by_reference_mismatch_is_skipped_until_restored(tmp_path, server, capsys, change):
    writer = make_run(tmp_path, finished=False)
    ref = tmp_path / "reference.bin"
    ref.write_bytes(b"original")
    recorded = writer.add_artifact(ref, "model", "model", {}, copy=False)
    if change == "missing":
        ref.unlink()
    elif change == "resized":
        ref.write_bytes(b"wrong-size")
    elif change == "mtime":
        os.utime(ref, (time.time() + 50, time.time() + 50))
    else:
        ref.unlink()
        ref.symlink_to(tmp_path / "elsewhere")
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    output = capsys.readouterr().out
    if change == "symlink":
        assert "refusing symlink" in output
    else:
        assert "changed since logged" in output
    assert "synced" in output
    assert state(writer)["artifacts"] == []
    assert state(writer)["finalized"] and state(writer)["complete"]
    assert not server.matching("/artifacts/register-version")
    assert len(server.matching("/finalize")) == 1
    before = len(server.calls)
    assert sync(tmp_path, writer.run_id) == 0
    again = capsys.readouterr().out
    assert "up to date" in again
    assert "changed since logged" not in again and "refusing symlink" not in again
    assert len(server.calls) == before
    assert sync(tmp_path, extra=("--all",)) == 0
    assert len(server.calls) == before
    if ref.is_symlink() or not ref.exists():
        ref.unlink(missing_ok=True)
    ref.write_bytes(b"original")
    os.utime(ref, (recorded["mtime"], recorded["mtime"]))
    assert sync(tmp_path, extra=("--all",)) == 0
    restored = capsys.readouterr().out
    assert recorded["path"] in restored
    assert state(writer)["complete"]
    assert len(server.matching("/finalize")) == 1
    assert len(server.matching("/artifacts/register-version")) == 1


def test_running_stale_and_resume_terminal_generation(tmp_path, server, capsys):
    writer = make_run(tmp_path, finished=False)
    assert sync(tmp_path, writer.run_id) == 0
    assert "partial (running)" in capsys.readouterr().out
    assert not server.matching("/finalize")
    old = time.time() - 300
    os.utime(writer.run_dir / "meta.json", (old, old))
    os.utime(writer.run_dir / "scalars" / "_time.bin", (old, old))
    assert sync(tmp_path, extra=("--all",)) == 0
    assert not state(writer)["finalized"]
    writer.finish("finished", {"done": 1})
    assert sync(tmp_path, extra=("--all",)) == 0
    assert state(writer)["complete"]
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    resumed.write_scalars([(10, {"loss": 1.0})])
    resumed.finish("crashed", {"done": 2})
    assert sync(tmp_path, extra=("--all",)) == 0
    assert server.init_count == 1
    assert len(server.matching("/finalize")) == 2
    assert state(writer)["complete"]


def test_all_selects_incomplete_and_changed_with_environment_root(tmp_path, server, capsys):
    first = make_run(tmp_path, "first")
    assert sync(tmp_path, "first") == 0
    capsys.readouterr()
    second = make_run(tmp_path, "second", finished=False)
    assert main(["sync", "--all", "--host", BASE, "--api-key", "sync-key"]) == 0
    output = capsys.readouterr().out
    assert "second" in output and "first" not in output
    second.finish("finished")
    assert sync(tmp_path, extra=("--all",)) == 0
    assert state(second)["complete"] and state(first)["complete"]
    before = len(server.calls)
    assert sync(tmp_path, extra=("--all",)) == 0
    assert len(server.calls) == before


def test_dry_run_counts_no_requests_or_state(tmp_path, server, capsys):
    writer = make_run(tmp_path, finished=False)
    add_code(writer)
    writer.add_media("image", 1, b"image", "image/png", None)
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"weights")
    writer.add_artifact(artifact, "model", "model", {}, copy=True)
    writer.write_scalars([(4, {"bad": float("nan")})])
    writer.finish("finished")
    assert sync(tmp_path, extra=("--all", "--dry-run")) == 0
    assert not server.calls
    assert not (writer.run_dir / "sync.json").exists()
    output = capsys.readouterr().out
    assert "dry-run (would send)" in output
    assert "points=6 files=1 media=1 artifacts=1 skipped=1 replaced=0" in output
    assert sync(tmp_path, writer.run_id) == 0
    original = (writer.run_dir / "sync.json").read_bytes()
    before = len(server.calls)
    assert sync(tmp_path, writer.run_id, extra=("--dry-run",)) == 0
    assert len(server.calls) == before
    assert (writer.run_dir / "sync.json").read_bytes() == original


def test_per_host_state_isolation(tmp_path, server):
    second_host = "https://second.test"
    server.add_host(second_host)
    writer = make_run(tmp_path)
    assert sync(tmp_path, writer.run_id) == 0
    first_state = state(writer)
    assert sync(tmp_path, extra=("--all",), host=second_host + "/") == 0
    assert state(writer) == first_state
    assert state(writer, second_host)["remote_run_id"] == "remote-2"
    assert state(writer, second_host)["offsets"] == first_state["offsets"]
    assert server.init_count == 2
    assert len(server.matching("/finalize")) == 2
    before = len(server.calls)
    assert sync(tmp_path, writer.run_id, host=second_host) == 0
    assert len(server.calls) == before


def test_failure_continues_other_runs_and_exit_codes(tmp_path, server, capsys):
    make_run(tmp_path, "good")
    assert sync(tmp_path, "absent", "good") == 1
    output = capsys.readouterr().out
    assert "absent  ?/?  failed:" in output and "good  demo/train  synced" in output
    assert main(["sync"]) == 2
    assert "RUN_ID" in capsys.readouterr().err
    assert sync(tmp_path, "good", extra=("--watch", "0")) == 2
    assert sync(tmp_path, "good", extra=("--watch", "nan")) == 2
    assert sync(tmp_path, extra=("--all",)) == 0


def test_interrupt_resumes_and_prints_summary(tmp_path, server, capsys):
    writer = make_run(tmp_path, points=2500)
    server.interrupt_batch = 2
    assert sync(tmp_path, writer.run_id) == 130
    output = capsys.readouterr().out
    assert "partial (interrupted)" in output and "Interrupted: complete=0 partial=1 failed=0" in output
    assert state(writer)["offsets"] == {"loss": 2000 * 8, "acc": 2000 * 8}
    server.interrupt_batch = None
    assert sync(tmp_path, writer.run_id) == 0
    assert server.init_count == 1 and state(writer)["complete"]


def test_watch_until_complete_and_interrupt(tmp_path, server, monkeypatch, capsys):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, finished=False)
    sleeps = []

    def finish(seconds):
        sleeps.append(seconds)
        writer.write_scalars([(4, {"loss": 0.25})])
        writer.finish("finished")

    monkeypatch.setattr(sync_module.time, "sleep", finish)
    assert sync(tmp_path, writer.run_id, extra=("--watch", "0.1")) == 0
    assert sleeps == [0.1] and state(writer)["complete"]
    assert len(server.matching("/finalize")) == 1
    other = make_run(tmp_path, "other", finished=False)

    def interrupt(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(sync_module.time, "sleep", interrupt)
    assert sync(tmp_path, other.run_id, extra=("--watch", "0.1")) == 130
    assert "Interrupted:" in capsys.readouterr().out


def test_uint32_scalar_steps_and_snapshot_hash_on_init(tmp_path, server):
    writer = make_run(tmp_path, "wide", finished=False, points=0, snapshot_hash="captured-hash")
    writer.write_scalars([(2**31, {"loss": 1.0}), (2**32 - 1, {"loss": 2.0})])
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert [item["step"] for item in server.bodies("/log/batch")[0]["items"]] == [2**31, 2**32 - 1]
    assert server.bodies("/init")[0]["snapshot_hash"] == "captured-hash"
    assert "snapshot_hash" not in server.bodies("/finalize")[0]


def test_scalar_step_above_uint32_is_rejected(tmp_path, server, capsys, monkeypatch):
    writer = make_run(tmp_path, "bad", points=1)
    original = RunReader.read_records

    def fake(self, key, offset=0, limit=None):
        if self.run_id == writer.run_id and offset == 0:
            return [(2**32, 1.0)], 8
        return original(self, key, offset, limit=limit)

    monkeypatch.setattr(RunReader, "read_records", fake)
    assert sync(tmp_path, writer.run_id) == 1
    assert "step out of range" in capsys.readouterr().out
    assert not server.matching("/log/batch")


def test_torn_scalar_tail_not_consumed(tmp_path, server):
    writer = make_run(tmp_path)
    path = writer.run_dir / "scalars" / "0.bin"
    with path.open("ab") as handle:
        handle.write(b"torn")
    assert sync(tmp_path, writer.run_id) == 0
    assert state(writer)["offsets"]["loss"] == 24


def test_config_failure_keeps_previous_hash(tmp_path, server):
    writer = make_run(tmp_path, finished=False)
    assert sync(tmp_path, writer.run_id) == 0
    old_hash = state(writer)["config_hash"]
    writer.set_config({"new": True})
    server.mock.replace(responses.PATCH, re.compile(re.escape(BASE) + r"/runs/[^/]+/config$"), status=400)
    assert sync(tmp_path, writer.run_id) == 1
    assert state(writer)["config_hash"] == old_hash
    server.mock.replace(responses.PATCH, re.compile(re.escape(BASE) + r"/runs/[^/]+/config$"), json={})
    assert sync(tmp_path, writer.run_id) == 0
    assert state(writer)["config_hash"] != old_hash


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-h"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("run", "sync", "login", "logout", "whoami"):
        assert command in output


def test_run_changed_during_upload_is_not_finalized(tmp_path, server):
    writer = make_run(tmp_path)
    changed = []

    def resume_during_upload(request):
        resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
        resumed.write_scalars([(10, {"new": 1.0})])
        changed.append(resumed)
        return 200, {}, "{}"

    # Avoid producing unbounded new points in the callback on each scalar round.
    def once(request):
        return resume_during_upload(request) if not changed else (200, {}, "{}")

    server.mock.remove(responses.POST, BASE + "/log/batch")
    server.mock.add_callback(responses.POST, BASE + "/log/batch", callback=once)
    assert sync(tmp_path, writer.run_id) == 0
    assert not server.matching("/finalize")
    assert not state(writer)["complete"]
    assert not state(writer)["finalized"]
    changed[0].finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.matching("/finalize")) == 1
    assert state(writer)["complete"]




def _same_bytes_code(writer):
    blob = b"print(1)\n"
    writer.add_code(
        [
            {"path": "a.py", "content_type": "text/x-python", "source": "code"},
            {"path": "b.py", "content_type": "text/x-python", "source": "code"},
        ],
        [("a.py", blob), ("b.py", blob)],
    )
    return blob


def test_same_bytes_at_two_paths_survive_a_batch_failure(tmp_path, server, monkeypatch):
    import agenticml.sync as sync_module
    monkeypatch.setattr(sync_module, "CODE_FILES", 1)
    writer = make_run(tmp_path, finished=False, points=0)
    blob = _same_bytes_code(writer)
    digest = hashlib.sha256(blob).hexdigest()
    writer.finish("finished")
    server.fail_register = 2
    assert sync(tmp_path, writer.run_id) == 1
    assert state(writer)["code"] == [f"a.py|{digest}"]
    server.fail_register = None
    assert sync(tmp_path, writer.run_id) == 0
    registered = []
    for call in server.matching("/artifacts/register"):
        if call.response.status_code == 200:
            registered.append([item["path"] for item in json.loads(call.request.body)["files"]])
    assert registered == [["a.py"], ["b.py"]]
    assert state(writer)["code"] == [f"a.py|{digest}", f"b.py|{digest}"]
    assert server.init_count == 1




def test_duplicate_media_and_out_of_range_steps(tmp_path, server, capsys):
    writer = make_run(tmp_path, finished=False, points=0)
    writer.add_media("preview", 2, b"PNG", "image/png", "c")
    writer.add_media("preview", 2, b"PNG", "image/png", "c")
    writer.add_media("preview", 2**31 - 1, b"edge", "image/png", None)
    writer.add_media("preview", 2**31, b"too-big", "image/png", None)
    writer.add_media("other", -1, b"neg", "image/png", None)
    writer.add_media("too_low", -2**31 - 1, b"low", "image/png", None)
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.matching("/log_media")) == 4
    output = capsys.readouterr().out
    assert "out of range" in output and "skipped=2" in output
    saved = state(writer)["media"]
    assert set(saved) == {"m:0", "m:1", "m:2", "m:4"}
    skipped_ids = {item["id"] for item in state(writer)["skipped"]}
    assert skipped_ids == {"m:3", "m:5"}
    assert all(item["reason"] == "step out of range" for item in state(writer)["skipped"])
    before = len(server.matching("/log_media"))
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.matching("/log_media")) == before
    assert "out of range" not in capsys.readouterr().out


def test_nonfinite_and_nonjson_are_replaced(tmp_path, server, capsys, monkeypatch):
    writer = make_run(tmp_path, finished=False, points=0)
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"weights")
    writer.add_artifact(artifact, "model", "file", {"epoch": 1}, copy=True)
    writer.finish("finished", {"best": 1})
    real_meta = RunReader.meta
    real_events = RunReader.events

    def meta(self):
        data = real_meta(self)
        data["config"] = {"lr": float("nan"), "hi": float("inf"), "obj": Path("model.bin")}
        data["summary"] = {"score": float("-inf")}
        return data

    def events(self):
        rows = real_events(self)
        for row in rows:
            if row.get("kind") == "artifact":
                row["metadata"] = {"ratio": float("nan"), "obj": Path("ckpt")}
        return rows

    # run_fingerprint json-encodes meta(). Freeze the on-disk hash first so
    # the injected Path values exercise payload coercion only.
    from agenticml.sync import _source_hash as real_source_hash
    frozen = real_source_hash(RunReader(tmp_path, writer.run_id))
    monkeypatch.setattr(RunReader, "meta", meta)
    monkeypatch.setattr(RunReader, "events", events)
    monkeypatch.setattr("agenticml.sync._source_hash", lambda _reader: frozen)
    assert sync(tmp_path, writer.run_id) == 0
    payload = server.bodies("/init")[0]
    assert payload["config"] == {"lr": None, "hi": None, "obj": "model.bin"}
    assert server.bodies("/finalize")[0]["summary"] == {"score": None}
    assert server.bodies("/artifacts/register-version")[0]["metadata"] == {"ratio": None, "obj": "ckpt"}
    assert "replaced=6" in capsys.readouterr().out


def test_parent_run_id_is_remote_or_omitted(tmp_path, server):
    parent = make_run(tmp_path, "parent-run")
    assert sync(tmp_path, parent.run_id) == 0
    child = make_run(tmp_path, "child-run", parent_run_id=parent.run_id)
    assert sync(tmp_path, child.run_id) == 0
    child_init = server.bodies("/init")[1]
    assert child_init["parent_run_id"] == "remote-1"
    assert parent.run_id not in json.dumps(child_init)
    local_parent = make_run(tmp_path, "local-parent")
    unsynced = make_run(tmp_path, "child-unsynced", parent_run_id=local_parent.run_id)
    assert sync(tmp_path, unsynced.run_id) == 0
    assert "parent_run_id" not in server.bodies("/init")[-1]
    missing = make_run(tmp_path, "child-missing", parent_run_id="missing-parent")
    assert sync(tmp_path, missing.run_id) == 0
    assert "parent_run_id" not in server.bodies("/init")[-1]
    second = "https://second.test"
    server.add_host(second)
    assert sync(tmp_path, child.run_id, host=second) == 0
    remote_init = [json.loads(call.request.body) for call in server.calls
                   if call.request.url == second + "/init"]
    assert "parent_run_id" not in remote_init[0]


def test_sync_refuses_to_start_without_an_api_key(tmp_path, server, capsys, monkeypatch):
    writer = make_run(tmp_path)
    touched = []

    def boom(*_args, **_kwargs):
        touched.append(True)
        raise RuntimeError("touched")

    monkeypatch.setattr("agenticml.sync.list_runs", boom)
    monkeypatch.setattr("agenticml.sync.RunReader", boom)
    assert main(["sync", writer.run_id, "--dir", str(tmp_path), "--host", BASE, "--all"]) == 1
    assert touched == []
    assert "not logged in — run `agenticml login`" in capsys.readouterr().err
    assert not server.calls
    assert not (writer.run_dir / "sync.json").exists()


def test_directory_fsync_after_sync_json_replace(tmp_path, server, monkeypatch):
    writer = make_run(tmp_path, points=0)
    writer.finish("finished")
    real_open = os.open
    real_fsync = os.fsync
    directories = set()
    synced = []

    def spy_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == writer.run_dir:
            directories.add(fd)
        return fd

    def spy_fsync(fd):
        if fd in directories:
            synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    assert sync(tmp_path, writer.run_id) == 0
    assert synced


def test_locked_run_is_skipped(tmp_path, server, capsys):
    import fcntl
    writer = make_run(tmp_path)
    fd = os.open(writer.run_dir / "sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert sync(tmp_path, writer.run_id) == 1
        output = capsys.readouterr().out
        assert "another sync is running for this run" in output
        assert server.init_count == 0
        assert not (writer.run_dir / "sync.json").exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert sync(tmp_path, writer.run_id) == 0
    assert server.init_count == 1


def test_windows_skips_the_run_lock(tmp_path, monkeypatch):
    import agenticml.sync as sync_module
    monkeypatch.setattr(sync_module.os, "name", "nt")
    assert sync_module._acquire_lock(tmp_path) is None


def test_watch_status_is_the_last_pass_and_permanent_errors_stop(tmp_path, server, monkeypatch, capsys):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, points=1)
    calls = {"n": 0}

    def batch(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {}, json.dumps({"detail": "try again"})
        return 200, {}, "{}"

    server.mock.remove(responses.POST, BASE + "/log/batch")
    server.mock.add_callback(responses.POST, BASE + "/log/batch", callback=batch)
    slept = []
    monkeypatch.setattr(sync_module.time, "sleep", lambda seconds: slept.append(seconds))
    assert sync(tmp_path, writer.run_id, extra=("--watch", "0.2")) == 0
    assert slept == [0.2]
    assert state(writer)["complete"]

    other = make_run(tmp_path, "permanent", points=1)
    detail = ("x" * 200) + "ENDMARKER"

    def rejected(request):
        return 400, {"X-Debug": "header-secret-value"}, json.dumps({"detail": detail})

    server.mock.remove(responses.POST, BASE + "/log/batch")
    server.mock.add_callback(responses.POST, BASE + "/log/batch", callback=rejected)

    def fail_sleep(_seconds):
        raise AssertionError("watch waited on a permanent failure")

    monkeypatch.setattr(sync_module.time, "sleep", fail_sleep)
    assert sync(tmp_path, other.run_id, extra=("--watch", "0.2")) == 1
    output = capsys.readouterr().out + capsys.readouterr().err
    assert "HTTP 400: " + ("x" * 200) in output
    assert "ENDMARKER" not in output
    assert "header-secret-value" not in output


def test_watch_all_picks_up_new_runs(tmp_path, server, monkeypatch):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, "running", finished=False, points=1)
    made = []

    def tick(_seconds):
        if not made:
            made.append(make_run(tmp_path, "later"))
        else:
            writer.finish("finished", {})

    monkeypatch.setattr(sync_module.time, "sleep", tick)
    assert sync(tmp_path, extra=("--all", "--watch", "0.2")) == 0
    assert state(made[0])["complete"]
    assert state(writer)["complete"]


def test_insecure_http_host_is_opt_in(tmp_path, server, capsys):
    writer = make_run(tmp_path, points=0)
    writer.finish("finished")
    assert main(["sync", writer.run_id, "--dir", str(tmp_path), "--host", "http://example.com",
                 "--api-key", "sync-key"]) == 1
    assert "insecure" in capsys.readouterr().err
    assert not server.calls
    server.add_host("http://example.com")
    assert sync(tmp_path, writer.run_id, host="http://example.com", extra=("--insecure",)) == 0
    assert state(writer, "http://example.com")["complete"]


def test_security_code_symlink_hardlink_and_mismatch_refused(tmp_path, server, capsys):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"sensitive ssh key")
    writer = make_run(tmp_path, finished=False, points=0)
    manifest = add_code(writer, 3)

    # 1. Plant symlink for manifest[0]
    sha0 = manifest[0]["sha256"]
    obj0 = tmp_path / "objects" / sha0[:2] / sha0
    obj0.unlink()
    obj0.symlink_to(secret)

    # 2. Plant hard link for manifest[1]
    sha1 = manifest[1]["sha256"]
    obj1 = tmp_path / "objects" / sha1[:2] / sha1
    obj1.unlink()
    dummy = tmp_path / "dummy.txt"
    dummy.write_bytes(b"dummy")
    os.link(dummy, obj1)

    # 3. Content mismatch for manifest[2]
    sha2 = manifest[2]["sha256"]
    obj2 = tmp_path / "objects" / sha2[:2] / sha2
    obj2.write_bytes(b"corrupted bytes different from sha2")

    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    output = capsys.readouterr().out
    assert "refusing symlinked object" in output
    assert "refusing hard-linked object" in output
    assert "sha256 mismatch" in output
    assert "skipped=3" in output
    # None of them uploaded. Permanent plants and the sha mismatch are listed
    # and do not stay pending.
    assert len(server.matching("/artifacts/register")) == 0
    saved = state(writer)
    assert len(saved["code"]) == 0
    assert saved["complete"]
    assert {item["reason"] for item in saved["skipped"]} == {"symlink", "hardlink", "sha256 mismatch"}
    assert sync(tmp_path, writer.run_id) == 0
    assert "up to date" in capsys.readouterr().out
    assert len(server.matching("/artifacts/register")) == 0


def test_security_media_and_copied_artifact_symlink_and_hardlink_refused(tmp_path, server, capsys):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"secret")
    writer = make_run(tmp_path, finished=False, points=0)
    writer.add_media("preview", 1, b"PNG-1", "image/png", None)
    writer.add_media("preview", 2, b"PNG-2", "image/png", None)
    artifact_file1 = tmp_path / "art1.bin"
    artifact_file1.write_bytes(b"artifact1")
    artifact_file2 = tmp_path / "art2.bin"
    artifact_file2.write_bytes(b"artifact2")
    art1_event = writer.add_artifact(artifact_file1, "art1", "file", {}, copy=True)
    art2_event = writer.add_artifact(artifact_file2, "art2", "file", {}, copy=True)

    # Symlink media 1
    media_rows = list(RunReader(tmp_path, writer.run_id).media().values())[0]
    m_sha1 = media_rows[0]["sha256"]
    m_obj1 = tmp_path / "objects" / m_sha1[:2] / m_sha1
    m_obj1.unlink()
    m_obj1.symlink_to(secret)

    # Hardlink media 2
    m_sha2 = media_rows[1]["sha256"]
    m_obj2 = tmp_path / "objects" / m_sha2[:2] / m_sha2
    m_obj2.unlink()
    dummy = tmp_path / "dummy.txt"
    dummy.write_bytes(b"dummy")
    os.link(dummy, m_obj2)

    # Symlink artifact 1
    a_sha1 = art1_event["sha256"]
    a_obj1 = tmp_path / "objects" / a_sha1[:2] / a_sha1
    a_obj1.unlink()
    a_obj1.symlink_to(secret)

    # Hardlink artifact 2
    a_sha2 = art2_event["sha256"]
    a_obj2 = tmp_path / "objects" / a_sha2[:2] / a_sha2
    a_obj2.unlink()
    dummy2 = tmp_path / "dummy2.txt"
    dummy2.write_bytes(b"dummy2")
    os.link(dummy2, a_obj2)

    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    output = capsys.readouterr().out
    assert "refusing symlinked object" in output
    assert "refusing hard-linked object" in output
    assert len(server.matching("/log_media")) == 0
    assert len(server.matching("/artifacts/register-version")) == 0
    saved = state(writer)
    assert len(saved["media"]) == 0
    assert len(saved["artifacts"]) == 0
    assert saved["complete"]
    assert {item["reason"] for item in saved["skipped"]} == {"symlink", "hardlink"}
    assert len(saved["skipped"]) == 4
    before = len(server.calls)
    assert sync(tmp_path, writer.run_id) == 0
    assert "up to date" in capsys.readouterr().out
    assert len(server.calls) == before


def test_lineage_ordering_parents_first(tmp_path, server):
    # Test 1: explicit run IDs with child given first
    parent = make_run(tmp_path, "parent-1")
    child = make_run(tmp_path, "child-1", parent_run_id=parent.run_id)
    assert sync(tmp_path, child.run_id, parent.run_id) == 0
    init_calls = server.bodies("/init")
    assert len(init_calls) == 2
    assert init_calls[0]["name"] == "train"
    assert "parent_run_id" not in init_calls[0]
    assert init_calls[1]["parent_run_id"] == "remote-1"

    # Test 2: --all ordering
    parent2 = make_run(tmp_path, "parent-2")
    child2 = make_run(tmp_path, "child-2", parent_run_id=parent2.run_id)
    assert sync(tmp_path, extra=("--all",)) == 0
    init_calls2 = server.bodies("/init")
    assert len(init_calls2) == 4
    assert init_calls2[2]["name"] == "train"
    assert "parent_run_id" not in init_calls2[2]
    assert init_calls2[3]["parent_run_id"] == "remote-3"

    # Test 3: clock skew (child timestamp earlier than parent timestamp)
    parent3 = make_run(tmp_path, "parent-3")
    child3 = make_run(tmp_path, "child-3", parent_run_id=parent3.run_id)
    p3_meta_path = tmp_path / "runs" / parent3.run_id / "meta.json"
    c3_meta_path = tmp_path / "runs" / child3.run_id / "meta.json"
    p3_meta = json.loads(p3_meta_path.read_text())
    c3_meta = json.loads(c3_meta_path.read_text())
    p3_meta["created_at"] = "2026-05-10T12:00:00.000000Z"
    c3_meta["created_at"] = "2026-05-10T10:00:00.000000Z"
    p3_meta_path.write_text(json.dumps(p3_meta))
    c3_meta_path.write_text(json.dumps(c3_meta))
    assert sync(tmp_path, child3.run_id, parent3.run_id) == 0
    init_calls3 = server.bodies("/init")
    assert len(init_calls3) == 6
    assert init_calls3[5]["parent_run_id"] == "remote-5"


def test_quiet_live_run_watch_waits_for_finish(tmp_path, server, capsys, monkeypatch):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, finished=False, points=1)
    old = time.time() - 200
    os.utime(writer.run_dir / "meta.json", (old, old))
    os.utime(writer.run_dir / "scalars" / "_time.bin", (old, old))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        writer.finish("finished")

    monkeypatch.setattr(sync_module.time, "sleep", sleep)
    assert sync(tmp_path, writer.run_id, extra=("--watch", "0.2")) == 0
    assert slept == [0.2]
    assert server.bodies("/finalize")[0]["status"] == "finished"


def test_dead_run_finalizes_crashed_and_can_resume(tmp_path, server, monkeypatch):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, finished=False, points=1)
    writer.set_summary({"best": 3})
    writer.close()  # Same on-disk state and unlocked lifetime lock as SIGKILL.
    os.utime(writer.run_dir / "run.lock", (0, 0))
    before = (writer.run_dir / "meta.json").read_bytes()
    monkeypatch.setattr(sync_module.time, "sleep", lambda _: pytest.fail("watch waited on dead run"))
    assert sync(tmp_path, writer.run_id, extra=("--watch", "0.2")) == 0
    assert server.bodies("/finalize") == [{"summary": {"best": 3, "_exit_reason": "process died"},
                                           "status": "crashed"}]
    assert state(writer)["complete"]
    assert (writer.run_dir / "meta.json").read_bytes() == before
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    assert sync(tmp_path, writer.run_id) == 0
    assert not state(writer)["complete"]
    resumed.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert server.bodies("/finalize")[-1]["status"] == "finished"


def test_watch_401_403_permanent_failure(tmp_path, server, capsys, monkeypatch):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, points=1)

    def rejected(request):
        return 401, {}, json.dumps({"detail": "Invalid key"})

    server.mock.remove(responses.POST, BASE + "/log/batch")
    server.mock.add_callback(responses.POST, BASE + "/log/batch", callback=rejected)

    slept = []
    monkeypatch.setattr(sync_module.time, "sleep", lambda s: slept.append(s))
    assert sync(tmp_path, writer.run_id, extra=("--watch", "0.2")) == 1
    output = capsys.readouterr().out
    assert "API key rejected — run `agenticml login`" in output
    assert slept == []  # Did not retry


def test_dry_run_does_not_require_login(tmp_path, server, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_API_KEY", raising=False)
    writer = make_run(tmp_path, points=1)
    # Dry run should succeed without any login or credentials
    assert main(["sync", writer.run_id, "--dir", str(tmp_path), "--host", BASE, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dry-run (would send)" in output


def test_terminal_safety_strips_control_chars_and_ansi(tmp_path, server, capsys):
    writer = make_run(tmp_path, points=1)
    unsafe_detail = "\x1b[31;1mDangerous\x1b[0m\x00\x08\r\n\tMessage\x7f" + ("!" * 250)

    def fail_with_control(request):
        return 400, {}, json.dumps({"detail": unsafe_detail})

    server.mock.remove(responses.POST, BASE + "/log/batch")
    server.mock.add_callback(responses.POST, BASE + "/log/batch", callback=fail_with_control)

    assert sync(tmp_path, writer.run_id) == 1
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\x00" not in output
    assert "\x08" not in output
    assert "\x7f" not in output
    assert "DangerousMessage" in output
    # Kept within 200 characters cap for detail
    detail_part = output.split("HTTP 400: ")[1].split("\n")[0].split("  points=")[0]
    assert len(detail_part) <= 200


def test_payload_validation_coerces_malformed_meta(tmp_path, server, capsys):
    writer = make_run(
        tmp_path, finished=False, points=0,
        tags="not-a-list",
        name=12345,
        project=67890,
        notes={"invalid": "notes"},
        group=["invalid-group"],
        job_type=True,
    )
    add_code(writer, 1)
    writer.finish("finished")
    # Corrupt code content_type in manifest
    manifest_path = tmp_path / "runs" / writer.run_id / "events.jsonl"
    lines = manifest_path.read_text().splitlines()
    new_lines = []
    for line in lines:
        data = json.loads(line)
        if data.get("kind") == "code":
            for item in data.get("manifest", []):
                item["content_type"] = "invalid-no-slash"
        new_lines.append(json.dumps(data))
    manifest_path.write_text("\n".join(new_lines) + "\n")

    assert sync(tmp_path, writer.run_id) == 0
    init_payload = server.bodies("/init")[0]
    assert init_payload["tags"] == []
    assert init_payload["name"] == "12345"
    assert init_payload["project"] == "67890"
    assert init_payload["notes"] is None
    assert init_payload["group"] is None
    assert init_payload["job_type"] is None

    # Code content_type was coerced to application/octet-stream
    code_files = server.bodies("/artifacts/register")[0]["files"]
    assert code_files[0]["content_type"] == "application/octet-stream"


def test_remote_404_prompts_reset_and_reset_clears_host(tmp_path, server, capsys):
    writer = make_run(tmp_path, points=1)
    assert sync(tmp_path, writer.run_id) == 0
    assert server.init_count == 1
    capsys.readouterr()

    # Now simulate switching workspace/account on same host -> remote run 404s on log/batch
    writer_resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    writer_resumed.write_scalars([(2, {"loss": 0.5})])
    writer_resumed.finish("finished")

    fail_404 = [True]

    def not_found(request):
        if fail_404[0]:
            return 404, {}, json.dumps({"detail": "Not found"})
        return 200, {}, "{}"

    server.mock.add_callback(responses.PATCH, re.compile(r".*/runs/remote-1/config"), callback=not_found)
    server.mock.add_callback(responses.POST, re.compile(r".*/log/batch"), callback=not_found)

    # Next sync attempt hits 404 on first request
    assert sync(tmp_path, writer.run_id) == 1
    out = capsys.readouterr().out
    assert f"remote run not found on {BASE} — use `agenticml sync --reset {writer.run_id}`" in out

    # Test CLI validation for --reset
    assert main(["sync", "--reset", "--all", "--dir", str(tmp_path)]) == 2
    assert "requires explicit run ids, not --all" in capsys.readouterr().err

    assert main(["sync", "--reset", "--dir", str(tmp_path)]) == 2
    assert "requires explicit run ids" in capsys.readouterr().err

    # Reset host state
    assert main(["sync", "--reset", writer.run_id, "--dir", str(tmp_path), "--host", BASE]) == 0
    sync_data = json.loads((writer.run_dir / "sync.json").read_text())
    assert BASE not in sync_data["hosts"]

    # Now allow server to succeed and sync again -> performs a fresh /init
    fail_404[0] = False
    assert sync(tmp_path, writer.run_id) == 0
    assert server.init_count == 2


def test_sync_prune_big_and_small_series(tmp_path, server, capsys):
    writer = RunWriter.create(tmp_path, run_id="prune-test-run", meta={"project": "p", "name": "n"})
    big_count = OVERVIEW_MIN_POINTS + 1
    # Big series (> 65536 points) gets an overview; prune may drop its .bin.
    writer.write_scalars([(i, {"big": float(i)}) for i in range(big_count)])
    # Small series (<= 65536 points) keeps its .bin.
    writer.write_scalars([(i, {"small": float(i)}) for i in range(10)])
    writer.finish("finished")

    scalars_dir = writer.run_dir / "scalars"
    big_id = writer._series["big"].id
    small_id = writer._series["small"].id

    big_bin = scalars_dir / f"{big_id}.bin"
    big_ovw = scalars_dir / f"{big_id}.ovw"
    small_bin = scalars_dir / f"{small_id}.bin"
    small_ovw = scalars_dir / f"{small_id}.ovw"

    assert big_bin.is_file()
    assert big_ovw.is_file()
    assert small_bin.is_file()
    assert not small_ovw.exists()

    # Run sync with --prune
    assert main(["sync", "--prune", writer.run_id, "--dir", str(tmp_path), "--host", BASE, "--api-key", "key"]) == 0

    # Big series .bin is gone, .ovw is kept
    assert not big_bin.exists()
    assert big_ovw.is_file()

    # Small series .bin is kept
    assert small_bin.is_file()

    # _time.bin and index.json are kept
    assert (scalars_dir / "_time.bin").is_file()
    assert (scalars_dir / "index.json").is_file()

    # sync.json records pruned: true
    sync_data = json.loads((writer.run_dir / "sync.json").read_text())
    assert sync_data.get("pruned") is True or sync_data["hosts"][BASE].get("pruned") is True

    # RunReader.series still returns the overview
    reader = RunReader(tmp_path, writer.run_id)
    big_series = reader.series("big")
    assert big_series["mode"] == "overview"
    assert big_series["total"] == big_count

    small_series = reader.series("small")
    assert small_series["mode"] == "raw"
    assert small_series["total"] == 10

    # RunWriter.resume raises ValueError
    with pytest.raises(ValueError, match="was pruned after sync; it cannot be resumed"):
        RunWriter.resume(tmp_path, writer.run_id, must=True)

    # The host that already has the snapshot is up to date. Another host,
    # and this host after --reset, must not be given the remaining series.
    assert sync(tmp_path, writer.run_id) == 0
    second = "https://second.test"
    server.add_host(second)
    assert sync(tmp_path, writer.run_id, host=second) == 1
    refused = capsys.readouterr().out
    assert f"run was pruned after syncing to {BASE}" in refused
    assert "full scalar history is no longer local" in refused
    assert server.init_count == 1
    assert main(["sync", "--reset", writer.run_id, "--dir", str(tmp_path), "--host", BASE]) == 0
    assert sync(tmp_path, writer.run_id) == 1
    assert "full scalar history is no longer local" in capsys.readouterr().out
    assert server.init_count == 1


def test_prune_skips_when_the_run_is_open(tmp_path, server, capsys):
    writer = make_run(tmp_path, points=3)
    assert sync(tmp_path, writer.run_id) == 0
    from agenticml.store.layout import try_lock_run, unlock_run
    handle = try_lock_run(writer.run_dir)
    assert handle is not None
    try:
        assert sync(tmp_path, writer.run_id, extra=("--prune",)) == 0
    finally:
        unlock_run(handle)
    assert "skipping prune: run is open" in capsys.readouterr().out
    assert (writer.run_dir / "scalars" / "0.bin").is_file()
    assert not state(writer).get("pruned")


@pytest.mark.parametrize("already_synced", [False, True])
def test_prune_refuses_run_changed_in_lock_gap(tmp_path, server, monkeypatch, capsys, already_synced):
    import agenticml.sync as sync_mod

    writer = make_run(tmp_path)

    def cover_series():
        reader = RunReader(tmp_path, writer.run_id)
        for key in reader.keys():
            overview = Overview()
            overview.add_many(reader.series(key)["points"])
            series_id = writer._series[key].id
            (writer.run_dir / "scalars" / f"{series_id}.ovw").write_bytes(overview.to_bytes())

    cover_series()
    if already_synced:
        assert sync(tmp_path, writer.run_id) == 0
    original = sync_mod.prune_run
    before = {}
    fingerprints = []

    def raced_prune(root, run_id, keys, **kwargs):
        fingerprints.append(state(writer)["source_hash"])
        resumed = RunWriter.resume(root, run_id, must=True)
        resumed.write_scalars([(3, {"loss": 3.0, "acc": 0.75})])
        resumed.finish("finished", {"best": 0.75})
        # New overviews cover the appended data, so count checks alone pass.
        cover_series()
        before.update({path: path.read_bytes() for path in (writer.run_dir / "scalars").iterdir()})
        return original(root, run_id, keys, **kwargs)

    monkeypatch.setattr(sync_mod, "prune_run", raced_prune)
    assert sync(tmp_path, writer.run_id, extra=("--prune",)) == 0
    assert "skipping prune: run changed since sync; not pruning" in capsys.readouterr().out
    assert len(fingerprints) == 1
    assert {path: path.read_bytes() for path in (writer.run_dir / "scalars").iterdir()} == before
    saved = state(writer)
    assert saved["source_hash"] == fingerprints[0]
    assert saved["source_hash"] != run_fingerprint(tmp_path, writer.run_id)
    assert not saved.get("pruned")
    assert not json.loads((writer.run_dir / "sync.json").read_text()).get("pruned")


def test_prune_saves_post_prune_fingerprint_so_gc_keeps_it_synced(tmp_path, server, capsys):
    writer = RunWriter.create(
        tmp_path, run_id="prune-fingerprint", meta={"project": "p", "name": "n"},
    )
    writer.write_scalars([(i, {"loss": float(i)}) for i in range(3)])
    writer.finish("finished")
    overview = Overview()
    overview.add_many([(i, float(i)) for i in range(3)])
    series_id = writer._series["loss"].id
    scalars = writer.run_dir / "scalars"
    bin_path = scalars / f"{series_id}.bin"
    # finish() only writes an overview above OVERVIEW_MIN_POINTS, and drops a
    # smaller one. Plant a covering overview afterwards so --prune has work.
    (scalars / f"{series_id}.ovw").write_bytes(overview.to_bytes())

    assert sync(tmp_path, writer.run_id) == 0
    before = run_fingerprint(tmp_path, writer.run_id)
    saved = state(writer)
    assert saved["complete"] is True
    assert saved["source_hash"] == before
    assert not saved.get("pruned")

    assert sync(tmp_path, writer.run_id, extra=("--prune",)) == 0
    assert "skipping prune" not in capsys.readouterr().out
    assert not bin_path.is_file()
    after = run_fingerprint(tmp_path, writer.run_id)
    assert after != before
    saved = state(writer)
    assert saved["complete"] is True
    assert saved["pruned"] is True
    assert saved["source_hash"] == after

    # usage() and default collect() share gc's synced check: complete plus a
    # source_hash equal to the current fingerprint. A pre-prune hash would be
    # reported unsynced and left in place.
    rows = {row["run_id"]: row for row in usage(tmp_path)["runs"]}
    assert rows[writer.run_id]["synced"] is True
    report = collect(tmp_path)
    assert report["deleted_runs"] == [writer.run_id]
    assert writer.run_id not in report["in_use"]
    assert not writer.run_dir.exists()


def test_media_id_is_stable_across_thinning(tmp_path, server, monkeypatch):
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PER_KEY", "2")
    writer = RunWriter.create(
        tmp_path, run_id="media-thin", meta={"project": "demo", "name": "train"},
    )
    writer.add_media("preview", 1, b"a", "image/png", None)
    writer.add_media("preview", 2, b"b", "image/png", None)
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert set(state(writer)["media"]) == {"m:0", "m:1"}
    before = len(server.matching("/log_media"))
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    resumed.add_media("preview", 3, b"c", "image/png", None)
    ids = [row["id"] for row in RunReader(tmp_path, writer.run_id).media()["preview"]]
    assert ids == [0, 2]
    resumed.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert len(server.matching("/log_media")) == before + 1
    assert "m:0" in state(writer)["media"]
    assert "m:2" in state(writer)["media"]
    assert state(writer)["media"].count("m:0") == 1






def test_missing_media_object_stays_incomplete(tmp_path, server):
    writer = make_run(tmp_path, finished=False, points=0)
    writer.add_media("preview", 1, b"PNG", "image/png", None)
    row = RunReader(tmp_path, writer.run_id).media()["preview"][0]
    writer.finish("finished")
    obj = tmp_path / "objects" / row["sha256"][:2] / row["sha256"]
    obj.unlink()
    assert sync(tmp_path, writer.run_id) == 0
    assert not state(writer)["complete"]
    assert state(writer)["media"] == []
    assert state(writer)["skipped"] == []
    assert server.matching("/log_media") == []
    obj.write_bytes(b"PNG")
    assert sync(tmp_path, writer.run_id) == 0
    assert state(writer)["complete"]
    assert len(server.matching("/log_media")) == 1


def test_acknowledged_scalar_prefix_is_not_resent(tmp_path, server, monkeypatch):
    import agenticml.sync as sync_module
    monkeypatch.setattr(sync_module, "BATCH_ITEMS", 1)
    writer = RunWriter.create(tmp_path, run_id="prefix", meta={"project": "p", "name": "n"})
    writer.write_scalars([(5, {"a": 1.0}), (1, {"a": 2.0})])
    writer.write_scalars([(1, {"b": 3.0})])
    writer.finish("finished")
    server.fail_batch = 2
    assert sync(tmp_path, writer.run_id) == 1
    saved = state(writer)["offsets"]
    assert saved.get("b") == 8
    assert saved.get("a", 0) == 0
    server.fail_batch = None
    assert sync(tmp_path, writer.run_id) == 0
    accepted = [
        item
        for call in server.matching("/log/batch")
        if call.response.status_code == 200
        for item in json.loads(call.request.body)["items"]
    ]
    assert [item["step"] for item in accepted if "b" in item["metrics"]] == [1]
    assert [(item["step"], item["metrics"]["a"]) for item in accepted if "a" in item["metrics"]] == [
        (5, 1.0), (1, 2.0),
    ]




def test_sync_redacts_config_and_argv_from_existing_store(tmp_path, server):
    writer = make_run(tmp_path, finished=False, points=0)
    writer.set_config({"api_key": "local-secret", "nested": {"authToken": "hidden", "lr": 0.1},
                       "_argv": ["--password=abc", "--credential", "def", "--epochs", "5"]})
    writer.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    config = server.bodies("/init")[0]["config"]
    assert config == {"api_key": "***", "nested": {"authToken": "***", "lr": 0.1},
                      "_argv": ["--password=***", "--credential", "def", "--epochs", "5"]}
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    resumed.set_config({"secret": "new-secret", "lr": 0.2})
    resumed.finish("finished")
    assert sync(tmp_path, writer.run_id) == 0
    assert server.bodies("/config", "PATCH")[-1]["config"] == {"secret": "***", "lr": 0.2}


def test_sync_reuses_fingerprint_within_unchanged_step(tmp_path, server, monkeypatch):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path)
    original = sync_module.run_fingerprint
    calls = []

    def fingerprint(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(sync_module, "run_fingerprint", fingerprint)
    assert sync(tmp_path, writer.run_id) == 0
    assert len(calls) == 2  # Start snapshot and the check after upload.
    calls.clear()
    assert sync(tmp_path, writer.run_id) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("terminal", [False, True])
def test_finalize_holds_run_lock(tmp_path, server, terminal):
    from agenticml.store.layout import is_run_locked
    writer = make_run(tmp_path, finished=terminal, points=1)
    if not terminal:
        writer.close()
        os.utime(writer.run_dir / "run.lock", (0, 0))
    checked = []
    def finalize(request):
        assert is_run_locked(writer.run_dir) is True
        checked.append(True)
        return 200, {}, "{}"
    server.mock.remove(responses.POST, re.compile(re.escape(BASE) + r"/runs/[^/]+/finalize$"))
    server.mock.add_callback(responses.POST, re.compile(re.escape(BASE) + r"/runs/[^/]+/finalize$"), callback=finalize)
    assert sync(tmp_path, writer.run_id) == 0
    assert checked == [True]
    assert is_run_locked(writer.run_dir) is False


@pytest.mark.parametrize("terminal", [False, True])
def test_resume_before_finalize_lock_skips_active_run(tmp_path, server, monkeypatch, capsys, terminal):
    import agenticml.sync as sync_module
    writer = make_run(tmp_path, finished=terminal, points=1)
    if not terminal:
        writer.close()
        os.utime(writer.run_dir / "run.lock", (0, 0))
    original = sync_module.try_lock_run
    resumed = []
    def acquire(directory):
        resumed.append(RunWriter.resume(tmp_path, writer.run_id, must=True))
        return original(directory)
    monkeypatch.setattr(sync_module, "try_lock_run", acquire)
    try:
        assert sync(tmp_path, writer.run_id) == 0
        assert "run is active" in capsys.readouterr().out
        assert not server.matching("/finalize")
        assert not state(writer)["complete"]
    finally:
        for run in resumed:
            run.close()


def test_sync_snapshots_scalar_sizes_before_requests(tmp_path, server, monkeypatch):
    import agenticml.sync as sync_module
    monkeypatch.setattr(sync_module, "ROUND_RECORDS", 4)
    writer = make_run(tmp_path, finished=False, points=4)
    # Every upload adds a full round for every key. Reading a moving EOF would
    # never terminate. The assertion bounds failures without hanging pytest.
    calls = []
    def append(request):
        calls.append(True)
        assert len(calls) <= 2
        writer.write_scalars([(step, {"loss": float(step), "acc": 0.5}) for step in range(4, 8)])
        return 200, {}, "{}"
    server.mock.remove(responses.POST, BASE + "/log/batch")
    server.mock.add_callback(responses.POST, BASE + "/log/batch", callback=append)
    try:
        assert sync(tmp_path, writer.run_id) == 0
        assert len(calls) == 2
        assert state(writer)["offsets"] == {"loss": 32, "acc": 32}
        assert not state(writer)["complete"]
    finally:
        writer.close()
