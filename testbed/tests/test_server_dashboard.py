"""Browser coverage for the server dashboard login, last synced, and overflow."""
from datetime import datetime, timedelta
import json
import re
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from agenticml.server.app import make_server
from agenticml.server.db.repos import RunRepo
from agenticml.server.db.workspaces import InviteRepo, WorkspaceRepo
from agenticml.server.runs import adopt_directory, reconcile
from agenticml.server.workspaces import prepare_store
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
    adopt_directory(httpd.root, httpd.root / "runs" / "demo-run", httpd.run_index, remote={
        "owner": "gpu-box", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": layout.format_timestamp(),
    })
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


SETTINGS_LABELS = {
    "account": "Account", "workspace": "Workspace", "members": "Members",
    "keys": "API keys", "activity": "Activity",
}


def settings_card(page, section):
    return page.locator(f"#settings-{section}")


def open_settings(page, section="account"):
    """Open Settings from the top bar, then the section from its nav; return the card."""
    if "#/settings" not in page.url:
        page.get_by_role("link", name="Settings", exact=True).click()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
    if section != "account":
        link = page.get_by_role("navigation", name="Settings", exact=True).get_by_role(
            "link", name=SETTINGS_LABELS[section], exact=True)
        link.click()
        # The hashchange that selects the section arrives after the click returns.
        expect(link).to_have_attribute("aria-current", "true")
    card = settings_card(page, section)
    expect(card).to_be_visible()
    return card


def test_login_sync_key_refused_admin_shows_run(page, server_ui):
    page.allow_http_errors = True
    sign_in(page, server_ui, server_ui["sync"])
    expect(page.get_by_role("alert")).to_have_text("this is a sync key; sign in with an admin key")
    page.locator("#key").fill(server_ui["admin"])
    page.locator("#login").get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page).to_have_url(re.compile(r"/w/default/"))
    expect(page.get_by_role("link", name="synced-run", exact=True)).to_be_visible()
    expect(page.get_by_text("Signed in as admin · owner")).to_be_visible()


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
    reconcile(httpd.root, httpd.run_index)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "admin": admin}
    try:
        sign_in(page, server, admin)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.get_by_text("Signed in as admin · owner")).to_be_visible()
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


def test_login_layout_wide_and_narrow(page, server_ui):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_ui["url"])
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    expect(page.locator(".auth-brand")).to_be_visible()
    expect(page.locator(".auth-brand .auth-logo")).to_be_visible()
    expect(page.locator(".auth-compact-logo")).to_be_hidden()
    assert_no_overflow(page)
    page.set_viewport_size({"width": 375, "height": 812})
    expect(page.locator(".auth-brand")).to_be_hidden()
    expect(page.locator(".auth-compact-logo .auth-logo")).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("button", name="Sign in with an admin key", exact=True).click()
    expect(page.locator("#key")).to_be_visible()
    assert_no_overflow(page)


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


def test_non_default_workspace_dashboard_loads_its_own_runs(page, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    httpd.users.add("admin", role="admin")
    httpd.users.set_password("admin", "password1234")
    writer = RunWriter.create(
        httpd.root, run_id="default-run", meta={"project": "demo", "name": "default-only"})
    writer.finish("finished")
    writer.close()
    adopt_directory(httpd.root, httpd.root / "runs" / "default-run", httpd.run_index, remote={
        "owner": "admin", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": layout.format_timestamp(),
    })
    ws = WorkspaceRepo(httpd.db).create("team-a", name="Team A", owner="admin")
    root = prepare_store(httpd.data_dir, ws["id"])
    writer = RunWriter.create(
        root, run_id="alpha-run", meta={"project": "alpha", "name": "alpha-only"})
    writer.finish("finished")
    writer.close()
    index = RunRepo(httpd.db, ws["id"])
    adopt_directory(root, root / "runs" / "alpha-run", index, remote={
        "owner": "admin", "status": "finished", "fingerprint": "b" * 64,
        "last_sync_at": layout.format_timestamp(),
    })
    reconcile(root, index)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}"}
    try:
        sign_in_password(page, server, "admin", "password1234")
        expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
        page.goto(f"{server['url']}/w/team-a/")
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page).to_have_url(re.compile(r"/w/team-a/"))
        expect(page.get_by_role("link", name="alpha-only", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="default-only", exact=True)).to_have_count(0)
        page.goto(f"{server['url']}/w/default/")
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="default-only", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="alpha-only", exact=True)).to_have_count(0)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _seed_workspace_run(httpd, slug, name, run_id, run_name, fingerprint):
    ws = WorkspaceRepo(httpd.db).create(slug, name=name, owner="admin")
    root = prepare_store(httpd.data_dir, ws["id"])
    writer = RunWriter.create(
        root, run_id=run_id, meta={"project": slug, "name": run_name})
    writer.finish("finished")
    writer.close()
    index = RunRepo(httpd.db, ws["id"])
    adopt_directory(root, root / "runs" / run_id, index, remote={
        "owner": "admin", "status": "finished", "fingerprint": fingerprint,
        "last_sync_at": layout.format_timestamp(),
    })
    reconcile(root, index)
    return ws


