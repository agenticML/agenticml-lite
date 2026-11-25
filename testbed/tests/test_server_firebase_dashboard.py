"""Browser coverage for the Firebase sign-in page (package 18b.2)."""
import json
import re
import threading
import time

import pytest
from agenticml.server.app import make_server
from agenticml.server.firebase import CertificateSource, FirebaseConfig

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import assert_no_overflow, browser, context, open_settings, page

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
  // Both vendor URLs are fulfilled with this body, so it runs twice: every
  // global is created only once.
  var opts = global.__FAKE_FIREBASE__ || {};
  // opts.errors maps an SDK method to the error code it rejects with ("" is an
  // error without a code); opts.hold lists methods whose promise stays pending
  // until the test calls window.__FAKE_RELEASE__[method]().
  var errors = opts.errors || {};
  var held = opts.hold || [];
  var calls = global.__FAKE_FIREBASE_CALLS__ = global.__FAKE_FIREBASE_CALLS__ || {
    signInWithPopup: 0,
    signInWithRedirect: 0,
    signInWithEmailAndPassword: [],
    createUserWithEmailAndPassword: [],
    sendPasswordResetEmail: [],
    sendEmailVerification: 0,
    signOut: 0,
    getRedirectResult: 0,
    getIdToken: 0,
    setPersistence: [],
    initializeApp: [],
    order: []
  };
  var release = global.__FAKE_RELEASE__ = global.__FAKE_RELEASE__ || {};
  function outcome(name, make) {
    if (Object.prototype.hasOwnProperty.call(errors, name)) {
      var code = errors[name];
      var err = new Error(code || "fake error");
      if (code) err.code = code;
      return Promise.reject(err);
    }
    return Promise.resolve(make ? make() : undefined);
  }
  function settle(name, make) {
    if (held.indexOf(name) >= 0) {
      return new Promise(function (resolve) {
        release[name] = function () { resolve(outcome(name, make)); };
      });
    }
    return outcome(name, make);
  }
  function user() {
    return {
      getIdToken: function () {
        calls.getIdToken += 1;
        calls.order.push("getIdToken");
        return settle("getIdToken", function () { return "fake-id-token"; });
      },
      sendEmailVerification: function () {
        calls.sendEmailVerification += 1;
        calls.order.push("sendEmailVerification");
        return settle("sendEmailVerification");
      }
    };
  }
  var currentUser = null;
  function signedIn() {
    currentUser = user();
    return { user: currentUser };
  }
  function auth() {
    return {
      setPersistence: function (persistence) {
        calls.setPersistence.push(persistence);
        calls.order.push("setPersistence");
        return settle("setPersistence");
      },
      signInWithPopup: function () {
        calls.signInWithPopup += 1;
        calls.order.push("signInWithPopup");
        if (opts.popupBlocked) {
          var blocked = new Error("popup blocked");
          blocked.code = "auth/popup-blocked";
          return Promise.reject(blocked);
        }
        return settle("signInWithPopup", signedIn);
      },
      signInWithRedirect: function () {
        calls.signInWithRedirect += 1;
        calls.order.push("signInWithRedirect");
        return Promise.resolve();
      },
      signInWithEmailAndPassword: function (email, password) {
        calls.signInWithEmailAndPassword.push([email, password]);
        calls.order.push("signInWithEmailAndPassword");
        return settle("signInWithEmailAndPassword", signedIn);
      },
      createUserWithEmailAndPassword: function (email, password) {
        calls.createUserWithEmailAndPassword.push([email, password]);
        calls.order.push("createUserWithEmailAndPassword");
        return settle("createUserWithEmailAndPassword", signedIn);
      },
      sendPasswordResetEmail: function (email) {
        calls.sendPasswordResetEmail.push(email);
        calls.order.push("sendPasswordResetEmail");
        return settle("sendPasswordResetEmail");
      },
      getRedirectResult: function () {
        calls.getRedirectResult += 1;
        calls.order.push("getRedirectResult");
        if (opts.redirectUser) {
          return Promise.resolve(signedIn());
        }
        return Promise.resolve({ user: null });
      },
      signOut: function () {
        calls.signOut += 1;
        calls.order.push("signOut");
        // A page that navigates loses __FAKE_FIREBASE_CALLS__; an exposed
        // function lives across navigations.
        if (typeof global.__recordSignOut === "function") global.__recordSignOut();
        currentUser = null;
        return Promise.resolve();
      },
      get currentUser() { return currentUser; }
    };
  }
  auth.GoogleAuthProvider = function () {};
  auth.Auth = { Persistence: { NONE: "NONE" } };
  global.firebase = {
    initializeApp: function (config) { calls.initializeApp.push(config); },
    auth: auth
  };
})(typeof globalThis !== "undefined" ? globalThis : window);
"""


class _NoCerts:
    status_code = 200
    headers = {"Cache-Control": "max-age=3600"}

    def json(self):
        return {}


def _serve_firebase(tmp_path, **extra):
    httpd = make_server(
        tmp_path / "data", port=0,
        firebase=FirebaseConfig(
            project="demo-project", api_key="test-api-key-1",
            auth_domain="demo-project.firebaseapp.com"),
        cert_source=CertificateSource(fetch=lambda: _NoCerts()),
        **extra,
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


@pytest.fixture
def firebase_ui(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    yield from _serve_firebase(tmp_path)


LEGAL_BASE = "https://docs.example.org/legal"
LEGAL_LINKS = {
    "Terms": f"{LEGAL_BASE}/terms/",
    "Acceptable use policy": f"{LEGAL_BASE}/acceptable-use/",
    "Privacy policy": f"{LEGAL_BASE}/privacy/",
}
LEGAL_TEXT = (
    "By continuing you agree to the Terms and the Acceptable use policy. "
    "See the Privacy policy.")


@pytest.fixture
def firebase_ui_legal(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    yield from _serve_firebase(tmp_path, legal_base_url=LEGAL_BASE)


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


def test_team_page_uses_firebase_copy(page, firebase_ui):
    httpd = firebase_ui["httpd"]
    httpd.users.add("admin", role="admin")
    httpd.users.add("alice", role="member")
    stub_vendor(page)
    # Password and key sign-in are off with Firebase: mint the session directly.
    token, _ = httpd.web._session_cookie_for("admin", "firebase")
    page.context.add_cookies([{
        "name": httpd.web._cookie_name(), "value": token, "url": firebase_ui["url"]}])
    page.goto(firebase_ui["url"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    members = open_settings(page, "members")
    form = members.get_by_role("form", name="Invite someone", exact=True)
    form.get_by_label("Login or email", exact=True).fill("cara@example.com")
    form.get_by_role("button", name="Send invite", exact=True).click()
    expect(form.get_by_text(
        "This invite link is shown once. They sign in with Google or email and password, "
        "using this email; the email must be verified.")).to_be_visible()
    expect(form).not_to_contain_text("choose their own password")
    # Package 30.4: the hosted link keeps its token in the fragment, out of ALB logs.
    link = form.locator(".key-value").inner_text()
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/invite#token=[A-Za-z0-9_-]+", link), link
    # Package 24.11c: Firebase members sign in at /login, so there is no
    # per-member "Invite link" (the API answers 409); Remove stays.
    alice = page.get_by_role("row").filter(has_text="alice")
    expect(alice.get_by_role("button", name="Remove", exact=True)).to_be_visible()
    expect(alice.get_by_role("button", name="Invite link", exact=True)).to_have_count(0)
    page.set_viewport_size({"width": 375, "height": 800})
    assert_no_overflow(page)


def test_admin_adds_new_email_as_pending_server_admin(page, firebase_ui):
    from agenticml.server.db.workspaces import AccountRepo

    httpd = firebase_ui["httpd"]
    httpd.users.add("admin", role="admin")
    AccountRepo(httpd.db).set_server_admin("admin", True)
    stub_vendor(page)
    token, _ = httpd.web._session_cookie_for("admin", "firebase")
    page.context.add_cookies([{
        "name": httpd.web._cookie_name(), "value": token, "url": firebase_ui["url"]}])
    page.goto(f"{firebase_ui['url']}/admin/")
    expect(page.get_by_role("heading", name="Server admins", exact=True)).to_be_visible()
    page.get_by_role("button", name="Add server admin", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add server admin", exact=True)
    expect(dialog).to_contain_text(
        "If no account exists for this email, one is created; it is linked when they first sign in.")
    dialog.get_by_label("Email or login", exact=True).fill("fresh@example.com")
    dialog.get_by_role("button", name="Add", exact=True).click()
    table = page.get_by_role("table", name="Server admins", exact=True)
    row = table.get_by_role("row").filter(has=page.locator('td[data-label="Login"]').filter(
        has_text=re.compile(r"^fresh@example\.com$")))
    expect(row).to_be_visible()
    expect(row.locator('td[data-label="Sign-in"]')).to_have_text("pending")
    expect(row.locator('td[data-label="Source"]')).to_have_text("manual")
    expect(page.get_by_role("status").filter(has_text="Created an account for fresh@example.com")).to_be_visible()


def test_email_admin_creates_key_and_changes_role(page, firebase_ui):
    httpd = firebase_ui["httpd"]
    httpd.users.add("boss@example.com", role="admin")
    httpd.users.add("bob@example.com", role="member")
    stub_vendor(page)
    token, _ = httpd.web._session_cookie_for("boss@example.com", "firebase")
    page.context.add_cookies([{
        "name": httpd.web._cookie_name(), "value": token, "url": firebase_ui["url"]}])
    page.goto(firebase_ui["url"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    open_settings(page, "keys")
    expect(page.get_by_role("table", name="Keys", exact=True)).to_be_visible()
    page.get_by_role("button", name="New key", exact=True).click()
    dialog = page.get_by_role("dialog", name="New key", exact=True)
    dialog.get_by_label("Name", exact=True).fill("laptop")
    dialog.get_by_role("button", name="Create", exact=True).click()
    expect(dialog.locator(".key-value")).to_have_text(re.compile(r"aml_[A-Za-z0-9_-]+"))
    expect(page.get_by_role("alert")).to_have_count(0)
    dialog.get_by_role("button", name="Close", exact=True).click()
    assert [(key.name, key.user) for key in httpd.keys.list() if key.name == "laptop"] == [
        ("laptop", "boss@example.com")]
    open_settings(page, "members")
    row = page.get_by_role("row").filter(has_text="bob@example.com")
    with page.expect_response(lambda r: r.request.method == "PATCH") as patched:
        row.get_by_label("Role for bob@example.com", exact=True).select_option("viewer")
    assert patched.value.status == 200
    expect(row.get_by_label("Role for bob@example.com", exact=True)).to_have_value("viewer")
    expect(page.get_by_role("alert")).to_have_count(0)
    assert httpd.users.get("bob@example.com").role == "viewer"


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


@pytest.mark.parametrize("path", ["/login", "/invite?token=x"])
def test_firebase_sign_in_shows_policy_links_when_configured(page, firebase_ui_legal, path):
    stub_vendor(page)
    page.goto(f"{firebase_ui_legal['url']}{path}")
    legal = page.locator("#legal")
    expect(legal).to_be_visible()
    expect(legal).to_have_text(LEGAL_TEXT)
    links = legal.locator("a")
    expect(links).to_have_count(3)
    for index, (text, href) in enumerate(LEGAL_LINKS.items()):
        link = links.nth(index)
        expect(link).to_have_text(text)
        expect(link).to_have_attribute("href", href)
        expect(link).to_have_attribute("target", "_blank")
        expect(link).to_have_attribute("rel", "noopener noreferrer")


@pytest.mark.parametrize("path", ["/login", "/invite?token=x"])
def test_firebase_sign_in_has_no_policy_links_by_default(page, firebase_ui, path):
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}{path}")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()
    expect(page.locator("#legal")).to_be_hidden()
    expect(page.locator("#legal a")).to_have_count(0)


def test_firebase_policy_links_ignore_non_https_urls(page, firebase_ui_legal):
    stub_vendor(page)
    page.route(
        re.compile(r"/auth/firebase/config$"),
        json_auth(200, {
            "projectId": "demo-project", "apiKey": "k", "authDomain": "demo-project.firebaseapp.com",
            "legal": {"terms": "javascript:alert(1)", "acceptable_use": "https://x.example/a/",
                      "privacy": "https://x.example/p/"}}))
    page.goto(f"{firebase_ui_legal['url']}/login")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()
    expect(page.locator("#legal")).to_be_hidden()
    expect(page.locator("#legal a")).to_have_count(0)


def test_firebase_policy_links_no_overflow_at_375(page, firebase_ui_legal):
    stub_vendor(page)
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{firebase_ui_legal['url']}/login")
    expect(page.locator("#legal")).to_be_visible()
    assert_no_overflow(page)
    page.goto(f"{firebase_ui_legal['url']}/invite?token=x")
    expect(page.locator("#legal")).to_be_visible()
    assert_no_overflow(page)


# --- Package 30.4: Firebase sign-in page test gaps ------------------------------------
#
# The SDK is a fake (FAKE_FIREBASE), so these pin what the page does around it:
# when it signs out, how it persists, what is disabled while a request is in
# flight, which texts it shows, and that it writes nothing to web storage.

GENERIC = "Sign-in failed. Try again."
WRONG_PASSWORD = "Wrong email or password."
TOO_MANY_ATTEMPTS = "Too many attempts. Try again later."
ALREADY_EXISTS = "An account with this email already exists. Sign in instead."
WEAK_PASSWORD = "Choose a longer password."
HOME_HTML = "<!doctype html><html><body><h1>Home</h1></body></html>"
FIREBASE_CONFIG = {
    "apiKey": "test-api-key-1",
    "authDomain": "demo-project.firebaseapp.com",
    "projectId": "demo-project",
}


def wait_until(page, predicate, timeout=2.0):
    """Poll `predicate` while pumping the Playwright loop (no sleeping)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(20)
    return predicate()


