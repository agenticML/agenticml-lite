"""Browser coverage for OIDC sign-in (package 36.1): /login, /invite, callback pages."""
from pathlib import Path
import re
import sys

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

# The fake provider and the server helper live with the unit tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from oidc_fake import FakeProvider, oidc_server  # noqa: E402
from test_server_dashboard import (  # noqa: E402,F401
    assert_no_overflow, browser, context, open_settings, page)

pytestmark = [pytest.mark.browser, pytest.mark.ui]

PHONE = {"width": 375, "height": 800}


@pytest.fixture
def provider():
    fake = FakeProvider()
    yield fake
    fake.close()


@pytest.fixture
def oidc_ui(tmp_path, monkeypatch, provider):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    logout = provider.base + "/logout"
    with oidc_server(tmp_path / "data", provider, open_signup=True, logout_url=logout,
                     label="Sign in with Acme SSO") as httpd:
        yield {"url": provider.public_url, "httpd": httpd, "provider": provider}


@pytest.fixture
def cross_site_ui(tmp_path, monkeypatch):
    """The app on 127.0.0.1 and the provider on localhost: different sites for SameSite."""
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    fake = FakeProvider(host="localhost")
    try:
        with oidc_server(tmp_path / "data", fake, open_signup=True,
                         logout_url=fake.base + "/logout", label="Sign in with Acme SSO") as httpd:
            yield {"url": fake.public_url, "httpd": httpd, "provider": fake}
    finally:
        fake.close()


def sign_in_button(page):
    return page.get_by_role("button", name="Sign in with Acme SSO", exact=True)


def button_colors(page):
    return page.eval_on_selector(
        "#signin", "(el) => { const s = getComputedStyle(el); return [s.color, s.backgroundColor]; }")


