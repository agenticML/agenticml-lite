"""Browser coverage for the Settings page (members, account, keys), and its role-aware nav."""
import re
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.server.audit import AuditLog
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.runs import adopt_directory
from agenticml.server.users import UserStore
from agenticml.server.workspaces import prepare_store
from agenticml.store import RunWriter, layout

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import (
    assert_no_overflow, browser, context, open_settings, page, settings_card, sign_in,
    sign_in_password,
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


def assert_card_at_bar(page, section):
    """The card's top sits just under the sticky bar, or the page is scrolled as far as it goes."""
    bar = page.locator(".topbar").bounding_box()
    card = settings_card(page, section).bounding_box()
    scroll = page.evaluate(
        "({y: window.scrollY, max: document.documentElement.scrollHeight - window.innerHeight})")
    assert scroll["y"] > 0, "page did not scroll"
    at_bar = abs(card["y"] - (bar["y"] + bar["height"] + 16)) <= 2
    at_end = abs(scroll["y"] - scroll["max"]) <= 1
    assert at_bar or at_end, f"card at {card['y']}, bar bottom {bar['y'] + bar['height']}, scroll {scroll}"


def hold_requests(page, pattern, method="GET"):
    """Hold matching requests until release() is called; returns (held, release)."""
    held = []

    def handler(route):
        if route.request.method == method:
            held.append(route)
        else:
            route.continue_()

    page.route(pattern, handler)

    def release():
        page.wait_for_function("() => true")
        for route in held:
            route.continue_()
        held.clear()

    return held, release


def user_row(page, name):
    return page.get_by_role("row").filter(has=page.locator('td[data-label="Name"]').filter(
        has_text=re.compile(f"^{re.escape(name)}$")))


def test_admin_owner_role_invite_revoke_and_activity(page, context, team_server):
    httpd = team_server["httpd"]
    other = WorkspaceRepo(httpd.db).create("other", name="Other", owner="admin")
    prepare_store(httpd.data_dir, other["id"])
    UserStore(httpd.data_dir / "server", db=httpd.db, workspace_id=other["id"]).add("bob", "member")
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_text("Signed in as admin · owner")).to_be_visible()
    open_settings(page, "members")
    owner = user_row(page, "admin")
    expect(owner.locator('td[data-label="Role"]')).to_have_text("owner")
    expect(owner.get_by_label("Role for admin", exact=True)).to_have_count(0)
    expect(owner.get_by_role("button", name="Invite link", exact=True)).to_have_count(0)
    expect(owner.get_by_role("button", name="Remove", exact=True)).to_have_count(0)
    alice = user_row(page, "alice")
    expect(alice.get_by_role("button", name="Invite link", exact=True)).to_be_visible()
    alice.get_by_label("Role for alice", exact=True).select_option("viewer")
    expect(alice.get_by_label("Role for alice", exact=True)).to_have_value("viewer")
    members = settings_card(page, "members")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("bob")
    expect(form.get_by_label("Role", exact=True)).to_have_value("member")
    expect(form).to_contain_text("Members can sync runs, add notes and manage their own keys.")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.get_by_text(
        "This login already has an account and was added to the workspace.")).to_be_visible()
    expect(user_row(page, "bob")).to_be_visible()
    form.get_by_label("Login or email", exact=True).fill("cara")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.get_by_text("This invite link is shown once. They choose their own password.")).to_be_visible()
    link = form.locator(".key-value").inner_text()
    assert "/invite?token=" in link
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    form.get_by_role("button", name="Copy invite link", exact=True).click()
    expect(form.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == link
    pending = page.get_by_role("table", name="Pending invites", exact=True)
    expect(pending).to_contain_text("cara")
    pending.get_by_role("button", name="Revoke", exact=True).click()
    revoke = page.get_by_role("dialog", name="Revoke invite for cara?")
    revoke.get_by_role("button", name="Revoke", exact=True).click()
    expect(page.get_by_text("No pending invites.")).to_be_visible()
    user_row(page, "bob").get_by_role("button", name="Remove", exact=True).click()
    remove = page.get_by_role("dialog")
    expect(remove).to_contain_text("key")
    remove.get_by_role("button", name="Remove", exact=True).click()
    expect(user_row(page, "bob")).to_have_count(0)
    activity = page.get_by_role("table", name="Activity", exact=True)
    expect(activity).to_contain_text("user.role")
    expect(activity).to_contain_text("invite.create")
    expect(activity).to_contain_text("invite.revoke")
    expect(activity).to_contain_text("user.remove")


def test_member_keys_scope_and_no_admin_cards(page, team_server):
    sign_in_password(page, team_server, "alice", PASSWORD)
    expect(page.get_by_text("Signed in as alice · member")).to_be_visible()
    expect(page.get_by_role("link", name="Team", exact=True)).to_have_count(0)
    open_settings(page, "keys")
    expect(page.locator(".settings-card h2")).to_have_text(["Your account", "API keys"])
    expect(page.locator("#settings-members")).to_have_count(0)
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


def test_nav_follows_current_workspace_role_after_switch(page, team_server):
    httpd = team_server["httpd"]
    httpd.users.add("mira", role="viewer")
    httpd.users.set_password("mira", PASSWORD)
    gamma = WorkspaceRepo(httpd.db).create("gamma", name="Gamma", owner="mira")
    prepare_store(httpd.data_dir, gamma["id"])
    sign_in_password(page, team_server, "mira", PASSWORD)
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    page.goto(f"{team_server['url']}/w/default/")
    expect(page.get_by_text("Signed in as mira · viewer")).to_be_visible()
    expect(page.get_by_role("link", name="Keys", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Team", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Account", exact=True)).to_be_hidden()
    open_settings(page)
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    expect(nav.get_by_role("link")).to_have_text(["Account"])
    settings_card(page, "account").get_by_role("button", name="Switch to Gamma").click()
    expect(page).to_have_url(re.compile(r"/w/gamma/"))
    expect(page.get_by_text("Signed in as mira · owner")).to_be_visible()
    open_settings(page)
    expect(nav.get_by_role("link")).to_have_text(
        ["Account", "Workspace", "Members", "API keys", "Activity"])


def test_viewer_sees_only_account_and_no_notes_editor(page, team_server):
    sign_in_password(page, team_server, "vera", PASSWORD)
    expect(page.get_by_text("Signed in as vera · viewer")).to_be_visible()
    expect(page.get_by_role("link", name="Keys", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Team", exact=True)).to_have_count(0)
    expect(page.locator("#nav-keys, #nav-team")).to_have_count(0)
    open_settings(page)
    expect(page.get_by_role("navigation", name="Settings", exact=True).get_by_role(
        "link")).to_have_text(["Account"])
    expect(page.locator(".settings-card")).to_have_count(1)
    page.go_back()
    page.get_by_role("link", name="synced-run", exact=True).click()
    expect(page.get_by_role("heading", name="synced-run", exact=True)).to_be_visible()
    expect(page.get_by_label("Notes", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Save note", exact=True)).to_have_count(0)
    expect(page.locator(".run-owner")).to_have_text("admin")


def test_account_change_password_and_wrong_current(page, team_server):
    page.allow_http_errors = True
    sign_in_password(page, team_server, "alice", PASSWORD)
    card = open_settings(page)
    expect(card.get_by_text("alice", exact=True)).to_be_visible()
    expect(card.get_by_text("member", exact=True)).to_be_visible()
    expect(card.get_by_role("list", name="Workspaces", exact=True)).to_have_count(0)
    expect(card.get_by_role("link", name="Admin", exact=True)).to_have_count(0)
    card.get_by_text("Change password", exact=True).first.click()
    card.get_by_label("Current password", exact=True).fill("wrong-password")
    card.get_by_label("New password", exact=True).fill("newpassword12")
    card.get_by_label("Confirm password", exact=True).fill("newpassword12")
    card.get_by_role("button", name="Change password", exact=True).click()
    expect(card.get_by_role("alert")).to_have_text("invalid current password")
    card.get_by_label("Current password", exact=True).fill(PASSWORD)
    card.get_by_role("button", name="Change password", exact=True).click()
    expect(card.get_by_role("status")).to_have_text("Password updated")
    card.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    page.locator("#user").fill("alice")
    page.locator("#password").fill("newpassword12")
    page.locator("#password-login").get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


def test_no_overflow_settings_owner_member_viewer(page, team_server):
    page.allow_http_errors = True
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert_no_overflow(page)
    open_settings(page)
    assert_no_overflow(page)
    open_settings(page, "members")
    assert_no_overflow(page)
    form = settings_card(page, "members").get_by_role("form", name="Invite someone", exact=True)
    long_login = "a" * 60 + "@" + "b" * 60 + ".example.com"
    form.get_by_label("Login or email", exact=True).fill(long_login)
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.locator(".key-value")).to_be_visible()
    assert_no_overflow(page)
    page.route("**/api/w/default/invites", lambda route: route.fulfill(
        status=400, json={"error": "x" * 300}) if route.request.method == "POST"
        else route.continue_())
    form.get_by_label("Login or email", exact=True).fill("long-error")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.get_by_role("alert")).to_be_visible()
    assert_no_overflow(page)
    page.unroute("**/api/w/default/invites")
    for section in ("workspace", "keys", "activity"):
        open_settings(page, section)
        assert_no_overflow(page)
    open_settings(page, "account")
    settings_card(page, "account").get_by_text("Change password", exact=True).first.click()
    expect(settings_card(page, "account").get_by_label("New password", exact=True)).to_be_visible()
    assert_no_overflow(page)
    page.get_by_role("button", name="Sign out", exact=True).first.click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    for user, sections in (("alice", ["account", "keys"]), ("vera", ["account"])):
        sign_in_password(page, team_server, user, PASSWORD)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        for section in sections:
            open_settings(page, section)
            assert_no_overflow(page)
        open_settings(page, "account")
        settings_card(page, "account").get_by_text("Change password", exact=True).first.click()
        expect(settings_card(page, "account").get_by_label("New password", exact=True)).to_be_visible()
        assert_no_overflow(page)
        page.get_by_role("button", name="Sign out", exact=True).first.click()
        expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()


def test_admin_keys_user_column_filter_and_inactive(page, team_server):
    httpd = team_server["httpd"]
    httpd.users.add("demoted", role="admin")
    httpd.keys.create("old-admin", scope="admin", user="demoted")
    httpd.users.set_role("demoted", "member")
    sign_in(page, team_server, team_server["admin"])
    open_settings(page, "keys")
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
    open_settings(page, "members")
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
    open_settings(page, "activity")
    expect(page.get_by_role("table", name="Activity", exact=True)).to_be_visible()
    older = page.get_by_role("button", name="Load older", exact=True)
    expect(older).to_be_enabled()
    older.click()
    expect(page.get_by_role("table", name="Activity", exact=True)).to_contain_text("u000")


def test_team_copy_for_password_server(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    members = open_settings(page, "members")
    user_row(page, "alice").get_by_role("button", name="Invite link", exact=True).click()
    dialog = page.get_by_role("dialog", name="Invite alice?", exact=True)
    expect(dialog).to_contain_text("This signs them out and clears their password.")
    dialog.get_by_role("button", name="Close", exact=True).click()
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("cara")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form).to_contain_text("This invite link is shown once. They choose their own password.")
    expect(form).not_to_contain_text("They sign in with Google or email and password")


def test_settings_nav_cards_and_top_bar_by_role(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("link", name="Settings", exact=True)).to_be_visible()
    for gone in ("Keys", "Team"):
        expect(page.get_by_role("link", name=gone, exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Account", exact=True)).to_be_hidden()
    expect(page.locator("#host")).to_have_text("")
    open_settings(page)
    expect(page.get_by_role("link", name="Settings", exact=True)).to_have_attribute(
        "aria-current", "page")
    expect(page.locator(".breadcrumb")).to_have_text("agenticml / settings")
    expect(page.get_by_text("Workspace · Default")).to_be_visible()
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    # The admin-key owner is a server admin: Admin in the bar, a separated link in the nav.
    expect(nav.get_by_role("link")).to_have_text(
        ["Account", "Workspace", "Members", "API keys", "Activity", "Server admin ↗"])
    expect(nav.get_by_role("link", name="Server admin ↗")).to_have_attribute("href", "/admin/")
    expect(page.get_by_role("link", name="Admin", exact=True)).to_have_attribute("href", "/admin/")
    expect(page.locator(".settings-card h2")).to_have_text(
        ["Your account", "Workspace", "Members", "API keys", "Activity"])
    # One Sign out in the top bar and one in the Account card.
    expect(page.get_by_role("button", name="Sign out", exact=True)).to_have_count(2)


def test_settings_member_and_viewer_cards(page, team_server):
    sign_in_password(page, team_server, "alice", PASSWORD)
    expect(page.get_by_text("Signed in as alice · member")).to_be_visible()
    open_settings(page)
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    expect(nav.get_by_role("link")).to_have_text(["Account", "API keys"])
    expect(page.locator(".settings-card h2")).to_have_text(["Your account", "API keys"])
    expect(page.get_by_role("link", name="Admin", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Sign out", exact=True).first.click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    sign_in_password(page, team_server, "vera", PASSWORD)
    open_settings(page)
    expect(nav.get_by_role("link")).to_have_text(["Account"])
    expect(page.locator(".settings-card h2")).to_have_text(["Your account"])


def test_old_keys_and_team_links_redirect(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    page.goto(f"{team_server['url']}/w/default/#/keys")
    expect(page).to_have_url(re.compile(r"/w/default/#/settings/keys$"))
    expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()
    expect(page.get_by_role("navigation", name="Settings", exact=True).get_by_role(
        "link", name="API keys", exact=True)).to_have_attribute("aria-current", "true")
    page.goto(f"{team_server['url']}/w/default/#/team")
    expect(page).to_have_url(re.compile(r"/w/default/#/settings/members$"))
    expect(page.get_by_role("table", name="Users", exact=True)).to_be_visible()
    page.goto(f"{team_server['url']}/w/default/#/settings/nonsense")
    expect(page).to_have_url(re.compile(r"/w/default/#/settings/account$"))
    expect(page.get_by_role("navigation", name="Settings", exact=True).get_by_role(
        "link", name="Account", exact=True)).to_have_attribute("aria-current", "true")


def test_member_cannot_open_members_section(page, team_server):
    sign_in_password(page, team_server, "alice", PASSWORD)
    expect(page.get_by_text("Signed in as alice · member")).to_be_visible()
    page.goto(f"{team_server['url']}/w/default/#/team")
    expect(page).to_have_url(re.compile(r"/#/settings/account$"))
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Users", exact=True)).to_have_count(0)


def test_settings_nav_click_scrolls_without_refetch(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    # Let every section finish loading, so only a refetch by the nav click can show up.
    for name in ("Keys", "Users", "Activity"):
        expect(page.get_by_role("table", name=name, exact=True)).to_be_visible()
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "/api/" in request.url else None)
    page.evaluate("document.querySelector('.settings-page').dataset.marker = 'kept'")
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    nav.get_by_role("link", name="Activity", exact=True).click()
    expect(page).to_have_url(re.compile(r"#/settings/activity$"))
    expect(nav.get_by_role("link", name="Activity", exact=True)).to_have_attribute(
        "aria-current", "true")
    expect(page.get_by_role("heading", name="Activity", exact=True)).to_be_focused()
    expect(settings_card(page, "activity")).to_be_in_viewport()
    nav.get_by_role("link", name="API keys", exact=True).click()
    expect(page.get_by_role("heading", name="API keys", exact=True)).to_be_focused()
    expect(settings_card(page, "keys")).to_be_in_viewport()
    assert page.evaluate("document.querySelector('.settings-page').dataset.marker") == "kept"
    assert requests == []
    # Leaving Settings and coming back mounts a fresh page.
    page.get_by_role("link", name="agenticml / settings", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    page.get_by_role("link", name="Settings", exact=True).click()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
    assert page.evaluate("document.querySelector('.settings-page').dataset.marker") is None


def test_inline_invite_link_is_shown_once_and_errors_show_under_form(page, team_server):
    page.allow_http_errors = True
    sign_in(page, team_server, team_server["admin"])
    members = open_settings(page, "members")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Role", exact=True).select_option("viewer")
    expect(form).to_contain_text("Viewers can read runs and notes.")
    form.get_by_label("Login or email", exact=True).fill("cara")
    form.get_by_role("button", name="Send invite", exact=True).click()
    first = form.locator(".key-value")
    expect(first).to_have_text(re.compile(r"/invite\?token="))
    first_link = first.inner_text()
    expect(page.get_by_role("table", name="Pending invites", exact=True)).to_contain_text("cara")
    form.get_by_label("Login or email", exact=True).fill("dan")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(page.get_by_role("table", name="Pending invites", exact=True)).to_contain_text("dan")
    second_link = form.locator(".key-value").inner_text()
    assert second_link != first_link
    assert first_link not in page.content()
    expect(form.locator(".key-value")).to_have_count(1)
    # A server error shows under the form and wipes the old link.
    page.route("**/api/w/default/invites", lambda route: route.fulfill(
        status=400, json={"error": "invite refused"}) if route.request.method == "POST"
        else route.continue_())
    form.get_by_label("Login or email", exact=True).fill("frank")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.get_by_role("alert")).to_have_text("invite refused")
    page.unroute("**/api/w/default/invites")
    expect(form.locator(".key-value")).to_have_count(0)
    assert second_link not in page.content()
    # Leaving the page wipes a shown link too.
    form.get_by_label("Login or email", exact=True).fill("erin")
    form.get_by_role("button", name="Send invite", exact=True).click()
    third = form.locator(".key-value")
    expect(third).to_have_text(re.compile(r"/invite\?token="))
    third_link = third.inner_text()
    node = third.element_handle()
    page.get_by_role("link", name="agenticml / settings", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert third_link not in page.content()
    # The detached node itself was blanked, not just removed from the page.
    assert node.evaluate("node => node.textContent") == ""


def test_back_inside_settings_closes_key_dialog_and_wipes_secrets(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    open_settings(page, "keys")
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    dialog.get_by_label("Name", exact=True).fill("back-key")
    dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.locator(".key-value")).to_have_text(re.compile(r"aml_[A-Za-z0-9_-]+"))
    value = dialog.locator(".key-value").inner_text()
    node = dialog.locator(".key-value").element_handle()
    with page.expect_response(
            lambda r: r.url.endswith("/api/w/default/keys") and r.request.method == "GET"):
        page.go_back()
    expect(page).to_have_url(re.compile(r"#/settings$"))
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".key-value")).to_have_count(0)
    assert value not in page.content()
    assert node.evaluate("node => node.textContent") == ""
    # The close handler refreshed the table; it must not pull focus or scroll back.
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_focused()
    page.wait_for_timeout(300)
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_focused()
    assert page.evaluate("window.scrollY") == 0


def test_back_inside_settings_with_team_dialog_keeps_focus_and_scroll(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    open_settings(page, "members")
    user_row(page, "alice").get_by_role("button", name="Remove", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(1)
    with page.expect_response(
            lambda r: r.url.endswith("/api/w/default/members") and r.request.method == "GET"):
        page.go_back()
    expect(page).to_have_url(re.compile(r"#/settings$"))
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_focused()
    page.wait_for_timeout(300)
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_focused()
    assert page.evaluate("window.scrollY") == 0
    assert user_row(page, "alice").count() == 1


def test_invite_sending_while_leaving_the_section_shows_nothing_late(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    members = open_settings(page, "members")
    held, release = hold_requests(page, "**/api/w/default/invites", method="POST")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("late")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/w/default/invites")):
        form.get_by_role("button", name="Send invite", exact=True).click()
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    nav.get_by_role("link", name="API keys", exact=True).click()
    expect(page.get_by_role("heading", name="API keys", exact=True)).to_be_focused()
    with page.expect_response(
            lambda r: r.url.endswith("/api/w/default/invites") and r.request.method == "POST"):
        release()
    page.wait_for_timeout(500)
    assert "/invite?token=" not in page.content()
    expect(page.locator(".key-value")).to_have_count(0)
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("heading", name="API keys", exact=True)).to_be_focused()


def test_invite_link_resend_while_leaving_the_section_shows_nothing_late(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    open_settings(page, "members")
    held, release = hold_requests(page, "**/api/w/default/members/alice/invite", method="POST")
    user_row(page, "alice").get_by_role("button", name="Invite link", exact=True).click()
    dialog = page.get_by_role("dialog", name="Invite alice?", exact=True)
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/alice/invite")):
        dialog.get_by_role("button", name="Create invite link", exact=True).click()
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/settings$"))
    expect(page.get_by_role("dialog")).to_have_count(0)
    with page.expect_response(lambda r: r.url.endswith("/alice/invite")):
        release()
    page.wait_for_timeout(500)
    assert "/invite?token=" not in page.content()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".key-value")).to_have_count(0)


def test_back_inside_settings_wipes_inline_invite_link(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    members = open_settings(page, "members")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("dora")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.locator(".key-value")).to_have_text(re.compile(r"/invite\?token="))
    link = form.locator(".key-value").inner_text()
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/settings$"))
    expect(page.locator(".key-value")).to_have_count(0)
    assert link not in page.content()


def test_malformed_section_falls_back_to_account(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    page.goto(f"{team_server['url']}/w/default/#/settings/%E0%A4%A")
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_visible()
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    expect(nav.get_by_role("link", name="Account", exact=True)).to_have_attribute("aria-current", "true")
    nav.get_by_role("link", name="API keys", exact=True).click()
    expect(nav.get_by_role("link", name="API keys", exact=True)).to_have_attribute("aria-current", "true")
    page.goto(f"{team_server['url']}/w/default/#/settings/%zz")
    expect(nav.get_by_role("link", name="Account", exact=True)).to_have_attribute("aria-current", "true")
    expect(nav.get_by_role("link", name="API keys", exact=True)).not_to_have_attribute("aria-current", "true")
    expect(page.get_by_role("heading", name="Your account", exact=True)).to_be_focused()


def test_deep_link_scrolls_after_cards_load_and_marks_nav_at_once(page, team_server):
    # 700px: the top bar wraps (<= 760px) while the tables keep their normal layout.
    page.set_viewport_size({"width": 700, "height": 700})
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    held, release = hold_requests(page, "**/api/w/default/keys")
    with page.expect_request(lambda r: r.url.endswith("/api/w/default/keys")):
        page.goto(f"{team_server['url']}/w/default/#/keys")
    expect(page).to_have_url(re.compile(r"#/settings/keys$"))
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    # Still loading: the nav already marks the section, and nothing has scrolled yet.
    expect(nav.get_by_role("link", name="API keys", exact=True)).to_have_attribute("aria-current", "true")
    assert page.evaluate("window.scrollY") == 0
    release()
    expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()
    for name in ("Users", "Activity"):
        expect(page.get_by_role("table", name=name, exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="API keys", exact=True)).to_be_focused()
    assert_card_at_bar(page, "keys")


def test_deep_link_reveal_does_not_steal_focus_from_a_field_in_use(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    held, release = hold_requests(page, "**/api/w/default/keys")
    with page.expect_request(lambda r: r.url.endswith("/api/w/default/keys")):
        page.goto(f"{team_server['url']}/w/default/#/settings/keys")
    account = settings_card(page, "account")
    account.get_by_text("Change password", exact=True).first.click()
    field = account.get_by_label("New password", exact=True)
    field.click()
    field.press_sequentially("typing")
    release()
    expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()
    page.wait_for_timeout(300)
    expect(field).to_be_focused()
    expect(field).to_have_value("typing")


def test_modified_click_on_current_nav_item_is_left_to_the_browser(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    open_settings(page)
    for name in ("Keys", "Users", "Activity"):
        expect(page.get_by_role("table", name=name, exact=True)).to_be_visible()
    expect(page.locator(".settings-page[data-loaded]")).to_be_attached()
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    link = nav.get_by_role("link", name="Account", exact=True)
    expect(link).to_have_attribute("aria-current", "true")
    page.evaluate("window.scrollTo(0, 400)")
    page.evaluate("document.activeElement.blur()")
    # Record whether the link's own handler cancelled the click, then cancel it here so
    # the test does not open a tab.
    prevented = link.evaluate("""node => {
        let seen = null;
        const probe = (event) => { seen = event.defaultPrevented; event.preventDefault(); };
        document.addEventListener('click', probe, {once: true});
        node.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, button: 0, ctrlKey: true}));
        return seen;
    }""")
    assert prevented is False
    assert page.evaluate("window.scrollY") == 400


def test_nav_click_on_current_section_scrolls_to_it(page, team_server):
    page.set_viewport_size({"width": 1280, "height": 500})
    sign_in(page, team_server, team_server["admin"])
    open_settings(page, "activity")
    for name in ("Keys", "Users", "Activity"):
        expect(page.get_by_role("table", name=name, exact=True)).to_be_visible()
    # The page scrolls once more when every card has loaded; wait for that to finish.
    expect(page.locator(".settings-page[data-loaded]")).to_be_attached()
    page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
    # Precondition: the Activity card is off screen and nothing is focused in it.
    expect(settings_card(page, "activity")).not_to_be_in_viewport()
    expect(page.get_by_role("heading", name="Activity", exact=True)).not_to_be_focused()
    nav = page.get_by_role("navigation", name="Settings", exact=True)
    nav.get_by_role("link", name="Activity", exact=True).click()
    expect(page.get_by_role("heading", name="Activity", exact=True)).to_be_focused()
    expect(settings_card(page, "activity")).to_be_in_viewport()
    assert_card_at_bar(page, "activity")


def test_invite_of_existing_account_announces_and_keeps_focus(page, team_server):
    sign_in(page, team_server, team_server["admin"])
    httpd = team_server["httpd"]
    other = WorkspaceRepo(httpd.db).create("other", name="Other", owner="admin")
    prepare_store(httpd.data_dir, other["id"])
    UserStore(httpd.data_dir / "server", db=httpd.db, workspace_id=other["id"]).add("bob", "member")
    members = open_settings(page, "members")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("bob")
    form.get_by_role("button", name="Send invite", exact=True).click()
    status = form.get_by_role("status").filter(
        has_text="This login already has an account and was added to the workspace.")
    expect(status).to_have_attribute("aria-live", "polite")
    expect(form.get_by_label("Login or email", exact=True)).to_be_focused()


def test_first_password_adds_current_password_field(page, team_server):
    httpd = team_server["httpd"]
    httpd.users.add("newbie", role="admin")
    _, key = httpd.keys.create("newbie-admin", scope="admin", user="newbie")
    sign_in(page, team_server, key)
    # The password endpoint is stubbed; this checks the form, not the server.
    page.route("**/api/account/password", lambda route: route.fulfill(status=200, json={}))
    card = open_settings(page)
    card.get_by_text("Change password", exact=True).first.click()
    expect(card.get_by_label("Current password", exact=True)).to_have_count(0)
    card.get_by_label("New password", exact=True).fill("newpassword12")
    card.get_by_label("Confirm password", exact=True).fill("newpassword12")
    card.get_by_role("button", name="Change password", exact=True).click()
    expect(card.get_by_role("status")).to_have_text("Password updated")
    expect(card.get_by_label("Current password", exact=True)).to_be_visible()
