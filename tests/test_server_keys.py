"""Key persistence, hot reload, and operator CLI checks."""
import hashlib
import json
import re
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from agenticml import cli
from agenticml.server import keys as key_module
from agenticml.server.app import make_server, resolve_data_dir
from agenticml.server.keys import KeyStore
from agenticml.store import layout


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    directory = tmp_path / "server"
    directory.mkdir()
    return KeyStore(directory)


def test_create_and_public_record(keys):
    record, key = keys.create("gpu-box-1")
    assert re.fullmatch(r"aml_[A-Za-z0-9_-]{43}", key)
    assert record.sha256 == hashlib.sha256(key.encode()).hexdigest()
    assert record.prefix == key[:8]
    assert record.scope == "sync"
    assert record.created_by == "cli"
    layout.parse_timestamp(record.created_at)
    assert record.last_used_at is None
    raw = keys.path.read_text()
    assert key not in raw
    assert record.sha256 in raw
    assert stat.S_IMODE(keys.path.stat().st_mode) == 0o600
    assert "sha256" not in record.to_public()
    assert record.sha256 not in repr(record)
    assert keys.list() == [record]
    assert keys.verify(key) == record
    assert keys.verify("wrong") is None
    assert not keys.has_admin()
    assert keys.revoke(record.name)
    assert not keys.revoke(record.name)
    assert keys.verify(key) is None


@pytest.mark.parametrize("name", ["", "env", "a/b", "..x/", "a b", "ä", "x" * 65, None])
def test_bad_names(keys, name):
    with pytest.raises(ValueError):
        keys.create(name)


def test_duplicate_scope_and_admin_recovery(keys):
    keys.create("admin", "admin")
    assert keys.has_admin()
    with pytest.raises(ValueError):
        keys.create("admin")
    with pytest.raises(ValueError):
        keys.create("x", "viewer")
    assert keys.revoke("admin")  # CLI recovery permits removing the last admin.
    assert not keys.has_admin()
    with pytest.raises(ValueError):
        keys.revoke("env")


def test_env_key(keys):
    key = "a" * 32
    store = KeyStore(keys.server_dir, key)
    _, file_key = store.create("file")
    assert store.verify(file_key).name == "file"
    assert store.verify(key).name == "env"
    assert store.verify(key).scope == "admin"
    assert store.verify(key).created_by == "env"
    assert [r.name for r in store.list()] == ["file", "env"]
    assert store.has_admin()
    store.touch(store.verify(key))
    assert store.verify(key).last_used_at
    assert "env" not in store.path.read_text()


@pytest.mark.parametrize("value", ["", "a" * 31, "a" * 257, "a" * 32 + "\n", " " * 32, "ä" * 32])
def test_bad_env_key(tmp_path, value, monkeypatch, capsys):
    with pytest.raises(ValueError, match="AGENTICML_SERVER_KEY"):
        make_server(tmp_path, port=0, env_key=value)
    monkeypatch.setenv("AGENTICML_SERVER_KEY", value)
    assert cli.main(["server", "serve", "--data", str(tmp_path), "--port", "0"]) == 1
    assert "AGENTICML_SERVER_KEY" in capsys.readouterr().err


def test_reload_and_corruption(keys, caplog):
    other = KeyStore(keys.server_dir)
    _, first = other.create("first")
    assert keys.verify(first).name == "first"
    good = keys.path.read_bytes()
    keys.path.write_bytes(b"not JSON")
    for _ in range(3):
        assert keys.verify(first) is None
    assert sum("file keys disabled" in r.message for r in caplog.records) == 1
    keys.path.write_bytes(good)
    assert keys.verify(first).name == "first"
    assert other.revoke("first")
    assert keys.verify(first) is None
    keys.path.unlink()
    assert keys.list() == []


@pytest.mark.parametrize("change", ["version", "keys", "name", "duplicate", "hash", "scope", "timestamp", "type"])
def test_invalid_records_fail_closed(keys, change):
    _, secret = keys.create("one")
    obj = json.loads(keys.path.read_text())
    if change == "version":
        obj["version"] = True
    elif change == "keys":
        obj["keys"] = {}
    elif change == "duplicate":
        obj["keys"] *= 2
    else:
        field, value = {"name": ("name", "env"), "hash": ("sha256", "z" * 64),
                        "scope": ("scope", "viewer"), "timestamp": ("last_used_at", "bad"),
                        "type": ("created_by", 5)}[change]
        obj["keys"][0][field] = value
    keys.path.write_text(json.dumps(obj))
    assert keys.verify(secret) is None


def test_symlink_unreadable_and_env_survives(keys, monkeypatch):
    _, secret = keys.create("one")
    original = keys.path.with_name("original")
    keys.path.rename(original)
    keys.path.symlink_to(original)
    store = KeyStore(keys.server_dir, "a" * 32)
    assert store.verify(secret) is None
    assert store.verify("a" * 32).name == "env"
    keys.path.unlink()
    original.rename(keys.path)
    def denied(path):
        raise PermissionError("denied")
    monkeypatch.setattr(layout, "read_file_nofollow", denied)
    assert store.verify(secret) is None