def test_workspace_switcher_changes_runs_and_sets_cookie(page, context, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    httpd.users.add("admin", role="admin")
    httpd.users.set_password("admin", "password1234")
    writer = RunWriter.create(
        httpd.root, run_id="default-run", meta={"project": "demo", "name": "default-only"})
    writer.finish("finished")
    writer.close()
    adopt_directory(httpd.root, httpd.root / "runs" / "default-run", httpd.run_index, remote={
        "owner": "admin", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": layout.format_timestamp(),
    })
    _seed_workspace_run(httpd, "team-a", "Team A", "alpha-run", "alpha-only", "b" * 64)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}"}
    try:
        sign_in_password(page, server, "admin", "password1234")
        expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
        page.goto(f"{server['url']}/w/default/")
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.get_by_text("Signed in as admin · owner")).to_be_visible()
        expect(page.get_by_role("link", name="default-only", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="alpha-only", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Account", exact=True)).to_be_hidden()
        card = open_settings(page)
        workspaces = card.get_by_role("list", name="Workspaces", exact=True)
        current = workspaces.get_by_role("listitem").filter(has_text="Default")
        expect(current.get_by_text("current", exact=True)).to_be_visible()
        expect(current).to_contain_text("owner")
        expect(current.get_by_role("button")).to_have_count(0)
        other = workspaces.get_by_role("listitem").filter(has_text="Team A")
        expect(other.get_by_role("button", name="Switch to Team A")).to_be_visible()
        expect(other).to_contain_text("owner")
        # The Admin link is in the top bar only: not in the Settings nav or the card.
        expect(page.get_by_role("link", name="Admin", exact=True)).to_be_visible()
        expect(page.get_by_role("navigation", name="Settings", exact=True).locator(
            'a[href="/admin/"]')).to_have_count(0)
        expect(card.get_by_role("link", name="Admin", exact=True)).to_have_count(0)
        page.set_viewport_size({"width": 375, "height": 812})
        assert_no_overflow(page)
        other.get_by_role("button", name="Switch to Team A").click()
        expect(page).to_have_url(re.compile(r"/w/team-a/"))
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="alpha-only", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="default-only", exact=True)).to_have_count(0)
        cookies = {cookie["name"]: cookie for cookie in context.cookies()}
        assert cookies["aml_ws"]["value"] == "team-a"
        assert cookies["aml_ws"]["path"] == "/"
        assert cookies["aml_ws"]["sameSite"] == "Lax"
        assert cookies["aml_ws"].get("secure") in (False, None)
        assert_no_overflow(page)
        switched = open_settings(page).get_by_role("list", name="Workspaces", exact=True)
        expect(switched.get_by_role("listitem").filter(has_text="Team A").get_by_text(
            "current", exact=True)).to_be_visible()
        expect(switched.get_by_role("button", name="Switch to Default")).to_be_visible()
        assert_no_overflow(page)
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
        expect(page.locator(".auth-shell")).to_be_visible()
        expect(page.locator(".field-underline")).to_have_count(2)
        expect(page.locator(".auth-brand")).to_be_hidden()
        assert_no_overflow(page)
        page.locator("#password").fill("password1234")
        page.locator("#password2").fill("password1235")
        page.get_by_role("button", name="Save password", exact=True).click()
        expect(page.locator("#error")).to_have_text("Passwords do not match")
        assert_no_overflow(page)
        page.locator("#password2").fill("password1234")
        page.get_by_role("button", name="Save password", exact=True).click()
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        assert_no_overflow(page)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_invite_existing_account_page_joins_and_overflow(page, tmp_path, monkeypatch):
    # The account lives in the default workspace; the invite is into a second
    # one, so the page offers "Join workspace" and redeeming answers 409 (the
    # account keeps its password and signs in at /login).
    page.allow_http_errors = True
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    httpd.users.add("alice", role="admin")
    beta = WorkspaceRepo(httpd.db).create("beta", owner="alice")
    prepare_store(httpd.data_dir, beta["id"])
    # The invite exists first; the account is then created in the default workspace.
    token = InviteRepo(httpd.db, beta["id"]).create("carol", "member", "alice")
    httpd.users.add("carol", role="member")
    httpd.users.set_password("carol", "password1234")
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(f"{url}/invite?token={token}")
        expect(page.get_by_role("heading", name="You already have an account", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Join workspace", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Sign in", exact=True)).to_have_attribute("href", "/login")
        expect(page.locator("input[type=password]")).to_have_count(0)
        expect(page.locator(".auth-shell")).to_be_visible()
        expect(page).to_have_url(f"{url}/invite")
        assert token not in page.url
        assert "carol" not in page.content()
        assert_no_overflow(page)
        # A server error shows its message and re-enables the button.
        page.route("**/invite", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error": "boom"}')
            if route.request.method == "POST" else route.continue_())
        page.get_by_role("button", name="Join workspace", exact=True).click()
        expect(page.locator("#error")).to_have_text("boom")
        expect(page.get_by_role("button", name="Join workspace", exact=True)).to_be_enabled()
        assert_no_overflow(page)
        page.unroute("**/invite")
        # The real redeem answers 409 for an existing account and lands on /login.
        page.get_by_role("button", name="Join workspace", exact=True).click()
        expect(page).to_have_url(f"{url}/login")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_metric_picker_sort_filter_remembered_and_overflow(page, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    httpd = make_server(data, port=0)
    _, admin = httpd.keys.create("admin", "admin")
    # Non-monotonic loss so last/min/max differ: 0.2, 3.0, 0.4 → last 0.4, min 0.2, max 3.0.
    # gamma has no acc so it sorts last when acc is picked. Dotted config key lr.min
    # must still match the comparison grammar, not the metric token.
    fixtures = (
        ("low-last", [0.2, 3.0, 0.4], [0.5, 0.8, 0.85], 0.1),
        ("mid-last", [1.0, 0.5, 0.8], [0.9, 0.99, 0.95], 0.1),
        ("high-last", [2.0, 0.1, 1.5], None, 0.2),
    )
    for name, losses, accs, lr_min in fixtures:
        writer = RunWriter.create(
            httpd.root, run_id=name,
            meta={"project": "demo", "name": name, "task": "train",
                  "config": {"lr.min": lr_min}})
        points = []
        for step, loss in enumerate(losses):
            payload = {"loss": loss}
            if accs is not None:
                payload["acc"] = accs[step]
            points.append((step, payload))
        writer.write_scalars(points)
        writer.finish("finished")
        writer.close()
        adopt_directory(httpd.root, httpd.root / "runs" / name, httpd.run_index, remote={
            "owner": "admin", "status": "finished", "fingerprint": "a" * 64,
            "last_sync_at": layout.format_timestamp(),
        })
        httpd.run_index.rebuild_metrics(name, httpd.root)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "admin": admin}

    def names():
        return page.locator(".run-row .run-name").all_text_contents()

    def metric_cell(run_name):
        return page.locator(".run-row").filter(
            has=page.get_by_role("link", name=run_name, exact=True)
        ).locator(".run-metric")

    try:
        sign_in(page, server, admin)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        picker = page.get_by_test_id("runs-metric")
        stat = page.get_by_test_id("runs-metric-stat")
        expect(picker).to_be_visible()
        picker.select_option("loss")
        stat.select_option("last")
        expect(page.locator(".run-row.has-picked-metric")).to_have_count(3)
        expect(page.locator(".run-row").first).to_contain_text("low-last")
        assert names()[:3] == ["low-last", "mid-last", "high-last"]
        expect(metric_cell("low-last")).to_have_text("0.4")
        last_order = names()[:3]
        last_value = metric_cell("low-last").inner_text()
        stat.select_option("min")
        expect(page.locator(".run-row").first).to_contain_text("high-last")
        assert names()[:3] == ["high-last", "low-last", "mid-last"]
        expect(metric_cell("high-last")).to_have_text("0.1")
        expect(metric_cell("low-last")).to_have_text("0.2")
        assert names()[:3] != last_order
        expect(metric_cell("low-last")).not_to_have_text(last_value)
        stat.select_option("last")
        page.get_by_label("Filter runs").fill("loss.min<0.15")
        expect(page.locator(".run-row")).to_have_count(1)
        expect(page.get_by_role("link", name="high-last", exact=True)).to_be_visible()
        page.get_by_label("Filter runs").fill("")
        expect(page.locator(".run-row")).to_have_count(3)
        picker.select_option("acc")
        expect(page.locator(".run-row").first).to_contain_text("mid-last")
        assert names()[:3] == ["mid-last", "low-last", "high-last"]
        expect(metric_cell("mid-last")).to_have_text("0.95")
        expect(metric_cell("low-last")).to_have_text("0.85")
        expect(metric_cell("high-last")).to_have_text("—")
        picker.select_option("loss")
        page.get_by_label("Filter runs").fill("acc.max>0.9")
        expect(page.locator(".run-row")).to_have_count(1)
        expect(page.get_by_role("link", name="mid-last", exact=True)).to_be_visible()
        page.get_by_label("Filter runs").fill("lr.min=0.1")
        expect(page.locator(".run-row")).to_have_count(2)
        expect(page.get_by_role("link", name="low-last", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="mid-last", exact=True)).to_be_visible()
        page.get_by_label("Filter runs").fill("")
        expect(page.locator(".run-row")).to_have_count(3)
        held = []

        def hold_acc(route):
            metrics = parse_qs(urlparse(route.request.url).query).get("metrics", [""])[0]
            if metrics == "acc":
                held.append(route)
                return
            route.continue_()

        page.route("**/api/w/*/runs*", hold_acc)
        try:
            picker.select_option("acc")
            picker.select_option("loss")
            expect(picker).to_have_value("loss")
            expect(page.locator(".run-row").first).to_contain_text("low-last")
            assert names()[:3] == ["low-last", "mid-last", "high-last"]
            expect(metric_cell("low-last")).to_have_text("0.4")
            assert held, "older acc request should still be in flight"
            for pending in held:
                pending.continue_()
            expect(page.locator(".run-row").first).to_contain_text("low-last")
            assert names()[:3] == ["low-last", "mid-last", "high-last"]
            expect(metric_cell("low-last")).to_have_text("0.4")
        finally:
            page.unroute("**/api/w/*/runs*")
        page.set_viewport_size({"width": 375, "height": 812})
        assert_no_overflow(page)
        expect(page.locator(".run-picked-inline").first).to_be_visible()
        expect(page.locator(".run-row .run-metric").first).to_be_hidden()
        page.reload()
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.get_by_test_id("runs-metric")).to_have_value("loss")
        expect(page.get_by_test_id("runs-metric-stat")).to_have_value("last")
        expect(page.locator(".run-picked-inline").first).to_be_visible()
        expect(page.locator(".run-row .run-metric").first).to_be_hidden()
        assert_no_overflow(page)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.fixture
