"""Browser coverage for the server-admin page and /workspaces picker."""
from datetime import datetime, timedelta, timezone
import re
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.server.db.stats import StatsRepo
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.workspaces import prepare_store
from agenticml.store import layout

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import (
    assert_no_overflow, browser, context, open_settings, page, sign_in_password,
)
from test_server_keys_dashboard import assert_dialog_fits

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


def seed_usage(httpd, snapshots=True):
    """Usage the Usage tiles show: alice active 3 days ago, bytes and runs, two daily snapshots."""
    now = datetime.now(timezone.utc)
    ws = {row["slug"]: row["id"] for row in WorkspaceRepo(httpd.db).list()}
    with httpd.db.transaction(write=True) as tx:
        tx.cross_workspace("test seed")
        tx.execute("UPDATE accounts SET last_active_at=? WHERE login=?",
                   (layout.format_timestamp(now - timedelta(days=3)), "alice"))
        for slug, stored, uploaded in (("default", 3_000_000_000, 1_500_000_000),
                                       ("shared", 2_000_000_000, 500_000_000)):
            tx.execute(httpd.db.dialect.upsert(
                "workspace_usage", ("workspace_id", "stored_bytes", "upload_day", "upload_bytes"),
                ("workspace_id",), ("stored_bytes", "upload_day", "upload_bytes")),
                (ws[slug], stored, now.date().isoformat(), uploaded))
        for slug, run_id in (("default", "r1"), ("default", "r2"), ("shared", "r3")):
            tx.execute("INSERT INTO runs (workspace_id, run_id, list_row, updated_at) VALUES (?, ?, ?, ?)",
                       (ws[slug], run_id, "{}", layout.format_timestamp(now)))
    if snapshots:
        for days in (2, 1):
            StatsRepo(httpd.db).snapshot(now=now - timedelta(days=days))


def usage_tile(page, label):
    return page.locator(".usage-tiles > div").filter(has=page.locator("dt").filter(
        has_text=re.compile(f"^{re.escape(label)}$")))


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
    card = open_settings(page)
    expect(card.locator("dd").filter(has_text=re.compile(r"^owner$"))).to_be_visible()


