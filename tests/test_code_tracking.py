"""Tests for code-snapshot collection: ignore rules, size caps, import tracing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

from agenticml.code_tracking import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    RuntimeFileCollector,
    _is_store_artifact,
    compute_snapshot_hash,
    default_run_name,
    find_repo_root,
    manifest_to_json,
)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A repo with pyproject, source, ignored data, a venv, an entrypoint."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text("def helper(): return 1\n")
    (tmp_path / "src" / "main.py").write_text(
        "from .lib import helper\n"
        "from src.lib import helper as h2\n"
        "print(helper())\n"
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "huge.bin").write_bytes(b"x" * 1024)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "leaked.py").write_text("# should never be tracked")
    (tmp_path / ".gitignore").write_text("data/\n*.log\n")
    (tmp_path / "noisy.log").write_text("log line\n")
    return tmp_path


def test_find_repo_root_walks_up(fake_repo):
    nested = fake_repo / "src"
    assert find_repo_root(nested) == fake_repo


@pytest.mark.parametrize("marker", ["pyproject.toml", "setup.py", ".git"])
def test_default_run_name_relative_to_nearest_repo(tmp_path, marker):
    (tmp_path / "pyproject.toml").touch()
    repo = tmp_path / "nested"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (repo / marker).touch()
    script = scripts / "train.py"
    script.write_text("pass\n")
    assert default_run_name(script) == "scripts/train.py"
    assert default_run_name(str(script)) == "scripts/train.py"


def test_default_run_name_without_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("agenticml.code_tracking.PROJECT_MARKERS", (".missing-marker",))
    script = tmp_path / "train.py"
    script.write_text("pass\n")
    assert default_run_name(script) == script.resolve().as_posix()
    assert find_repo_root(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize("path", [None, "", "missing.py", "."])
def test_default_run_name_without_file(path):
    assert default_run_name(path) is None


def test_default_run_name_resolves_symlinks(fake_repo, tmp_path):
    link = tmp_path / "train.py"
    link.symlink_to(fake_repo / "src" / "main.py")
    assert default_run_name(link) == "src/main.py"


def test_default_run_name_ignores_os_errors(tmp_path, monkeypatch):
    def fail(path):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "resolve", fail)
    assert default_run_name(tmp_path / "train.py") is None


def test_max_file_bytes_skips_oversized(fake_repo):
    big = fake_repo / "src" / "big.py"
    big.write_bytes(b"x" * 5000)

    manifest, files = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=[big],
        max_file_bytes=1000,
    ).close()
    paths = {f.relative_path for f in files}
    assert "src/big.py" not in paths
    assert "src/big.py" in manifest["skipped"]["oversized"]


def test_max_total_bytes_caps_snapshot(fake_repo):
    for i in range(10):
        (fake_repo / "src" / f"f{i}.py").write_text("x = 1\n" * 200)

    manifest, files = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=list((fake_repo / "src").glob("f*.py")),
        max_total_bytes=2000,
    ).close()
    total = sum(f.size for f in files)
    assert total <= 2000
    assert manifest["skipped"]["over_total"]


def test_manifest_is_deterministic(fake_repo):
    m1, _ = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=[fake_repo / "src" / "lib.py"],
    ).close()
    m2, _ = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=[fake_repo / "src" / "lib.py"],
    ).close()
    assert manifest_to_json(m1) == manifest_to_json(m2)
    assert compute_snapshot_hash(m1) == compute_snapshot_hash(m2)


def test_snapshot_hash_changes_on_content_change(fake_repo):
    m1, _ = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=[fake_repo / "src" / "lib.py"],
    ).close()
    (fake_repo / "src" / "lib.py").write_text("def helper(): return 999\n")
    m2, _ = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=[fake_repo / "src" / "lib.py"],
    ).close()
    assert compute_snapshot_hash(m1) != compute_snapshot_hash(m2)


def test_extra_files_outside_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "main.py").write_text("print(1)\n")

    outside = tmp_path.parent / "outside.py"
    outside.write_text("# external file\n")
    try:
        _manifest, files = RuntimeFileCollector(
            entrypoint=tmp_path / "main.py",
            extra_files=[str(outside)],
        ).close()
        # The outside file gets prefixed with "extra/"
        paths = {f.relative_path for f in files}
        assert any(p.startswith("extra/") and p.endswith("outside.py") for p in paths)
    finally:
        outside.unlink(missing_ok=True)


def test_default_caps_are_sane():
    assert DEFAULT_MAX_FILE_BYTES == 10 * 1024 * 1024
    assert DEFAULT_MAX_TOTAL_BYTES == 100 * 1024 * 1024


def test_runtime_collector_tracks_imports_and_read_files(fake_repo):
    config = fake_repo / "settings.yaml"
    template = fake_repo / "src" / "prompt.jinja"
    config.write_text("epochs: 3\n")
    template.write_text("Hello {{ name }}\n")

    collector = RuntimeFileCollector(entrypoint=fake_repo / "src" / "main.py")
    module = types.ModuleType("runtime_demo_module")
    module.__file__ = str(fake_repo / "src" / "lib.py")
    sys.modules[module.__name__] = module
    try:
        config.read_text()
        with template.open("r", encoding="utf-8") as handle:
            handle.read()
        manifest, files = collector.close()
    finally:
        sys.modules.pop(module.__name__, None)

    by_path = {item.relative_path: item.source for item in files}
    assert by_path["src/main.py"] == "entrypoint"
    assert by_path["src/lib.py"] == "runtime_import"
    assert by_path["settings.yaml"] == "runtime_read"
    assert by_path["src/prompt.jinja"] == "runtime_read"
    assert manifest["tracking_mode"] == "runtime"


def test_runtime_collector_ignores_writes_and_supports_external_roots(fake_repo, tmp_path):
    external = tmp_path / "shared"
    external.mkdir()
    shared = external / "template.txt"
    shared.write_text("shared")
    generated = fake_repo / "generated.txt"

    collector = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        source_roots=[external],
    )
    shared.read_text()
    generated.write_text("output")
    _manifest, files = collector.close()

    paths = {item.relative_path for item in files}
    assert any(path.endswith("shared/template.txt") for path in paths)
    assert "generated.txt" not in paths


def test_runtime_collector_explicit_file_outside_roots(fake_repo, tmp_path):
    external = tmp_path.parent / f"{tmp_path.name}-manual.cfg"
    external.write_text("manual=true\n")
    try:
        collector = RuntimeFileCollector(entrypoint=fake_repo / "src" / "main.py")
        collector.add_explicit(external)
        _manifest, files = collector.close()
        assert f"extra/{external.name}" in {item.relative_path for item in files}
    finally:
        external.unlink(missing_ok=True)


def test_runtime_collector_explicit_file_overrides_ignore(fake_repo):
    ignored = fake_repo / "manual.log"
    ignored.write_text("required input\n")
    collector = RuntimeFileCollector(
        entrypoint=fake_repo / "src" / "main.py",
        extra_files=[ignored],
    )
    _manifest, files = collector.close()
    by_path = {item.relative_path: item.source for item in files}
    assert by_path["manual.log"] == "extra"


def test_runtime_collector_excludes_installation_artifacts(fake_repo):
    metadata = fake_repo / "src" / "demo.egg-info" / "PKG-INFO"
    built_module = fake_repo / "build" / "lib" / "generated.py"
    wheel = fake_repo / "dist" / "demo.whl"
    for path in (metadata, built_module, wheel):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n")

    loaded = types.ModuleType("generated_build_module")
    loaded.__file__ = str(built_module)
    sys.modules[loaded.__name__] = loaded
    try:
        collector = RuntimeFileCollector(
            entrypoint=fake_repo / "src" / "main.py",
            extra_files=[metadata],
        )
        metadata.read_text()
        wheel.read_bytes()
        _manifest, files = collector.close()
    finally:
        sys.modules.pop(loaded.__name__, None)

    paths = {item.relative_path for item in files}
    assert "src/main.py" in paths
    assert not any("egg-info" in path for path in paths)
    assert not any(path.startswith(("build/", "dist/")) for path in paths)


def test_runtime_collector_excludes_site_packages_and_custom_venv(fake_repo):
    site_module = fake_repo / "vendor" / "lib" / "site-packages" / "dependency.py"
    site_module.parent.mkdir(parents=True)
    site_module.write_text("VALUE = 1\n")

    custom_venv = fake_repo / "python-runtime"
    custom_venv.mkdir()
    (custom_venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    venv_module = custom_venv / "lib" / "dependency.py"
    venv_module.parent.mkdir()
    venv_module.write_text("VALUE = 2\n")

    site_loaded = types.ModuleType("site_dependency")
    site_loaded.__file__ = str(site_module)
    venv_loaded = types.ModuleType("venv_dependency")
    venv_loaded.__file__ = str(venv_module)
    sys.modules[site_loaded.__name__] = site_loaded
    sys.modules[venv_loaded.__name__] = venv_loaded
    try:
        collector = RuntimeFileCollector(entrypoint=fake_repo / "src" / "main.py")
        _manifest, files = collector.close()
    finally:
        sys.modules.pop(site_loaded.__name__, None)
        sys.modules.pop(venv_loaded.__name__, None)

    paths = {item.relative_path for item in files}
    assert not any("site-packages" in path for path in paths)
    assert not any(path.startswith("python-runtime/") for path in paths)


def test_runtime_collector_excludes_agenticml_package(tmp_path, monkeypatch):
    """A fake project that contains the package directory must not snapshot it."""
    import agenticml
    from agenticml.code_tracking import _agenticml_package_dir

    project = tmp_path / "project"
    package = project / "src" / "agenticml"
    package.mkdir(parents=True)
    init_py = package / "__init__.py"
    init_py.write_text("marker = 1\n")
    module_py = package / "helper.py"
    module_py.write_text("marker = 2\n")
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    train = project / "train.py"
    train.write_text("print('train')\n")
    user = project / "user_lib.py"
    user.write_text("marker = 3\n")
    notes = project / "agenticml_notes.txt"
    notes.write_text("keep\n")

    monkeypatch.setattr(agenticml, "__file__", str(init_py))
    _agenticml_package_dir.cache_clear()
    loaded = types.ModuleType("fake_agenticml_helper")
    loaded.__file__ = str(module_py)
    sys.modules[loaded.__name__] = loaded
    try:
        collector = RuntimeFileCollector(entrypoint=train)
        module_py.read_text()
        user.read_text()
        notes.read_text()
        _manifest, files = collector.close()
        paths = {item.relative_path for item in files}
        assert paths >= {"train.py", "user_lib.py", "agenticml_notes.txt"}
        assert not any(
            path == "src/agenticml" or path.startswith("src/agenticml/") for path in paths
        )

        explicit = RuntimeFileCollector(entrypoint=train)
        explicit.add_explicit(module_py)
        _manifest, explicit_files = explicit.close()
        assert "src/agenticml/helper.py" in {item.relative_path for item in explicit_files}
    finally:
        sys.modules.pop(loaded.__name__, None)
        _agenticml_package_dir.cache_clear()


def _code_event_paths(events_path: Path) -> list[str]:
    paths = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event.get("kind") != "code":
            continue
        for entry in event.get("manifest") or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.append(entry["path"])
    return paths


def _under_store(relative: str, project: Path, store: Path) -> bool:
    try:
        (project / relative).resolve().relative_to(store.resolve())
    except ValueError:
        return False
    return True


def _store_artifact_path(relative: str) -> bool:
    root = Path("/agenticml-store")
    return _is_store_artifact(root / relative, root)


def test_runtime_collector_excludes_store_artifacts_not_project_at_store_root(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    train = tmp_path / "train.py"
    train.write_text("print('train')\n")
    settings = tmp_path / "settings.yaml"
    settings.write_text("epochs: 1\n")
    run_id = "20240101T120000-abcd1234"
    events = tmp_path / "runs" / run_id / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text("{}\n")
    shard = tmp_path / "objects" / "ab" / ("a" * 64)
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"blob")
    object_tmp = tmp_path / "objects" / ".tmp.1.2.abcd1234.tmp"
    object_tmp.write_text("tmp\n")
    lineage_head = tmp_path / "lineage" / "demo-abcd1234.git" / "HEAD"
    lineage_head.parent.mkdir(parents=True)
    lineage_head.write_text("ref: refs/heads/main\n")
    lineage_lock = tmp_path / "lineage" / "demo-abcd1234.git.lock"
    lineage_lock.write_text("lock\n")
    lineage_agent_lock = tmp_path / "lineage" / "agenticml.lock"
    lineage_agent_lock.write_text("lock\n")
    worktree_file = tmp_path / "worktrees" / "demo-abcd1234" / run_id / "checked_out.py"
    worktree_file.parent.mkdir(parents=True)
    worktree_file.write_text("x = 1\n")
    usage = tmp_path / "usage.json"
    usage.write_text("{}\n")
    tmp = tmp_path / ".usage.json.1.2.abcd1234.tmp"
    tmp.write_text("tmp\n")

    collector = RuntimeFileCollector(entrypoint=train, store_root=tmp_path)
    settings.read_text()
    events.read_text()
    shard.read_bytes()
    object_tmp.read_text()
    lineage_head.read_text()
    lineage_lock.read_text()
    lineage_agent_lock.read_text()
    worktree_file.read_text()
    usage.read_text()
    tmp.read_text()
    _manifest, files = collector.close()
    paths = {item.relative_path for item in files}
    assert "train.py" in paths
    assert "settings.yaml" in paths
    assert "usage.json" not in paths
    assert tmp.name not in paths
    assert f"runs/{run_id}/events.jsonl" not in paths
    assert f"objects/ab/{'a' * 64}" not in paths
    assert "objects/.tmp.1.2.abcd1234.tmp" not in paths
    assert "lineage/demo-abcd1234.git/HEAD" not in paths
    assert "lineage/demo-abcd1234.git.lock" not in paths
    assert "lineage/agenticml.lock" not in paths
    assert f"worktrees/demo-abcd1234/{run_id}/checked_out.py" not in paths
    assert not any(_store_artifact_path(path) for path in paths)


def test_runtime_collector_tracks_project_files_sharing_store_dir_names(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    train = tmp_path / "train.py"
    train.write_text("print('train')\n")
    app = tmp_path / "server" / "app.py"
    app.parent.mkdir()
    app.write_text("APP = 1\n")
    config = tmp_path / "runs" / "config.yaml"
    config.parent.mkdir()
    config.write_text("epochs: 1\n")
    readme = tmp_path / "objects" / "readme.txt"
    readme.parent.mkdir()
    readme.write_text("notes\n")
    run_id = "20240101T120000-abcd1234"
    events = tmp_path / "runs" / run_id / "events.jsonl"
    events.parent.mkdir()
    events.write_text("{}\n")

    loaded = types.ModuleType("project_server_app")
    loaded.__file__ = str(app)
    sys.modules[loaded.__name__] = loaded
    try:
        collector = RuntimeFileCollector(entrypoint=train, store_root=tmp_path)
        app.read_text()
        config.read_text()
        readme.read_text()
        events.read_text()
        _manifest, files = collector.close()
    finally:
        sys.modules.pop(loaded.__name__, None)

    paths = {item.relative_path: item.source for item in files}
    assert "train.py" in paths
    assert paths["server/app.py"] in {"runtime_import", "runtime_read"}
    assert paths["runs/config.yaml"] == "runtime_read"
    assert paths["objects/readme.txt"] == "runtime_read"
    assert f"runs/{run_id}/events.jsonl" not in paths


def test_runtime_collector_keeps_project_when_store_is_ancestor(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    train = project / "train.py"
    train.write_text("print('train')\n")
    lib = project / "lib.py"
    lib.write_text("x = 1\n")

    collector = RuntimeFileCollector(entrypoint=train, store_root=tmp_path)
    lib.read_text()
    _manifest, files = collector.close()
    paths = {item.relative_path for item in files}
    assert "train.py" in paths
    assert "lib.py" in paths


def test_run_does_not_track_store_files_inside_project(tmp_path, monkeypatch):
    from agenticml.run import Run
    from agenticml.store.reader import RunReader

    (tmp_path / ".git").mkdir()
    train = tmp_path / "train.py"
    train.write_text("print('train')\n")
    settings = tmp_path / "settings.yaml"
    settings.write_text("epochs: 1\n")
    store = tmp_path / "agenticml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(train)])

    with Run(
        "p", "n", capture_env=False, system_metrics=False, auto_track=False, lineage=False,
    ) as run:
        settings.read_text()
        run.log({"loss": 0.1})

    assert run.root == store.resolve()
    reader = RunReader(run.root, run.run_id)
    manifest_paths = [entry["path"] for entry in reader.code_manifest()]
    event_paths = _code_event_paths(run.root / "runs" / run.run_id / "events.jsonl")
    assert "train.py" in manifest_paths
    assert "settings.yaml" in manifest_paths
    assert not any(_under_store(path, tmp_path, store) for path in manifest_paths)
    assert not any(_under_store(path, tmp_path, store) for path in event_paths)


def test_run_tracks_project_files_when_store_is_outside(tmp_path, monkeypatch):
    from agenticml.run import Run
    from agenticml.store.reader import RunReader

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    train = project / "train.py"
    train.write_text("print('train')\n")
    settings = project / "settings.yaml"
    settings.write_text("epochs: 1\n")
    store = tmp_path / "store"
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", [str(train)])

    with Run(
        "p", "n", dir=store, capture_env=False, system_metrics=False,
        auto_track=False, lineage=False,
    ) as run:
        settings.read_text()
        run.log({"loss": 0.1})

    assert run.root == store.resolve()
    reader = RunReader(run.root, run.run_id)
    manifest_paths = [entry["path"] for entry in reader.code_manifest()]
    event_paths = _code_event_paths(run.root / "runs" / run.run_id / "events.jsonl")
    assert "train.py" in manifest_paths
    assert "settings.yaml" in manifest_paths
    assert not any(_under_store(path, project, store) for path in manifest_paths)
    assert not any(_under_store(path, project, store) for path in event_paths)


def test_run_tracks_project_files_when_store_is_project_root(tmp_path, monkeypatch):
    from agenticml.run import Run
    from agenticml.store.reader import RunReader

    (tmp_path / ".git").mkdir()
    train = tmp_path / "train.py"
    train.write_text("print('train')\n")
    settings = tmp_path / "settings.yaml"
    settings.write_text("epochs: 1\n")
    app = tmp_path / "server" / "app.py"
    app.parent.mkdir()
    app.write_text("APP = 1\n")
    config = tmp_path / "runs" / "config.yaml"
    config.parent.mkdir()
    config.write_text("epochs: 1\n")
    readme = tmp_path / "objects" / "readme.txt"
    readme.parent.mkdir()
    readme.write_text("notes\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(train)])

    loaded = types.ModuleType("project_server_app_run")
    loaded.__file__ = str(app)
    sys.modules[loaded.__name__] = loaded
    try:
        with Run(
            "p", "n", dir=".", capture_env=False, system_metrics=False,
            auto_track=False, lineage=False,
        ) as run:
            settings.read_text()
            app.read_text()
            config.read_text()
            readme.read_text()
            run.log({"loss": 0.1})
            (run.root / "runs" / run.run_id / "events.jsonl").read_text()
    finally:
        sys.modules.pop(loaded.__name__, None)

    assert run.root == tmp_path.resolve()
    reader = RunReader(run.root, run.run_id)
    manifest_paths = [entry["path"] for entry in reader.code_manifest()]
    event_paths = _code_event_paths(run.root / "runs" / run.run_id / "events.jsonl")
    assert "train.py" in manifest_paths
    assert "settings.yaml" in manifest_paths
    assert "server/app.py" in manifest_paths
    assert "runs/config.yaml" in manifest_paths
    assert "objects/readme.txt" in manifest_paths
    assert f"runs/{run.run_id}/events.jsonl" not in manifest_paths
    assert not any(_store_artifact_path(path) for path in manifest_paths)
    assert not any(_store_artifact_path(path) for path in event_paths)


def test_run_tracks_project_files_when_store_is_parent(tmp_path, monkeypatch):
    from agenticml.run import Run
    from agenticml.store.reader import RunReader

    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    train = project / "train.py"
    train.write_text("print('train')\n")
    settings = project / "settings.yaml"
    settings.write_text("epochs: 1\n")
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", [str(train)])

    with Run(
        "p", "n", dir=tmp_path, capture_env=False, system_metrics=False,
        auto_track=False, lineage=False,
    ) as run:
        settings.read_text()
        run.log({"loss": 0.1})

    assert run.root == tmp_path.resolve()
    reader = RunReader(run.root, run.run_id)
    manifest_paths = [entry["path"] for entry in reader.code_manifest()]
    event_paths = _code_event_paths(run.root / "runs" / run.run_id / "events.jsonl")
    assert "train.py" in manifest_paths
    assert "settings.yaml" in manifest_paths
    assert not any(_store_artifact_path(path) for path in manifest_paths)
    assert not any(_store_artifact_path(path) for path in event_paths)
