"""Browser coverage for the Contact us / Feedback button on server dashboards (package 37.2)."""
from contextlib import ExitStack
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import agenticml
from agenticml.server.app import make_server
from agenticml.server.runs import reconcile
from agenticml.store import RunWriter

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import (
    assert_no_overflow, browser, context, page, sign_in_password,
)

pytestmark = [pytest.mark.browser, pytest.mark.ui]

PASSWORD = "password1234"
TEAM_EMAIL = "contact@agenticml.xyz"
LAB_EMAIL = "help@lab.example.org"
UNSET = object()


@pytest.fixture
def start_server(tmp_path, monkeypatch):
    """Start a server with the given ``contact_email`` (UNSET passes nothing)."""
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    stack = ExitStack()
    count = [0]

    def start(contact=UNSET, runs=0):
        count[0] += 1
        kwargs = {} if contact is UNSET else {"contact_email": contact}
        httpd = make_server(tmp_path / f"data-{count[0]}", port=0, **kwargs)
        httpd.users.add("admin", role="admin")
        httpd.users.set_password("admin", PASSWORD)
        for index in range(runs):
            writer = RunWriter.create(
                httpd.root, run_id=f"run-{index:03d}", meta={"project": "p", "name": f"run-{index:03d}"})
            writer.finish("finished")
            writer.close()
        if runs:
            reconcile(httpd.root, httpd.run_index)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

        stack.callback(stop)
        return {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "httpd": httpd}

    try:
        yield start
    finally:
        stack.close()


def sign_in(page, server):
    sign_in_password(page, server, "admin", PASSWORD)
    expect(page).to_have_url(re.compile(r"/w/default/"))
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


def fab(page):
    return page.locator("#contact")


def panel(page):
    return page.locator("#contact-panel")


def draft(page):
    """The decoded mailto: link in the open panel: (address, subject, body lines)."""
    href = page.locator("#contact-mail").get_attribute("href")
    parts = urlsplit(href)
    assert parts.scheme == "mailto"
    query = parse_qs(parts.query, keep_blank_values=True)
    assert set(query) == {"subject", "body"}
    return unquote(parts.path), query["subject"][0], query["body"][0].split("\n")


def open_panel(page):
    fab(page).click()
    expect(panel(page)).to_be_visible()


def test_operator_mode_opens_closes_and_drafts_an_email(page, start_server):
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    expect(fab(page)).to_be_visible()
    expect(fab(page)).to_have_text("Contact us")
    expect(fab(page)).to_have_attribute("aria-label", "Contact us")
    expect(panel(page)).to_be_hidden()
    open_panel(page)
    expect(page.get_by_role("dialog", name="Contact us")).to_be_visible()
    expect(page.locator("#contact-note")).to_be_hidden()
    expect(page.locator("#contact-address")).to_have_text(LAB_EMAIL)
    expect(page.locator("#contact-mail")).to_be_focused()
    address, subject, body = draft(page)
    assert address == LAB_EMAIL
    assert subject == "agenticml: default"
    assert body == ["", "", "---", "Page: /w/default/", "Workspace: default",
                    f"Server: agenticml-lite {agenticml.__version__}"]
    page.keyboard.press("Escape")
    expect(panel(page)).to_be_hidden()
    expect(fab(page)).to_be_focused()
    # The Close button closes it too, and the draft follows the page route.
    page.evaluate("location.hash = '#/settings'")
    open_panel(page)
    assert "Page: /w/default/#/settings" in draft(page)[2]
    page.get_by_role("button", name="Close", exact=True).click()
    expect(panel(page)).to_be_hidden()
    expect(fab(page)).to_be_focused()


def wait_for_page_line(page, line):
    """Poll the open panel's draft until it has this Page line (wait_for_function is refused by the CSP)."""
    for _ in range(50):
        if line in draft(page)[2]:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the draft never got {line!r}: {draft(page)[2]}")