def test_verify_compares_every_record(keys, monkeypatch):
    _, first = keys.create("one")
    keys.create("two")
    calls = []
    compare = key_module.hmac.compare_digest
    def counted(a, b):
        calls.append(b)
        return compare(a, b)
    monkeypatch.setattr(key_module.hmac, "compare_digest", counted)
    assert keys.verify(first).name == "one"
    assert len(calls) == 2
    calls.clear()
    assert keys.verify("wrong") is None
    assert len(calls) == 2


def test_touch_throttles_and_does_not_resurrect(keys, monkeypatch):
    _, secret = keys.create("one")
    record = keys.verify(secret)
    now = layout.parse_timestamp(layout.format_timestamp())
    clock = [now.timestamp()]
    monkeypatch.setattr(key_module.time, "time", lambda: clock[0])
    keys.touch(record)
    first = keys.verify(secret)
    inode = keys.path.stat().st_ino
    keys.touch(record)
    assert keys.path.stat().st_ino == inode
    clock[0] += 61
    keys.touch(record)
    assert keys.path.stat().st_ino != inode
    assert first.last_used_at is not None
    other = KeyStore(keys.server_dir)
    other.revoke("one")
    other.create("one")  # Same name, different digest must also stay untouched.
    keys.touch(record)
    assert keys.list()[0].last_used_at is None
    other.revoke("one")
    keys.touch(record)
    assert keys.list() == []


def test_touch_errors_are_advisory(keys, monkeypatch, caplog):
    record, _ = keys.create("one")
    def failed(records):
        raise OSError("failed")
    monkeypatch.setattr(keys, "_write", failed)
    keys.touch(record)
    assert "could not update key last_used_at" in caplog.text


def test_concurrent_key_creates(keys):
    def create(index):
        return KeyStore(keys.server_dir).create(str(index))
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(create, range(16)))
    assert len(keys.list()) == len(records) == 16


def test_cli_works_with_server_lock(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    server = make_server(tmp_path, port=0)
    data = ["--data", str(tmp_path)]
    try:
        assert cli.main(["server", "keys", "create", "--name", "one", *data]) == 0
        output = capsys.readouterr()
        secret = output.out.strip()
        assert re.fullmatch(r"aml_[A-Za-z0-9_-]{43}", secret)
        assert "shown once; store it now" in output.err
        digest = hashlib.sha256(secret.encode()).hexdigest()
        assert server.keys.verify(secret).name == "one"
        assert cli.main(["server", "keys", "list", *data, "--json"]) == 0
        output = capsys.readouterr()
        row = json.loads(output.out)[0]
        assert row["name"] == "one" and row["scope"] == "sync"
        assert "sha256" not in row and digest not in output.out and secret not in output.out
        assert cli.main(["server", "keys", "list", *data]) == 0
        text = capsys.readouterr().out
        assert all(label in text for label in ("name", "scope", "prefix", "created_at", "created_by", "last_used_at"))
        assert digest not in text and secret not in text
        assert cli.main(["server", "keys", "revoke", "one", *data]) == 0
        assert server.keys.verify(secret) is None
        assert cli.main(["server", "keys", "revoke", "unknown", *data]) == 1
        assert "unknown key" in capsys.readouterr().err
    finally:
        server.server_close()


def test_data_dir_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_SERVER_DATA", str(tmp_path / "env"))
    assert resolve_data_dir() == tmp_path / "env"
    assert resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"
    monkeypatch.delenv("AGENTICML_SERVER_DATA")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_data_dir() == tmp_path / ".agenticml-server"


def test_env_prefix_never_exposes_secret(keys, monkeypatch, capsys):
    secret = "operator-secret-" + "a" * 32
    store = KeyStore(keys.server_dir, secret)
    assert store.verify(secret).prefix == "env"
    assert store.verify(secret).to_public()["prefix"] == "env"
    monkeypatch.setenv("AGENTICML_SERVER_KEY", secret)
    for json_output in ([], ["--json"]):
        assert cli.main(["server", "keys", "list", "--data", str(keys.server_dir.parent), *json_output]) == 0
        output = capsys.readouterr()
        assert "env" in output.out
        assert secret[:8] not in output.out + output.err


def test_cli_keys_creates_private_data_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "nested" / "data"
    assert cli.main(["server", "keys", "create", "--name", "one", "--data", str(data)]) == 0
    capsys.readouterr()
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE((data / "server").stat().st_mode) == 0o700


@pytest.mark.parametrize("env_key", ["", "short", "private-invalid-secret\n" + "x" * 32])
def test_cli_keys_recovers_from_invalid_env(tmp_path, monkeypatch, capsys, env_key):
    monkeypatch.setenv("AGENTICML_SERVER_KEY", env_key)
    data = ["--data", str(tmp_path)]
    assert cli.main(["server", "keys", "create", "--name", "one", *data]) == 0
    created = capsys.readouterr()
    assert "warning" not in created.err
    for json_output in ([], ["--json"]):
        assert cli.main(["server", "keys", "list", *data, *json_output]) == 0
        output = capsys.readouterr()
        assert len(output.err.splitlines()) == 1
        assert "warning: AGENTICML_SERVER_KEY" in output.err
        assert "listing file keys only" in output.err
        assert "private-invalid-secret" not in output.out + output.err
        if json_output:
            assert [row["name"] for row in json.loads(output.out)] == ["one"]
        else:
            assert "one" in output.out and "env" not in output.out
    assert cli.main(["server", "keys", "revoke", "one", *data]) == 0
    assert not capsys.readouterr().err
    assert KeyStore(tmp_path / "server").list() == []