def record_sign_outs(page):
    """Count SDK signOut calls from outside the page, across navigations."""
    signouts = []
    page.expose_function("__recordSignOut", lambda: signouts.append(1))
    return signouts


def wait_ready(page):
    """The page has bound its handlers once it asks the SDK for a redirect result."""
    page.wait_for_function(
        "window.__FAKE_FIREBASE_CALLS__ && window.__FAKE_FIREBASE_CALLS__.getRedirectResult >= 1")


def sign_in_email(page, url, path="/login"):
    stub_vendor(page)
    page.goto(f"{url}{path}")
    wait_ready(page)
    page.locator("#email").fill("ada@example.com")
    page.locator("#password").fill("password12")
    page.locator("#email-login").get_by_role("button", name="Sign in", exact=True).click()


def hold_home(page, url):
    """Keep the navigation to `/` pending so the sign-in page is still alive."""
    held = []
    page.route(re.compile(re.escape(url) + r"/?$"), lambda route: held.append(route))
    return held


def release_home(held):
    for route in held:
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HOME_HTML)


def sign_in_replies(invite_reply):
    """An /auth/firebase handler: `invite_reply` for a posted invite, else 200."""
    posted = []

    def on_auth(route):
        payload = json.loads(route.request.post_data or "{}")
        posted.append(payload)
        if payload.get("invite"):
            route.fulfill(
                status=403, content_type="application/json", body=json.dumps(invite_reply))
            return
        route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":true,"user":"ada@example.com"}')
    return posted, on_auth