def test_draft_follows_the_route_while_the_panel_is_open(page, start_server):
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    page.evaluate("location.hash = '#/settings'")
    open_panel(page)
    assert "Page: /w/default/#/settings" in draft(page)[2]
    page.go_back()  # hash routing leaves the popover open
    expect(panel(page)).to_be_visible()
    wait_for_page_line(page, "Page: /w/default/")
    page.evaluate("location.hash = '#/runs'")
    wait_for_page_line(page, "Page: /w/default/#/runs")


def test_light_dismiss_does_not_move_focus_to_the_button(page, start_server):
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    open_panel(page)
    page.get_by_role("heading", name="Runs", exact=True).click()
    expect(panel(page)).to_be_hidden()
    expect(fab(page)).not_to_be_focused()
    # A stray Space (page scroll) must not open the panel again.
    page.keyboard.press("Space")
    expect(panel(page)).to_be_hidden()


def test_status_region_stays_rendered_and_repeats_are_announced(page, context, start_server):
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    open_panel(page)
    status = page.locator("#contact-status")
    assert status.get_attribute("hidden") is None
    assert status.get_attribute("role") == "status"
    page.evaluate(
        "(() => { const s = document.querySelector('#contact-status'); window.__texts = [];"
        " new MutationObserver(() => window.__texts.push(s.textContent)).observe("
        "s, {childList: true, characterData: true, subtree: true}); })()")
    copy = page.get_by_role("button", name="Copy address", exact=True)
    copy.click()
    expect(status).to_have_text("Copied")
    copy.click()
    for _ in range(50):  # wait_for_function evals a string, which the page's CSP refuses
        if page.evaluate("window.__texts.filter(t => t === 'Copied').length") >= 2:
            break
        page.wait_for_timeout(100)
    else:
        raise AssertionError("the second Copy did not announce again")
    assert status.get_attribute("hidden") is None


def test_operator_mode_on_the_admin_page(page, start_server):
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    page.get_by_role("link", name="Admin", exact=True).click()
    expect(page.get_by_role("heading", name="Admin", exact=True)).to_be_visible()
    expect(fab(page)).to_have_text("Contact us")
    open_panel(page)
    address, subject, body = draft(page)
    assert (address, subject) == (LAB_EMAIL, "agenticml: admin")
    assert body[:3] == ["", "", "---"]
    assert body[3].startswith("Page: /admin")
    assert not any(line.startswith("Workspace:") for line in body)
    assert body[-1] == f"Server: agenticml-lite {agenticml.__version__}"


def test_copy_address_puts_it_on_the_clipboard(page, context, start_server):
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    open_panel(page)
    page.get_by_role("button", name="Copy address", exact=True).click()
    expect(page.locator("#contact-status")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == LAB_EMAIL
    expect(page.locator("#contact-status")).to_have_text("")  # cleared after two seconds


def test_copy_without_a_clipboard_selects_the_address(page, start_server):
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined})")
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    open_panel(page)
    page.get_by_role("button", name="Copy address", exact=True).click()
    expect(page.locator("#contact-status")).to_have_text("Select the address above to copy it")
    assert page.evaluate("getSelection().toString()") == LAB_EMAIL


def test_copy_refused_shows_the_fallback_without_console_errors(page, start_server):
    page.add_init_script(
        "Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new Error('denied'))}})")
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    open_panel(page)
    page.get_by_role("button", name="Copy address", exact=True).click()
    expect(page.locator("#contact-status")).to_have_text("Select the address above to copy it")


@pytest.mark.parametrize("contact", [UNSET, "", "  "])
def test_project_mode_is_a_feedback_button_to_the_team(page, start_server, contact):
    server = start_server(contact)
    sign_in(page, server)
    expect(fab(page)).to_have_text("Feedback")
    expect(fab(page)).to_have_attribute("aria-label", "Feedback")
    open_panel(page)
    expect(page.get_by_role("dialog", name="Feedback to the agenticml-lite team")).to_be_visible()
    expect(page.locator("#contact-note")).to_have_text(
        "This writes to the people who make agenticml-lite, not to your server admin. "
        "For access or account problems, ask your server admin.")
    expect(page.locator("#contact-address")).to_have_text(TEAM_EMAIL)
    address, subject, body = draft(page)
    assert address == TEAM_EMAIL
    assert subject == "agenticml (self-hosted): feedback"
    assert body == ["", "", "---", f"Server: agenticml-lite {agenticml.__version__}"]
    assert "default" not in "\n".join(body)


