"""Browser coverage for the server-admin page and /workspaces picker."""
import re
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.workspaces import prepare_store

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import assert_no_overflow, browser, context, page, sign_in_password

pytestmark = [pytest.mark.browser, pytest.mark.ui]

PASSWORD = "password1234"


@pytest.fixture
def admin_server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    httpd.users.add("admin", role="admin")
    httpd.users.set_password("admin", PASSWORD)
    httpd.users.add("alice", role="member")
    httpd.users.set_password("alice", PASSWORD)
    other = WorkspaceRepo(httpd.db).create("shared", name="Shared", owner="admin")
    prepare_store(httpd.data_dir, other["id"])
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "httpd": httpd}
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def workspace_row(page, slug):
    return page.get_by_role("row").filter(has=page.locator('td[data-label="Slug"]').filter(
        has_text=re.compile(f"^{re.escape(slug)}$")))


def open_admin(page, server):
    sign_in_password(page, server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    page.get_by_role("link", name="Admin", exact=True).click()
    expect(page).to_have_url(re.compile(r"/admin/?"))
    expect(page.get_by_role("heading", name="Admin", exact=True)).to_be_visible()


def test_admin_lists_creates_renames_suspends_and_default_locked(page, admin_server):
    open_admin(page, admin_server)
    table = page.get_by_role("table", name="Workspaces", exact=True)
    expect(table).to_be_visible()
    default = workspace_row(page, "default")
    expect(default).to_be_visible()
    expect(default.locator('td[data-label="Status"]')).to_have_text("active")
    expect(default.get_by_role("button", name="Suspend", exact=True)).to_be_disabled()
    default.get_by_role("button", name="Rename", exact=True).click()
    rename = page.get_by_role("dialog", name="Rename workspace", exact=True)
    expect(rename.get_by_label("Slug", exact=True)).to_be_disabled()
    rename.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="New workspace", exact=True).click()
    create = page.get_by_role("dialog", name="New workspace", exact=True)
    create.get_by_label("Slug", exact=True).fill("lab")
    create.get_by_label("Name", exact=True).fill("Lab")
    create.get_by_label("Owner login", exact=True).fill("admin")
    create.get_by_role("button", name="Create", exact=True).click()
    lab = workspace_row(page, "lab")
    expect(lab).to_be_visible()
    expect(lab.locator('td[data-label="Name"]')).to_have_text("Lab")
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("workspace.create")
    lab.get_by_role("button", name="Rename", exact=True).click()
    rename = page.get_by_role("dialog", name="Rename workspace", exact=True)
    expect(rename.get_by_label("Slug", exact=True)).to_be_enabled()
    rename.get_by_label("Name", exact=True).fill("Lab Two")
    rename.get_by_label("Slug", exact=True).fill("lab-two")
    rename.get_by_role("button", name="Save", exact=True).click()
    renamed = workspace_row(page, "lab-two")
    expect(renamed).to_be_visible()
    expect(renamed.locator('td[data-label="Name"]')).to_have_text("Lab Two")
    expect(workspace_row(page, "lab")).to_have_count(0)
    renamed.get_by_role("button", name="Suspend", exact=True).click()
    expect(renamed.locator('td[data-label="Status"]')).to_have_text("suspended")
    renamed.get_by_role("button", name="Resume", exact=True).click()
    expect(renamed.locator('td[data-label="Status"]')).to_have_text("active")
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("workspace.suspend")
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("workspace.resume")


def test_non_admin_admin_path_is_unknown_page(page, admin_server):
    page.allow_http_errors = True
    sign_in_password(page, admin_server, "alice", PASSWORD)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    response = page.goto(f"{admin_server['url']}/admin/")
    assert response is not None and response.status == 404
    expect(page.locator("body")).to_contain_text("not found")


def test_picker_lists_workspaces_owner_label_and_admin_link(page, admin_server):
    sign_in_password(page, admin_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    picker = page.get_by_role("list", name="Workspaces", exact=True)
    expect(picker).to_contain_text("Default")
    expect(picker).to_contain_text("Shared")
    expect(picker).to_contain_text("owner")
    expect(page.get_by_role("link", name="Admin", exact=True)).to_be_visible()
    page.locator('a[href="/w/default/"]').click()
    expect(page).to_have_url(re.compile(r"/w/default/"))
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.get_by_text("Signed in as admin · owner")).to_be_visible()
    page.get_by_role("button", name="Account", exact=True).click()
    dialog = page.get_by_role("dialog", name="Account", exact=True)
    expect(dialog.locator("dd").filter(has_text=re.compile(r"^owner$"))).to_be_visible()


def test_no_overflow_admin_and_picker(page, admin_server):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in_password(page, admin_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("link", name="Admin", exact=True).click()
    expect(page.get_by_role("heading", name="Admin", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Workspaces", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("link", name="Workspaces", exact=True).click()
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    assert_no_overflow(page)