TERMINAL_FAILURES = [
    pytest.param(403, {"error": "no access", "reason": "no_account"}, NO_ACCOUNT, id="no_account"),
    pytest.param(
        403, {"error": "That email already has an account.", "reason": "account_exists"},
        "That email already has an account.", id="account_exists"),
    pytest.param(401, {"error": "login required"}, "login required", id="401"),
    pytest.param(429, {"error": TOO_MANY}, TOO_MANY, id="429"),
    pytest.param(503, {"error": "try again soon"}, "try again soon", id="503"),
    pytest.param(403, {"error": "no access"}, "no access", id="plain-403"),
]


@pytest.mark.parametrize("status,payload,text", TERMINAL_FAILURES)
def test_firebase_signs_out_on_terminal_failures(page, firebase_ui, status, payload, text):
    page.allow_http_errors = True
    signouts = record_sign_outs(page)
    stub_auth(page, json_auth(status, payload))
    sign_in_email(page, firebase_ui["url"])
    expect(page.get_by_role("alert")).to_have_text(text)
    assert wait_until(page, lambda: len(signouts) >= 1)
    assert len(signouts) == 1
    expect(page.locator("#submit")).to_be_enabled()
    expect(page).to_have_url(re.compile(r"/login$"))


def test_firebase_keeps_the_user_while_resend_is_offered(page, firebase_ui):
    page.allow_http_errors = True
    signouts = record_sign_outs(page)
    stub_auth(page, json_auth(403, {"error": "verify", "reason": "email_unverified"}))
    sign_in_email(page, firebase_ui["url"])
    expect(page.get_by_role("alert")).to_have_text(VERIFY_EMAIL)
    expect(page.get_by_role("button", name="Resend email", exact=True)).to_be_visible()
    assert signouts == []