def metric_ui(tmp_path, monkeypatch):
    """A server with one run that logged ``loss``; ``status`` picks running or finished."""
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    started = []

    def start(status):
        httpd = make_server(tmp_path / "data", port=0)
        _, admin = httpd.keys.create("admin", "admin")
        writer = RunWriter.create(
            httpd.root, run_id="metric-run",
            meta={"project": "demo", "name": "metric-run", "task": "train"})
        writer.write_scalars([(0, {"loss": 1.0}), (1, {"loss": 0.5})])
        if status == "finished":
            writer.finish("finished")
        writer.close()
        adopt_directory(httpd.root, httpd.root / "runs" / "metric-run", httpd.run_index, remote={
            "owner": "admin", "status": status, "fingerprint": "a" * 64,
            "last_sync_at": layout.format_timestamp(),
        })
        httpd.run_index.rebuild_metrics("metric-run", httpd.root)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        started.append((httpd, thread))
        return {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "admin": admin}

    yield start
    for httpd, thread in started:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def stub_metrics(page, names, held=None):
    """Serve ``names`` (a mutable list) as the workspace metric list."""
    def handle(route):
        if held is not None:
            held.append(route)
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"metrics": list(names)}))

    page.route("**/api/w/*/metrics", handle)


def run_requests(page):
    seen = []
    page.on("request", lambda request: seen.append(request.url) if "/api/w/default/runs?" in request.url
            or request.url.endswith("/api/w/default/runs") else None)
    return seen


