"""Browser coverage for the Team page, account dialog, and role-aware nav."""
import re
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.server.audit import AuditLog
from agenticml.server.runs import adopt_directory
from agenticml.store import RunWriter, layout

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import (
    assert_no_overflow, browser, context, page, sign_in, sign_in_password,
)
from test_server_keys_dashboard import assert_dialog_fits, key_row

pytestmark = [pytest.mark.browser, pytest.mark.ui]

PASSWORD = "password1234"


@pytest.fixture
def team_server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    _, admin = httpd.keys.create("admin", "admin")
    httpd.users.set_password("admin", PASSWORD)
    httpd.users.add("alice", role="member")
    httpd.users.set_password("alice", PASSWORD)
    httpd.keys.create("alice-laptop", user="alice")
    httpd.users.add("vera", role="viewer")
    httpd.users.set_password("vera", PASSWORD)
    writer = RunWriter.create(httpd.root, run_id="demo-run", meta={"project": "demo", "name": "synced-run"})
    writer.finish("finished")
    writer.close()
    adopt_directory(httpd.root, httpd.root / "runs" / "demo-run", httpd.run_index, remote={
        "owner": "admin", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": layout.format_timestamp(),
    })
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "httpd": httpd, "admin": admin}
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def user_row(page, name):
    return page.get_by_role("row").filter(has=page.locator('td[data-label="Name"]').filter(
        has_text=re.compile(f"^{re.escape(name)}$")))