def test_firebase_keeps_the_user_until_continue_without_the_invite(page, firebase_ui):
    page.allow_http_errors = True
    signouts = record_sign_outs(page)
    posted, on_auth = sign_in_replies(
        {"error": "this invite is no longer valid", "reason": "invite_invalid"})
    stub_auth(page, on_auth)
    held = hold_home(page, firebase_ui["url"])
    try:
        sign_in_email(page, firebase_ui["url"], "/invite?token=stale")
        expect(page.get_by_role("alert")).to_have_text("this invite is no longer valid")
        expect(page.get_by_role("button", name="Continue without the invite", exact=True)).to_be_visible()
        assert signouts == []
        page.get_by_role("button", name="Continue without the invite", exact=True).click()
        assert wait_until(page, lambda: len(held) == 1)
        # Counted before the navigation: the old page's pagehide would add one.
        assert len(signouts) == 1
        assert [("invite" in body) for body in posted] == [True, False]
    finally:
        release_home(held)
    expect(page).to_have_url(re.compile(r"/$"))


def test_firebase_success_signs_out_once_then_navigates_home(page, firebase_ui):
    signouts = record_sign_outs(page)
    # Locator calls and evaluate() wait for a pending navigation, so the button
    # state is read by the old page itself just before it unloads.
    buttons = []
    page.expose_function("__recordButtons", lambda state: buttons.append(state))
    page.add_init_script(
        """
        window.addEventListener("beforeunload", () => {
          const submit = document.querySelector("#submit");
          const google = document.querySelector("#google");
          if (submit && google) window.__recordButtons([submit.disabled, google.disabled]);
        });
        """
    )
    stub_auth(page, json_auth(200, {"ok": True, "user": "ada@example.com"}))
    held = hold_home(page, firebase_ui["url"])
    try:
        sign_in_email(page, firebase_ui["url"])
        assert wait_until(page, lambda: len(held) == 1)
        # Counted before the navigation: the old page's pagehide would add one.
        assert len(signouts) == 1
    finally:
        release_home(held)
    expect(page).to_have_url(re.compile(r"/$"))
    expect(page.get_by_role("heading", name="Home", exact=True)).to_be_visible()
    # The redirect was in flight: both buttons stayed disabled until navigation.
    assert wait_until(page, lambda: len(buttons) >= 1)
    assert buttons[0] == [True, True]


