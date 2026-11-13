"""Browser coverage for the Firebase sign-in page (package 18b.2)."""
import json
import re
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.server.firebase import CertificateSource, FirebaseConfig

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import assert_no_overflow, browser, context, page

pytestmark = [pytest.mark.browser, pytest.mark.ui]

NO_ACCOUNT = (
    "Your account has no access to this server. Ask a workspace admin for an invite.")
VERIFY_EMAIL = "Check your inbox to verify your email, then sign in again."
POPUP_BLOCKED_INVITE = (
    "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.")
WRONG_INVITE = "this invite is for a different or unverified email"
TOO_MANY = "too many failed logins; try again later"

FAKE_FIREBASE = """
(function (global) {
  var opts = global.__FAKE_FIREBASE__ || {};
  var calls = global.__FAKE_FIREBASE_CALLS__ = {
    signInWithPopup: 0,
    signInWithRedirect: 0,
    signInWithEmailAndPassword: [],
    createUserWithEmailAndPassword: [],
    sendPasswordResetEmail: [],
    sendEmailVerification: 0,
    signOut: 0,
    getRedirectResult: 0
  };
  function user() {
    return {
      getIdToken: function () { return Promise.resolve("fake-id-token"); },
      sendEmailVerification: function () {
        calls.sendEmailVerification += 1;
        return Promise.resolve();
      }
    };
  }
  var currentUser = null;
  function auth() {
    return {
      setPersistence: function () { return Promise.resolve(); },
      signInWithPopup: function () {
        calls.signInWithPopup += 1;
        if (opts.popupBlocked) {
          var blocked = new Error("popup blocked");
          blocked.code = "auth/popup-blocked";
          return Promise.reject(blocked);
        }
        currentUser = user();
        return Promise.resolve({ user: currentUser });
      },
      signInWithRedirect: function () {
        calls.signInWithRedirect += 1;
        return Promise.resolve();
      },
      signInWithEmailAndPassword: function (email, password) {
        calls.signInWithEmailAndPassword.push([email, password]);
        currentUser = user();
        return Promise.resolve({ user: currentUser });
      },
      createUserWithEmailAndPassword: function (email, password) {
        calls.createUserWithEmailAndPassword.push([email, password]);
        currentUser = user();
        return Promise.resolve({ user: currentUser });
      },
      sendPasswordResetEmail: function (email) {
        calls.sendPasswordResetEmail.push(email);
        return Promise.resolve();
      },
      getRedirectResult: function () {
        calls.getRedirectResult += 1;
        if (opts.redirectUser) {
          currentUser = user();
          return Promise.resolve({ user: currentUser });
        }
        return Promise.resolve({ user: null });
      },
      signOut: function () {
        calls.signOut += 1;
        currentUser = null;
        return Promise.resolve();
      },
      get currentUser() { return currentUser; }
    };
  }
  auth.GoogleAuthProvider = function () {};
  auth.Auth = { Persistence: { NONE: "NONE" } };
  global.firebase = {
    initializeApp: function () {},
    auth: auth
  };
})(typeof globalThis !== "undefined" ? globalThis : window);
"""


class _NoCerts:
    status_code = 200
    headers = {"Cache-Control": "max-age=3600"}

    def json(self):
        return {}


@pytest.fixture
def firebase_ui(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(
        tmp_path / "data", port=0,
        firebase=FirebaseConfig(
            project="demo-project", api_key="test-api-key-1",
            auth_domain="demo-project.firebaseapp.com"),
        cert_source=CertificateSource(fetch=lambda: _NoCerts()),
    )
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "httpd": httpd}
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def stub_vendor(page):
    page.route(
        "**/static/vendor/firebase/*.js",
        lambda route: route.fulfill(
            status=200, content_type="text/javascript; charset=utf-8", body=FAKE_FIREBASE),
    )


def stub_auth(page, handler):
    page.route(re.compile(r"/auth/firebase$"), handler)


def fake_opts(page, **opts):
    page.add_init_script(f"window.__FAKE_FIREBASE__ = {json.dumps(opts)};")


def fake_calls(page):
    return page.evaluate("window.__FAKE_FIREBASE_CALLS__")


