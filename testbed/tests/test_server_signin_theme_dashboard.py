"""Browser coverage for the theme on the sign-in pages (package 30.4).

The sign-in pages follow the dashboard's saved `agenticml-theme` (system, light
or dark), set before first paint by the classic /static/theme.js, with a
"Theme: <pref>" toggle that uses the same key and cycle as the dashboard.
"""
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import assert_no_overflow, browser, context, page, server_ui, sign_in
from test_server_firebase_dashboard import firebase_ui, stub_vendor
from test_server_keys_dashboard import fresh_server

pytestmark = [pytest.mark.browser, pytest.mark.ui]

KEY = "agenticml-theme"
DARK_SURFACE = "rgb(20, 24, 26)"


def save_theme(page, url, value):
    """Store the saved theme on the server's origin, then leave the page."""
    page.goto(f"{url}/login")
    page.evaluate("([key, value]) => localStorage.setItem(key, value)", [KEY, value])


def form_side_background(page):
    return page.locator(".auth-form-side").evaluate("node => getComputedStyle(node).backgroundColor")


def assert_dark(page):
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert form_side_background(page) == DARK_SURFACE


def assert_light(page):
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert form_side_background(page) != DARK_SURFACE


def test_saved_dark_theme_applies_to_password_login(page, server_ui):
    save_theme(page, server_ui["url"], "dark")
    page.reload()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    assert_dark(page)
    # The brand panel is dark in every theme.
    page.evaluate("localStorage.setItem('agenticml-theme', 'light')")
    page.reload()
    assert_light(page)
    brand = page.locator(".auth-brand").evaluate("node => getComputedStyle(node).backgroundColor")
    assert brand != form_side_background(page)


def test_saved_dark_theme_applies_to_setup_page(page, fresh_server):
    save_theme(page, fresh_server["url"], "dark")
    token = fresh_server["httpd"].web.setup_token
    page.goto(f"{fresh_server['url']}/setup?token={token}")
    expect(page.get_by_role("heading", name="Create admin account", exact=True)).to_be_visible()
    assert_dark(page)


@pytest.mark.parametrize("path", ["/login", "/invite?token=x"])
def test_saved_dark_theme_applies_to_hosted_pages(page, firebase_ui, path):
    stub_vendor(page)
    save_theme(page, firebase_ui["url"], "dark")
    page.goto(f"{firebase_ui['url']}{path}")
    expect(page.get_by_role("heading", name="Sign in to your workspace", exact=True)).to_be_visible()
    assert_dark(page)


def test_explicit_light_wins_over_a_dark_system(page, server_ui):
    page.emulate_media(color_scheme="dark")
    save_theme(page, server_ui["url"], "light")
    page.reload()
    assert_light(page)


def test_system_theme_follows_the_system_without_reload(page, server_ui):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{server_ui['url']}/login")
    assert_dark(page)
    expect(page.locator("#auth-theme")).to_have_text("Theme: system")
    page.emulate_media(color_scheme="light")
    assert_light(page)


@pytest.mark.parametrize("saved", ["purple", ""])
def test_invalid_saved_theme_behaves_as_system(page, server_ui, saved):
    save_theme(page, server_ui["url"], saved)
    page.reload()
    assert_light(page)
    expect(page.locator("#auth-theme")).to_have_text("Theme: system")
    page.emulate_media(color_scheme="dark")
    assert_dark(page)


def test_toggle_cycles_persists_and_writes_only_its_key(page, server_ui):
    page.goto(f"{server_ui['url']}/login")
    toggle = page.get_by_role("button", name="Theme: system", exact=True)
    expect(toggle).to_be_visible()
    toggle.click()
    expect(page.get_by_role("button", name="Theme: light", exact=True)).to_be_visible()
    assert_light(page)
    assert page.evaluate("localStorage.getItem('agenticml-theme')") == "light"
    page.get_by_role("button", name="Theme: light", exact=True).click()
    expect(page.get_by_role("button", name="Theme: dark", exact=True)).to_be_visible()
    assert_dark(page)
    assert page.evaluate("localStorage.getItem('agenticml-theme')") == "dark"
    assert page.evaluate("Object.keys(localStorage)") == [KEY]
    page.reload()
    assert_dark(page)
    expect(page.get_by_role("button", name="Theme: dark", exact=True)).to_be_visible()
    page.get_by_role("button", name="Theme: dark", exact=True).click()
    expect(page.get_by_role("button", name="Theme: system", exact=True)).to_be_visible()
    assert page.evaluate("localStorage.getItem('agenticml-theme')") == "system"
    assert_light(page)


