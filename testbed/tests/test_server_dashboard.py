"""Browser coverage for the server dashboard login, last synced, and overflow."""
import json
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.store import RunWriter, layout

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

pytestmark = [pytest.mark.browser, pytest.mark.ui]


@pytest.fixture(scope="module")
def browser():
    manager = sync_playwright().start()
    try:
        launched = manager.chromium.launch()
    except Exception as exc:
        manager.stop()
        pytest.skip(f"Chromium cannot launch: {exc}")
    try:
        yield launched
    finally:
        launched.close()
        manager.stop()


@pytest.fixture
def context(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US", color_scheme="light")
    ctx.route("**/favicon.ico", lambda route: route.fulfill(status=204, body=""))
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    tab = context.new_page()
    problems = []
    tab.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
    tab.on("console", lambda message: problems.append(f"console: {message.text}") if message.type == "error" else None)
    tab.on("response", lambda response: problems.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
    tab.on("requestfailed", lambda request: problems.append(f"failed: {request.url}: {request.failure}") if "ERR_ABORTED" not in (request.failure or "") else None)
    tab.allow_http_errors = False
    yield tab
    if tab.allow_http_errors:
        problems = [problem for problem in problems if not problem.startswith(("HTTP 4", "HTTP 503", "console: Failed to load resource:"))]
    assert not problems, "browser errors:\n" + "\n".join(problems)
    tab.close()


@pytest.fixture
def server_ui(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    httpd = make_server(data, port=0)
    _, admin = httpd.keys.create("admin", "admin")
    _, sync = httpd.keys.create("gpu-box", "sync")
    writer = RunWriter.create(httpd.root, run_id="demo-run", meta={"project": "demo", "name": "synced-run"})
    writer.finish("finished")
    writer.close()
    remote = {
        "owner": "gpu-box", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": layout.format_timestamp(), "sizes": {},
    }
    (httpd.root / "runs" / "demo-run" / "remote.json").write_text(json.dumps(remote))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "admin": admin, "sync": sync, "run_id": "demo-run"}
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def assert_no_overflow(page):
    overflow = page.evaluate(
        "Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth, "
        "document.body.scrollWidth - document.body.clientWidth)"
    )
    assert overflow <= 0, f"horizontal overflow by {overflow}px"


def sign_in(page, server, key):
    page.goto(server["url"])
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    page.get_by_role("button", name="Sign in with an admin key", exact=True).click()
    page.locator("#key").fill(key)
    page.locator("#login").get_by_role("button", name="Sign in", exact=True).click()


def sign_in_password(page, server, user, password):
    page.goto(server["url"])
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    page.locator("#user").fill(user)
    page.locator("#password").fill(password)
    page.locator("#password-login").get_by_role("button", name="Sign in", exact=True).click()


def test_login_sync_key_refused_admin_shows_run(page, server_ui):
    page.allow_http_errors = True
    sign_in(page, server_ui, server_ui["sync"])
    expect(page.get_by_role("alert")).to_have_text("this is a sync key; sign in with an admin key")
    page.locator("#key").fill(server_ui["admin"])
    page.locator("#login").get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="synced-run", exact=True)).to_be_visible()
    expect(page.get_by_text("Signed in as admin")).to_be_visible()


def test_last_synced_and_sign_out(page, server_ui):
    sign_in(page, server_ui, server_ui["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    page.get_by_role("link", name="synced-run", exact=True).click()
    expect(page.get_by_role("heading", name="synced-run", exact=True)).to_be_visible()
    expect(page.locator(".poll-status")).to_contain_text("Last synced")
    expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    page.goto(server_ui["url"])
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()


def _lineage_pair(root):
    ids = []
    for index, name in enumerate(("parent-run", "child-run")):
        writer = RunWriter.create(root, meta={
            "project": "demo", "name": name, "task": "asr", "task_source": "entry",
        })
        writer.finish("finished")
        writer.close()
        path = root / "runs" / writer.run_id / "meta.json"
        meta = json.loads(path.read_text())
        parent = ids[0] if ids else None
        meta["parent_run_id"] = parent
        meta["task"] = "asr"
        meta["task_source"] = "entry"
        meta["lineage"] = {
            "version": 1,
            "commit": f"{index + 1:040x}",
            "tree": ("cd" if index == 0 else "ab") * 20,
            "parent_commit": None if index == 0 else f"{index:040x}",
            "parent_run_id": parent,
            "parent_inference": None if index == 0 else "explicit",
            "project_key": "demo-0123abcd",
            "repo_root": "/tmp/demo",
            "base_commit": "abc1234",
            "base_branch": "main",
            "base_dirty": False,
            "error": None,
        }
        path.write_text(json.dumps(meta))
        ids.append(writer.run_id)
    return ids


def test_lineage_inspector_shows_server_code_diff_message(page, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    httpd = make_server(data, port=0)
    _, admin = httpd.keys.create("admin", "admin")
    ids = _lineage_pair(httpd.root)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "admin": admin}
    try:
        sign_in(page, server, admin)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.get_by_text("Signed in as admin")).to_be_visible()
        page.get_by_role("button", name="Open lineage of asr", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        page.locator(f'.lv-node[data-run-id="{ids[1]}"] .lv-box').click()
        inspector = dialog.get_by_role("complementary", name="Selected run")
        expect(page.locator(".lv-node.is-focused")).to_have_attribute("data-run-id", ids[1])
        code = inspector.locator(".kv-list > div").filter(has=page.locator("dt", has_text="code")).locator("dd")
        expect(code).to_have_text("code diff not available on this server")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_no_overflow_login_list_and_detail(page, server_ui):
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(server_ui["url"])
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("button", name="Sign in with an admin key", exact=True).click()
    page.locator("#key").fill(server_ui["admin"])
    page.locator("#login").get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("link", name="synced-run", exact=True).click()
    expect(page.get_by_role("heading", name="synced-run", exact=True)).to_be_visible()
    assert_no_overflow(page)


def test_password_sign_in(page, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    httpd.users.add("alice", role="member")
    httpd.users.set_password("alice", "password1234")
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}"}
    try:
        sign_in_password(page, server, "alice", "password1234")
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_invite_page_sets_password_and_overflow(page, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    httpd.users.add("bob", role="member")
    token = httpd.users.create_invite("bob")
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(f"{url}/invite?token={token}")
        expect(page.get_by_role("heading", name="Choose a password", exact=True)).to_be_visible()
        expect(page).to_have_url(f"{url}/invite")
        assert token not in page.url
        assert_no_overflow(page)
        page.locator("#password").fill("password1234")
        page.locator("#password2").fill("password1234")
        page.get_by_role("button", name="Save password", exact=True).click()
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        assert_no_overflow(page)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