def fulfill_home(page, url):
    page.route(
        re.compile(re.escape(url) + r"/?$"),
        lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body="<!doctype html><html><body><h1>Home</h1></body></html>")
        if route.request.method in ("GET", "HEAD") else route.continue_(),
    )


def json_auth(status, payload):
    def on_auth(route):
        route.fulfill(
            status=status, content_type="application/json",
            body=json.dumps(payload))
    return on_auth


def test_firebase_sign_in_success_navigates_home(page, firebase_ui):
    posted = []

    def on_auth(route):
        posted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"user":"ada@example.com"}')

    stub_vendor(page)
    stub_auth(page, on_auth)
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("heading", name="Sign in to your workspace", exact=True)).to_be_visible()
    fulfill_home(page, firebase_ui["url"])
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page).to_have_url(re.compile(r"/$"))
    expect(page.get_by_role("heading", name="Home", exact=True)).to_be_visible()
    assert posted and posted[0]["id_token"] == "fake-id-token"
    assert "invite" not in posted[0]
    assert "fake-id-token" not in page.url
    assert "id_token" not in page.url


def test_firebase_no_account_shows_message(page, firebase_ui):
    page.allow_http_errors = True

    stub_vendor(page)
    stub_auth(page, json_auth(403, {"error": "no access", "reason": "no_account"}))
    page.goto(f"{firebase_ui['url']}/login")
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    expect(page.get_by_role("button", name="Continue without the invite", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Resend email", exact=True)).to_have_count(0)
    expect(page).to_have_url(re.compile(r"/login$"))


def test_firebase_redirect_result_still_binds_google(page, firebase_ui):
    page.allow_http_errors = True
    posted = []

    def on_auth(route):
        posted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=403, content_type="application/json",
            body=json.dumps({"error": "no access", "reason": "no_account"}))

    fake_opts(page, redirectUser=True)
    stub_vendor(page)
    stub_auth(page, on_auth)
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    assert len(posted) == 1
    with page.expect_request(lambda req: req.url.endswith("/auth/firebase") and req.method == "POST"):
        page.get_by_role("button", name="Continue with Google", exact=True).click()
    assert len(posted) == 2
    assert posted[1]["id_token"] == "fake-id-token"


def test_firebase_invite_invalid_reposts_without_invite(page, firebase_ui):
    page.allow_http_errors = True
    posted = []

    def on_auth(route):
        payload = json.loads(route.request.post_data or "{}")
        posted.append(payload)
        if payload.get("invite"):
            route.fulfill(
                status=403, content_type="application/json",
                body=json.dumps({
                    "error": "this invite is no longer valid",
                    "reason": "invite_invalid",
                }))
            return
        route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"user":"ada@example.com"}')

    stub_vendor(page)
    stub_auth(page, on_auth)
    page.goto(f"{firebase_ui['url']}/invite?token=not-a-real-invite")
    expect(page.get_by_role("heading", name="Sign in to your workspace", exact=True)).to_be_visible()
    assert "token=" not in page.url
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("this invite is no longer valid")
    fulfill_home(page, firebase_ui["url"])
    page.get_by_role("button", name="Continue without the invite", exact=True).click()
    expect(page).to_have_url(re.compile(r"/$"))
    assert len(posted) == 2
    assert posted[0]["invite"] == "not-a-real-invite"
    assert "invite" not in posted[1]
    assert posted[1]["id_token"] == "fake-id-token"


def test_firebase_wrong_invite_shows_invite_text(page, firebase_ui):
    page.allow_http_errors = True
    stub_vendor(page)
    stub_auth(page, json_auth(403, {"error": WRONG_INVITE}))
    page.goto(f"{firebase_ui['url']}/invite?token=wrong-invite")
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(WRONG_INVITE)


def test_firebase_email_unverified_resend(page, firebase_ui):
    page.allow_http_errors = True
    stub_vendor(page)
    stub_auth(page, json_auth(403, {
        "error": "verify your email", "reason": "email_unverified"}))
    page.goto(f"{firebase_ui['url']}/login")
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(VERIFY_EMAIL)
    expect(page.get_by_role("button", name="Resend email", exact=True)).to_be_visible()
    page.get_by_role("button", name="Resend email", exact=True).click()
    page.wait_for_function("window.__FAKE_FIREBASE_CALLS__.sendEmailVerification >= 1")
    assert fake_calls(page)["sendEmailVerification"] >= 1


