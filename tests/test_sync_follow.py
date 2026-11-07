"""`agenticml sync --follow`: backoff, heartbeat, 401, signals, and CLI checks."""
from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from agenticml.client import Client, SyncStopped
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.keys import KeyStore
from agenticml import sync

from test_client import fake_http
from test_sync import command, identical, push, running, state, writer_at


@pytest.fixture
def remote(tmp_path, monkeypatch):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as server:
        _, key = KeyStore(tmp_path / "data" / "server").create("box", scope="sync")
        url = "http://127.0.0.1:%d" % server.server_address[1]
        with Client(url, key, timeout=2, max_retries=0) as client:
            yield server, client


@pytest.fixture
def follow_ctx(monkeypatch):
    import agenticml.client as client_mod

    ctx = {"clients": [], "after_stop": []}
    real = client_mod.Client

    class Tracking(real):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("max_retries", 0)
            kwargs.setdefault("timeout", 2)
            super().__init__(*args, **kwargs)
            ctx["clients"].append(self)
            original = self.session.request

            def logged(method, url, **kw):
                if self.stop is not None and self.stop.is_set():
                    ctx["after_stop"].append((method, url))
                return original(method, url, **kw)

            self.session.request = logged

    ctx["real"] = real
    ctx["tracking"] = Tracking
    monkeypatch.setattr(client_mod, "Client", Tracking)
    return ctx


def capture_prints(monkeypatch):
    chunks = {"out": [], "err": []}
    real = builtins.print

    def fake(*args, **kwargs):
        file = kwargs.get("file", sys.stdout)
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args) + end
        (chunks["err"] if file is sys.stderr else chunks["out"]).append(text)
        real(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", fake)
    return chunks


def spawn(fn):
    box = {}

    def run():
        try:
            box["code"] = fn()
        except BaseException as exc:
            box["exc"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def stop_follow(ctx):
    for client in ctx["clients"]:
        if client.stop is not None:
            client.stop.set()


def join_follow(thread, box, ctx=None, timeout=5):
    thread.join(timeout)
    if thread.is_alive():
        if ctx is not None:
            stop_follow(ctx)
        thread.join(1)
        pytest.fail("follow loop did not exit")
    if "exc" in box:
        raise box["exc"]
    return box.get("code")


def wait_until(pred, thread, box, ctx, timeout=4, msg="condition not met"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        if not thread.is_alive():
            if "exc" in box:
                raise box["exc"]
            pytest.fail("%s; exit=%s" % (msg, box.get("code")))
        time.sleep(0.01)
    stop_follow(ctx)
    join_follow(thread, box, ctx, timeout=1)
    pytest.fail(msg)


def test_server_down_then_back(tmp_path, monkeypatch, follow_ctx):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(sync, "BACKOFF_MAX", 0.08)
    monkeypatch.setattr(sync.random, "uniform", lambda a, b: b)
    data = tmp_path / "data"
    prepare_data_dir(data)
    _, key = KeyStore(data / "server").create("box", scope="sync")
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = "http://127.0.0.1:%d" % port
    writer = writer_at(tmp_path)
    remote = (None, SimpleNamespace(base_url=url, api_key=key))
    printed = capture_prints(monkeypatch)
    thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "0.03"))
    wait_until(lambda: len(re.findall(r"retrying in ([0-9.]+)s", "".join(printed["out"]))) >= 3,
               thread, box, follow_ctx, msg="backoff lines did not appear")
    delays = [float(x) for x in re.findall(r"retrying in ([0-9.]+)s", "".join(printed["out"]))]
    assert delays[0] < delays[1]
    assert delays[-1] == pytest.approx(0.08)
    assert all(d <= 0.08 + 1e-9 for d in delays)
    assert "server unreachable" in "".join(printed["out"])
    server = make_server(data, port=port)
    serve = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    serve.start()
    try:
        code = join_follow(thread, box, follow_ctx, timeout=5)
        assert code == 0
        identical((server, SimpleNamespace(base_url=url, api_key=key)), writer)
    finally:
        server.shutdown()
        serve.join(timeout=5)
        server.server_close()


def test_new_run_picked_up(remote, tmp_path, monkeypatch, follow_ctx):
    root = tmp_path / "local"
    root.mkdir()
    store = SimpleNamespace(root=root)
    printed = capture_prints(monkeypatch)
    thread, box = spawn(lambda: command(remote, store, "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: "following" in "".join(printed["out"]),
                   thread, box, follow_ctx, msg="follow did not start")
        writer = writer_at(tmp_path, run_id="late")

        def synced():
            try:
                identical(remote, writer)
                return True
            except AssertionError:
                return False

        wait_until(synced, thread, box, follow_ctx, msg="new run was not synced")
        identical(remote, writer)
    finally:
        stop_follow(follow_ctx)
        assert join_follow(thread, box, follow_ctx) == 0


def test_heartbeat_keeps_running_fresh(remote, tmp_path, monkeypatch, follow_ctx):
    monkeypatch.setattr(sync, "HEARTBEAT", 0.05)
    waits = []
    original_wait = threading.Event.wait

    def wrapped(self, timeout=None):
        if timeout is not None:
            for client in follow_ctx["clients"]:
                if client.stop is self:
                    waits.append(timeout)
                    break
        return original_wait(self, timeout)

    monkeypatch.setattr(threading.Event, "wait", wrapped)
    writer = writer_at(tmp_path, status=None)
    thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "1"))
    try:
        path = remote[0].root / "runs" / writer.run_id / "remote.json"

        def last_sync():
            if not path.is_file():
                return None
            return json.loads(path.read_bytes()).get("last_sync_at")

        wait_until(lambda: last_sync() is not None, thread, box, follow_ctx, msg="run did not sync")
        first = last_sync()
        wait_until(lambda: last_sync() not in (None, first),
                   thread, box, follow_ctx, msg="last_sync_at did not advance")
        assert last_sync() > first
        assert any(w == pytest.approx(0.05) for w in waits)
    finally:
        stop_follow(follow_ctx)
        join_follow(thread, box, follow_ctx)
        writer.close()