def freeze_clock(page):
    """Fake timers that only move when the test advances them."""
    page.clock.install()
    page.clock.pause_at(datetime.now() + timedelta(minutes=1))


def advance_until(page, check, step=5000, attempts=8):
    """Run the fake clock in steps, giving each response real time, until ``check`` passes."""
    for _ in range(attempts):
        page.clock.run_for(step)
        try:
            check()
            return
        except AssertionError:
            continue
    check()


def test_metric_picker_new_metric_appears_without_reload(page, metric_ui):
    server = metric_ui("running")
    names = []
    stub_metrics(page, names)
    page.clock.install()
    sign_in(page, server, server["admin"])
    expect(page.get_by_role("link", name="metric-run", exact=True)).to_be_visible()
    picker = page.get_by_test_id("runs-metric")
    expect(picker).to_be_hidden()
    names.append("loss")
    advance_until(page, lambda: expect(picker).to_be_visible(timeout=700))
    expect(picker.locator("option")).to_have_text(["None", "loss"])
    names.append("newmetric")
    advance_until(page, lambda: expect(picker.locator("option")).to_have_text(
        ["None", "loss", "newmetric"], timeout=700))
    picker.select_option("newmetric")
    expect(picker).to_have_value("newmetric")


def test_metric_picker_keeps_the_list_when_the_refresh_fails(page, metric_ui):
    server = metric_ui("running")
    names = ["loss"]
    stub_metrics(page, names)
    page.clock.install()
    sign_in(page, server, server["admin"])
    picker = page.get_by_test_id("runs-metric")
    expect(picker.locator("option")).to_have_text(["None", "loss"])
    page.unroute("**/api/w/*/metrics")
    page.allow_http_errors = True
    failures = []

    def fail(route):
        failures.append(route.request.url)
        route.fulfill(status=503, content_type="application/json", body='{"error": "down"}')

    page.route("**/api/w/*/metrics", fail)
    for _ in range(6):
        page.clock.run_for(5000)
        if failures:
            break
        page.wait_for_timeout(300)
    assert failures, "the metric list was never refreshed"
    expect(picker).to_be_visible()
    expect(picker.locator("option")).to_have_text(["None", "loss"])


