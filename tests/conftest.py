import pytest


@pytest.fixture(autouse=True)
def local_run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path / "agenticml"))
