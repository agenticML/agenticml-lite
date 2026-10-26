"""Tests for code-snapshot collection: ignore rules, size caps, import tracing."""

from __future__ import annotations

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
    collect_code_snapshot,
    compute_snapshot_hash,
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


def test_collect_respects_gitignore(fake_repo):
    manifest, files = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    paths = {f.relative_path for f in files}

    assert "src/main.py" in paths
    assert "src/lib.py" in paths
    assert "pyproject.toml" in paths
    # gitignored
    assert "noisy.log" not in paths
    assert not any(p.startswith("data/") for p in paths)
    # virtualenv hard-skip
    assert not any(p.startswith(".venv") for p in paths)


def test_collect_respects_agenticmlignore(fake_repo):
    (fake_repo / "src" / "lib.py").write_text("# secret stuff\n")
    (fake_repo / ".agenticmlignore").write_text("src/lib.py\n")

    _manifest, files = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    paths = {f.relative_path for f in files}
    assert "src/main.py" in paths
    assert "src/lib.py" not in paths


def test_repo_snapshot_excludes_packaging_and_build_artifacts(fake_repo):
    generated_files = [
        fake_repo / "build" / "lib" / "generated.py",
        fake_repo / "dist" / "demo.whl",
        fake_repo / ".eggs" / "dependency.egg" / "module.py",
        fake_repo / "pip-wheel-metadata" / "metadata.json",
        fake_repo / "src" / "demo.egg-info" / "PKG-INFO",
        fake_repo / "src" / "demo.dist-info" / "METADATA",
        fake_repo / "src" / "cached.pyc",
        fake_repo / "demo.egg-link",
        fake_repo / "demo.egg",
    ]
    for path in generated_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n")

    # Similar names are legitimate source directories and must remain tracked.
    (fake_repo / "builder").mkdir()
    (fake_repo / "builder" / "source.py").write_text("VALUE = 1\n")
    (fake_repo / "distribution").mkdir()
    (fake_repo / "distribution" / "source.py").write_text("VALUE = 2\n")

    _manifest, files = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    paths = {item.relative_path for item in files}

    assert "builder/source.py" in paths
    assert "distribution/source.py" in paths
    assert not any("egg-info" in path or "dist-info" in path for path in paths)
    assert not any(path.startswith(("build/", "dist/", ".eggs/")) for path in paths)
    assert "pip-wheel-metadata/metadata.json" not in paths
    assert "src/cached.pyc" not in paths
    assert "demo.egg-link" not in paths
    assert "demo.egg" not in paths


def test_max_file_bytes_skips_oversized(fake_repo):
    big = fake_repo / "src" / "big.py"
    big.write_bytes(b"x" * 5000)

    manifest, files = collect_code_snapshot(
        entrypoint=fake_repo / "src" / "main.py",
        max_file_bytes=1000,
    )
    paths = {f.relative_path for f in files}
    assert "src/big.py" not in paths
    assert "src/big.py" in manifest["skipped"]["oversized"]


def test_max_total_bytes_caps_snapshot(fake_repo):
    for i in range(10):
        (fake_repo / "src" / f"f{i}.py").write_text("x = 1\n" * 200)

    manifest, files = collect_code_snapshot(
        entrypoint=fake_repo / "src" / "main.py",
        max_total_bytes=2000,
    )
    total = sum(f.size for f in files)
    assert total <= 2000
    assert manifest["skipped"]["over_total"]


def test_manifest_is_deterministic(fake_repo):
    m1, _ = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    m2, _ = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    assert manifest_to_json(m1) == manifest_to_json(m2)
    assert compute_snapshot_hash(m1) == compute_snapshot_hash(m2)


def test_snapshot_hash_changes_on_content_change(fake_repo):
    m1, _ = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    (fake_repo / "src" / "lib.py").write_text("def helper(): return 999\n")
    m2, _ = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    assert compute_snapshot_hash(m1) != compute_snapshot_hash(m2)


def test_extra_files_outside_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "main.py").write_text("print(1)\n")

    outside = tmp_path.parent / "outside.py"
    outside.write_text("# external file\n")
    try:
        _manifest, files = collect_code_snapshot(
            entrypoint=tmp_path / "main.py",
            extra_files=[str(outside)],
        )
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


def test_repo_mode_prunes_custom_named_virtualenv(fake_repo):
    custom_venv = fake_repo / "runtime-python"
    custom_venv.mkdir()
    (custom_venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (custom_venv / "leaked.py").write_text("# must not be tracked\n")

    _manifest, files = collect_code_snapshot(entrypoint=fake_repo / "src" / "main.py")
    assert not any(item.relative_path.startswith("runtime-python/") for item in files)