def test_401_exits_3(remote, tmp_path, monkeypatch, follow_ctx):
    writer = writer_at(tmp_path, status=None)
    printed = capture_prints(monkeypatch)
    thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: writer.run_id in "".join(printed["out"]),
                   thread, box, follow_ctx, msg="follow did not print the run")
        remote[0].keys.revoke("box")
        assert join_follow(thread, box, follow_ctx) == 3
        err = "".join(printed["err"])
        assert "API key rejected" in err
        assert "agenticml login" in err
        assert remote[1].api_key not in err + "".join(printed["out"])
    finally:
        stop_follow(follow_ctx)
        if thread.is_alive():
            join_follow(thread, box, follow_ctx)
        writer.close()


def test_stop_during_append_saves_cursor(remote, tmp_path, monkeypatch, follow_ctx):
    monkeypatch.setattr(sync, "APPEND_CHUNK", 64)
    writer = writer_at(tmp_path, count=100)
    original = follow_ctx["real"].append

    def append_and_stop(self, *args, **kwargs):
        size = original(self, *args, **kwargs)
        if self.stop is not None:
            self.stop.set()
        return size

    monkeypatch.setattr(follow_ctx["tracking"], "append", append_and_stop)
    assert command(remote, writer, writer.run_id, "--follow", "--interval", "0.02") == 0
    assert follow_ctx["after_stop"] == []
    saved = state(remote, writer)
    assert saved["files"]
    assert command(remote, writer, writer.run_id) == 0
    identical(remote, writer)


def test_explicit_ids_exit_when_finished(remote, tmp_path, follow_ctx):
    writer = writer_at(tmp_path)
    assert command(remote, writer, writer.run_id, "--follow", "--interval", "0.02") == 0
    identical(remote, writer)


@pytest.mark.parametrize("args", [
    ["--follow", "--watch", "1"],
    ["--follow", "--dry-run"],
    ["--follow", "--reset"],
    ["run-1", "--follow", "--interval", "0"],
    ["run-1", "--follow", "--interval", "nan"],
    ["run-1", "--interval", "1"],
])
def test_follow_argument_checks(remote, tmp_path, args):
    writer = writer_at(tmp_path)
    assert command(remote, writer, *args) == 2