def test_metric_picker_saved_choice_returns_when_metric_appears(page, metric_ui):
    server = metric_ui("running")
    names = ["loss"]
    stub_metrics(page, names)
    page.add_init_script(
        "if (!localStorage.getItem('agenticml-runs-metric:default')) "
        "localStorage.setItem('agenticml-runs-metric:default', "
        "JSON.stringify({name: 'late', stat: 'min'}));")
    requests = run_requests(page)
    page.clock.install()
    sign_in(page, server, server["admin"])
    picker = page.get_by_test_id("runs-metric")
    stat = page.get_by_test_id("runs-metric-stat")
    expect(picker.locator("option")).to_have_text(["None", "loss"])
    expect(picker).to_have_value("")
    expect(stat).to_have_value("min")
    # The first /runs already carries the saved metric.
    assert "metrics=late" in requests[0], requests
    # Changing only the statistic must not overwrite the saved name.
    stat.select_option("max")
    saved = page.evaluate("JSON.parse(localStorage.getItem('agenticml-runs-metric:default'))")
    assert saved == {"name": "late", "stat": "max"}
    names.append("late")
    advance_until(page, lambda: expect(picker).to_have_value("late", timeout=700))
    expect(stat).to_have_value("max")


def test_metric_names_are_requested_in_parallel_with_the_first_runs(page, metric_ui):
    server = metric_ui("finished")
    held = []
    stub_metrics(page, ["loss"], held=held)
    sign_in(page, server, server["admin"])
    # The runs render while the metric list is still pending.
    expect(page.get_by_role("link", name="metric-run", exact=True)).to_be_visible()
    assert len(held) == 1, "the metric list should have been requested exactly once"
    picker = page.get_by_test_id("runs-metric")
    expect(picker).to_be_hidden()
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps({"metrics": ["loss"]}))
    expect(picker).to_be_visible()
    expect(picker.locator("option")).to_have_text(["None", "loss"])


