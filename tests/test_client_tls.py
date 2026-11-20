"""Client trust for private networks: CA bundles, saved http consent, TLS errors."""
from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest
import requests
import urllib3

from agenticml import auth
from agenticml import client as client_mod
from agenticml.cli import main
from agenticml.client import Client, tls_failure, tls_hint

from test_sync_follow import (  # noqa: F401  (follow_ctx is a fixture)
    capture_prints, follow_ctx, join_follow, spawn, stop_follow, wait_until,
)
from tls_helpers import make_test_certs

KEY = "tls-test-api-key-1234567890"
CERT_HINT = "TLS certificate check failed"
HANDSHAKE_HINT = "TLS handshake failed (the server closed the connection, or this is not an https port)"


@pytest.fixture(scope="session")
def certs(tmp_path_factory):
    return make_test_certs(tmp_path_factory.mktemp("tls"))


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "AGENTICML_CA_BUNDLE",
                 "AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path / "project-data"))


class _Quiet:
    def handle_error(self, request, client_address):  # handshake failures are expected
        pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/v2/whoami":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        hits = getattr(self.server, "hits", None)
        if hits is not None:
            hits.append(1)
        body = json.dumps({"protocol": 2, "key": "box", "scope": "sync", "features": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _TLSServer(_Quiet, ThreadingHTTPServer):
    daemon_threads = True


class _PlainServer(_Quiet, HTTPServer):
    pass


@contextmanager
def _serving(httpd):
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.fixture
def tls_server(certs):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certs.cert), str(certs.key))
    httpd = _TLSServer(("127.0.0.1", 0), _Handler)
    httpd.hits = []  # one entry per authenticated whoami
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True,
                                       do_handshake_on_connect=False)
    with _serving(httpd):
        yield httpd


@pytest.fixture
def tls_url(tls_server):
    return f"https://localhost:{tls_server.server_address[1]}"


@pytest.fixture
def plain_url():
    httpd = _PlainServer(("127.0.0.1", 0), _Handler)
    with _serving(httpd):
        yield f"http://127.0.0.1:{httpd.server_address[1]}"


_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