def test_quiet_heartbeats(remote, tmp_path, monkeypatch, follow_ctx):
    monkeypatch.setattr(sync, "HEARTBEAT", 0.03)
    writer = writer_at(tmp_path, status=None)
    printed = capture_prints(monkeypatch)
    commits = []
    original = follow_ctx["real"].commit

    def counted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        commits.append(1)
        return result

    monkeypatch.setattr(follow_ctx["tracking"], "commit", counted)
    thread = None
    box = None
    try:
        push(remote, writer)
        thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "0.03"))
        wait_until(lambda: len(commits) >= 4, thread, box, follow_ctx, msg="heartbeat commits did not run")
        lines = [ln for ln in "".join(printed["out"]).splitlines() if writer.run_id in ln]
        assert len(lines) == 1
    finally:
        stop_follow(follow_ctx)
        if thread is not None:
            join_follow(thread, box, follow_ctx)
        writer.close()


def _sync_subprocess(remote, writer, tmp_path, *, install_sigint=True):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("AGENTICML_API_KEY", None)
    env.pop("AGENTICML_HOST", None)
    if install_sigint:
        entry = (
            "import signal; "
            "signal.signal(signal.SIGINT, signal.default_int_handler); "
            "from agenticml.cli import main; raise SystemExit(main())"
        )
    else:
        entry = "from agenticml.cli import main; raise SystemExit(main())"
    return subprocess.Popen(
        [sys.executable, "-u", "-c", entry, "sync", "--follow", "--interval", "0.05",
         writer.run_id, "--host", remote[1].base_url, "--api-key", remote[1].api_key,
         "--dir", str(writer.root)],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_following(proc, timeout=5):
    deadline = time.monotonic() + timeout
    buf = []
    box = {}

    def reader():
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                buf.append(line)
                if "following" in line:
                    box["ok"] = True
                    return
        except Exception as exc:
            box["exc"] = exc

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    while time.monotonic() < deadline:
        if box.get("ok"):
            thread.join(0.2)
            return "".join(buf)
        if box.get("exc") is not None:
            pytest.fail("stdout reader failed: %s" % box["exc"])
        if proc.poll() is not None and not thread.is_alive():
            out, err = proc.communicate()
            pytest.fail("sync --follow exited early: %s%s" % ("".join(buf) + (out or ""), err))
        thread.join(0.05)
    proc.kill()
    thread.join(1)
    leftover, err = proc.communicate()
    pytest.fail("sync --follow never printed following: %s%s" % ("".join(buf) + (leftover or ""), err))


def _http_error(code):
    response = requests.Response()
    response.status_code = code
    return requests.HTTPError(response=response)


@pytest.mark.skipif(sys.platform != "linux" or not hasattr(signal, "SIGTERM"), reason="requires Linux SIGTERM")
@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 0), (signal.SIGINT, 130)])
def test_signal_exits_with_summary(remote, tmp_path, sig, code):
    writer = writer_at(tmp_path, status=None)
    proc = None
    try:
        proc = _sync_subprocess(remote, writer, tmp_path)
        shown = _wait_following(proc)
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == code, out + err
        text = shown + out
        assert "Interrupted:" in text
        assert remote[1].api_key not in text + err
    finally:
        writer.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


@pytest.mark.skipif(sys.platform != "linux" or not hasattr(signal, "SIGINT"), reason="requires Linux SIGINT")
def test_second_sigint_prints_summary(tmp_path, monkeypatch):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    release = threading.Event()

    def hang():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            release.wait(30)
        finally:
            conn.close()

    acceptor = threading.Thread(target=hang, daemon=True)
    acceptor.start()
    writer = writer_at(tmp_path, status=None)
    proc = None
    try:
        url = "http://127.0.0.1:%d" % sock.getsockname()[1]
        remote = (None, SimpleNamespace(base_url=url, api_key="secret-key"))
        proc = _sync_subprocess(remote, writer, tmp_path, install_sigint=False)
        shown = _wait_following(proc)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.1)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == 130, out + err
        text = shown + out
        assert "Interrupted:" in text
        assert "secret-key" not in text + err
    finally:
        release.set()
        sock.close()
        acceptor.join(1)
        writer.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_follow_backoff_high_consecutive_does_not_overflow(monkeypatch):
    monkeypatch.setattr(sync, "BACKOFF_MAX", 0.08)
    assert sync._follow_backoff_delay(0.03, 10_000) == 0.08
    monkeypatch.setattr(sync, "BACKOFF_MAX", 300.0)
    assert sync._follow_backoff_delay(30.0, 1024) == 300.0
    with pytest.raises(OverflowError):
        0.03 * (2 ** 1024)


