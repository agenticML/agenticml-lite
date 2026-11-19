"""Throwaway certificates for TLS tests, generated with the ``openssl`` CLI.

Two CAs and one server certificate signed by the first. Package 34.2 reuses
``make_test_certs`` with an extra SAN name for its TLS proxy test.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _openssl_version() -> tuple[int, int, int] | None:
    exe = shutil.which("openssl")
    if exe is None:
        return None
    try:
        text = subprocess.run([exe, "version"], capture_output=True, text=True,
                              timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"OpenSSL (\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def _run(*args) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True, timeout=60)


def _make_ca(directory: Path, name: str) -> tuple[Path, Path]:
    cert, key = directory / f"{name}.pem", directory / f"{name}.key"
    _run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", f"/CN=agenticml-test-{name}",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    return cert, key


def make_test_certs(directory, extra_sans=()):
    """Write two CAs and a server certificate (signed by CA 1) into ``directory``.

    The server certificate covers ``DNS:localhost``, ``IP:127.0.0.1`` and every
    entry of ``extra_sans`` (for example ``"DNS:agenticml.lab.internal"``), with
    ``serverAuth``. Returns a namespace of paths: ``ca1``, ``ca2`` (PEM files),
    ``cert`` and ``key`` (the server certificate and key).

    Skips the calling test when ``openssl`` is missing or older than 1.1.1.
    """
    version = _openssl_version()
    if version is None:
        pytest.skip("openssl CLI not available")
    if version < (1, 1, 1):
        pytest.skip("openssl older than 1.1.1")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ca1, ca1_key = _make_ca(directory, "ca1")
    ca2, _ca2_key = _make_ca(directory, "ca2")
    key, csr, cert = directory / "server.key", directory / "server.csr", directory / "server.pem"
    _run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", "/CN=localhost")
    sans = ",".join(["DNS:localhost", "IP:127.0.0.1", *extra_sans])
    extfile = directory / "server.ext"
    extfile.write_text(
        f"subjectAltName={sans}\n"
        "extendedKeyUsage=serverAuth\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n",
        encoding="utf-8",
    )
    _run("x509", "-req", "-in", str(csr), "-CA", str(ca1), "-CAkey", str(ca1_key),
         "-CAcreateserial", "-out", str(cert), "-days", "2", "-extfile", str(extfile))
    for path in (ca1, ca2, cert):
        path.chmod(0o644)  # not group-writable whatever the umask; the client checks this
    return SimpleNamespace(ca1=ca1, ca2=ca2, cert=cert, key=key)