def test_firebase_pagehide_signs_out(page, firebase_ui):
    signouts = record_sign_outs(page)
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    wait_ready(page)
    assert signouts == []
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    assert wait_until(page, lambda: len(signouts) == 1)
    assert fake_calls(page)["signOut"] == 1


def test_firebase_uses_in_memory_persistence(page, firebase_ui):
    page.allow_http_errors = True
    stub_vendor(page)
    stub_auth(page, json_auth(403, {"error": "no access", "reason": "no_account"}))
    page.goto(f"{firebase_ui['url']}/login")
    wait_ready(page)
    calls = fake_calls(page)
    assert calls["setPersistence"] == ["NONE"]
    assert calls["initializeApp"] == [FIREBASE_CONFIG]
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    order = fake_calls(page)["order"]
    assert order.index("setPersistence") < order.index("getRedirectResult")
    assert order.index("setPersistence") < order.index("signInWithPopup")


def test_firebase_persistence_failure_blocks_sign_in(page, firebase_ui):
    fake_opts(page, errors={"setPersistence": "auth/internal-error"})
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("alert")).to_have_text(GENERIC)
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    page.locator("#email").fill("ada@example.com")
    page.locator("#password").fill("password12")
    page.locator("#password").press("Enter")
    page.wait_for_timeout(300)
    calls = fake_calls(page)
    assert calls["signInWithPopup"] == 0
    assert calls["signInWithEmailAndPassword"] == []
    assert calls["getRedirectResult"] == 0
    # No native GET submit either: the password never reaches the URL.
    assert page.url == f"{firebase_ui['url']}/login"