def test_admin_add_role_invite_remove_and_activity(page, context, team_server):
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_text("Signed in as admin · admin")).to_be_visible()
    page.get_by_role("link", name="Team", exact=True).click()
    expect(page.get_by_role("heading", name="Team", exact=True)).to_be_visible()
    expect(user_row(page, "admin").get_by_role("button", name="Invite link", exact=True)).to_have_count(0)
    expect(user_row(page, "alice").get_by_role("button", name="Invite link", exact=True)).to_be_visible()
    page.get_by_role("button", name="Add user", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add user", exact=True)
    dialog.get_by_label("Name", exact=True).fill("bob")
    expect(dialog.get_by_label("Role", exact=True)).to_have_value("member")
    dialog.get_by_label("Identity", exact=True).fill("bob@example.com")
    dialog.get_by_role("button", name="Add user", exact=True).click()
    expect(dialog.get_by_text("This invite link is shown once. They choose their own password.")).to_be_visible()
    link = dialog.locator(".key-value").inner_text()
    assert "/invite?token=" in link
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    dialog.get_by_role("button", name="Copy invite link", exact=True).click()
    expect(dialog.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == link
    dialog.get_by_role("button", name="Close", exact=True).click()
    row = user_row(page, "bob")
    expect(row).to_be_visible()
    expect(row.locator('td[data-label="Sign-in"]')).to_have_text("invite pending")
    row.get_by_label("Role for bob", exact=True).select_option("viewer")
    expect(row.get_by_label("Role for bob", exact=True)).to_have_value("viewer")
    row.get_by_role("button", name="Invite link", exact=True).click()
    invite = page.get_by_role("dialog")
    expect(invite.get_by_text("This signs them out and clears their password.")).to_be_visible()
    invite.get_by_role("button", name="Create invite link", exact=True).click()
    expect(invite.locator(".key-value")).to_contain_text("/invite?token=")
    invite.get_by_role("button", name="Close", exact=True).click()
    row.get_by_role("button", name="Remove", exact=True).click()
    remove = page.get_by_role("dialog")
    expect(remove).to_contain_text("key")
    remove.get_by_role("button", name="Remove", exact=True).click()
    expect(user_row(page, "bob")).to_have_count(0)
    activity = page.get_by_role("table", name="Activity", exact=True)
    expect(activity).to_contain_text("user.add")
    expect(activity).to_contain_text("user.role")
    expect(activity).to_contain_text("user.invite")
    expect(activity).to_contain_text("user.remove")


def test_member_keys_scope_and_no_team_nav(page, team_server):
    sign_in_password(page, team_server, "alice", PASSWORD)
    expect(page.get_by_text("Signed in as alice · member")).to_be_visible()
    expect(page.get_by_role("link", name="Team", exact=True)).to_have_count(0)
    expect(page.locator("#nav-team")).to_be_hidden()
    page.get_by_role("link", name="Keys", exact=True).click()
    expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()
    expect(page.locator('th[scope="col"]', has_text="User")).to_have_count(0)
    expect(key_row(page, "alice-laptop")).to_be_visible()
    expect(key_row(page, "admin")).to_have_count(0)
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    expect(dialog.get_by_label("User", exact=True)).to_have_count(0)
    options = dialog.get_by_label("Scope", exact=True).locator("option")
    expect(options).to_have_count(1)
    expect(options).to_have_text("sync")
    dialog.get_by_role("button", name="Close", exact=True).click()


def test_viewer_hides_keys_team_and_notes_editor(page, team_server):
    sign_in_password(page, team_server, "vera", PASSWORD)
    expect(page.get_by_text("Signed in as vera · viewer")).to_be_visible()
    expect(page.get_by_role("link", name="Keys", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Team", exact=True)).to_have_count(0)
    expect(page.locator("#nav-keys")).to_be_hidden()
    expect(page.locator("#nav-team")).to_be_hidden()
    page.get_by_role("link", name="synced-run", exact=True).click()
    expect(page.get_by_role("heading", name="synced-run", exact=True)).to_be_visible()
    expect(page.get_by_label("Notes", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Save note", exact=True)).to_have_count(0)
    expect(page.locator(".run-owner")).to_have_text("admin")


def test_account_change_password_and_wrong_current(page, team_server):
    page.allow_http_errors = True
    sign_in_password(page, team_server, "alice", PASSWORD)
    page.get_by_role("button", name="Account", exact=True).click()
    dialog = page.get_by_role("dialog", name="Account", exact=True)
    expect(dialog.get_by_text("alice", exact=True)).to_be_visible()
    expect(dialog.get_by_text("member", exact=True)).to_be_visible()
    dialog.get_by_label("Current password", exact=True).fill("wrong-password")
    dialog.get_by_label("New password", exact=True).fill("newpassword12")
    dialog.get_by_label("Confirm password", exact=True).fill("newpassword12")
    dialog.get_by_role("button", name="Change password", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("invalid current password")
    dialog.get_by_label("Current password", exact=True).fill(PASSWORD)
    dialog.get_by_role("button", name="Change password", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text("Password updated")
    dialog.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    page.locator("#user").fill("alice")
    page.locator("#password").fill("newpassword12")
    page.locator("#password-login").get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


def test_no_overflow_team_keys_and_account(page, team_server):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("link", name="Team", exact=True).click()
    expect(page.get_by_role("heading", name="Team", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("button", name="Add user", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add user", exact=True)
    expect(dialog).to_be_visible()
    assert_no_overflow(page)
    assert_dialog_fits(page, dialog)
    dialog.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("link", name="Keys", exact=True).click()
    expect(page.get_by_role("heading", name="Keys", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("button", name="Account", exact=True).click()
    account = page.get_by_role("dialog", name="Account", exact=True)
    expect(account).to_be_visible()
    assert_no_overflow(page)
    assert_dialog_fits(page, account)


def test_admin_keys_user_column_filter_and_inactive(page, team_server):
    httpd = team_server["httpd"]
    httpd.users.add("demoted", role="admin")
    httpd.keys.create("old-admin", scope="admin", user="demoted")
    httpd.users.set_role("demoted", "member")
    sign_in(page, team_server, team_server["admin"])
    page.get_by_role("link", name="Keys", exact=True).click()
    expect(page.get_by_role("columnheader", name="User", exact=True)).to_be_visible()
    expect(key_row(page, "old-admin").get_by_text("inactive", exact=True)).to_be_visible()
    expect(key_row(page, "old-admin").get_by_text("inactive", exact=True)).to_have_attribute(
        "title", "only admins can use admin keys")
    page.get_by_label("Filter by user", exact=True).select_option("alice")
    expect(key_row(page, "alice-laptop")).to_be_visible()
    expect(key_row(page, "admin")).to_have_count(0)
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    expect(dialog.get_by_label("User", exact=True)).to_be_visible()
    dialog.get_by_label("User", exact=True).select_option("alice")
    expect(dialog.get_by_label("Scope", exact=True).locator("option[value=admin]")).to_be_disabled()


def test_identity_edit_keeps_other_row_draft(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    page.get_by_role("link", name="Team", exact=True).click()
    expect(page.get_by_role("heading", name="Team", exact=True)).to_be_visible()
    vera = user_row(page, "vera").get_by_label("Identity for vera", exact=True)
    vera.click()
    vera.press_sequentially("draft")
    alice = user_row(page, "alice").get_by_label("Identity for alice", exact=True)
    alice.fill("alice@example.com")
    alice.blur()
    expect(user_row(page, "alice").get_by_label("Identity for alice", exact=True)).to_have_value(
        "alice@example.com")
    expect(user_row(page, "vera").get_by_label("Identity for vera", exact=True)).to_have_value("draft")


def test_load_older_activity(page, team_server):
    httpd = team_server["httpd"]
    log = AuditLog(httpd.data_dir / "server", db=httpd.db, workspace_id=httpd.default_workspace_id)
    for index in range(201):
        log.record("admin", "password", "user.add", f"u{index:03d}", "127.0.0.1", {"role": "member"})
    sign_in(page, team_server, team_server["admin"])
    page.get_by_role("link", name="Team", exact=True).click()
    expect(page.get_by_role("table", name="Activity", exact=True)).to_be_visible()
    older = page.get_by_role("button", name="Load older", exact=True)
    expect(older).to_be_enabled()
    older.click()
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("u000")