def test_filter_debounce_is_250ms(page, metric_ui):
    server = metric_ui("finished")
    sign_in(page, server, server["admin"])
    expect(page.get_by_role("link", name="metric-run", exact=True)).to_be_visible()
    expect(page.get_by_test_id("runs-metric")).to_be_visible()
    freeze_clock(page)
    requests = run_requests(page)
    search = page.get_by_label("Filter runs")
    search.fill("loss.min<1")
    page.clock.fast_forward(200)
    search.fill("loss.min<2")
    page.clock.fast_forward(200)
    page.wait_for_timeout(300)
    assert requests == []
    with page.expect_response(lambda response: "metrics=loss" in response.url):
        page.clock.fast_forward(60)
    page.wait_for_timeout(200)
    assert len(requests) == 1
    # "loss" is known now: a further edit redraws without a request.
    search.fill("loss.min<3")
    page.clock.fast_forward(600)
    page.wait_for_timeout(300)
    assert len(requests) == 1
    expect(page.get_by_role("link", name="metric-run", exact=True)).to_be_visible()


def test_422_falls_back_to_the_picked_metric_and_remembers_the_rejection(page, metric_ui):
    server = metric_ui("finished")
    page.allow_http_errors = True
    sign_in(page, server, server["admin"])
    picker = page.get_by_test_id("runs-metric")
    expect(picker).to_be_visible()
    picker.select_option("loss")
    expect(page.locator(".run-row.has-picked-metric")).to_have_count(1)

    def stub(route):
        if "bogus" in route.request.url:
            route.fulfill(status=422, content_type="text/plain", body="invalid metrics")
        else:
            route.continue_()

    page.route("**/api/w/*/runs*", stub)
    requests = run_requests(page)
    freeze_clock(page)
    search = page.get_by_label("Filter runs")
    search.fill("bogus.min<1")
    with page.expect_request(lambda request: "bogus" in request.url):
        page.clock.fast_forward(300)
    page.wait_for_timeout(500)
    # One request with the unknown name, one retry with only the picked metric.
    assert len(requests) == 2 and "bogus" in requests[0] and "bogus" not in requests[1], requests
    assert requests[1].endswith("metrics=loss")
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(picker).to_have_value("loss")
    # The rejected name is not requested again on this page.
    search.fill("bogus.min<2")
    page.clock.fast_forward(600)
    page.wait_for_timeout(300)
    assert len(requests) == 2
