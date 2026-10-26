"""Smoke tests covering the public surface of the agenticml SDK."""

import json
import sys
import tempfile
from pathlib import Path

import agenticml


def test_version_set():
    assert agenticml.__version__


def test_public_surface():
    expected = {
        "Client",
        "Image",
        "Run",
        "config",
        "finish",
        "get_active_run",
        "get_rank",
        "init",
        "is_main_process",
        "log",
        "log_artifact",
        "summary",
        "track_files",
        "track_loss",
        "track_model",
        "track_optimizer",
        "track_dataloader",
    }
    assert expected.issubset(set(agenticml.__all__))


def test_client_constructs(monkeypatch):
    monkeypatch.delenv("AGENTICML_HOST", raising=False)
    monkeypatch.delenv("AGENTICML_API_KEY", raising=False)
    c = agenticml.Client(api_key="test")
    assert c.api_key == "test"
    assert c.base_url == "https://api.agenticml.xyz"


def test_client_strips_trailing_slash():
    c = agenticml.Client(base_url="https://api.agenticml.xyz/")
    assert c.base_url == "https://api.agenticml.xyz"


def test_client_reads_env(monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", "https://example.com")
    monkeypatch.setenv("AGENTICML_API_KEY", "envkey")
    c = agenticml.Client()
    assert c.base_url == "https://example.com"
    assert c.api_key == "envkey"


def test_disabled_mode_runs_full_lifecycle(monkeypatch):
    monkeypatch.setenv("AGENTICML_MODE", "disabled")
    run = agenticml.init("demo", "smoke")
    assert type(run).__name__ == "_DisabledRun"

    agenticml.log({"loss": 0.5})
    agenticml.log({"acc": 0.9}, step=10)
    agenticml.log({"partial": 1.0}, commit=False)
    agenticml.config["lr"] = 0.01
    agenticml.summary["final"] = "ok"
    agenticml.track_model(object())
    agenticml.track_optimizer(object())
    agenticml.track_loss(object())
    agenticml.track_dataloader(object())
    agenticml.finish()


def test_distributed_helpers(monkeypatch):
    for var in ("RANK", "LOCAL_RANK", "SLURM_PROCID", "PMI_RANK", "OMPI_COMM_WORLD_RANK"):
        monkeypatch.delenv(var, raising=False)
    assert agenticml.get_rank() == 0
    assert agenticml.is_main_process()

    monkeypatch.setenv("RANK", "3")
    assert agenticml.get_rank() == 3
    assert not agenticml.is_main_process()


def test_ignore_matcher():
    from agenticml.ignore import IgnoreMatcher

    m = IgnoreMatcher(["*.log", "!keep.log", "data/", "__pycache__/"])
    assert m.matches("foo.log")
    assert not m.matches("keep.log")
    assert m.matches("data", is_dir=True)
    assert m.matches("a/b/data", is_dir=True)
    assert m.matches("src/__pycache__", is_dir=True)
    assert not m.matches("src/foo.py")


def test_env_capture_returns_basics():
    from agenticml.env import capture_environment

    env = capture_environment()
    assert set(env.keys()) == {"python_version", "platform", "hostname", "packages"}
    assert env["python_version"]
    assert isinstance(env["packages"], dict)


def test_offline_run_writes_journal(monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("AGENTICML_OFFLINE_DIR", tmpdir)

        run = agenticml.init(
            "demo",
            "offline",
            mode="offline",
            track_source=False,
            capture_env=False,
            system_metrics=False,
            only_main_process=False,
        )
        assert run.run_id.startswith("offline-")

        agenticml.log({"loss": 0.5})
        agenticml.log({"acc": 0.9}, step=5)

        artifact_file = Path(tmpdir) / "model.bin"
        artifact_file.write_bytes(b"fake model weights")
        agenticml.log_artifact(str(artifact_file), name="my_model", type="model")

        agenticml.summary["final"] = 1.0
        agenticml.finish()

        run_dirs = [
            p for p in Path(tmpdir).iterdir() if p.is_dir() and p.name.startswith("offline-")
        ]
        assert len(run_dirs) == 1
        journal_lines = (run_dirs[0] / "journal.jsonl").read_text().splitlines()
        kinds = {line.split('"kind":', 1)[1].split('"')[1] for line in journal_lines}
        assert {"init", "log_artifact", "log_batch", "finalize"}.issubset(kinds)


def test_run_auto_step_and_commit_merging():
    from agenticml import Run
    from agenticml import run as run_mod

    class FakeClient:
        def __init__(self, *a, **kw):
            self.batches: list = []
            self.finalized = None

        def init_run(self, payload):
            return "fake-run-id"

        def log_batch(self, items):
            self.batches.append(list(items))

        def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
            self.finalized = (run_id, dict(summary), status, snapshot_hash)

        def upload_code(self, *a, **kw):
            return {}

        def get_run(self, run_id):
            return None

        def artifacts_exists(self, x):
            return []

        def log_artifact(self, *a, **kw):
            return {}

        def log_media(self, *a, **kw):
            return {}

    fake = FakeClient()
    original = run_mod.make_client
    run_mod.make_client = lambda mode, **kw: fake
    try:
        with Run(
            "p",
            "n",
            track_source=False,
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
        ) as r:
            r.log({"a": 1})
            r.log({"b": 2}, commit=False)
            r.log({"c": 3})
            r.log({"d": 4}, step=10)
            r.summary["done"] = True
            r._flush()
    finally:
        run_mod.make_client = original

    flat = [item for batch in fake.batches for item in batch]
    by_step = {item["step"]: item["metrics"] for item in flat}
    assert by_step[0] == {"a": 1}
    assert by_step[1] == {"b": 2, "c": 3}
    assert by_step[10] == {"d": 4}
    assert fake.finalized == ("fake-run-id", {"done": True}, "finished", None)


def test_resume_must_missing_raises():
    from agenticml import Run
    from agenticml import run as run_mod

    class StubMissing:
        def __init__(self, *a, **kw):
            pass

        def get_run(self, run_id):
            return None

        def init_run(self, payload):
            return "x"

        def log_batch(self, items):
            pass

        def finalize(self, *a, **kw):
            pass

        def artifacts_exists(self, x):
            return []

    original = run_mod.make_client
    run_mod.make_client = lambda mode, **kw: StubMissing()
    try:
        raised = False
        try:
            Run(
                "p",
                "n",
                id="missing",
                resume="must",
                track_source=False,
                capture_env=False,
                system_metrics=False,
            )
        except RuntimeError as e:
            raised = True
            assert "resume='must'" in str(e)
        assert raised, "expected RuntimeError"
    finally:
        run_mod.make_client = original


def test_runtime_source_sends_bytes_when_server_has_nothing(tmp_path, monkeypatch):
    """The complement of the dedup case: an empty server gets the content."""
    from agenticml import Run
    from agenticml import run as run_mod

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'runtime-demo'\n")
    entrypoint = tmp_path / "train.py"
    entrypoint.write_text("print('train')\n")
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    class FakeClient:
        def __init__(self):
            self.files = None

        def init_run(self, payload):
            return "runtime-run"

        def log_batch(self, items):
            pass

        def artifacts_exists(self, hashes):
            return []

        def upload_code(self, run_id, manifest_json, files):
            self.files = files
            return {"status": "ok"}

        def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
            pass

    fake = FakeClient()
    monkeypatch.setattr(run_mod, "make_client", lambda mode, **kwargs: fake)
    run = Run("p", "n", capture_env=False, system_metrics=False, flush_interval=999)
    run.finish()

    assert fake.files
    for item in fake.files:
        assert item["stored"] is False
        assert item["content"], f"{item['path']} should carry bytes"


def test_runtime_source_uploads_content_even_when_server_knows_the_hashes(tmp_path, monkeypatch):
    from agenticml import Run
    from agenticml import run as run_mod

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'runtime-demo'\n")
    entrypoint = tmp_path / "train.py"
    entrypoint.write_text("print('train')\n")
    config = tmp_path / "config.yaml"
    config.write_text("epochs: 2\n")
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    class FakeClient:
        def __init__(self):
            self.manifest = None
            self.files = None
            self.final_snapshot = None

        def init_run(self, payload):
            return "runtime-run"

        def log_batch(self, items):
            pass

        def artifacts_exists(self, hashes):
            return list(hashes)

        def upload_code(self, run_id, manifest_json, files):
            self.manifest = json.loads(manifest_json)
            self.files = files
            return {"status": "ok"}

        def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
            self.final_snapshot = snapshot_hash

    fake = FakeClient()
    monkeypatch.setattr(run_mod, "make_client", lambda mode, **kwargs: fake)
    run = Run(
        "p",
        "n",
        capture_env=False,
        system_metrics=False,
        flush_interval=999,
    )
    config.read_text()
    run.finish()

    assert fake.manifest["tracking_mode"] == "runtime"
    assert {item["path"] for item in fake.manifest["files"]} >= {"train.py", "config.yaml"}

    # Every file reaches upload_code even though the server reports holding
    # every hash. Bytes are skipped; registration is not. Objects are addressed
    # by content hash, so a file stored by an earlier run is already at the key
    # this run needs, and registration attaches it.
    #
    # The earlier bug dropped these files entirely: run-scoped keys meant the
    # content lived elsewhere, so skipping the upload left no object and no
    # artifact row, and the file vanished from the run.
    registered = {item["path"] for item in fake.files}
    assert registered >= {"train.py", "config.yaml"}
    for item in fake.files:
        assert item["sha256"], f"{item['path']} needs a sha256 to presign"
        # Marked as already stored, so the client will skip the PUT — but the
        # bytes are still carried, so a server that turns out not to have the
        # content can be recovered from rather than silently losing the file.
        assert item["stored"] is True
        assert item["content"]
    assert fake.final_snapshot
