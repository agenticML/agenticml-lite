"""Browser coverage for first-key setup and dashboard key management."""
import json
import os
import re
import shlex
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.store import RunWriter

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import (
    assert_no_overflow, browser, context, page, server_ui, sign_in,
)

pytestmark = [pytest.mark.browser, pytest.mark.ui]


@pytest.fixture
def fresh_server(tmp_path, monkeypatch, request):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0, **getattr(request, "param", {}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        host = f"[{host}]" if ":" in host else host
        yield {"url": f"http://{host}:{port}", "httpd": httpd}
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def open_keys(page, server):
    sign_in(page, server, server["admin"])
    page.get_by_role("link", name="Keys", exact=True).click()
    expect(page.get_by_role("heading", name="Keys", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()


def create_key(page, name, scope="sync"):
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    dialog.get_by_label("Name", exact=True).fill(name)
    dialog.get_by_label("Scope", exact=True).select_option(scope)
    dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.locator(".key-value")).to_have_text(re.compile(r"aml_[A-Za-z0-9_-]+"))
    return dialog, dialog.locator(".key-value").inner_text()


def key_row(page, name):
    return page.get_by_role("row").filter(has=page.locator('td[data-label="Name"]').filter(
        has_text=re.compile(f"^{re.escape(name)}$")))


def assert_key_cleared(page, value):
    expect(page.locator(".key-dialog")).to_have_count(0)
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert value not in page.content()
    assert value not in page.url
    assert page.evaluate(
        "value => !JSON.stringify(localStorage).includes(value) && "
        "!JSON.stringify(sessionStorage).includes(value)", value,
    )


def assert_dialog_fits(page, dialog):
    box = dialog.bounding_box()
    viewport = page.viewport_size
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= viewport["width"]
    assert box["y"] + box["height"] <= viewport["height"]
    assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")


def test_setup_creates_admin_and_opens_dashboard(page, fresh_server):
    page.set_viewport_size({"width": 375, "height": 812})
    token = fresh_server["httpd"].web.setup_token
    page.goto(f"{fresh_server['url']}/setup?token={token}")
    expect(page.get_by_role("heading", name="Create admin account", exact=True)).to_be_visible()
    expect(page).to_have_url(f"{fresh_server['url']}/setup")
    assert token not in page.url
    assert_no_overflow(page)
    page.locator("#password").fill("password1234")
    page.locator("#password2").fill("password1234")
    page.get_by_role("button", name="Create admin account", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert_no_overflow(page)


@pytest.mark.parametrize("body", [
    {}, {"ok": 1},
], ids=["missing-ok", "non-bool-ok"])
def test_setup_bad_success_body_keeps_form_and_token_for_retry(page, fresh_server, body):
    token = fresh_server["httpd"].web.setup_token
    page.goto(f"{fresh_server['url']}/setup?token={token}")
    page.locator("#password").fill("password1234")
    page.locator("#password2").fill("password1234")
    page.route("**/setup", lambda route: route.fulfill(status=201, json=body))
    page.get_by_role("button", name="Create admin account", exact=True).click()
    expect(page.locator("#error")).to_have_text("Setup failed (201)")
    expect(page.locator("#setup")).to_be_visible()
    assert fresh_server["httpd"].web.setup_token == token
    page.unroute("**/setup")
    page.get_by_role("button", name="Create admin account", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


@pytest.mark.parametrize("status,body", [
    (503, ""), (400, "null"), (400, '{"error": {"x": 1}}'),
], ids=["empty-body", "null-body", "non-string-error"])
def test_setup_bad_error_body_keeps_form_and_token_for_retry(page, fresh_server, status, body):
    page.allow_http_errors = True
    token = fresh_server["httpd"].web.setup_token
    page.goto(f"{fresh_server['url']}/setup?token={token}")
    page.get_by_label("User name", exact=True).fill("first-admin")
    page.locator("#password").fill("password1234")
    page.locator("#password2").fill("password1234")
    page.route("**/setup", lambda route: route.fulfill(
        status=status, content_type="application/json", body=body))
    page.get_by_role("button", name="Create admin account", exact=True).click()
    expect(page.locator("#error")).to_have_text(f"Setup failed ({status})")
    expect(page.locator("#setup")).to_be_visible()
    expect(page.get_by_label("User name", exact=True)).to_have_value("first-admin")
    expect(page.get_by_role("button", name="Create admin account", exact=True)).to_be_enabled()
    assert fresh_server["httpd"].web.setup_token == token
    page.unroute("**/setup")
    page.get_by_role("button", name="Create admin account", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


@pytest.mark.parametrize("fresh_server", [
    {"public_url": "http://ml.example.com/$(x);echo'quoted'"}, {"host": "::1"},
], ids=["public-url-metacharacters", "ipv6-bind"], indirect=True)
def test_login_command_quotes_shell_arguments(page, fresh_server):
    httpd = fresh_server["httpd"]
    _, admin = httpd.keys.create("admin", "admin")
    open_keys(page, {**fresh_server, "admin": admin})
    dialog, value = create_key(page, "worker")
    command = dialog.locator(".key-command").inner_text()
    host = httpd.public_url or fresh_server["url"]
    quoted_host = "'" + host.replace("'", "'\\''") + "'"
    assert command == f"agenticml login --host {quoted_host} --key '{value}'"
    assert shlex.split(command) == ["agenticml", "login", "--host", host, "--key", value]


@pytest.mark.parametrize("event,persisted", [("pagehide", False), ("pageshow", True)])
def test_page_navigation_clears_key_dialog(page, server_ui, event, persisted):
    open_keys(page, server_ui)
    _, value = create_key(page, "worker")
    assert page.evaluate("""({event, persisted}) => {
        const codes = [...document.querySelectorAll('.key-value, .key-command')];
        window.dispatchEvent(new PageTransitionEvent(event, {persisted}));
        return codes.length === 2 && codes.every(node => node.textContent === '');
    }""", {"event": event, "persisted": persisted})
    expect(page.locator(".key-value")).to_have_count(0)
    expect(page.locator(".key-command")).to_have_count(0)
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert_key_cleared(page, value)


def test_copy_status_waits_for_latest_copy(page, context, server_ui):
    open_keys(page, server_ui)
    dialog, _ = create_key(page, "worker")
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.clock.install()
    dialog.get_by_role("button", name="Copy key", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text("Copied")
    page.clock.fast_forward(1000)
    dialog.get_by_role("button", name="Copy command", exact=True).click()
    expect(dialog.get_by_role("button", name="Copied", exact=True)).to_have_count(2)
    page.clock.fast_forward(1100)
    expect(dialog.get_by_role("status")).to_have_text("Copied")
    expect(dialog.get_by_role("button", name="Copied", exact=True)).to_have_count(2)
    page.clock.fast_forward(1000)
    expect(dialog.get_by_role("status")).to_have_text("")
    expect(dialog.get_by_role("button", name="Copy key", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Copy command", exact=True)).to_be_visible()


def test_create_copy_close_and_revoke_sync_key(page, context, server_ui):
    open_keys(page, server_ui)
    dialog, value = create_key(page, "worker")
    expect(dialog.locator(".key-command")).to_have_text(
        f"agenticml login --host '{server_ui['url']}' --key '{value}'")
    expect(dialog.get_by_text("This key is shown once and cannot be recovered.", exact=True)).to_be_visible()
    expect(dialog.get_by_text(
        "No --public-url is set, so this uses the address you opened the dashboard at.", exact=True,
    )).to_be_visible()
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    copy_key = dialog.get_by_role("button", name="Copy key", exact=True)
    copy_key.click()
    expect(dialog.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    expect(dialog.get_by_role("status")).to_have_text("Copied")
    expect(dialog.get_by_role("status")).to_have_attribute("aria-live", "polite")
    assert page.evaluate("navigator.clipboard.readText()") == value
    expect(dialog.get_by_role("status")).to_have_text("")
    dialog.get_by_role("button", name="Copy command", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == f"agenticml login --host '{server_ui['url']}' --key '{value}'"
    expect(copy_key).to_be_visible()
    dialog.get_by_role("button", name="Close", exact=True).click()
    row = key_row(page, "worker")
    expect(row).to_be_visible()
    expect(row.get_by_role("cell", name="sync", exact=True)).to_be_visible()
    expect(row.get_by_role("cell", name="never", exact=True)).to_be_visible()
    expect(row.get_by_role("cell", name="admin", exact=True)).to_be_visible()
    expect(row.get_by_role("cell", name="0", exact=True)).to_be_visible()
    expect(row.get_by_role("cell", name=f"{value[:8]}…", exact=True)).to_be_visible()
    assert_key_cleared(page, value)
    row.get_by_role("button", name="Revoke", exact=True).click()
    confirm = page.get_by_role("dialog", name="Revoke worker?", exact=True)
    expect(confirm).to_contain_text("Machines using it stop syncing on their next request, and its dashboard sessions end.")
    confirm.get_by_role("button", name="Revoke", exact=True).click()
    expect(row).to_have_count(0)
    expect(page.get_by_role("button", name="New key", exact=True)).to_be_focused()


@pytest.mark.parametrize("clipboard", ["undefined", "reject"])
def test_copy_fallback_selects_text_and_announces_feedback(page, server_ui, clipboard):
    open_keys(page, server_ui)
    dialog, value = create_key(page, "worker")
    if clipboard == "undefined":
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: undefined})")
    else:
        page.evaluate("() => { navigator.clipboard.writeText = () => Promise.reject(new Error('unavailable')); }")
    for label, text in (("Copy key", value), ("Copy command", f"agenticml login --host '{server_ui['url']}' --key '{value}'")):
        dialog.get_by_role("button", name=label, exact=True).click()
        page.wait_for_function("text => window.getSelection().toString() === text", arg=text)
        feedback = dialog.get_by_role("button", name="Press Ctrl+C to copy", exact=True)
        expect(feedback).to_be_visible()
        expect(dialog.get_by_role("status")).to_have_text("Press Ctrl+C to copy")
        expect(dialog.get_by_role("status")).to_have_attribute("aria-live", "polite")
        expect(dialog.get_by_role("button", name=label, exact=True)).to_be_visible()
        expect(dialog.get_by_role("status")).to_have_text("")
    dialog.evaluate("node => node.close()")
    assert_key_cleared(page, value)
    assert page.evaluate("window.getSelection().rangeCount") == 0


def test_closing_key_dialog_preserves_outside_selection(page, server_ui):
    open_keys(page, server_ui)
    dialog, value = create_key(page, "worker")
    page.get_by_role("heading", name="Keys", exact=True).evaluate("""node => {
        const range = document.createRange();
        range.selectNodeContents(node);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
    }""")
    assert page.evaluate("window.getSelection().rangeCount") == 1
    assert page.evaluate("window.getSelection().getRangeAt(0).toString()") == "Keys"
    dialog.evaluate("node => node.close()")
    assert_key_cleared(page, value)
    assert page.evaluate("window.getSelection().rangeCount") == 1
    assert page.evaluate("window.getSelection().toString()") == "Keys"


def test_revoke_own_admin_signs_out(page, server_ui):
    open_keys(page, server_ui)
    dialog, value = create_key(page, "backup-admin", "admin")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(key_row(page, "backup-admin").locator('td[data-label="Scope"]')).to_have_text("admin")
    assert_key_cleared(page, value)
    key_row(page, "admin").get_by_role("button", name="Revoke", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Revoke", exact=True).click()
    expect(page).to_have_url(f"{server_ui['url']}/login")
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()


def test_closing_during_create_refreshes_after_the_write(page, server_ui):
    open_keys(page, server_ui)
    pending = []

    def hold_create(route):
        if route.request.method == "POST":
            pending.append(route)
        else:
            route.continue_()

    page.route("**/api/server/keys", hold_create)
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    dialog.get_by_label("Name", exact=True).fill("delayed-worker")
    with page.expect_request(lambda request: request.method == "POST"):
        dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.get_by_role("button", name="Create", exact=True)).to_be_disabled()
    with page.expect_response(lambda response: response.url.endswith("/api/server/keys")) as listing:
        dialog.get_by_role("button", name="Close", exact=True).click()
    listing.value.finished()
    expect(key_row(page, "delayed-worker")).to_have_count(0)
    with page.expect_response(lambda response: response.request.method == "POST") as created:
        pending[0].continue_()
    value = created.value.json()["key"]
    expect(key_row(page, "delayed-worker")).to_be_visible()
    assert_key_cleared(page, value)


def test_last_admin_error_is_inline(page, server_ui):
    page.allow_http_errors = True
    open_keys(page, server_ui)
    key_row(page, "admin").get_by_role("button", name="Revoke", exact=True).click()
    dialog = page.get_by_role("dialog", name="Revoke admin?", exact=True)
    dialog.get_by_role("button", name="Revoke", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text(
        "last admin key; create another admin key first, or use the CLI")
    expect(dialog.get_by_role("button", name="Revoke", exact=True)).to_be_enabled()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(key_row(page, "admin")).to_be_visible()


@pytest.mark.parametrize("width", [375, 481, 599, 600, 760])
def test_keys_no_overflow_and_dialog_focus_on_narrow_screens(page, server_ui, width):
    page.set_viewport_size({"width": width, "height": 812})
    open_keys(page, server_ui)
    assert_no_overflow(page)
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    dialog.get_by_label("Name", exact=True).fill("worker-" + "x" * 57)
    expect(dialog.get_by_label("Scope", exact=True)).to_have_value("sync")
    assert_no_overflow(page)
    assert_dialog_fits(page, dialog)
    dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.locator(".key-value")).to_be_visible()
    value = dialog.locator(".key-value").inner_text()
    expect(dialog.locator(".key-command")).to_have_text(
        f"agenticml login --host '{server_ui['url']}' --key '{value}'")
    assert_no_overflow(page)
    assert_dialog_fits(page, dialog)
    expect(dialog.get_by_role("button", name="Copy key", exact=True)).to_be_focused()
    dialog.get_by_role("button", name="Copy command", exact=True).focus()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Copy command", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(key_row(page, "worker-" + "x" * 57)).to_be_visible()
    assert_key_cleared(page, value)
    assert_no_overflow(page)
    table = page.get_by_role("table", name="Keys", exact=True)
    expect(table).to_have_attribute("role", "table")
    expect(table.locator("thead")).to_have_attribute("role", "rowgroup")
    expect(table.locator("tbody")).to_have_attribute("role", "rowgroup")
    expect(table.locator("tr").first).to_have_attribute("role", "row")
    expect(table.locator("th").first).to_have_attribute("role", "columnheader")
    expect(table.locator("td").first).to_have_attribute("role", "cell")
    if width < 600:
        expect(table.locator("thead")).to_have_css("position", "absolute")
        expect(table.locator("thead")).to_have_css("clip-path", "inset(50%)")
        assert table.locator("thead").bounding_box()["width"] <= 1
        expect(table).to_have_css("display", "block")
        expect(table.locator("td").first).to_have_css("display", "grid")
        assert page.locator(".table-scroll").evaluate("node => node.scrollWidth <= node.clientWidth")
    else:
        expect(table).to_have_css("display", "table")
        expect(table.locator("thead")).to_have_css("display", "table-header-group")
        expect(table.locator("thead")).to_have_css("position", "static")
        expect(table.locator("thead")).to_have_css("clip-path", "none")
        expect(table.locator("td").first).to_have_css("display", "table-cell")
    key_row(page, "worker-" + "x" * 57).get_by_role("button", name="Revoke", exact=True).click()
    assert_no_overflow(page)
    assert_dialog_fits(page, page.get_by_role("dialog"))
    page.get_by_role("dialog").get_by_role("button", name="Cancel", exact=True).click()


@pytest.fixture
def metrics_server(fresh_server):
    httpd = fresh_server["httpd"]
    _, admin = httpd.keys.create("admin", "admin")
    writer = RunWriter.create(httpd.root, run_id="metrics-run", meta={
        "project": "demo", "name": "metrics-run",
    })
    writer.write_scalars([(step, {f"metric/{index}": step + index for index in range(12)})
                          for step in range(3)])
    writer.finish("finished")
    writer.close()
    return {**fresh_server, "admin": admin, "run_id": writer.run_id}


def assert_topbar_contains_children(page):
    geometry = page.locator(".topbar").evaluate("""node => {
        const bar = node.getBoundingClientRect();
        return {
            top: bar.top, bottom: bar.bottom, left: bar.left, right: bar.right,
            scrollWidth: node.scrollWidth, clientWidth: node.clientWidth,
            children: [...node.querySelectorAll('*')].filter(child => {
                const rect = child.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 &&
                    getComputedStyle(child).visibility !== 'hidden';
            }).map(child => {
                const rect = child.getBoundingClientRect();
                return {name: child.id || child.className,
                    top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right};
            }),
        };
    }""")
    assert geometry["children"]
    for child in geometry["children"]:
        assert child["top"] >= geometry["top"] - 1, child
        assert child["bottom"] <= geometry["bottom"] + 1, child
        assert child["left"] >= geometry["left"] - 1, child
        assert child["right"] <= geometry["right"] + 1, child
    assert geometry["scrollWidth"] <= geometry["clientWidth"], geometry


@pytest.mark.parametrize("width", [375, 481, 600, 760, 800, 1280])
def test_server_topbar_contains_children_and_clears_sticky_toolbar(page, metrics_server, width):
    page.set_viewport_size({"width": width, "height": 812})
    open_keys(page, metrics_server)
    assert_topbar_contains_children(page)
    assert_no_overflow(page)
    page.evaluate("route => { location.hash = '#/run/' + route; }", metrics_server["run_id"])
    expect(page.get_by_role("heading", name="metrics-run", exact=True)).to_be_visible()
    toolbar = page.locator(".metrics-panel > .toolbar")
    expect(toolbar).to_be_visible()
    expect(page.locator(".chart-card")).to_have_count(12)
    assert_topbar_contains_children(page)
    assert_no_overflow(page)
    if width <= 760:
        expect(toolbar).to_have_css("position", "static")
        return
    expect(toolbar).to_have_css("position", "sticky")
    page.evaluate("""() => {
        const toolbar = document.querySelector('.metrics-panel > .toolbar');
        window.scrollTo(0, toolbar.getBoundingClientRect().top + window.scrollY + 200);
    }""")
    page.wait_for_function("""() => {
        const toolbar = document.querySelector('.metrics-panel > .toolbar');
        return Math.abs(toolbar.getBoundingClientRect().top - parseFloat(getComputedStyle(toolbar).top)) <= 1;
    }""")
    before = toolbar.bounding_box()["y"]
    page.evaluate("window.scrollBy(0, 100)")
    page.wait_for_function("""top => Math.abs(document.querySelector('.metrics-panel > .toolbar')
        .getBoundingClientRect().top - top) <= 1""", arg=before)
    assert toolbar.bounding_box()["y"] >= page.locator(".topbar").evaluate(
        "node => node.getBoundingClientRect().bottom")
    assert_topbar_contains_children(page)
    assert_no_overflow(page)


def test_create_errors_and_name_validation(page, server_ui):
    page.allow_http_errors = True
    open_keys(page, server_ui)
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog")
    name = dialog.get_by_label("Name", exact=True)
    name.fill("bad name")
    dialog.get_by_role("button", name="Create", exact=True).click()
    assert not name.evaluate("node => node.checkValidity()")
    for reserved in ("env", ".", ".."):
        name.fill(reserved)
        dialog.get_by_role("button", name="Create", exact=True).click()
        assert not name.evaluate("node => node.checkValidity()")
    name.fill("admin")
    dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("key name already exists")
    page.route("**/api/server/keys", lambda route: route.fulfill(
        status=400, json={"error": "invalid key scope"}))
    name.fill("worker")
    dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("invalid key scope")
    page.unroute("**/api/server/keys")
    dialog.get_by_role("button", name="Close", exact=True).click()


@pytest.mark.parametrize(("method", "status", "body"), [
    ("POST", 201, ""),
    ("POST", 201, "<html>Created</html>"),
    ("POST", 201, "{}"),
    ("POST", 201, "[]"),
    ("POST", 201, "null"),
    ("POST", 201, '"created"'),
    ("POST", 201, '{"key": "aml_test"}'),
    ("POST", 201, '{"host": "http://localhost"}'),
    ("POST", 201, '{"key": 1, "host": "http://localhost"}'),
    ("POST", 201, '{"key": "aml_test", "host": 1}'),
    ("POST", 400, '{"error": {"x": 1}}'),
    ("POST", 503, ""),
    ("POST", 411, "<html>Length required</html>"),
    ("POST", 413, "<html>Body too large</html>"),
    ("POST", 415, "<html>Unsupported media type</html>"),
    ("DELETE", 503, ""),
    ("GET", 200, ""),
    ("GET", 200, "<html>OK</html>"),
    ("GET", 200, "{}"),
    ("GET", 200, "[]"),
    ("GET", 200, '{"keys": {}}'),
    ("GET", 200, '{"keys": [null]}'),
    ("GET", 200, '{"keys": [[]]}'),
    ("GET", 200, '{"keys": ["key"]}'),
    ("GET", 200, '{"keys": [{}]}'),
    ("GET", 200, '{"keys": [{"name": 1, "scope": "sync", "prefix": "aml_test", "created_at": "now"}]}'),
    ("GET", 200, '{"keys": [{"name": "worker", "scope": null, "prefix": "aml_test", "created_at": "now"}]}'),
    ("GET", 200, '{"keys": [{"name": "worker", "scope": "sync", "prefix": [], "created_at": "now"}]}'),
    ("GET", 200, '{"keys": [{"name": "worker", "scope": "sync", "prefix": "aml_test", "created_at": 1}]}'),
    *[("GET", 200, json.dumps({"keys": [{
        "name": "worker", "scope": "sync", "prefix": "aml_test", "created_at": "now",
        "created_by": "admin", "last_used_at": None, "runs": 0, "revocable": True,
        field: value,
    }]})) for field, value in [
        ("created_by", None), ("created_by", {}), ("last_used_at", 1),
        ("last_used_at", []), ("runs", -1), ("runs", 1.5), ("runs", "1"),
        ("runs", True), ("revocable", None), ("revocable", 1),
    ]],
    ("GET", 400, '{"error": {"x": 1}}'),
    ("GET", 503, ""),
])
def test_unusable_key_responses_show_readable_status(page, server_ui, method, status, body):
    page.allow_http_errors = True
    open_keys(page, server_ui)
    page.route("**/api/server/keys**", lambda route: route.fulfill(
        status=status, content_type="text/html", body=body,
    ) if route.request.method == method else route.continue_())
    if method == "GET":
        page.locator(".breadcrumb").click()
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        page.get_by_role("link", name="Keys", exact=True).click()
        expect(page.get_by_role("alert")).to_have_text(f"Could not load keys ({status})")
        expect(page.locator(".key-value")).to_have_count(0)
        expect(page.locator(".key-command")).to_have_count(0)
    else:
        if method == "POST":
            page.get_by_role("button", name="New key", exact=True).click()
            page.get_by_role("dialog").get_by_label("Name", exact=True).fill("worker")
            label = "Create"
        else:
            key_row(page, "gpu-box").get_by_role("button", name="Revoke", exact=True).click()
            label = "Revoke"
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name=label, exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text(f"Key request failed ({status})")
        expect(dialog.get_by_role("button", name=label, exact=True)).to_be_enabled()
        expect(dialog.locator(".key-value")).to_have_count(0)
        expect(dialog.locator(".key-command")).to_have_count(0)


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_write_401_keeps_buttons_disabled_until_login(page, context, server_ui, method):
    page.allow_http_errors = True
    open_keys(page, server_ui)
    context.clear_cookies()
    page.route("**/api/server/keys**", lambda route: route.fulfill(
        status=401, json={"error": "login required"},
    ) if route.request.method == method else route.continue_())
    if method == "POST":
        page.get_by_role("button", name="New key", exact=True).click()
        page.get_by_role("dialog").get_by_label("Name", exact=True).fill("worker")
        label = "Create"
    else:
        key_row(page, "gpu-box").get_by_role("button", name="Revoke", exact=True).click()
        label = "Revoke"
    dialog = page.get_by_role("dialog")
    states = []
    page.on("console", lambda message: states.append(message.text)
            if message.text.startswith("key-write-state ") else None)
    dialog.evaluate("""node => {
        const error = node.querySelector('.error');
        const submit = node.querySelector('.btn-primary');
        const report = () => console.debug('key-write-state',
            error.hidden, submit.disabled);
        new MutationObserver(report).observe(node, {
            attributes: true, subtree: true, attributeFilter: ['hidden', 'disabled'],
        });
        window.addEventListener('beforeunload', report);
    }""")
    dialog.get_by_role("button", name=label, exact=True).click()
    expect(page).to_have_url(f"{server_ui['url']}/login")
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    assert states
    assert all(state == "key-write-state true true" for state in states)


def test_older_refresh_does_not_replace_newer_keys(page, server_ui):
    open_keys(page, server_ui)
    original = page.request.get(f"{server_ui['url']}/api/server/keys").json()
    pending_create = []
    page.route("**/api/server/keys", lambda route: pending_create.append(route)
               if route.request.method == "POST" else route.continue_())
    page.evaluate("""() => {
        const original = window.fetch;
        window.keyReads = [];
        window.fetch = (url, options) => url === '/api/server/keys' && !options.method
            ? new Promise(resolve => window.keyReads.push(data => resolve(new Response(JSON.stringify(data)))))
            : original(url, options);
    }""")
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill("worker")
    with page.expect_request(lambda request: request.method == "POST"):
        dialog.get_by_role("button", name="Create", exact=True).click()
    dialog.get_by_role("button", name="Close", exact=True).click()
    page.wait_for_function("() => window.keyReads.length === 1")
    with page.expect_response(lambda response: response.request.method == "POST"):
        pending_create[0].continue_()
    page.wait_for_function("() => window.keyReads.length === 2")
    latest = page.request.get(f"{server_ui['url']}/api/server/keys").json()
    page.evaluate("data => window.keyReads[1](data)", latest)
    expect(key_row(page, "worker")).to_be_visible()
    page.evaluate("""async data => {
        window.keyReads[0](data);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""", original)
    expect(key_row(page, "worker")).to_be_visible()


def test_env_key_row_has_no_revoke_button(page, tmp_path, monkeypatch):
    value = "env-admin-" + "x" * 32
    monkeypatch.setenv("AGENTICML_SERVER_KEY", value)
    httpd = make_server(tmp_path / "data", port=0, env_key=os.environ["AGENTICML_SERVER_KEY"])
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        server = {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "admin": value}
        open_keys(page, server)
        row = key_row(page, "env")
        expect(row).to_be_visible()
        expect(row.locator('td[data-label="Prefix"]')).to_have_text("env…")
        expect(row.locator('td[data-label="Revoke"]')).to_have_text("–")
        expect(row.get_by_role("button", name="Revoke", exact=True)).to_have_count(0)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.mark.parametrize("status", [401, 404])
def test_route_change_clears_secret_and_ignores_late_response(page, server_ui, status):
    open_keys(page, server_ui)
    dialog, value = create_key(page, "worker")
    page.evaluate("location.hash = '#/'")
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert_key_cleared(page, value)
    page.evaluate("""status => {
        const original = window.fetch;
        window.runReads = 0;
        window.fetch = (url, options) => {
            if (url === '/api/runs') window.runReads++;
            return url === '/api/server/keys' ? new Promise(resolve => {
                window.resolveKeys = () => resolve(new Response(null, {status}));
            }) : original(url, options);
        };
    }""", status)
    page.get_by_role("link", name="Keys", exact=True).click()
    expect(page.get_by_role("heading", name="Keys", exact=True)).to_be_visible()
    page.wait_for_function("() => typeof window.resolveKeys === 'function'")
    page.locator(".breadcrumb").click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="synced-run", exact=True)).to_be_visible()
    reads = page.evaluate("window.runReads")
    page.evaluate("""async () => {
        window.resolveKeys();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page).to_have_url(f"{server_ui['url']}/#/")
    assert page.evaluate("window.runReads") == reads


def test_keys_401_redirects_to_login(page, context, server_ui):
    page.allow_http_errors = True
    sign_in(page, server_ui, server_ui["admin"])
    expect(page.get_by_role("link", name="Keys", exact=True)).to_be_visible()
    context.clear_cookies()
    page.get_by_role("link", name="Keys", exact=True).click()
    expect(page).to_have_url(f"{server_ui['url']}/login")


def track_keys_listeners(page):
    page.evaluate("""() => {
        window.keysListeners = {pagehide: new Set(), pageshow: new Set()};
        window.keysListenerAdds = 0;
        const add = window.addEventListener;
        const remove = window.removeEventListener;
        window.addEventListener = function(type, listener, ...options) {
            window.keysListeners[type]?.add(listener);
            if (window.keysListeners[type]) window.keysListenerAdds++;
            return add.call(this, type, listener, ...options);
        };
        window.removeEventListener = function(type, listener, ...options) {
            window.keysListeners[type]?.delete(listener);
            return remove.call(this, type, listener, ...options);
        };
    }""")


@pytest.mark.parametrize("failure", ["refresh-401", "request-401", "bad-body"])
def test_keys_failure_removes_navigation_listeners(page, server_ui, failure):
    page.allow_http_errors = True
    sign_in(page, server_ui, server_ui["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    track_keys_listeners(page)
    # A 204 keeps the current document available for checking disposal before navigation.
    page.route("**/login", lambda route: route.fulfill(status=204))
    if failure == "request-401":
        page.get_by_role("link", name="Keys", exact=True).click()
        expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()
        assert page.evaluate("Object.values(window.keysListeners).every(listeners => listeners.size === 1)")
        page.route("**/api/server/keys", lambda route: route.fulfill(status=401, json={}))
        page.get_by_role("button", name="New key", exact=True).click()
        dialog = page.get_by_role("dialog", name="New key", exact=True)
        dialog.get_by_label("Name", exact=True).fill("worker")
        with page.expect_request(f"{server_ui['url']}/login"):
            dialog.get_by_role("button", name="Create", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
    else:
        page.route("**/api/server/keys", lambda route: route.fulfill(
            status=401 if failure == "refresh-401" else 200, json={}))
        if failure == "refresh-401":
            with page.expect_request(f"{server_ui['url']}/login"):
                page.get_by_role("link", name="Keys", exact=True).click()
        else:
            page.get_by_role("link", name="Keys", exact=True).click()
            expect(page.get_by_text("Could not load keys (200)", exact=True)).to_be_visible()
    assert page.evaluate("window.keysListenerAdds") == 2
    assert page.evaluate("Object.values(window.keysListeners).every(listeners => listeners.size === 0)")


def test_local_ui_hides_keys_and_falls_back_to_runs(page, tmp_path, ui_server_factory):
    page.allow_http_errors = True
    with ui_server_factory(tmp_path / "local") as server:
        page.goto(server.url)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.locator("#nav-keys")).to_be_hidden()
        track_keys_listeners(page)
        page.goto(f"{server.url}/#/keys")
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.locator("#nav-keys")).to_be_hidden()
        assert page.evaluate("window.keysListenerAdds") == 2
        assert page.evaluate("Object.values(window.keysListeners).every(listeners => listeners.size === 0)")
        page.goto(f"{server.url}/#/")
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        assert page.evaluate("Object.values(window.keysListeners).every(listeners => listeners.size === 0)")