def test_whoami_429_then_syncs(tmp_path, monkeypatch, follow_ctx):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(sync, "BACKOFF_MAX", 0.08)
    monkeypatch.setattr(sync.random, "uniform", lambda a, b: b)
    data = tmp_path / "data"
    prepare_data_dir(data)
    _, key = KeyStore(data / "server").create("box", scope="sync")
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)

    def busy(request):
        request._send(429, b'{"error":"slow down"}')

    with fake_http(busy) as (httpd, url):
        port = httpd.server_address[1]
        remote = (None, SimpleNamespace(base_url=url, api_key=key))
        thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "0.03"))
        wait_until(lambda: "server unreachable" in "".join(printed["out"]),
                   thread, box, follow_ctx, msg="429 whoami did not back off")
        assert thread.is_alive()
        httpd.shutdown()
    server = make_server(data, port=port)
    serve = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    serve.start()
    try:
        code = join_follow(thread, box, follow_ctx, timeout=5)
        assert code == 0
        identical((server, SimpleNamespace(base_url=url, api_key=key)), writer)
        assert "HTTP 429" in "".join(printed["out"])
        assert key not in "".join(printed["out"]) + "".join(printed["err"])
    finally:
        server.shutdown()
        serve.join(timeout=5)
        server.server_close()


def test_plain_sync_whoami_429_exits_1(tmp_path, monkeypatch, capsys):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))

    def busy(request):
        request._send(429, b'{"error":"slow down"}')

    writer = writer_at(tmp_path)
    with fake_http(busy) as (_httpd, url):
        remote = (None, SimpleNamespace(base_url=url, api_key="secret-key"))
        assert command(remote, writer, writer.run_id) == 1
    err = capsys.readouterr().err
    assert "HTTP 429" in err
    assert "secret-key" not in err


def _raise_http(code):
    def fail(*_args, **_kwargs):
        raise _http_error(code)
    return fail


def _raise_run_missing(*_args, **_kwargs):
    raise sync.RunMissingError(response=_http_error(404).response)


@pytest.mark.parametrize("patch_target,attr,factory", [
    ("tracking", "commit", lambda: _raise_http(429)),
    ("tracking", "commit", lambda: _raise_http(503)),
    ("sync_run", None, lambda: _raise_run_missing),
], ids=["429", "503", "run-missing"])
def test_per_run_unreachable_backs_off(remote, tmp_path, monkeypatch, follow_ctx, patch_target, attr, factory):
    monkeypatch.setattr(sync, "BACKOFF_MAX", 0.05)
    monkeypatch.setattr(sync.random, "uniform", lambda a, b: b)
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    fail = factory()
    if patch_target == "tracking":
        monkeypatch.setattr(follow_ctx["tracking"], attr, fail)
    else:
        monkeypatch.setattr(sync, "sync_run", fail)
    thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: "server unreachable" in "".join(printed["out"]),
                   thread, box, follow_ctx, msg="unreachable per-run error did not back off")
        assert thread.is_alive()
        assert box.get("code") is None
    finally:
        stop_follow(follow_ctx)
        assert join_follow(thread, box, follow_ctx) == 0