def test_firebase_buttons_disabled_while_email_sign_in_is_in_flight(page, firebase_ui):
    page.allow_http_errors = True
    fake_opts(page, hold=["signInWithEmailAndPassword"])
    stub_auth(page, json_auth(403, {"error": "no access", "reason": "no_account"}))
    sign_in_email(page, firebase_ui["url"])
    submit, google = page.locator("#submit"), page.locator("#google")
    expect(submit).to_be_disabled()
    expect(google).to_be_disabled()
    # A second submit and a click on Google are ignored while busy.
    page.evaluate("document.querySelector('#email-login').requestSubmit()")
    google.dispatch_event("click")
    page.evaluate("window.__FAKE_RELEASE__.signInWithEmailAndPassword()")
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    expect(submit).to_be_enabled()
    expect(google).to_be_enabled()
    calls = fake_calls(page)
    assert len(calls["signInWithEmailAndPassword"]) == 1
    assert calls["signInWithPopup"] == 0


def test_firebase_buttons_disabled_while_google_sign_in_is_in_flight(page, firebase_ui):
    page.allow_http_errors = True
    fake_opts(page, hold=["signInWithPopup"])
    stub_vendor(page)
    stub_auth(page, json_auth(403, {"error": "no access", "reason": "no_account"}))
    page.goto(f"{firebase_ui['url']}/login")
    wait_ready(page)
    submit, google = page.locator("#submit"), page.locator("#google")
    google.click()
    expect(submit).to_be_disabled()
    expect(google).to_be_disabled()
    google.dispatch_event("click")
    page.evaluate("window.__FAKE_RELEASE__.signInWithPopup()")
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    expect(submit).to_be_enabled()
    expect(google).to_be_enabled()
    assert fake_calls(page)["signInWithPopup"] == 1


MAPPED_ERRORS = [
    pytest.param("signin", "signInWithEmailAndPassword", "auth/invalid-credential", WRONG_PASSWORD, id="invalid-credential"),
    pytest.param("signin", "signInWithEmailAndPassword", "auth/wrong-password", WRONG_PASSWORD, id="wrong-password"),
    pytest.param("signin", "signInWithEmailAndPassword", "auth/user-not-found", WRONG_PASSWORD, id="user-not-found"),
    pytest.param("signin", "signInWithEmailAndPassword", "auth/too-many-requests", TOO_MANY_ATTEMPTS, id="too-many-requests"),
    pytest.param("signin", "signInWithEmailAndPassword", "auth/network-request-failed", GENERIC, id="unknown-code"),
    pytest.param("signin", "signInWithEmailAndPassword", "", GENERIC, id="no-code"),
    pytest.param("signup", "createUserWithEmailAndPassword", "auth/email-already-in-use", ALREADY_EXISTS, id="email-already-in-use"),
    pytest.param("signup", "createUserWithEmailAndPassword", "auth/weak-password", WEAK_PASSWORD, id="weak-password"),
    pytest.param("google", "signInWithPopup", "auth/internal-error", GENERIC, id="popup-internal-error"),
    pytest.param("forgot", "sendPasswordResetEmail", "auth/too-many-requests", TOO_MANY_ATTEMPTS, id="reset-too-many-requests"),
    pytest.param("signin", "getIdToken", "auth/network-request-failed", GENERIC, id="id-token-failure"),
]


def start_flow(page, flow):
    if flow == "signup":
        page.get_by_role("tab", name="Sign up", exact=True).click()
    if flow in ("signin", "signup", "forgot"):
        page.locator("#email").fill("ada@example.com")
        page.locator("#password").fill("password12")
    if flow == "signin":
        page.locator("#email-login").get_by_role("button", name="Sign in", exact=True).click()
    elif flow == "signup":
        page.locator("#email-login").get_by_role("button", name="Sign up", exact=True).click()
    elif flow == "google":
        page.get_by_role("button", name="Continue with Google", exact=True).click()
    elif flow == "forgot":
        page.get_by_role("button", name="Forgot password?", exact=True).click()


@pytest.mark.parametrize("flow,method,code,text", MAPPED_ERRORS)
def test_firebase_error_texts(page, firebase_ui, flow, method, code, text):
    posts = []
    page.on("request", lambda request: posts.append(request.url)
            if request.method == "POST" and request.url.endswith("/auth/firebase") else None)
    fake_opts(page, errors={method: code})
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    wait_ready(page)
    start_flow(page, flow)
    expect(page.get_by_role("alert")).to_have_text(text)
    if method != "getIdToken":
        # A failure inside the SDK never reaches the server.
        assert fake_calls(page)["getIdToken"] == 0
    assert posts == []
    expect(page.locator("#submit")).to_be_enabled()
    expect(page.locator("#google")).to_be_enabled()