@pytest.mark.parametrize("contact", ["off", "OFF"])
def test_off_shows_no_button(page, start_server, contact):
    server = start_server(contact)
    sign_in(page, server)
    expect(fab(page)).to_be_hidden()
    expect(page.get_by_role("button", name=re.compile("Contact us|Feedback"))).to_have_count(0)
    expect(page.locator("body.has-contact")).to_have_count(0)
    page.goto(f"{server['url']}/workspaces")
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Contact us", exact=True)).to_have_count(0)


def test_picker_has_the_contact_link_in_operator_mode_only(page, start_server):
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    page.goto(f"{server['url']}/workspaces")
    link = page.get_by_role("link", name="Contact us", exact=True)
    expect(link).to_have_attribute("href", f"mailto:{LAB_EMAIL}?subject=agenticml%3A%20workspaces")
    project = start_server()
    sign_in(page, project)
    page.goto(f"{project['url']}/workspaces")
    expect(page.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Contact us", exact=True)).to_have_count(0)


def test_local_dashboard_has_no_button(page, ui_server):
    page.allow_http_errors = True  # the local dashboard answers the session request with 404
    page.goto(ui_server.url)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.locator(".run-row").first).to_be_visible()
    expect(fab(page)).to_be_hidden()
    expect(page.locator("body.has-contact")).to_have_count(0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("contact", [UNSET, LAB_EMAIL])
def test_no_horizontal_overflow_at_375px_and_320px(page, start_server, scheme, contact):
    page.emulate_media(color_scheme=scheme)
    server = start_server(contact)
    for width in (375, 320):
        page.set_viewport_size({"width": width, "height": 812})
        if "/w/" in page.url:
            page.goto(f"{server['url']}/w/default/")
        else:
            sign_in(page, server)
        expect(fab(page)).to_be_visible()
        assert_no_overflow(page)
        expect(fab(page).locator(".contact-fab-label")).to_be_hidden()
        box = fab(page).bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        open_panel(page)
        assert_no_overflow(page)
        panel_box = panel(page).bounding_box()
        assert panel_box["x"] >= 15.5 and panel_box["x"] + panel_box["width"] <= width - 15.5
        assert panel_box["y"] >= 0
        page.keyboard.press("Escape")
        expect(panel(page)).to_be_hidden()


def test_wide_button_shows_icon_and_label(page, start_server):
    server = start_server()
    sign_in(page, server)
    expect(fab(page).locator(".contact-fab-label")).to_be_visible()
    expect(fab(page).locator("svg")).to_be_visible()


def test_last_row_and_button_can_be_scrolled_above_the_button(page, start_server):
    server = start_server(LAB_EMAIL, runs=30)
    page.set_viewport_size({"width": 375, "height": 700})
    sign_in(page, server)
    page.get_by_role("button", name="Show 25 more", exact=True).click()
    expect(page.locator(".run-row")).to_have_count(30)
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    more = page.get_by_role("button", name="Show latest 5 only", exact=True).bounding_box()
    button = fab(page).bounding_box()
    assert more["y"] + more["height"] <= button["y"], (more, button)


def test_a_modal_dialog_stays_above_the_button(page, start_server):
    server = start_server(LAB_EMAIL)
    sign_in(page, server)
    page.evaluate("document.querySelector('#lightbox').showModal()")
    hit = page.evaluate(
        "(() => { const b = document.querySelector('#contact').getBoundingClientRect();"
        " const el = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);"
        " return el.id === 'contact' ? 'button' : el.tagName; })()")
    assert hit == "DIALOG"  # the modal's backdrop covers the page, button included
    expect(page.locator("#lightbox")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#lightbox")).to_be_hidden()