def test_permanent_failure_is_not_retried(remote, tmp_path, monkeypatch, follow_ctx):
    writer_bad = writer_at(tmp_path, run_id="bad")
    writer_keep = writer_at(tmp_path, run_id="keep", status=None)
    seen = []
    keep_passes = []
    original = sync.sync_run

    def wrapped(reader, *args, **kwargs):
        if reader.run_id == writer_bad.run_id:
            seen.append(1)
            raise sync.OwnershipError("run id bad belongs to key other")
        keep_passes.append(1)
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(sync, "sync_run", wrapped)
    thread, box = spawn(lambda: command(
        remote, writer_keep, writer_bad.run_id, writer_keep.run_id, "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: len(seen) == 1 and len(keep_passes) >= 3,
                   thread, box, follow_ctx, msg="permanent run was retried or keep run stalled")
        assert seen == [1]
        assert thread.is_alive()
    finally:
        stop_follow(follow_ctx)
        join_follow(thread, box, follow_ctx)
        writer_keep.close()


def test_whoami_protocol_error_exits_1(tmp_path, monkeypatch, follow_ctx):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    printed = capture_prints(monkeypatch)

    def missing(request):
        request._send(404, b'{"error":"not found"}')

    writer = writer_at(tmp_path)
    with fake_http(missing) as (_httpd, url):
        remote = (None, SimpleNamespace(base_url=url, api_key="secret-key"))
        thread, box = spawn(lambda: command(remote, writer, writer.run_id, "--follow", "--interval", "0.02"))
        assert join_follow(thread, box, follow_ctx) == 1
    err = "".join(printed["err"])
    assert "is not an agenticml server with protocol 2" in err
    assert "secret-key" not in err + "".join(printed["out"])


def test_follow_unexpected_error_exits_1(remote, tmp_path, monkeypatch):
    printed = capture_prints(monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(sync, "_selected_ids", boom)
    writer = writer_at(tmp_path)
    assert command(remote, writer, "--follow", "--interval", "0.02") == 1
    assert "agenticml sync: RuntimeError" in "".join(printed["err"])


def test_follow_syncstopped_is_not_a_failure(remote, tmp_path, monkeypatch):
    printed = capture_prints(monkeypatch)

    def stopped(*_args, **_kwargs):
        raise SyncStopped()

    monkeypatch.setattr(sync, "_selected_ids", stopped)
    writer = writer_at(tmp_path)
    assert command(remote, writer, "--follow", "--interval", "0.02") == 0
    assert "Interrupted:" in "".join(printed["out"])
    assert "agenticml sync:" not in "".join(printed["err"])


def _count_emits(monkeypatch):
    seen = []
    real = sync._emit_follow

    def wrapped(result, *args, **kwargs):
        seen.append(result.run_id)
        return real(result, *args, **kwargs)

    monkeypatch.setattr(sync, "_emit_follow", wrapped)
    return seen


def _failure_clock(monkeypatch, start=0.0):
    now = {"t": start}
    real = sync._FailureLog
    monkeypatch.setattr(sync, "_FailureLog", lambda: real(clock=lambda: now["t"]))
    return now


def _failure_lines(printed, run_id):
    return [ln for ln in "".join(printed["out"]).splitlines()
            if run_id in ln and "failed:" in ln]


def test_follow_missing_run_prints_failure_once(remote, tmp_path, monkeypatch, follow_ctx):
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    emits = _count_emits(monkeypatch)
    thread, box = spawn(lambda: command(
        remote, writer, "missing-run", "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: emits.count("missing-run") >= 4, thread, box, follow_ctx,
                   msg="follow did not retry the missing run")
        lines = _failure_lines(printed, "missing-run")
        assert len(lines) == 1
        assert " (still failing)" not in lines[0]
        assert thread.is_alive()
    finally:
        stop_follow(follow_ctx)
        join_follow(thread, box, follow_ctx)


def test_follow_missing_run_prints_reminder_after_interval(remote, tmp_path, monkeypatch, follow_ctx):
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    clock = _failure_clock(monkeypatch)
    emits = _count_emits(monkeypatch)
    thread, box = spawn(lambda: command(
        remote, writer, "missing-run", "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: emits.count("missing-run") >= 2, thread, box, follow_ctx,
                   msg="follow did not print the missing run")
        first = _failure_lines(printed, "missing-run")
        assert len(first) == 1
        assert " (still failing)" not in first[0]
        clock["t"] = sync.FAILURE_REMINDER
        before = len(emits)
        wait_until(lambda: any("(still failing)" in ln for ln in _failure_lines(printed, "missing-run")),
                   thread, box, follow_ctx, msg="follow did not print a failure reminder")
        wait_until(lambda: len(emits) >= before + 3, thread, box, follow_ctx,
                   msg="follow did not keep retrying after the reminder")
        lines = _failure_lines(printed, "missing-run")
        assert len(lines) == 2
        assert " (still failing)" not in lines[0]
        assert lines[1].endswith(" (still failing)")
        assert lines[1] == lines[0] + " (still failing)"
    finally:
        stop_follow(follow_ctx)
        join_follow(thread, box, follow_ctx)


def test_follow_failure_then_success_prints_once_each(remote, tmp_path, monkeypatch, follow_ctx):
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    calls = []
    original = sync.sync_run

    def fail_once(reader, *args, **kwargs):
        if not calls:
            calls.append(1)
            raise OSError("disk")
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(sync, "sync_run", fail_once)
    thread, box = spawn(lambda: command(
        remote, writer, writer.run_id, "--follow", "--interval", "0.02"))
    code = join_follow(thread, box, follow_ctx, timeout=5)
    assert code == 0
    run_lines = [ln for ln in "".join(printed["out"]).splitlines() if writer.run_id in ln]
    fail_lines = [ln for ln in run_lines if "failed:" in ln]
    success_lines = [ln for ln in run_lines if "failed:" not in ln]
    assert len(fail_lines) == 1
    assert " (still failing)" not in fail_lines[0]
    assert "OSError" in fail_lines[0]
    assert len(success_lines) == 1


def test_watch_repeated_failure_prints_once(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    sleeps = []

    def after_several(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(sync.time, "sleep", after_several)
    assert command(remote, writer, "missing-run", "--watch", ".01") == 130
    lines = _failure_lines(printed, "missing-run")
    assert len(lines) == 1
    assert " (still failing)" not in lines[0]
    assert len(sleeps) == 3


def test_follow_fail_succeed_fail_prints_again(remote, tmp_path, monkeypatch, follow_ctx):
    writer = writer_at(tmp_path, status=None)
    printed = capture_prints(monkeypatch)
    calls = []
    original = sync.sync_run

    def fail_succeed_fail(reader, *args, **kwargs):
        n = len(calls)
        calls.append(1)
        if n % 2 == 0:
            raise OSError("disk")
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(sync, "sync_run", fail_succeed_fail)
    thread, box = spawn(lambda: command(
        remote, writer, writer.run_id, "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: len(_failure_lines(printed, writer.run_id)) >= 2,
                   thread, box, follow_ctx, msg="second failure after success was not printed")
        fail_lines = _failure_lines(printed, writer.run_id)
        success_lines = [ln for ln in "".join(printed["out"]).splitlines()
                         if writer.run_id in ln and "failed:" not in ln]
        assert " (still failing)" not in fail_lines[0]
        assert " (still failing)" not in fail_lines[1]
        assert "OSError" in fail_lines[0]
        assert "OSError" in fail_lines[1]
        assert len(success_lines) >= 1
    finally:
        stop_follow(follow_ctx)
        join_follow(thread, box, follow_ctx)
        writer.close()


def test_follow_failure_same_status_different_counts_prints_once(
        remote, tmp_path, monkeypatch, follow_ctx):
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    emits = _count_emits(monkeypatch)
    n = {"i": 0}

    def fail_with_progress(reader, host, client, result, **kwargs):
        n["i"] += 1
        result.files = n["i"]
        result.objects = n["i"]
        result.bytes = n["i"] * 10
        raise OSError("disk")

    monkeypatch.setattr(sync, "sync_run", fail_with_progress)
    thread, box = spawn(lambda: command(
        remote, writer, writer.run_id, "--follow", "--interval", "0.02"))
    try:
        wait_until(lambda: emits.count(writer.run_id) >= 4, thread, box, follow_ctx,
                   msg="follow did not retry the progressing failure")
        lines = _failure_lines(printed, writer.run_id)
        assert len(lines) == 1
        assert " (still failing)" not in lines[0]
        assert "files=1 objects=1 bytes=10" in lines[0]
        assert "files=2" not in "".join(printed["out"])
    finally:
        stop_follow(follow_ctx)
        join_follow(thread, box, follow_ctx)


def test_watch_failure_prints_reminder_after_interval(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    printed = capture_prints(monkeypatch)
    clock = _failure_clock(monkeypatch)
    sleeps = []

    def sleep_and_advance(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            clock["t"] = sync.FAILURE_REMINDER
        elif len(sleeps) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(sync.time, "sleep", sleep_and_advance)
    assert command(remote, writer, "missing-run", "--watch", ".01") == 130
    lines = _failure_lines(printed, "missing-run")
    assert len(lines) == 2
    assert " (still failing)" not in lines[0]
    assert lines[1].endswith(" (still failing)")
    assert lines[1] == lines[0] + " (still failing)"
    assert len(sleeps) == 3