def test_toggle_is_after_the_form_in_tab_order_and_has_no_aria_label(page, server_ui):
    page.goto(f"{server_ui['url']}/login")
    toggle = page.locator("#auth-theme")
    expect(toggle).not_to_have_attribute("aria-label", re.compile(".*"))
    page.locator("#user").focus()
    # user, password, Sign in, "Sign in with an admin key", then the toggle.
    for _ in range(4):
        page.keyboard.press("Tab")
    expect(toggle).to_be_focused()


def test_theme_follows_another_tab(page, context, server_ui):
    page.goto(f"{server_ui['url']}/login")
    assert_light(page)
    other = context.new_page()
    try:
        other.goto(f"{server_ui['url']}/login")
        other.get_by_role("button", name="Theme: system", exact=True).click()
        other.get_by_role("button", name="Theme: light", exact=True).click()
    finally:
        other.close()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("#auth-theme")).to_have_text("Theme: dark")


def test_blocked_storage_still_loads_and_toggles(page, server_ui):
    page.add_init_script(
        """
        const blocked = () => { throw new DOMException('blocked', 'SecurityError'); };
        Storage.prototype.getItem = blocked;
        Storage.prototype.setItem = blocked;
        """
    )
    page.goto(f"{server_ui['url']}/login")
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    assert_light(page)
    page.get_by_role("button", name="Theme: system", exact=True).click()
    page.get_by_role("button", name="Theme: light", exact=True).click()
    expect(page.get_by_role("button", name="Theme: dark", exact=True)).to_be_visible()
    assert_dark(page)


def test_saved_theme_carries_into_the_dashboard(page, server_ui):
    save_theme(page, server_ui["url"], "dark")
    sign_in(page, server_ui, server_ui["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("#theme")).to_have_text("Theme: dark")


def test_toggle_choice_carries_into_the_dashboard(page, server_ui):
    page.goto(f"{server_ui['url']}/login")
    for name in ("Theme: system", "Theme: light"):
        page.get_by_role("button", name=name, exact=True).click()
    expect(page.get_by_role("button", name="Theme: dark", exact=True)).to_be_visible()
    sign_in(page, server_ui, server_ui["admin"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.locator("#theme")).to_have_text("Theme: dark")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_toggle_fits_next_to_the_compact_logo_at_375(page, server_ui, theme):
    page.set_viewport_size({"width": 375, "height": 812})
    save_theme(page, server_ui["url"], theme)
    page.reload()
    expect(page.locator(".auth-compact-logo .auth-logo")).to_be_visible()
    assert_no_overflow(page)
    toggle = page.locator("#auth-theme").bounding_box()
    logo = page.locator(".auth-compact-logo .auth-logo").bounding_box()
    assert toggle["x"] >= 0 and toggle["x"] + toggle["width"] <= 375
    assert logo["x"] + logo["width"] <= toggle["x"], "the toggle overlaps the logo"
    # Not over the form either.
    title = page.locator(".auth-title").bounding_box()
    assert toggle["y"] + toggle["height"] <= title["y"]


def test_hosted_page_fits_with_the_toggle_in_dark_at_375(page, firebase_ui):
    stub_vendor(page)
    page.set_viewport_size({"width": 375, "height": 812})
    save_theme(page, firebase_ui["url"], "dark")
    page.goto(f"{firebase_ui['url']}/login")
    assert_dark(page)
    assert_no_overflow(page)
    toggle = page.locator("#auth-theme").bounding_box()
    assert toggle["x"] + toggle["width"] <= 375