@pytest.fixture
def non_tls_port():
    """A raw TCP listener that answers any bytes with an http 400, without a reset.

    http.server reads only the first line of a ClientHello and closes with unread
    data, so the kernel may send a RST before the client reads the answer. This
    one replies at once, half-closes, and drains until the client closes, so a TLS
    client always sees the plain-text reply (WRONG_VERSION_NUMBER).
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    sockets = [listener]
    threads = []
    lock = threading.Lock()

    def serve(conn):
        try:
            conn.settimeout(2)
            conn.sendall(_BAD_REQUEST)
            conn.shutdown(socket.SHUT_WR)
            while conn.recv(4096):
                pass
        except OSError:
            pass
        finally:
            conn.close()

    def accept_loop():
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            worker = threading.Thread(target=serve, args=(conn,), daemon=True)
            with lock:
                threads.append(worker)
            worker.start()

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()
    try:
        yield f"https://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        try:
            listener.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept()
        except OSError:
            pass
        listener.close()
        acceptor.join(timeout=5)
        with lock:
            pending = list(threads)
        for worker in pending:
            worker.join(timeout=5)


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def copy_bundle(source, target, mode=0o644):
    target.write_bytes(source.read_bytes())
    target.chmod(mode)  # the umask may leave it group-writable
    return target


def save(host, **entry):
    auth.write_credentials({"default_host": host, "hosts": {host: {"api_key": KEY, **entry}}})


def saved_entry(host):
    return json.loads(auth.credentials_path().read_text())["hosts"][host]


def run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- whoami and the CA bundle ---------------------------------------------------


def test_whoami_with_ca_bundle_succeeds(tls_url, certs, capsys):
    save(tls_url)
    code, out, err = run(["whoami", "--ca-bundle", str(certs.ca1)], capsys)
    assert code == 0, err
    assert "valid" in out


def test_whoami_untrusted_certificate_gives_hint_without_retries(tls_url, no_sleep, capsys):
    save(tls_url)
    started = time.monotonic()
    code, _out, err = run(["whoami"], capsys)
    assert code == 1
    assert CERT_HINT in err
    assert "--ca-bundle" in err and "AGENTICML_CA_BUNDLE" in err
    assert "SSLError" not in err and "CERTIFICATE_VERIFY_FAILED" not in err
    assert KEY not in err
    assert no_sleep == []
    assert time.monotonic() - started < 3


def test_login_untrusted_certificate_gives_hint(tls_url, no_sleep, capsys):
    code, _out, err = run(["login", "--host", tls_url, "--key", KEY], capsys)
    assert code == 1
    assert CERT_HINT in err and "SSLError" not in err
    assert no_sleep == []
    assert not auth.credentials_path().exists()


def test_client_does_not_retry_certificate_failures(monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(kwargs.get("verify"))
        raise requests.exceptions.SSLError(
            urllib3.exceptions.SSLError(ssl.SSLCertVerificationError("details")))

    with Client(base_url="https://lab.test", api_key=KEY, max_retries=3, ca_bundle="/x/ca.pem") as client:
        monkeypatch.setattr(client.session, "request", boom)
        with pytest.raises(requests.exceptions.SSLError):
            client.whoami()
    assert calls == ["/x/ca.pem"]


def test_client_retries_a_handshake_that_the_server_drops(no_sleep, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    accepted = []

    def closer():
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            accepted.append(1)
            conn.close()

    thread = threading.Thread(target=closer, daemon=True)
    thread.start()
    try:
        url = f"https://localhost:{listener.getsockname()[1]}"
        with Client(base_url=url, api_key=KEY, max_retries=2) as client:
            with pytest.raises(requests.ConnectionError):
                client.whoami()
    finally:
        try:
            listener.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept()
        except OSError:
            pass
        listener.close()
        thread.join(timeout=5)
    assert len(accepted) == 3  # 1 + max_retries
    assert len(no_sleep) == 2


def test_client_passes_nothing_without_a_bundle(monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        raise requests.exceptions.ConnectTimeout()

    with Client(base_url="https://lab.test", api_key=KEY, max_retries=0) as client:
        monkeypatch.setattr(client.session, "request", fake)
        with pytest.raises(requests.exceptions.ConnectTimeout):
            client.whoami()
    assert "verify" not in seen


# --- precedence ---------------------------------------------------------------


def test_flag_beats_saved_bundle_beats_environment(tls_url, certs, monkeypatch, capsys):
    monkeypatch.setenv("AGENTICML_CA_BUNDLE", str(certs.ca2))
    save(tls_url, ca_bundle=str(certs.ca2))
    # Flag (good) over saved (wrong) and env (wrong).
    assert run(["whoami", "--ca-bundle", str(certs.ca1)], capsys)[0] == 0
    # Flag (wrong) over saved (good).
    save(tls_url, ca_bundle=str(certs.ca1))
    code, _out, err = run(["whoami", "--ca-bundle", str(certs.ca2)], capsys)
    assert code == 1 and CERT_HINT in err
    # Saved (good) over env (wrong).
    assert run(["whoami"], capsys)[0] == 0
    # Env (good) when nothing is saved; env (wrong) fails.
    save(tls_url)
    monkeypatch.setenv("AGENTICML_CA_BUNDLE", str(certs.ca1))
    assert run(["whoami"], capsys)[0] == 0
    monkeypatch.setenv("AGENTICML_CA_BUNDLE", str(certs.ca2))
    code, _out, err = run(["whoami"], capsys)
    assert code == 1 and CERT_HINT in err


def test_resolve_connection_reports_the_unvalidated_choice(certs, monkeypatch):
    host = "https://lab.test"
    save(host, ca_bundle="/saved/ca.pem")
    assert auth.resolve_connection(ca_bundle="/flag.pem").ca_bundle == "/flag.pem"
    assert auth.resolve_connection().ca_bundle == "/saved/ca.pem"
    assert auth.resolve_connection(use_saved=False).ca_bundle is None
    monkeypatch.setenv("AGENTICML_CA_BUNDLE", "/env/ca.pem")
    assert auth.resolve_connection().ca_bundle == "/saved/ca.pem"
    assert auth.resolve_connection(use_saved=False).ca_bundle == "/env/ca.pem"
    save(host)
    assert auth.resolve_connection().ca_bundle == "/env/ca.pem"
    # A saved value that is not a string is ignored.
    save(host, ca_bundle=7)
    assert auth.resolve_connection().ca_bundle == "/env/ca.pem"
    # resolve() keeps its 2-tuple shape.
    assert auth.resolve() == (host, KEY)


def test_requests_environment_does_not_override_the_flag(tls_url, certs, monkeypatch, capsys):
    save(tls_url)
    # A valid but wrong CA, never a missing path.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certs.ca2))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(certs.ca2))
    assert run(["whoami", "--ca-bundle", str(certs.ca1)], capsys)[0] == 0


def test_requests_environment_still_applies_without_an_agenticml_bundle(
        tls_url, certs, monkeypatch, capsys):
    save(tls_url)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certs.ca1))
    assert run(["whoami"], capsys)[0] == 0


# --- login saves the bundle -----------------------------------------------------


def test_login_saves_absolute_bundle_and_later_commands_need_no_flag(
        tls_url, certs, tmp_path, monkeypatch, capsys):
    bundle = copy_bundle(certs.ca1, tmp_path / "lab-ca.pem")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(["login", "--host", tls_url, "--key", KEY, "--ca-bundle", "lab-ca.pem"], capsys)
    assert code == 0, err
    assert saved_entry(tls_url) == {"api_key": KEY, "ca_bundle": str(bundle)}
    monkeypatch.chdir(tmp_path / "config")  # a relative saved path would break here
    assert run(["whoami"], capsys)[0] == 0
    empty = tmp_path / "empty-runs"
    empty.mkdir()
    code, out, err = run(["sync", "--all", "--dir", str(empty)], capsys)
    assert code == 0, err
    assert "up to date" in out


def test_login_with_only_the_environment_variable_saves_no_bundle(
        tls_url, certs, monkeypatch, capsys):
    monkeypatch.setenv("AGENTICML_CA_BUNDLE", str(certs.ca1))
    code, _out, err = run(["login", "--host", tls_url, "--key", KEY], capsys)
    assert code == 0, err
    assert saved_entry(tls_url) == {"api_key": KEY}


def test_login_ignores_the_saved_bundle_and_drops_it(tls_url, certs, capsys):
    save(tls_url, ca_bundle=str(certs.ca1))
    code, _out, err = run(["login", "--host", tls_url, "--key", KEY], capsys)
    assert code == 1 and CERT_HINT in err
    code, _out, err = run(["login", "--host", tls_url, "--key", KEY, "--no-verify"], capsys)
    assert code == 0, err
    assert saved_entry(tls_url) == {"api_key": KEY}


def test_login_no_verify_still_validates_the_bundle(tmp_path, capsys):
    host = "https://lab.test"
    missing = tmp_path / "nope.pem"
    code, _out, err = run(["login", "--host", host, "--key", KEY, "--no-verify",
                           "--ca-bundle", str(missing)], capsys)
    assert code == 1
    assert f"CA bundle not found or unreadable: {missing}" in err
    assert not auth.credentials_path().exists()


# --- bad bundles ----------------------------------------------------------------


def test_bad_bundles_give_fixed_errors(tmp_path, certs, capsys):
    host = "https://lab.test"
    save(host)
    missing = tmp_path / "missing.pem"
    code, _out, err = run(["whoami", "--ca-bundle", str(missing)], capsys)
    assert code == 1
    assert err.strip() == f"agenticml whoami: CA bundle not found or unreadable: {missing}"

    directory = tmp_path / "a-dir"
    directory.mkdir()
    code, _out, err = run(["whoami", "--ca-bundle", str(directory)], capsys)
    assert code == 1 and f"CA bundle not found or unreadable: {directory}" in err

    loose = copy_bundle(certs.ca1, tmp_path / "loose.pem", 0o666)
    code, _out, err = run(["whoami", "--ca-bundle", str(loose)], capsys)
    assert code == 1
    assert f"CA bundle is writable by others: {loose}; chmod go-w" in err

    save(host, ca_bundle="relative/ca.pem")
    code, _out, err = run(["whoami"], capsys)
    assert code == 1 and "not absolute: relative/ca.pem" in err
    assert KEY not in err


def test_bundle_in_a_group_writable_directory_is_refused(tmp_path, certs, capsys):
    folder = tmp_path / "shared"
    folder.mkdir()
    bundle = copy_bundle(certs.ca1, folder / "ca.pem")
    folder.chmod(0o770)
    save("https://lab.test")
    code, _out, err = run(["whoami", "--ca-bundle", str(bundle)], capsys)
    assert code == 1
    assert f"CA bundle directory is writable by others: {folder}; chmod go-w {folder}" in err
    assert str(bundle) not in err


def test_symlinked_bundle_is_checked_at_its_target(tmp_path, certs, capsys):
    target = copy_bundle(certs.ca1, tmp_path / "real.pem")
    link = tmp_path / "link.pem"
    link.symlink_to(target)
    assert auth.load_ca_bundle(str(link)) == str(target.resolve())
    target.chmod(0o666)
    with pytest.raises(auth.CABundleError, match="writable by others"):
        auth.load_ca_bundle(str(link))


def test_offline_commands_never_load_the_bundle(tmp_path, capsys):
    host = "https://lab.test"
    save(host, ca_bundle=str(tmp_path / "deleted.pem"))
    code, out, err = run(["whoami", "--offline"], capsys)
    assert code == 0, err
    assert "unchecked (offline)" in out
    empty = tmp_path / "runs"
    empty.mkdir()
    assert run(["sync", "--all", "--dry-run", "--dir", str(empty)], capsys)[0] == 0
    code, _out, err = run(["sync", "--reset", "run-1", "--dir", str(empty)], capsys)
    assert "CA bundle" not in err  # the run does not exist, but the bundle was not loaded
    assert run(["logout"], capsys)[0] == 0


def test_http_hosts_never_load_a_bundle(plain_url, tmp_path, monkeypatch, capsys):
    save(plain_url)
    monkeypatch.setenv("AGENTICML_CA_BUNDLE", str(tmp_path / "missing.pem"))
    code, _out, err = run(["whoami"], capsys)
    assert code == 0, err


def test_follow_refuses_a_bad_bundle_at_start(tmp_path, capsys):
    save("https://lab.test", ca_bundle=str(tmp_path / "missing.pem"))
    runs = tmp_path / "runs"
    runs.mkdir()
    code, _out, err = run(["sync", "--follow", "--dir", str(runs)], capsys)
    assert code == 1
    assert "CA bundle not found or unreadable" in err


# --- saved insecure ---------------------------------------------------------------


def stub_whoami(monkeypatch):
    seen = []

    def fake(self):
        seen.append(self.base_url)
        return {"protocol": 2, "key": "box", "scope": "sync"}

    monkeypatch.setattr(Client, "whoami", fake)
    return seen


def test_saved_insecure_applies_to_that_exact_host_only(monkeypatch, capsys):
    seen = stub_whoami(monkeypatch)
    code, _out, err = run(["login", "--host", "http://10.0.0.5:8080", "--key", KEY,
                           "--no-verify", "--insecure"], capsys)
    assert code == 0, err
    assert saved_entry("http://10.0.0.5:8080") == {"api_key": KEY, "insecure": True}

    code, out, err = run(["whoami"], capsys)
    assert code == 0, err
    assert seen == ["http://10.0.0.5:8080"]
    warning = "warning: sending the API key over plain http to http://10.0.0.5:8080"
    assert err.count(warning) == 1
    assert KEY not in err

    # The environment host is compared after normalisation.
    monkeypatch.setenv("AGENTICML_HOST", "HTTP://10.0.0.5:8080/")
    assert run(["whoami"], capsys)[0] == 0
    monkeypatch.delenv("AGENTICML_HOST")

    for other in ("http://10.0.0.6:8080", "http://10.0.0.5:9090", "http://10.0.0.5"):
        code, _out, err = run(["whoami", "--offline", "--host", other], capsys)
        assert code == 1 and "insecure" in err
    assert len(seen) == 2


def test_saved_insecure_is_not_honoured_unless_true(capsys):
    for value in ("false", "true", 1, None):
        host = "http://10.0.0.5:8080"
        save(host, insecure=value)
        code, _out, err = run(["whoami", "--offline"], capsys)
        assert code == 1 and "insecure" in err, value


def test_login_does_not_read_the_saved_insecure(capsys):
    host = "http://10.0.0.5:8080"
    save(host, insecure=True)
    code, _out, err = run(["login", "--host", host, "--key", KEY, "--no-verify"], capsys)
    assert code == 1 and "insecure" in err
    assert saved_entry(host)["insecure"] is True  # untouched


def test_login_insecure_saves_nothing_for_https_or_local_hosts(capsys):
    for host in ("https://lab.test", "http://localhost:8080", "http://127.0.0.1:8080"):
        code, _out, err = run(["login", "--host", host, "--key", KEY, "--no-verify",
                               "--insecure"], capsys)
        assert code == 0, err
        assert "insecure" not in saved_entry(host)


def test_ipv6_literal_host_matches_in_normalised_form(monkeypatch, capsys):
    seen = stub_whoami(monkeypatch)
    run(["login", "--host", "http://[FD00::5]:8080", "--key", KEY, "--no-verify", "--insecure"], capsys)
    assert saved_entry("http://[fd00::5]:8080")["insecure"] is True
    assert run(["whoami"], capsys)[0] == 0
    assert seen == ["http://[fd00::5]:8080"]


def test_sync_to_a_saved_insecure_host_warns_once(monkeypatch, tmp_path, capsys):
    seen = stub_whoami(monkeypatch)
    host = "http://10.0.0.5:8080"
    save(host, insecure=True)
    empty = tmp_path / "runs"
    empty.mkdir()
    code, out, err = run(["sync", "--all", "--dir", str(empty)], capsys)
    assert code == 0, err
    assert err.count("sending the API key over plain http") == 1
    assert seen == [host]


def test_local_http_does_not_warn(plain_url, capsys):
    save(plain_url)
    code, _out, err = run(["whoami"], capsys)
    assert code == 0, err
    assert "plain http" not in err


# --- TLS failures -----------------------------------------------------------------


def test_https_url_on_a_plain_http_port_gives_the_handshake_hint(
        non_tls_port, no_sleep, tmp_path, capsys):
    host = non_tls_port
    save(host)
    code, _out, err = run(["whoami"], capsys)
    # Whether OpenSSL reports a protocol mismatch (no retry) or a reset (retried)
    # depends on the build, so only the outcome is asserted here; the retry rule is
    # covered by test_tls_permanent_*.
    assert code == 1
    assert HANDSHAKE_HINT in err and CERT_HINT not in err
    empty = tmp_path / "runs"
    empty.mkdir()
    code, _out, err = run(["sync", "--all", "--dir", str(empty)], capsys)
    assert code == 1
    assert f"agenticml sync: {HANDSHAKE_HINT}" in err


def test_tls_failure_classification():
    cert = ssl.SSLCertVerificationError("secret details")
    inner = urllib3.exceptions.SSLError(cert)
    outer = requests.exceptions.SSLError(urllib3.exceptions.MaxRetryError(None, "/x", reason=inner))
    assert tls_failure(outer) == "cert"
    assert tls_failure(requests.exceptions.SSLError("wrong version")) == "tls"
    assert tls_failure(requests.exceptions.ConnectionError("refused")) is None
    assert tls_failure(ValueError("x")) is None
    wrapped = requests.exceptions.ConnectionError("wrapped")
    wrapped.__cause__ = cert
    assert tls_failure(wrapped) == "cert"
    loop = ValueError("loop")
    loop.__cause__ = loop
    assert tls_failure(loop) is None
    assert "secret details" not in tls_hint(outer)
    assert tls_hint(ValueError("x")) is None


def test_follow_keeps_retrying_on_an_untrusted_certificate(
        tls_url, tmp_path, monkeypatch, follow_ctx):
    from agenticml import sync

    monkeypatch.setattr(sync, "BACKOFF_MAX", 0.08)
    monkeypatch.setattr(sync.random, "uniform", lambda a, b: b)
    save(tls_url)
    runs = tmp_path / "runs"
    runs.mkdir()
    chunks = capture_prints(monkeypatch)
    thread, box = spawn(lambda: main(["sync", "--follow", "--interval", "0.03", "--dir", str(runs)]))

    def lines():
        return [t for t in chunks["out"] if "server unreachable" in t]

    wait_until(lambda: len(lines()) >= 2, thread, box, follow_ctx, msg="no retry lines")
    stop_follow(follow_ctx)
    join_follow(thread, box, follow_ctx)
    for text in lines():
        assert text.startswith(f"server unreachable ({CERT_HINT}")
        assert "); retrying in " in text
        assert "SSLError" not in text
    assert thread.is_alive() is False


def test_redirect_text_has_no_location(monkeypatch):
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "https://elsewhere.invalid/secret-path")
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = HTTPServer(("127.0.0.1", 0), Redirect)
    with _serving(httpd):
        with Client(base_url=f"http://127.0.0.1:{httpd.server_address[1]}", api_key=KEY) as client:
            with pytest.raises(ValueError, match="use the final https:// URL as --host") as caught:
                client.whoami()
    assert "elsewhere" not in str(caught.value) and "secret-path" not in str(caught.value)


def start_follow(monkeypatch, runs, extra=()):
    from agenticml import sync

    monkeypatch.setattr(sync, "BACKOFF_MAX", 0.08)
    monkeypatch.setattr(sync.random, "uniform", lambda a, b: b)
    chunks = capture_prints(monkeypatch)
    argv = ["sync", "--follow", "--interval", "0.03", "--dir", str(runs), *extra]
    thread, box = spawn(lambda: main(argv))
    return chunks, thread, box


def test_follow_with_a_saved_bundle_completes_passes_without_unreachable_lines(
        tls_server, tls_url, certs, tmp_path, monkeypatch, follow_ctx):
    bundle = copy_bundle(certs.ca1, tmp_path / "lab-ca.pem")
    save(tls_url, ca_bundle=str(bundle))
    runs = tmp_path / "runs"
    runs.mkdir()
    chunks, thread, box = start_follow(monkeypatch, runs)
    wait_until(lambda: tls_server.hits, thread, box, follow_ctx, msg="no whoami reached the server")
    wait_until(lambda: any(t.startswith("following ") for t in chunks["out"]), thread, box,
               follow_ctx, msg="follow did not start")
    time.sleep(0.2)  # a few more passes
    stop_follow(follow_ctx)
    assert join_follow(thread, box, follow_ctx) == 0
    assert not [t for t in chunks["out"] if "server unreachable" in t]


def test_follow_survives_a_bundle_that_disappears_and_recovers(
        tls_server, tls_url, certs, tmp_path, monkeypatch, follow_ctx):
    bundle = copy_bundle(certs.ca1, tmp_path / "lab-ca.pem")
    save(tls_url, ca_bundle=str(bundle))
    runs = tmp_path / "runs"
    runs.mkdir()
    chunks, thread, box = start_follow(monkeypatch, runs)
    wait_until(lambda: tls_server.hits, thread, box, follow_ctx, msg="no whoami reached the server")
    # The identity is cached per client, so use a fresh failure path: remove the
    # file, then force requests through by dropping the cached identity.  The
    # follow thread polls every 0.03 s, so a request already in flight (or the
    # first whoami, whose hit the server logs before the client caches the
    # reply) can finish after a one-off reset and re-cache the identity, and
    # nothing would ever re-read the bundle.  So repeat the reset on every poll
    # until the retry line shows up.
    bundle.unlink()

    def lines():
        return [t for t in chunks["out"] if "server unreachable" in t]

    def reset_clients():
        for client in list(follow_ctx["clients"]):
            # A pooled keep-alive TLS connection would never re-read the deleted file.
            client.session.close()
            client.__dict__.pop("_sync_identity", None)

    def vanished_noticed():
        reset_clients()
        return lines()

    wait_until(vanished_noticed, thread, box, follow_ctx, msg="no retry line after the bundle vanished")
    assert thread.is_alive()
    for text in lines():
        assert text.startswith("server unreachable (CA bundle not found or unreadable: ")
        assert str(bundle) in text and "); retrying in " in text
        assert "OSError" not in text
    before = len(tls_server.hits)
    copy_bundle(certs.ca1, bundle)

    def recovered():
        # Drop any connection opened before the bundle came back, on every poll.
        for client in list(follow_ctx["clients"]):
            client.session.close()
        return len(tls_server.hits) > before

    wait_until(recovered, thread, box, follow_ctx,
               msg="follow did not recover after the bundle came back")
    stop_follow(follow_ctx)
    assert join_follow(thread, box, follow_ctx) == 0


def test_one_shot_commands_report_a_vanished_bundle(tls_url, certs, tmp_path, monkeypatch, capsys):
    bundle = copy_bundle(certs.ca1, tmp_path / "lab-ca.pem")
    save(tls_url)
    real = auth.load_ca_bundle

    def load_then_delete(path, **kwargs):
        final = real(path, **kwargs)
        os.unlink(final)  # the file disappears between validation and the request
        return final

    monkeypatch.setattr(auth, "load_ca_bundle", load_then_delete)
    code, _out, err = run(["whoami", "--ca-bundle", str(bundle)], capsys)
    assert code == 1
    assert f"agenticml whoami: CA bundle not found or unreadable: {bundle}" in err
    assert "OSError" not in err


def test_follow_to_a_saved_insecure_host_warns_once(monkeypatch, tmp_path, follow_ctx):
    seen = stub_whoami(monkeypatch)
    host = "http://10.0.0.5:8080"
    save(host, insecure=True)
    runs = tmp_path / "runs"
    runs.mkdir()
    chunks, thread, box = start_follow(monkeypatch, runs)
    wait_until(lambda: seen and any(t.startswith("following ") for t in chunks["out"]),
               thread, box, follow_ctx, msg="follow did not start")
    time.sleep(0.2)
    stop_follow(follow_ctx)
    assert join_follow(thread, box, follow_ctx) == 0
    warnings = [t for t in chunks["err"] if "sending the API key over plain http" in t]
    assert warnings == [f"warning: sending the API key over plain http to {host}\n"]
    assert not [t for t in chunks["err"] if "insecure" in t]


def test_verifying_login_over_plain_http_warns_once(monkeypatch, capsys):
    seen = stub_whoami(monkeypatch)
    host = "http://10.0.0.5:8080"
    code, _out, err = run(["login", "--host", host, "--key", KEY, "--insecure"], capsys)
    assert code == 0, err
    assert seen == [host]
    assert err.count(f"warning: sending the API key over plain http to {host}") == 1
    assert KEY not in err
    assert saved_entry(host) == {"api_key": KEY, "insecure": True}


# --- which TLS failures are retried -----------------------------------------------


def _ssl_error(reason, text=None):
    error = ssl.SSLError(1, text or f"[SSL: {reason}] {reason.lower().replace('_', ' ')} (_ssl.c:1)")
    error.reason = reason
    return error


def _wrapped(inner):
    """The way requests wraps an ssl error: SSLError > MaxRetryError(reason=SSLError) > ssl."""
    pool_error = urllib3.exceptions.SSLError(inner)
    retry = urllib3.exceptions.MaxRetryError(None, "/v2/whoami", reason=pool_error)
    return requests.exceptions.SSLError(retry)


PERMANENT_CHAINS = {
    "wrong version": lambda: _wrapped(_ssl_error("WRONG_VERSION_NUMBER")),
    "http request": lambda: _wrapped(_ssl_error("HTTP_REQUEST")),
    "unknown protocol": lambda: _wrapped(_ssl_error("UNKNOWN_PROTOCOL")),
    "wrong ssl version": lambda: _wrapped(_ssl_error("WRONG_SSL_VERSION")),
    "record layer": lambda: _wrapped(_ssl_error("RECORD_LAYER_FAILURE")),
    "packet length": lambda: _wrapped(_ssl_error("PACKET_LENGTH_TOO_LONG")),
    "message only": lambda: _wrapped(ssl.SSLError("[SSL] Record Layer Failure (_ssl.c:1)")),
    "urllib3 keeps the message": lambda: requests.exceptions.SSLError(
        urllib3.exceptions.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")),
    "certificate": lambda: _wrapped(ssl.SSLCertVerificationError(1, "verify failed")),
}
TRANSIENT_CHAINS = {
    "eof": lambda: _wrapped(ssl.SSLEOFError(8, "EOF occurred in violation of protocol")),
    "reset": lambda: requests.exceptions.SSLError(
        urllib3.exceptions.MaxRetryError(
            None, "/x", reason=urllib3.exceptions.ProtocolError(ConnectionResetError(104)))),
    "alert": lambda: _wrapped(_ssl_error("SSLV3_ALERT_HANDSHAKE_FAILURE")),
}


@pytest.mark.parametrize("name", sorted(PERMANENT_CHAINS))
def test_tls_permanent_failures(name):
    assert client_mod._tls_permanent(PERMANENT_CHAINS[name]()) is True


@pytest.mark.parametrize("name", sorted(TRANSIENT_CHAINS))
def test_tls_permanent_is_false_for_transient_failures(name):
    assert client_mod._tls_permanent(TRANSIENT_CHAINS[name]()) is False


def test_tls_permanent_is_false_for_a_plain_connection_reset():
    reset = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(None, "/x", reason=ConnectionResetError(104, "reset")))
    assert client_mod._tls_permanent(reset) is False


@pytest.mark.parametrize("name", sorted(PERMANENT_CHAINS))
def test_client_raises_at_once_for_permanent_tls_failures(name, no_sleep, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise PERMANENT_CHAINS[name]()

    with Client(base_url="https://lab.test", api_key=KEY, max_retries=3) as client:
        monkeypatch.setattr(client.session, "request", boom)
        with pytest.raises(requests.exceptions.SSLError):
            client.whoami()
    assert len(calls) == 1 and no_sleep == []


@pytest.mark.parametrize("name", sorted(TRANSIENT_CHAINS))
def test_client_retries_transient_tls_failures(name, no_sleep, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise TRANSIENT_CHAINS[name]()

    with Client(base_url="https://lab.test", api_key=KEY, max_retries=2) as client:
        monkeypatch.setattr(client.session, "request", boom)
        with pytest.raises(requests.exceptions.SSLError):
            client.whoami()
    assert len(calls) == 3 and len(no_sleep) == 2