def test_firebase_create_account_sends_verification(page, firebase_ui):
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    page.get_by_role("tab", name="Sign up", exact=True).click()
    page.locator("#email").fill("ada@example.com")
    page.locator("#password").fill("password12")
    page.get_by_role("button", name="Sign up", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(VERIFY_EMAIL)
    calls = fake_calls(page)
    assert calls["createUserWithEmailAndPassword"] == [["ada@example.com", "password12"]]
    assert calls["sendEmailVerification"] >= 1


def test_firebase_forgot_password_sends_reset(page, firebase_ui):
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    page.locator("#email").fill("ada@example.com")
    page.get_by_role("button", name="Forgot password?", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(
        "Check your inbox for a password reset email.")
    assert fake_calls(page)["sendPasswordResetEmail"] == ["ada@example.com"]


def test_firebase_popup_blocked_on_login_uses_redirect(page, firebase_ui):
    fake_opts(page, popupBlocked=True)
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    page.wait_for_function("window.__FAKE_FIREBASE_CALLS__.signInWithRedirect >= 1")
    assert fake_calls(page)["signInWithRedirect"] >= 1
    expect(page).to_have_url(re.compile(r"/login$"))


def test_firebase_popup_blocked_on_invite_does_not_redirect(page, firebase_ui):
    fake_opts(page, popupBlocked=True)
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/invite?token=kept-in-memory")
    assert "token=" not in page.url
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(POPUP_BLOCKED_INVITE)
    assert fake_calls(page)["signInWithRedirect"] == 0
    expect(page).to_have_url(re.compile(r"/invite$"))


def test_firebase_429_shows_server_text(page, firebase_ui):
    page.allow_http_errors = True
    stub_vendor(page)
    stub_auth(page, json_auth(429, {"error": TOO_MANY}))
    page.goto(f"{firebase_ui['url']}/login")
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(TOO_MANY)


def test_firebase_sign_in_no_overflow_at_375(page, firebase_ui):
    stub_vendor(page)
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("heading", name="Sign in to your workspace", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()
    assert_no_overflow(page)
    longest = max(
        (NO_ACCOUNT, VERIFY_EMAIL, POPUP_BLOCKED_INVITE, WRONG_INVITE, TOO_MANY),
        key=len,
    )
    page.evaluate(
        """(message) => {
          const error = document.querySelector("#error");
          error.hidden = false;
          error.textContent = message;
          document.querySelector("#resend").hidden = false;
          document.querySelector("#continue-without-invite").hidden = false;
        }""",
        longest,
    )
    expect(page.get_by_role("alert")).to_have_text(longest)
    expect(page.get_by_role("button", name="Resend email", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Continue without the invite", exact=True)).to_be_visible()
    assert_no_overflow(page)


SIGN_IN_TITLE = "Sign in to your workspace"
SIGN_IN_SUB = "Pick up your runs and comparisons where you left them."
SIGN_UP_TITLE = "Create your workspace"
SIGN_UP_SUB = "Start logging experiments in minutes."


def test_firebase_tabs_switch_sign_in_and_sign_up(page, firebase_ui):
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    sign_in_tab = page.get_by_role("tab", name="Sign in", exact=True)
    sign_up_tab = page.get_by_role("tab", name="Sign up", exact=True)
    submit = page.locator("#email-login button[type=submit]")
    forgot = page.get_by_role("button", name="Forgot password?", exact=True)
    expect(page.get_by_role("heading", name=SIGN_IN_TITLE, exact=True)).to_be_visible()
    expect(page.locator("#auth-sub")).to_have_text(SIGN_IN_SUB)
    expect(sign_in_tab).to_have_attribute("aria-selected", "true")
    expect(sign_up_tab).to_have_attribute("aria-selected", "false")
    expect(submit).to_have_text("Sign in")
    expect(forgot).to_be_visible()
    expect(page.locator("#password")).to_have_attribute("autocomplete", "current-password")

    sign_up_tab.click()
    expect(page.get_by_role("heading", name=SIGN_UP_TITLE, exact=True)).to_be_visible()
    expect(page.locator("#auth-sub")).to_have_text(SIGN_UP_SUB)
    expect(sign_in_tab).to_have_attribute("aria-selected", "false")
    expect(sign_up_tab).to_have_attribute("aria-selected", "true")
    expect(submit).to_have_text("Sign up")
    expect(forgot).to_be_hidden()
    expect(page.locator("#password")).to_have_attribute("autocomplete", "new-password")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()

    sign_in_tab.click()
    expect(page.get_by_role("heading", name=SIGN_IN_TITLE, exact=True)).to_be_visible()
    expect(page.locator("#auth-sub")).to_have_text(SIGN_IN_SUB)
    expect(sign_in_tab).to_have_attribute("aria-selected", "true")
    expect(submit).to_have_text("Sign in")
    expect(forgot).to_be_visible()


def test_firebase_tabs_switch_with_arrow_keys(page, firebase_ui):
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    sign_in_tab = page.get_by_role("tab", name="Sign in", exact=True)
    sign_up_tab = page.get_by_role("tab", name="Sign up", exact=True)
    sign_in_tab.focus()
    page.keyboard.press("ArrowRight")
    expect(sign_up_tab).to_have_attribute("aria-selected", "true")
    expect(sign_up_tab).to_be_focused()
    expect(sign_in_tab).to_have_attribute("tabindex", "-1")
    expect(page.get_by_role("heading", name=SIGN_UP_TITLE, exact=True)).to_be_visible()
    page.keyboard.press("ArrowLeft")
    expect(sign_in_tab).to_have_attribute("aria-selected", "true")
    expect(sign_in_tab).to_be_focused()
    expect(sign_up_tab).to_have_attribute("tabindex", "-1")
    expect(page.get_by_role("heading", name=SIGN_IN_TITLE, exact=True)).to_be_visible()


def test_firebase_tabs_and_show_password_work_without_firebase_config(page, firebase_ui):
    page.allow_http_errors = True
    page.route(
        re.compile(r"/auth/firebase/config$"),
        lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"off"}'))
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("alert")).to_be_visible()
    page.get_by_role("tab", name="Sign up", exact=True).click()
    expect(page.get_by_role("heading", name=SIGN_UP_TITLE, exact=True)).to_be_visible()
    page.get_by_role("button", name="show password", exact=True).click()
    expect(page.locator("#password")).to_have_attribute("type", "text")


def test_firebase_enter_without_firebase_config_does_not_submit_natively(page, firebase_ui):
    page.allow_http_errors = True
    page.route(
        re.compile(r"/auth/firebase/config$"),
        lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"off"}'))
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("alert")).to_be_visible()
    page.locator("#email").fill("ada@example.com")
    page.locator("#password").fill("password12")
    page.locator("#password").press("Enter")
    page.wait_for_timeout(300)
    assert "password=" not in page.url
    assert "?" not in page.url
    assert page.url.endswith("/login")


def test_firebase_show_password_toggles_input_type(page, firebase_ui):
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    password = page.locator("#password")
    toggle = page.locator("#show-password")
    expect(password).to_have_attribute("type", "password")
    expect(toggle).to_have_text("show password")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    expect(password).to_have_attribute("type", "text")
    expect(toggle).to_have_text("hide password")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    toggle.click()
    expect(password).to_have_attribute("type", "password")
    expect(toggle).to_have_text("show password")
    expect(toggle).to_have_attribute("aria-pressed", "false")


def test_firebase_sign_in_layout_wide_and_narrow(page, firebase_ui):
    stub_vendor(page)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.locator(".auth-brand")).to_be_visible()
    expect(page.locator(".auth-brand .auth-logo")).to_be_visible()
    expect(page.locator(".auth-compact-logo")).to_be_hidden()
    assert_no_overflow(page)
    page.set_viewport_size({"width": 375, "height": 812})
    expect(page.locator(".auth-brand")).to_be_hidden()
    expect(page.locator(".auth-compact-logo .auth-logo")).to_be_visible()
    assert_no_overflow(page)
