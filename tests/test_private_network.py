"""Package 34.2: the private-network kit, end to end through a TLS proxy.

A small TLS-terminating TCP proxy with a private CA stands in for Caddy or
nginx. The real server sits behind it with a public URL on the lab name, and
the real client and dashboard requests go through it.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenticml import cli
from agenticml.server.app import make_server
from test_server_web import cookie_parts
from test_sync import identical, writer_at
from tls_helpers import make_test_certs

KIT = Path(__file__).resolve().parent.parent / "deploy" / "private-network"
LAB_NAME = "agenticml.lab.internal"


class TlsProxy:
    """Terminate TLS on a loopback port and copy bytes to a plain-http upstream."""

    def __init__(self, certs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certs.cert), str(certs.key))
        self.context = context
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.upstream_port = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []
        self.threads = []
        self.acceptor = None

    def start(self, upstream_port):
        self.upstream_port = upstream_port
        self.acceptor = threading.Thread(target=self._accept, daemon=True)
        self.acceptor.start()

    def _track(self, sock):
        with self.lock:
            self.sockets.append(sock)
        return sock

    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self.lock:
            self.threads.append(thread)
        thread.start()

    def _accept(self):
        while not self.stopping.is_set():
            try:
                raw, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._track(raw)
            self._spawn(self._serve, raw)

    def _serve(self, raw):
        try:
            raw.settimeout(10)
            client = self._track(self.context.wrap_socket(raw, server_side=True))
            upstream = self._track(socket.create_connection(("127.0.0.1", self.upstream_port), timeout=10))
        except (OSError, ssl.SSLError):
            self._close(raw)
            return
        self._spawn(self._pump, upstream, client)
        self._pump(client, upstream)

    @staticmethod
    def _close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _pump(self, source, sink):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            self._close(source)
            self._close(sink)

    def stop(self):
        self.stopping.set()
        self._close(self.listener)
        if self.acceptor is not None:
            self.acceptor.join(timeout=5)
        with self.lock:
            sockets, threads = list(self.sockets), list(self.threads)
        for sock in sockets:
            self._close(sock)
        for thread in threads:
            thread.join(timeout=5)
        assert self.acceptor is None or not self.acceptor.is_alive()
        assert not any(thread.is_alive() for thread in threads)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY", "AGENTICML_CA_BUNDLE",
                 "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    cert_dir.chmod(0o755)  # the client refuses a CA bundle in a group-writable directory
    certs = make_test_certs(cert_dir, extra_sans=(f"DNS:{LAB_NAME}",))
    proxy = TlsProxy(certs)  # bound first, to learn the port for the public URL
    httpd = thread = None
    try:
        httpd = make_server(tmp_path / "data", host="127.0.0.1", port=0,
                            public_url=f"https://{LAB_NAME}:{proxy.port}",
                            trusted_proxies=("127.0.0.1/32",))
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        proxy.start(httpd.server_address[1])
        yield SimpleNamespace(httpd=httpd, port=proxy.port, certs=certs, host=f"{LAB_NAME}:{proxy.port}")
    finally:
        proxy.stop()
        if httpd is not None:
            if thread is not None:
                httpd.shutdown()
                thread.join(timeout=5)
            httpd.server_close()
            assert thread is None or not thread.is_alive()


def test_sync_through_tls_proxy(lab, tmp_path, capsys, monkeypatch):
    _, key = lab.httpd.keys.create("box", scope="sync")
    writer = writer_at(tmp_path, rich=True)
    url = f"https://localhost:{lab.port}"
    assert cli.main(["login", "--host", url, "--key", key, "--ca-bundle", str(lab.certs.ca1)]) == 0
    assert cli.main(["sync", "--all", "--dir", str(writer.root)]) == 0
    assert "synced" in capsys.readouterr().out
    identical((lab.httpd, SimpleNamespace(base_url=url, api_key=key)), writer)
    # The saved bundle is per host: without it the proxy's certificate is untrusted.
    monkeypatch.setenv("AGENTICML_API_KEY", key)
    assert cli.main(["whoami", "--host", f"https://127.0.0.1:{lab.port}"]) == 1
    assert "TLS certificate check failed" in capsys.readouterr().err


def dashboard_login(lab, origin, *, host=None):
    context = ssl.create_default_context(cafile=str(lab.certs.ca1))
    conn = http.client.HTTPSConnection("localhost", lab.port, context=context, timeout=10)
    try:
        conn.request("POST", "/login", body=json.dumps({"user": "alice", "password": PASSWORD}).encode(), headers={
            "Host": host or lab.host,
            "Origin": origin,
            "X-AgenticML-Request": "1",
            "Content-Type": "application/json",
        })
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


PASSWORD = "a-long-enough-password-1"


def test_dashboard_login_through_tls_proxy(lab):
    lab.httpd.users.add("alice", role="admin")
    lab.httpd.users.set_password("alice", PASSWORD)

    status, headers, body = dashboard_login(lab, f"https://{lab.host}")
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    cookies = [cookie_parts(value) for name, value in headers if name.lower() == "set-cookie"]
    session = [(value, flags) for name, value, flags in cookies if name == "__Host-aml_session"]
    assert len(session) == 1 and session[0][0]
    assert session[0][1].get("secure") is True

    for origin in (f"http://{lab.host}", "https://evil.example"):
        status, _, body = dashboard_login(lab, origin)
        assert status == 403, origin
        assert json.loads(body) == {"error": "cross-site request refused"}


def uncommented(path):
    lines = (KIT / path).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_nginx_conf_rules():
    text = uncommented("nginx.conf")
    for needle in ("client_max_body_size 4g;", "proxy_request_buffering off;", "proxy_http_version 1.1;",
                   "proxy_set_header Host $http_host;", 'proxy_set_header X-Forwarded-Email "";',
                   'proxy_set_header X-Amzn-Oidc-Identity "";', 'proxy_set_header Tailscale-User-Login "";',
                   "proxy_pass http://127.0.0.1:8080;"):
        assert needle in text, needle
    for name in ("proxy_read_timeout", "proxy_send_timeout"):
        match = re.search(rf"^\s*{name}\s+(\d+)([smhd]?);", text, re.M)
        assert match, name
        seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
        assert seconds >= 3 * 3600, name
    formats = re.findall(r"^\s*log_format\s+agenticml\s+(.+?);\s*$", text, re.M)
    assert len(formats) == 1
    assert re.search(r"\$(request|request_uri|args|query_string|is_args)(?![A-Za-z0-9_])", formats[0]) is None
    assert "$uri" in formats[0]
    assert re.search(r"^\s*access_log\s+\S+\s+agenticml;", text, re.M)


def test_caddyfile_rules():
    text = uncommented("Caddyfile")
    assert "reverse_proxy 127.0.0.1:8080" in text
    for name in ("X-Forwarded-Email", "X-Amzn-Oidc-Identity", "Tailscale-User-Login"):
        assert f"header_up -{name}" in text
    assert "tls internal" in text
    assert "trusted_proxies" not in text


def test_env_example_rules():
    text = uncommented("agenticml-server.env.example")
    active = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    assert active["AGENTICML_SERVER_PUBLIC_URL"].startswith("https://")
    proxies = active["AGENTICML_SERVER_TRUSTED_PROXIES"].split(",")
    assert proxies
    for entry in proxies:
        address, _, prefix = entry.strip().partition("/")
        assert prefix in ("32", "128"), entry
    assert "AGENTICML_SERVER_KEY" not in active
    assert not [name for name in active if "IDENTITY" in name]


def test_sync_unit_lines_are_comments():
    text = (KIT.parent / "systemd" / "agenticml-sync.service").read_text(encoding="utf-8")
    assert "# Environment=AGENTICML_CA_BUNDLE=/etc/ssl/certs/lab-ca.pem" in text
    assert "# Environment=NO_PROXY=" in text
    assert not re.search(r"^Environment=", text, re.M)
