"""scripts/monitor_hosted.py (weekly hosted check) and .github/workflows/monitor.yml."""
from __future__ import annotations

import importlib.util
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor_hosted", ROOT / "scripts" / "monitor_hosted.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

HOST = "https://app.example.test"


class Response:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answers each (method, url) from a table of lists of answers.

    An answer is an int status, bytes (200 with that body), a (status, body)
    pair, or an exception instance (raised). The last answer repeats.
    """

    def __init__(self, table):
        self.table = {k: list(v) if isinstance(v, list) else [v] for k, v in table.items()}
        self.calls = []

    def __call__(self, request, timeout):
        key = (request.get_method(), request.full_url)
        self.calls.append(key)
        answers = self.table[key]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return Response(200, answer)
        if isinstance(answer, tuple):
            status, body = answer
        else:
            status, body = answer, b""
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b""))
        return Response(status, body)


def no_sleep(_seconds):
    sleeps.append(_seconds)


sleeps: list = []


@pytest.fixture(autouse=True)
def clear_sleeps():
    sleeps.clear()


OK_BODY = json.dumps({"ok": True}).encode()


def health(*answers):
    return FakeOpener({("GET", HOST + "/healthz"): list(answers)})


# ------------------------------------------------------------------ health

def test_health_passes_on_ok_true():
    opener = health(OK_BODY)
    assert monitor.check_health(HOST, opener, no_sleep) == []
    assert opener.calls == [("GET", HOST + "/healthz")]
    assert sleeps == []


@pytest.mark.parametrize("answer, text", [
    (json.dumps({"ok": False}).encode(), "unexpected answer"),
    (b"not json", "unexpected answer"),
    ((200, json.dumps({"ok": True, "extra": 1}).encode()), "unexpected answer"),
    (500, "HTTP 500"),
    (404, "HTTP 404"),
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "Connection refused"),
    (TimeoutError("timed out"), "TimeoutError"),
])
def test_health_failures_are_bounded_by_three_attempts(answer, text):
    opener = health(answer)
    (failure,) = monitor.check_health(HOST, opener, no_sleep)
    assert failure.startswith("healthz: ") and text in failure
    assert len(opener.calls) == 3
    assert sleeps == [monitor.HEALTH_DELAY] * 2


def test_health_recovers_within_the_retries():
    opener = health(503, urllib.error.URLError("reset"), OK_BODY)
    assert monitor.check_health(HOST, opener, no_sleep) == []
    assert len(opener.calls) == 3


def test_health_url_joins_without_a_double_slash():
    opener = health(OK_BODY)
    monitor.check_health(HOST + "/", opener, no_sleep)
    assert opener.calls == [("GET", HOST + "/healthz")]


# ------------------------------------------------------------ external links

def write_docs(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


MAIN_RAW = "https://raw.githubusercontent.com/agenticML/agenticml-lite/main/deploy/systemd/a.service"


def test_external_links_skips_the_configured_prefixes(tmp_path, monkeypatch):
    docs = write_docs(tmp_path, {"a.md": f"Raw: {MAIN_RAW} and https://other.dev/path\n"})
    assert monitor.external_links(docs) == ["https://other.dev/path", MAIN_RAW]
    monkeypatch.setattr(monitor, "SKIPPED_PREFIXES", ("https://raw.githubusercontent.com/agenticML/agenticml-lite/main/",))
    assert monitor.external_links(docs) == ["https://other.dev/path"]


def test_external_links_keeps_only_real_deduplicated_urls(tmp_path):
    docs = write_docs(tmp_path, {
        "a.md": (
            "See [x](https://real.sample.org/page) and https://github.com/agenticML/agenticml-lite/issues.\n"
            "Again https://github.com/agenticML/agenticml-lite/issues, and **https://other.dev/path**.\n"
            "Placeholders: https://<hostname>/x https://app.agenticml.xyz/w/<slug>/runs https://aml.example.com\n"
            "https://example.com/y http://127.0.0.1:6006 http://localhost:8080/ https://x.example/z\n"
            "`pip install git+https://github.com/agenticML/agenticml-lite`\n"
            "Ours: https://app.agenticml.xyz/login https://api.agenticml.xyz https://docs.agenticml.xyz/a "
            "https://agenticml.github.io/agenticml-lite/hosted/\n"
            "Elided https://... and https://<machine>\n"
            "Raw: https://raw.githubusercontent.com/agenticML/agenticml-lite/main/deploy/systemd/a.service "
            "https://raw.githubusercontent.com/agenticML/agenticml-lite/v3.0.0/deploy/systemd/a.service\n"
        ),
        "sub/b.mdx": "Link https://other.dev/path and https://third.dev/.\n",
        "sub/c.txt": "https://ignored.dev/not-markdown\n",
    })
    assert monitor.external_links(docs) == [
        "https://github.com/agenticML/agenticml-lite/issues",
        "https://other.dev/path",
        MAIN_RAW,
        "https://raw.githubusercontent.com/agenticML/agenticml-lite/v3.0.0/deploy/systemd/a.service",
        "https://real.sample.org/page",
        "https://third.dev/",
    ]


def test_external_links_of_the_real_docs_site_are_not_placeholders():
    urls = monitor.external_links(monitor.DEFAULT_DOCS_DIR)
    assert urls
    for url in urls:
        host = monitor.urlsplit(url).hostname
        assert "." in host and host not in monitor.IGNORED_HOSTS, url
        assert not url.startswith(monitor.SKIPPED_PREFIXES), url


# ---------------------------------------------------------------- one link

URL = "https://other.dev/page"


def link_opener(head, get=None):
    table = {("HEAD", URL): head}
    if get is not None:
        table[("GET", URL)] = get
    return FakeOpener(table)


def test_link_ok_and_redirect_status():
    # urllib follows redirects itself; the final status is what the check sees.
    assert monitor.check_link(URL, link_opener(200), no_sleep) == ("ok", "HTTP 200")
    assert monitor.check_link(URL, link_opener(204), no_sleep)[0] == "ok"


@pytest.mark.parametrize("status", [404, 410])
def test_link_client_errors_fail_without_retries(status):
    opener = link_opener(status)
    assert monitor.check_link(URL, opener, no_sleep) == ("fail", f"HTTP {status}")
    assert opener.calls == [("HEAD", URL)]


def test_link_server_errors_retry_then_fail_or_recover():
    opener = link_opener(503)
    assert monitor.check_link(URL, opener, no_sleep) == ("fail", "HTTP 503")
    assert len(opener.calls) == monitor.LINK_ATTEMPTS
    assert monitor.check_link(URL, link_opener([502, 200]), no_sleep)[0] == "ok"


def test_link_connection_errors_fail_after_retries():
    opener = link_opener(urllib.error.URLError("certificate verify failed"))
    outcome, reason = monitor.check_link(URL, opener, no_sleep)
    assert outcome == "fail" and "certificate verify failed" in reason
    assert len(opener.calls) == monitor.LINK_ATTEMPTS


@pytest.mark.parametrize("status", [405, 501])
def test_link_head_not_allowed_falls_back_to_get(status):
    opener = link_opener(status, 200)
    assert monitor.check_link(URL, opener, no_sleep)[0] == "ok"
    assert opener.calls == [("HEAD", URL), ("GET", URL)]
    assert monitor.check_link(URL, link_opener(status, 404), no_sleep)[0] == "fail"


def test_rate_limits_are_warnings():
    assert monitor.check_link(URL, link_opener(429), no_sleep)[0] == "warn"
    github = "https://github.com/agenticML/agenticml-lite/issues"
    for status in (403, 429):
        outcome, _ = monitor.check_link(github, FakeOpener({("HEAD", github): status}), no_sleep)
        assert outcome == "warn"
    # A 403 elsewhere is a real failure.
    assert monitor.check_link(URL, link_opener(403), no_sleep)[0] == "fail"


# -------------------------------------------------------------------- main

def docs_with(tmp_path, *urls):
    return write_docs(tmp_path / "docs", {"page.md": "\n".join(urls) + "\n"})


def main_opener(health_answers, links):
    table = {("GET", HOST + "/healthz"): health_answers}
    table.update({("HEAD", url): answer for url, answer in links.items()})
    return FakeOpener(table)


def run_main(args, opener):
    out = io.StringIO()
    code = monitor.main(args, opener=opener, sleep=no_sleep, out=out)
    return code, out.getvalue()


def test_main_exits_zero_when_everything_passes(tmp_path):
    docs = docs_with(tmp_path, URL)
    report = tmp_path / "report.md"
    code, out = run_main(["--host", HOST, "--docs-dir", str(docs), "--report", str(report)],
                         main_opener(OK_BODY, {URL: 200}))
    assert code == 0
    assert "Result: ok" in out and "Docs links checked: 1" in out
    assert report.read_text() == out


def test_main_exits_one_on_a_failing_health_check_and_reports_it(tmp_path):
    docs = docs_with(tmp_path, URL)
    report = tmp_path / "report.md"
    code, out = run_main(["--host", HOST, "--docs-dir", str(docs), "--report", str(report)],
                         main_opener(500, {URL: 200}))
    assert code == 1
    assert "healthz: HTTP 500" in report.read_text()


def test_main_report_names_a_broken_link_and_warnings(tmp_path):
    broken, limited = "https://broken.dev/gone", "https://limited.dev/page"
    docs = docs_with(tmp_path, broken, limited)
    report = tmp_path / "report.md"
    code, _ = run_main(["--host", HOST, "--docs-dir", str(docs), "--report", str(report)],
                       main_opener(OK_BODY, {broken: 404, limited: 429}))
    text = report.read_text()
    assert code == 1
    assert f"- link {broken}: HTTP 404" in text
    assert "## Warnings" in text and limited in text.split("## Warnings")[1]
    assert limited not in text.split("## Warnings")[0]


def test_main_warnings_alone_do_not_fail(tmp_path):
    docs = docs_with(tmp_path, URL)
    code, out = run_main(["--host", HOST, "--docs-dir", str(docs)], main_opener(OK_BODY, {URL: 429}))
    assert code == 0 and "## Warnings" in out


def test_main_no_links_only_checks_health(tmp_path):
    opener = main_opener(OK_BODY, {})
    code, out = run_main(["--host", HOST, "--no-links", "--docs-dir", str(tmp_path / "missing")], opener)
    assert code == 0 and opener.calls == [("GET", HOST + "/healthz")]
    assert "skipped" in out


def test_main_usage_errors_exit_two(tmp_path, capsys):
    opener = main_opener(OK_BODY, {})
    code, _ = run_main(["--host", HOST, "--docs-dir", str(tmp_path / "missing")], opener)
    assert code == 2 and "docs directory not found" in capsys.readouterr().err
    code, _ = run_main(["--host", "ftp://x", "--docs-dir", str(tmp_path)], opener)
    assert code == 2
    assert opener.calls == []
    with pytest.raises(SystemExit) as exc:
        monitor.main(["--bogus"], opener=opener, sleep=no_sleep)
    assert exc.value.code == 2


def test_main_uses_the_environment_host(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", HOST)
    opener = main_opener(OK_BODY, {})
    code, out = run_main(["--no-links"], opener)
    assert code == 0 and f"Host: {HOST}" in out


# ----------------------------------------------------------------- workflow

def load_monitor_workflow():
    return yaml.safe_load((ROOT / ".github/workflows/monitor.yml").read_text())


def test_monitor_workflow_triggers_and_guards():
    workflow = load_monitor_workflow()
    triggers = workflow[True] if True in workflow else workflow["on"]  # PyYAML reads `on` as True
    assert len(triggers["schedule"]) == 1 and triggers["schedule"][0]["cron"] == "17 6 * * 1"
    assert "workflow_dispatch" in triggers
    assert workflow["concurrency"]["group"] == "hosted-monitor"
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert "agenticML/agenticml-lite" in job["if"] and "github.repository" in job["if"]
    assert job["timeout-minutes"] <= 30


def test_monitor_workflow_runs_use_env_only_and_report_through_gh():
    workflow = load_monitor_workflow()
    (job,) = workflow["jobs"].values()
    assert job["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert job["env"]["GH_REPO"] == "${{ github.repository }}"
    runs = [s["run"] for s in job["steps"] if "run" in s]
    assert runs
    for run in runs:
        assert "${{" not in run, run
    text = "\n".join(runs)
    assert re.search(r"gh issue create .*--label hosted", text)
    assert "--title 'Hosted monitor failed'" in text
    assert "gh issue comment" in text and "gh issue close" in text
    assert "secrets." not in text
    check = next(s for s in job["steps"] if s.get("id") == "check")
    assert check["continue-on-error"] is True
    assert check["run"].startswith("python scripts/monitor_hosted.py")
    conditions = [s.get("if") for s in job["steps"]]
    assert "steps.check.outcome == 'failure'" in conditions
    assert "steps.check.outcome == 'success'" in conditions
    assert all("uses" not in s or re.search(r"@[0-9a-f]{40}$", s["uses"]) for s in job["steps"])
