#!/usr/bin/env python3
"""Weekly check of the hosted agenticml service and the docs site's links.

Owner/CI tool, not shipped in the wheel; standard library only.

    python scripts/monitor_hosted.py [--host URL] [--docs-dir PATH]
                                     [--report FILE] [--no-links]

It requests ``<host>/healthz`` (three attempts, five seconds apart) and every
external link in the docs site's Markdown files. A broken health check or link
is a failure; GitHub rate limits (403/429) are warnings. The report lists URLs
and status codes only, so it is safe to post in a public issue.

Host: ``AGENTICML_HOST`` (default https://app.agenticml.xyz).
Exit code: 0 when nothing failed, 1 when a check failed, 2 for bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOST = "https://app.agenticml.xyz"
DEFAULT_DOCS_DIR = Path(__file__).resolve().parent.parent / "website" / "src" / "content" / "docs"
USER_AGENT = "agenticml-site-monitor"
HEALTH_TIMEOUT = 15
HEALTH_ATTEMPTS = 3
HEALTH_DELAY = 5
LINK_TIMEOUT = 20
LINK_ATTEMPTS = 3  # the first try and two retries
LINK_DELAY = 2

# Our own hosts: the health check covers the app, and the docs site's internal
# links are checked at build time. api.agenticml.xyz is an example host in the
# docs, not a service.
IGNORED_HOSTS = frozenset({
    "app.agenticml.xyz",
    "api.agenticml.xyz",
    "docs.agenticml.xyz",
    "agenticml.github.io",
    "localhost",
    "127.0.0.1",
})
# Names reserved by RFC 2606/6761 and ICANN (.internal) that never resolve on the public internet.
RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost", "internal"})
# URL prefixes the link check skips. Empty: raw files on `main` exist now that
# 3.0 is merged there. Add a prefix here when a link cannot resolve yet.
SKIPPED_PREFIXES = ()
GITHUB_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "api.github.com"})

URL_RE = re.compile(r"(?<![+\w])https?://[^\s)\]<>\"'`]+")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class Usage(Exception):
    """Bad arguments or environment; exit code 2."""


def _open(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


def _request(url, method):
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def _status(opener, url, method, timeout):
    """Return (status, body bytes) or raise URLError/OSError for transport errors."""
    try:
        with opener(_request(url, method), timeout=timeout) as response:
            body = response.read() if method == "GET" else b""
            return response.status, body
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, b""


def _describe(exc):
    reason = getattr(exc, "reason", None) or exc
    return f"{type(exc).__name__}: {reason}"


def check_health(host, opener=_open, sleep=time.sleep):
    """Return a list of failure lines; empty when /healthz answers {"ok": true}."""
    url = host.rstrip("/") + "/healthz"
    failure = ""
    for attempt in range(HEALTH_ATTEMPTS):
        if attempt:
            sleep(HEALTH_DELAY)
        try:
            status, body = _status(opener, url, "GET", HEALTH_TIMEOUT)
        except (OSError, ValueError) as exc:
            failure = f"healthz: {_describe(exc)}"
            continue
        if status != 200:
            failure = f"healthz: HTTP {status}"
            continue
        try:
            ok = json.loads(body) == {"ok": True}
        except ValueError:
            ok = False
        if ok:
            return []
        failure = "healthz: unexpected answer (expected {\"ok\": true})"
    return [failure]


def _is_example(host):
    """True for reserved placeholder names.

    Examples: x.example, example.com, aml.example.org, agenticml.lab.internal,
    app.example.test.
    """
    labels = host.split(".")
    return labels[-1] in RESERVED_TLDS or labels[-2:-1] == ["example"]


def external_links(docs_dir):
    """Return the sorted external URLs of the docs Markdown files worth checking."""
    found = set()
    for path in sorted(Path(docs_dir).rglob("*")):
        if path.suffix not in (".md", ".mdx") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;:*_!?")
            if text[match.end():match.end() + 1] in ("<", "{", "$"):
                continue  # a placeholder such as https://host/<slug>
            if url.startswith(SKIPPED_PREFIXES):
                continue
            host = (urlsplit(url).hostname or "").lower()
            if host in IGNORED_HOSTS or not HOST_RE.match(host) or _is_example(host):
                continue
            found.add(url)
    return sorted(found)


def check_link(url, opener=_open, sleep=time.sleep):
    """Return ('ok' | 'warn' | 'fail', reason) for one external URL."""
    host = (urlsplit(url).hostname or "").lower()
    reason = ""
    for attempt in range(LINK_ATTEMPTS):
        if attempt:
            sleep(LINK_DELAY)
        try:
            status, _ = _status(opener, url, "HEAD", LINK_TIMEOUT)
            if status in (405, 501):
                status, _ = _status(opener, url, "GET", LINK_TIMEOUT)
        except (OSError, ValueError) as exc:
            reason = _describe(exc)
            continue
        if status < 400:
            return "ok", f"HTTP {status}"
        if status == 429 or (status == 403 and host in GITHUB_HOSTS):
            return "warn", f"HTTP {status} (rate limited or blocked; not counted as a failure)"
        reason = f"HTTP {status}"
        if status < 500:
            break  # 404 and friends will not heal by retrying
    return "fail", reason


def run_checks(host, docs_dir, links=True, opener=_open, sleep=time.sleep):
    """Return (failures, warnings, checked link count)."""
    failures = list(check_health(host, opener, sleep))
    warnings = []
    urls = external_links(docs_dir) if links else []
    for url in urls:
        outcome, reason = check_link(url, opener, sleep)
        if outcome == "fail":
            failures.append(f"link {url}: {reason}")
        elif outcome == "warn":
            warnings.append(f"link {url}: {reason}")
    return failures, warnings, len(urls)


def render_report(host, failures, warnings, link_count, links=True):
    lines = ["# Hosted monitor report", "", f"Host: {host}", ""]
    lines.append("Result: " + ("failed" if failures else "ok"))
    lines.append(f"Docs links checked: {link_count if links else 'skipped'}")
    if failures:
        lines += ["", "## Failures", ""] + [f"- {line}" for line in failures]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {line}" for line in warnings]
    return "\n".join(lines) + "\n"


def _host_from(value):
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Usage(f"host must be an http(s) URL, got {value!r}")
    return value.rstrip("/")


def main(argv=None, opener=_open, sleep=time.sleep, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default=os.environ.get("AGENTICML_HOST") or DEFAULT_HOST)
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--report", help="also write the Markdown report to this file")
    parser.add_argument("--no-links", action="store_true", help="only check /healthz")
    args = parser.parse_args(argv)
    try:
        host = _host_from(args.host)
        if not args.no_links and not Path(args.docs_dir).is_dir():
            raise Usage(f"docs directory not found: {args.docs_dir}")
    except Usage as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failures, warnings, count = run_checks(host, args.docs_dir, not args.no_links, opener, sleep)
    report = render_report(host, failures, warnings, count, not args.no_links)
    out.write(report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