def test_admin_page_keeps_account_dialog_and_workspace_pages_get_admin_link(page, admin_server):
    sign_in_password(page, admin_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    page.locator('a[href="/w/default/"]').click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    # Server admins get Admin in the top bar; the Account button is for /admin only.
    expect(page.locator("#nav-admin")).to_be_visible()
    expect(page.locator("#account")).to_be_hidden()
    page.locator("#nav-admin").click()
    expect(page).to_have_url(re.compile(r"/admin/?"))
    expect(page.get_by_role("heading", name="Admin", exact=True)).to_be_visible()
    expect(page.locator("#nav-settings")).to_be_hidden()
    expect(page.locator("#nav-admin")).to_be_hidden()
    # The top bar holds the only Workspaces link (the heading no longer repeats it).
    expect(page.get_by_role("link", name="Workspaces", exact=True)).to_have_count(1)
    expect(page.locator("#nav-workspaces")).to_be_visible()
    page.get_by_role("button", name="Account", exact=True).click()
    dialog = page.get_by_role("dialog", name="Account", exact=True)
    expect(dialog.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    expect(dialog.get_by_role("link", name="Admin", exact=True)).to_be_visible()
    expect(dialog.get_by_label("New password", exact=True)).to_be_visible()
    # Password mode has the form the Firebase dialog omits (see test_server_firebase_dashboard.py).
    expect(dialog.get_by_label("Confirm password", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Change password", exact=True)).to_be_visible()
    expect(dialog.locator(".identity div").filter(has_text="Sign-in").locator("dd")).to_have_text("password")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_have_count(0)


def test_no_overflow_admin_and_picker(page, admin_server):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in_password(page, admin_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("link", name="Admin", exact=True).click()
    expect(page.get_by_role("heading", name="Admin", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Workspaces", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Server admins", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("button", name="Add server admin", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add server admin", exact=True)
    expect(dialog).to_be_visible()
    assert_no_overflow(page)
    dialog.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Account", exact=True).click()
    account = page.get_by_role("dialog", name="Account", exact=True)
    expect(account).to_be_visible()
    assert_no_overflow(page)
    assert_dialog_fits(page, account)
    account.get_by_role("button", name="Close", exact=True).click()
    page.locator("#nav-workspaces").click()
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    assert_no_overflow(page)


def test_top_bar_workspaces_link_and_sign_out_everywhere(page, admin_server):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in_password(page, admin_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    # The picker has Sign out but no Workspaces link of its own.
    expect(page.locator("#nav-workspaces")).to_have_count(0)
    expect(page.locator("#sign-out")).to_be_visible()
    assert_no_overflow(page)
    page.locator('a[href="/w/default/"]').click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    link = page.locator("#nav-workspaces")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", "/workspaces")
    expect(page.locator("#sign-out")).to_be_visible()
    assert_no_overflow(page)
    link.click()
    expect(page).to_have_url(re.compile(r"/workspaces$"))
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    page.locator("#sign-out").click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    page.goto(admin_server["url"] + "/workspaces")
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()


def admin_row(page, login):
    table = page.get_by_role("table", name="Server admins", exact=True)
    return table.get_by_role("row").filter(has=page.locator('td[data-label="Login"]').filter(
        has_text=re.compile(f"^{re.escape(login)}$")))


def test_server_admins_section_add_remove_and_last_admin(page, admin_server):
    page.allow_http_errors = True  # the unknown account (404) and last admin (409) answers
    open_admin(page, admin_server)
    expect(page.get_by_role("heading", name="Server admins", exact=True)).to_be_visible()
    table = page.get_by_role("table", name="Server admins", exact=True)
    for label in ("Login", "Source", "Sign-in", "Actions"):
        expect(table.get_by_role("columnheader", name=label, exact=True)).to_be_visible()
    first = admin_row(page, "admin")
    expect(first).to_be_visible()
    expect(first.locator('td[data-label="Source"]')).to_have_text("manual")
    expect(first.locator('td[data-label="Sign-in"]')).to_have_text("Password")

    # Without Firebase an unknown email is refused and the hint says so.
    page.get_by_role("button", name="Add server admin", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add server admin", exact=True)
    expect(dialog).to_contain_text("The account must already exist.")
    expect(dialog).not_to_contain_text("one is created")
    dialog.get_by_label("Email or login", exact=True).fill("no-such@example.com")
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("unknown account")
    dialog.get_by_label("Email or login", exact=True).fill("alice")
    dialog.get_by_role("button", name="Add", exact=True).click()
    added = admin_row(page, "alice")
    expect(added).to_be_visible()
    expect(added.locator('td[data-label="Sign-in"]')).to_have_text("Password")
    expect(page.get_by_role("status").filter(has_text="alice is now a server admin")).to_be_visible()
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("account.server_admin")

    added.get_by_role("button", name="Remove", exact=True).click()
    confirm = page.get_by_role("dialog", name="Remove alice?", exact=True)
    expect(confirm).to_contain_text("next sign-in")
    confirm.get_by_role("button", name="Remove", exact=True).click()
    expect(admin_row(page, "alice")).to_have_count(0)

    admin_row(page, "admin").get_by_role("button", name="Remove", exact=True).click()
    confirm = page.get_by_role("dialog", name="Remove admin?", exact=True)
    confirm.get_by_role("button", name="Remove", exact=True).click()
    expect(confirm.get_by_role("alert")).to_have_text("cannot remove the last server admin")
    confirm.get_by_role("button", name="Close", exact=True).click()
    expect(admin_row(page, "admin")).to_be_visible()


def test_server_admin_removing_themselves_leaves_the_admin_page(page, admin_server):
    page.allow_http_errors = True  # /admin/ is an unknown page for a non-admin
    open_admin(page, admin_server)
    page.get_by_role("button", name="Add server admin", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add server admin", exact=True)
    dialog.get_by_label("Email or login", exact=True).fill("alice")
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(admin_row(page, "alice")).to_be_visible()
    admin_row(page, "admin").get_by_role("button", name="Remove", exact=True).click()
    confirm = page.get_by_role("dialog", name="Remove admin?", exact=True)
    confirm.get_by_role("button", name="Remove", exact=True).click()
    expect(page).not_to_have_url(re.compile(r"/admin"))
    expect(page).to_have_url(re.compile(r"/(workspaces|w/default/)"))
    expect(page.get_by_role("alert")).to_have_count(0)
    response = page.goto(f"{admin_server['url']}/admin/")
    assert response is not None and response.status == 404


def test_new_workspace_dialog_hint(page, admin_server):
    open_admin(page, admin_server)
    page.get_by_role("button", name="New workspace", exact=True).click()
    create = page.get_by_role("dialog", name="New workspace", exact=True)
    expect(create.get_by_label("Owner login", exact=True)).to_be_visible()
    expect(create).to_contain_text(
        "If no account exists for this email, one is created; it is linked when they first sign in.")


def test_archive_and_restore_a_workspace(page, admin_server):
    """Package 24.10: Archive moves a workspace to the Archived table; Restore brings it back suspended."""
    open_admin(page, admin_server)
    status = page.get_by_role("status")
    expect(page.get_by_role("table", name="Archived workspaces", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Archived", exact=True)).to_be_hidden()
    default = workspace_row(page, "default")
    expect(default.get_by_role("button", name="Archive", exact=True)).to_have_count(0)
    shared = workspace_row(page, "shared")
    shared.get_by_role("button", name="Archive", exact=True).click()
    dialog = page.get_by_role("dialog", name="Archive shared?", exact=True)
    expect(dialog).to_contain_text(
        "Nobody can open or sync to it until it is restored. Its data is kept.")
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(shared).to_be_visible()
    shared.get_by_role("button", name="Archive", exact=True).click()
    page.get_by_role("dialog", name="Archive shared?", exact=True).get_by_role(
        "button", name="Archive", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(status).to_have_text("Archived shared.")
    expect(workspace_row(page, "shared").locator('td[data-label="Status"]')).to_have_count(0)
    archived = page.get_by_role("table", name="Archived workspaces", exact=True)
    expect(archived).to_be_visible()
    expect(page.get_by_role("heading", name="Archived", exact=True)).to_be_visible()
    row = archived.get_by_role("row").filter(has=page.locator('td[data-label="Slug"]').filter(
        has_text=re.compile("^shared$")))
    expect(row).to_be_visible()
    for label in ("Name", "Plan", "Storage", "Archived", "Actions"):
        expect(row.locator(f'td[data-label="{label}"]')).to_have_count(1)
    expect(page.get_by_role("table", name="Workspaces", exact=True)).not_to_contain_text("shared")
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("workspace.archive")
    row.get_by_role("button", name="Restore", exact=True).click()
    expect(status).to_have_text("Restored shared; it is suspended until you resume it.")
    expect(page.get_by_role("table", name="Archived workspaces", exact=True)).to_have_count(0)
    restored = workspace_row(page, "shared")
    expect(restored.locator('td[data-label="Status"]')).to_have_text("suspended")
    restored.get_by_role("button", name="Resume", exact=True).click()
    expect(restored.locator('td[data-label="Status"]')).to_have_text("active")


def test_no_overflow_with_archive_dialog_and_archived_table(page, admin_server):
    page.set_viewport_size({"width": 375, "height": 812})
    open_admin(page, admin_server)
    shared = workspace_row(page, "shared")
    shared.get_by_role("button", name="Archive", exact=True).click()
    dialog = page.get_by_role("dialog", name="Archive shared?", exact=True)
    expect(dialog).to_be_visible()
    assert_no_overflow(page)
    dialog.get_by_role("button", name="Archive", exact=True).click()
    expect(page.get_by_role("table", name="Archived workspaces", exact=True)).to_be_visible()
    assert_no_overflow(page)


def test_usage_section_shows_tiles_and_growth_charts(page, admin_server):
    seed_usage(admin_server["httpd"])
    open_admin(page, admin_server)
    expect(page.get_by_role("heading", name="Usage", exact=True)).to_be_visible()
    # Usage sits above the workspaces table.
    assert page.get_by_role("heading", name="Usage", exact=True).bounding_box()["y"] < \
        page.get_by_role("table", name="Workspaces", exact=True).bounding_box()["y"]
    expect(usage_tile(page, "Accounts").locator("dd")).to_have_text("2+2 in 7 days")
    # alice 3 days ago; admin signed in just now.
    expect(usage_tile(page, "Active users").locator("dd")).to_have_text("21 day: 1 · 30 days: 2")
    expect(usage_tile(page, "Workspaces").locator("dd")).to_have_text("20 suspended · 0 archived")
    expect(usage_tile(page, "Storage").locator("dd")).to_have_text("5.0 GBUploads today: 2.0 GB")
    expect(usage_tile(page, "Runs").locator("dd")).to_have_text("3")
    grid = page.locator(".chart-grid")
    for title in ("Accounts", "Active users (7 days)", "Storage (GB)"):
        expect(grid.locator(".chart-card").filter(
            has=page.get_by_role("heading", name=title, exact=True))).to_have_count(1)
        expect(grid.get_by_label(f"{title} by day", exact=True)).to_be_visible()
    expect(grid.locator("canvas")).to_have_count(3)
    expect(page.get_by_text("Growth charts appear after two daily snapshots.")).to_have_count(0)
    # The rest of the page is unaffected.
    expect(workspace_row(page, "default")).to_be_visible()


def test_usage_section_without_snapshots_shows_note_and_no_charts(page, admin_server):
    seed_usage(admin_server["httpd"], snapshots=False)
    open_admin(page, admin_server)
    expect(usage_tile(page, "Runs").locator("dd")).to_have_text("3")
    expect(page.get_by_text("Growth charts appear after two daily snapshots.")).to_be_visible()
    expect(page.locator(".usage-tiles").locator("xpath=ancestor::div[contains(@class,'workspace')]")
           .locator("canvas")).to_have_count(0)
    expect(page.locator(".chart-grid")).to_have_count(0)


def test_usage_failure_shows_in_its_section_only(page, admin_server):
    page.allow_http_errors = True  # the stubbed 503 answer, as the API gives when the database is down
    page.route("**/api/admin/stats", lambda route: route.fulfill(
        status=503, content_type="application/json", body='{"error": "usage statistics unavailable"}'))
    open_admin(page, admin_server)
    expect(page.get_by_role("alert").filter(has_text="usage statistics unavailable")).to_be_visible()
    expect(page.locator(".usage-tiles")).to_have_count(0)
    expect(workspace_row(page, "default")).to_be_visible()
    expect(page.get_by_role("table", name="Server admins", exact=True)).to_be_visible()


def test_no_overflow_with_usage_section(page, admin_server):
    seed_usage(admin_server["httpd"])
    page.set_viewport_size({"width": 375, "height": 812})
    open_admin(page, admin_server)
    expect(page.locator(".chart-grid canvas")).to_have_count(3)
    expect(usage_tile(page, "Storage")).to_be_visible()
    assert_no_overflow(page)