@pytest.mark.parametrize("code", ["auth/popup-closed-by-user", "auth/cancelled-popup-request"])
def test_firebase_silent_errors_show_nothing(page, firebase_ui, code):
    fake_opts(page, errors={"signInWithPopup": code})
    stub_vendor(page)
    page.goto(f"{firebase_ui['url']}/login")
    wait_ready(page)
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.locator("#google")).to_be_enabled()
    expect(page.locator("#submit")).to_be_enabled()
    assert fake_calls(page)["signInWithPopup"] == 1
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.locator("#error")).to_be_hidden()


def storage_state(page):
    return page.evaluate(
        """() => ({
          local: Object.keys(localStorage),
          session: Object.keys(sessionStorage),
          dump: JSON.stringify(localStorage) + JSON.stringify(sessionStorage),
          writes: window.__WRITES__,
          cookie: document.cookie,
        })"""
    )


def test_firebase_page_writes_nothing_to_web_storage(page, firebase_ui):
    # The fake SDK cannot prove what the real SDK keeps in IndexedDB; that is
    # the owner's check against the real Firebase project (see the roadmap).
    # The page's own code writes nothing, and this test never clicks the theme
    # toggle, which is the only thing on the page that may write (its own key).
    page.allow_http_errors = True
    page.add_init_script(
        """
        window.__WRITES__ = [];
        for (const name of ["setItem", "removeItem", "clear"]) {
          const original = Storage.prototype[name];
          Storage.prototype[name] = function (...args) {
            window.__WRITES__.push([name, ...args]);
            return original.apply(this, args);
          };
        }
        """
    )
    secrets = ["kept-in-memory", "fake-id-token", "password12"]
    stub_vendor(page)
    stub_auth(page, json_auth(403, {"error": "no access", "reason": "no_account"}))

    def assert_clean():
        state = storage_state(page)
        assert state["local"] == [] and state["session"] == []
        assert state["writes"] == []
        assert state["cookie"] == ""
        for secret in secrets:
            assert secret not in state["dump"] and secret not in page.url

    page.goto(f"{firebase_ui['url']}/login")
    wait_ready(page)
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    assert_clean()
    page.locator("#email").fill("ada@example.com")
    page.locator("#password").fill("password12")
    page.locator("#email-login").get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    assert_clean()
    page.get_by_role("button", name="Forgot password?", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("Check your inbox for a password reset email.")
    assert_clean()
    page.get_by_role("tab", name="Sign up", exact=True).click()
    page.locator("#email-login").get_by_role("button", name="Sign up", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(VERIFY_EMAIL)
    assert_clean()
    # An invite token stays in memory only (and out of the address bar).
    page.goto(f"{firebase_ui['url']}/invite#token=kept-in-memory")
    wait_ready(page)
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    assert_clean()
    assert page.context.cookies() == []


@pytest.mark.parametrize("path,expected", [
    ("/invite#token=frag-token", "frag-token"),
    ("/invite?token=legacy-token", "legacy-token"),
    ("/invite?token=old-token#token=new-token", "new-token"),
], ids=["fragment", "legacy-query", "fragment-wins"])
def test_firebase_invite_token_is_read_from_the_fragment_then_the_query(
        page, firebase_ui, path, expected):
    page.allow_http_errors = True
    posted = []
    requests = []
    page.on("request", lambda request: requests.append(request.url))

    def on_auth(route):
        posted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=403, content_type="application/json",
            body=json.dumps({"error": "no access", "reason": "no_account"}))

    stub_vendor(page)
    stub_auth(page, on_auth)
    page.goto(f"{firebase_ui['url']}{path}")
    wait_ready(page)
    # Neither form survives in the address bar.
    assert page.url == f"{firebase_ui['url']}/invite"
    page.get_by_role("button", name="Continue with Google", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text(NO_ACCOUNT)
    assert [body.get("invite") for body in posted] == [expected]
    # A fragment is never sent: no request line carries the new-style token.
    assert not any("frag-token" in url or "new-token" in url for url in requests)


def admin_session(page, httpd, url):
    httpd.users.add("admin", role="admin")
    token, _ = httpd.web._session_cookie_for("admin", "firebase")
    page.context.add_cookies([{"name": httpd.web._cookie_name(), "value": token, "url": url}])


def test_account_dialog_has_no_password_form_with_firebase(page, firebase_ui):
    from agenticml.server.db.workspaces import AccountRepo

    httpd = firebase_ui["httpd"]
    admin_session(page, httpd, firebase_ui["url"])
    AccountRepo(httpd.db).set_server_admin("admin", True)
    page.goto(f"{firebase_ui['url']}/admin/")
    page.get_by_role("button", name="Account", exact=True).click()
    dialog = page.get_by_role("dialog", name="Account", exact=True)
    expect(dialog.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    identity = dialog.locator(".identity")
    expect(identity.locator("div").filter(has_text="User").locator("dd")).to_have_text("admin")
    expect(identity.locator("div").filter(has_text="Sign-in").locator("dd")).to_have_text("firebase")
    expect(dialog.get_by_label("New password", exact=True)).to_have_count(0)
    expect(dialog.get_by_label("Confirm password", exact=True)).to_have_count(0)
    expect(dialog.get_by_label("Current password", exact=True)).to_have_count(0)
    expect(dialog.get_by_role("button", name="Change password", exact=True)).to_have_count(0)
    expect(dialog.get_by_text("Change password")).to_have_count(0)
    page.set_viewport_size({"width": 375, "height": 812})
    assert_no_overflow(page)
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_have_count(0)


def test_settings_account_card_has_no_password_form_with_firebase(page, firebase_ui):
    admin_session(page, firebase_ui["httpd"], firebase_ui["url"])
    page.goto(firebase_ui["url"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    card = open_settings(page, "account")
    identity = card.locator(".identity")
    expect(identity.locator("div").filter(has_text="Sign-in").locator("dd")).to_have_text("firebase")
    expect(card.get_by_text("Change password")).to_have_count(0)
    expect(card.get_by_label("New password", exact=True)).to_have_count(0)


CONTACT_EMAIL = "help@lab.example.org"


@pytest.mark.parametrize("path", ["/login", "/invite?token=x"])
def test_firebase_sign_in_shows_contact_link_in_operator_mode(page, tmp_path, monkeypatch, path):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    stub_vendor(page)
    for server in _serve_firebase(tmp_path, contact_email=CONTACT_EMAIL):
        page.goto(f"{server['url']}{path}")
        contact = page.locator("#contact")
        expect(contact).to_be_visible()
        expect(contact).to_have_text("Questions? Contact us")
        link = contact.get_by_role("link", name="Contact us", exact=True)
        expect(link).to_have_attribute(
            "href", f"mailto:{CONTACT_EMAIL}?subject=agenticml%3A%20sign-in")
        # Shown even though no legal links are set.
        expect(page.locator("#legal")).to_be_hidden()


def test_firebase_sign_in_contact_link_beside_policy_links(page, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    stub_vendor(page)
    for server in _serve_firebase(tmp_path, contact_email=CONTACT_EMAIL, legal_base_url=LEGAL_BASE):
        page.goto(f"{server['url']}/login")
        expect(page.locator("#legal")).to_have_text(LEGAL_TEXT)
        expect(page.locator("#contact")).to_have_text("Questions? Contact us")


@pytest.mark.parametrize("contact", [None, "", "off"])
def test_firebase_sign_in_has_no_contact_link_otherwise(page, tmp_path, monkeypatch, contact):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    stub_vendor(page)
    for server in _serve_firebase(tmp_path, contact_email=contact):
        page.goto(f"{server['url']}/login")
        expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()
        expect(page.locator("#contact")).to_be_hidden()
        expect(page.locator("#contact a")).to_have_count(0)


def test_firebase_contact_link_ignores_a_bad_address_from_the_config(page, firebase_ui):
    stub_vendor(page)
    page.route(
        re.compile(r"/auth/firebase/config$"),
        json_auth(200, {
            "projectId": "demo-project", "apiKey": "k", "authDomain": "demo-project.firebaseapp.com",
            "contact_email": "a@x.org?cc=b@y.org"}))
    page.goto(f"{firebase_ui['url']}/login")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()
    expect(page.locator("#contact")).to_be_hidden()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_firebase_contact_link_no_overflow_at_375(page, tmp_path, monkeypatch, scheme):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    stub_vendor(page)
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": 375, "height": 812})
    for server in _serve_firebase(tmp_path, contact_email=CONTACT_EMAIL, legal_base_url=LEGAL_BASE):
        page.goto(f"{server['url']}/login")
        expect(page.locator("#contact")).to_be_visible()
        assert_no_overflow(page)