def test_login_page_phone_layout_and_button(page, oidc_ui):
    page.set_viewport_size(PHONE)
    page.goto(oidc_ui["url"] + "/login")
    button = sign_in_button(page)
    expect(button).to_be_visible()
    expect(page.locator("#signin-form")).to_have_attribute("action", "/auth/oidc/start")
    expect(page.locator("#signin-form")).to_have_attribute("method", "get")
    expect(page.locator("#error")).to_be_hidden()
    expect(page.locator("#invite-note")).to_be_hidden()
    assert_no_overflow(page)
    # Dark and light both lay out the same.
    page.emulate_media(color_scheme="dark")
    assert_no_overflow(page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_button_text_stays_readable_on_hover(page, oidc_ui, scheme):
    page.emulate_media(color_scheme=scheme)
    page.goto(oidc_ui["url"] + "/login")
    button = sign_in_button(page)
    expect(button).to_be_visible()
    resting = button_colors(page)
    button.hover()
    page.wait_for_timeout(400)               # past the colour transition
    color, background = button_colors(page)
    assert color != background, f"text and background are both {color} on hover ({scheme})"
    assert color == resting[0], "the text colour must not change on hover"


def test_invite_page_posts_the_token_and_keeps_it_out_of_the_address(page, oidc_ui):
    page.set_viewport_size(PHONE)
    token = "T" * 43
    page.goto(oidc_ui["url"] + "/invite#token=" + token)
    expect(page.locator("#invite-note")).to_be_visible()
    expect(page.locator("#signin-form")).to_have_attribute("method", "post")
    expect(page.locator("input[name=invite]")).to_have_value(token)
    assert page.url == oidc_ui["url"] + "/invite"
    assert_no_overflow(page)


def test_invite_sign_in_never_puts_the_token_in_a_request_line(page, oidc_ui):
    from agenticml.server.db.workspaces import AccountRepo, InviteRepo, MembershipRepo

    httpd = oidc_ui["httpd"]
    token = InviteRepo(httpd.db, httpd.default_workspace_id).create(
        "ada@example.com", "member", "admin")
    seen = []
    page.on("request", lambda request: seen.append((request.method, request.url)))
    page.goto(oidc_ui["url"] + "/invite#token=" + token)
    sign_in_button(page).click()
    page.wait_for_url(lambda url: "/invite" not in url and "/auth/oidc" not in url)
    assert any(method == "POST" and url.endswith("/auth/oidc/start") for method, url in seen)
    assert all(token not in url for _, url in seen), seen
    assert InviteRepo.find_pending(httpd.db, token) is None
    account = AccountRepo(httpd.db).get("ada@example.com")
    assert MembershipRepo(httpd.db, httpd.default_workspace_id).get(account["id"]) is not None


def test_failed_invite_sign_in_keeps_the_invite_for_try_again(page, oidc_ui):
    from agenticml.server.db.workspaces import InviteRepo

    page.allow_http_errors = True
    page.set_viewport_size(PHONE)
    httpd = oidc_ui["httpd"]
    token = InviteRepo(httpd.db, httpd.default_workspace_id).create(
        "someone-else@example.com", "member", "admin")
    page.goto(oidc_ui["url"] + "/invite#token=" + token)
    sign_in_button(page).click()             # the provider signs in as ada: the wrong email
    expect(page.locator("#error")).to_contain_text("different or unverified email")
    expect(page).to_have_url(oidc_ui["url"] + "/login")
    expect(page.locator("#signin-form")).to_have_attribute("method", "post")
    expect(page.locator("input[name=invite]")).to_have_value(token)
    expect(page.locator("#invite-note")).to_be_visible()
    assert_no_overflow(page)


def test_error_and_unavailable_pages_phone_layout(page, oidc_ui):
    page.allow_http_errors = True
    page.set_viewport_size(PHONE)
    page.goto(oidc_ui["url"] + "/auth/oidc/callback?error=access_denied&state=x&code=secret")
    expect(page.locator("#error")).to_contain_text("Sign-in was cancelled or refused")
    expect(sign_in_button(page)).to_be_visible()
    # The provider's answer is scrubbed from the address bar.
    expect(page).to_have_url(oidc_ui["url"] + "/login")
    assert_no_overflow(page)
    oidc_ui["provider"].fail = {"discovery": 503}
    page.goto(oidc_ui["url"] + "/auth/oidc/start")
    expect(page.locator("#error")).to_contain_text("temporarily unavailable")
    assert_no_overflow(page)


def test_full_sign_in_then_sign_out_through_the_provider(page, oidc_ui):
    page.set_viewport_size(PHONE)
    page.goto(oidc_ui["url"] + "/login")
    sign_in_button(page).click()
    # start -> provider authorize (auto-approve) -> callback -> redirect page -> dashboard.
    page.wait_for_url(lambda url: "/login" not in url and "/auth/oidc" not in url)
    assert page.url.startswith(oidc_ui["url"])
    session = page.evaluate(
        "fetch('/api/account/session', {credentials: 'same-origin'}).then((r) => r.json())")
    assert session["auth"] == "oidc" and session["user"] == "ada@example.com"
    assert_no_overflow(page)
    page.get_by_role("button", name="Sign out", exact=True).first.click()
    # Sign-out goes through the provider's logout, then back to /login.
    expect(page).to_have_url(oidc_ui["url"] + "/login")
    expect(sign_in_button(page)).to_be_visible()
    kinds = [kind for kind, _ in oidc_ui["provider"].requests]
    assert "logout" in kinds
    assert_no_overflow(page)


def test_sign_in_returning_from_another_site(page, cross_site_ui):
    """The provider is on localhost, the app on 127.0.0.1: different sites. The binding
    cookie (Lax) must arrive on the cross-site callback, the 200 page must set the Strict
    session cookie, and its meta refresh must land on a signed-in dashboard."""
    app = cross_site_ui["url"]
    assert cross_site_ui["provider"].base.startswith("http://localhost:")
    assert app.startswith("http://127.0.0.1:")
    seen = []
    page.on("request", lambda request: seen.append(request))
    page.goto(app + "/login")
    sign_in_button(page).click()
    page.wait_for_url(lambda url: "/login" not in url and "/auth/oidc" not in url)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    callbacks = [r for r in seen if "/auth/oidc/callback" in r.url]
    assert len(callbacks) == 1
    headers = callbacks[0].all_headers()
    assert headers["sec-fetch-site"] == "cross-site", headers
    assert re.search(r"aml_oidc_[0-7]=", headers.get("cookie", "")), headers
    # The meta refresh to "/" is a navigation started by our own page, so it carried the
    # Strict session cookie (the reason the callback answers with a page, not a 302).
    landing = [r for r in seen if r.url.rstrip("/") == app and r.is_navigation_request()]
    assert landing, [r.url for r in seen]
    landing_headers = landing[-1].all_headers()
    assert "aml_session=" in landing_headers.get("cookie", ""), landing_headers
    cookies = {c["name"]: c for c in page.context.cookies()}
    assert cookies["aml_session"]["sameSite"] == "Strict"
    assert not [name for name in cookies if name.startswith("aml_oidc_")]   # cleared
    session = page.evaluate(
        "fetch('/api/account/session', {credentials: 'same-origin'}).then((r) => r.json())")
    assert session["auth"] == "oidc" and session["user"] == "ada@example.com"


def test_team_page_uses_oidc_copy_and_has_no_reinvite(page, oidc_ui):
    httpd = oidc_ui["httpd"]
    httpd.users.add("admin", role="admin")
    httpd.users.add("alice", role="member")
    # No password or key sign-in with OIDC: mint the session directly.
    token, _ = httpd.web._session_cookie_for("admin", "oidc")
    page.context.add_cookies([{
        "name": httpd.web._cookie_name(), "value": token, "url": oidc_ui["url"]}])
    page.goto(oidc_ui["url"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    members = open_settings(page, "members")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("cara@example.com")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.get_by_text(
        "This invite link is shown once. They sign in with your identity provider, "
        "using this email; the email must be verified.")).to_be_visible()
    link = form.locator(".key-value").inner_text()
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/invite#token=[A-Za-z0-9_-]+", link), link
    # Package 24.11c on OIDC: members sign in at /login, so no per-member "Invite link".
    alice = page.get_by_role("row").filter(has_text="alice")
    expect(alice.get_by_role("button", name="Remove", exact=True)).to_be_visible()
    expect(alice.get_by_role("button", name="Invite link", exact=True)).to_have_count(0)
    page.set_viewport_size(PHONE)
    assert_no_overflow(page)
