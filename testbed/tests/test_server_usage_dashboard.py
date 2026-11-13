"""Browser coverage for usage, banners, new workspace, and admin plan edit (18c.3)."""
import json
import re
import threading

import pytest
from agenticml.server.app import make_server
from agenticml.server.db.usage import UsageRepo
from agenticml.server.db.workspaces import AccountRepo, WorkspaceRepo
from agenticml.server.limits import Plans

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from test_server_dashboard import (
    assert_no_overflow, browser, context, open_settings, page, settings_card, sign_in,
    sign_in_password,
)

pytestmark = [pytest.mark.browser, pytest.mark.ui]

PASSWORD = "password1234"
FREE = {
    "storage_bytes": 5_000_000_000,
    "upload_bytes_per_day": 2_000_000_000,
    "requests_per_minute": 600,
    "people": 5,
}


def _plans(free_workspaces=1, **limits):
    return Plans({
        "default_plan": "free",
        "free_workspaces_per_account": free_workspaces,
        "plans": {"free": {**FREE, **limits}},
    })


def _serve(httpd):
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    return thread, url


def _stop(httpd, thread):
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


@pytest.fixture
def usage_server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0, plans=_plans())
    _, admin = httpd.keys.create("admin", "admin")
    httpd.users.set_password("admin", PASSWORD)
    thread, url = _serve(httpd)
    try:
        yield {"url": url, "httpd": httpd, "admin": admin}
    finally:
        _stop(httpd, thread)


@pytest.fixture
def signup_ui(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0, plans=_plans(2))
    _, admin = httpd.keys.create("admin", "admin")
    httpd.users.set_password("admin", PASSWORD)
    account = AccountRepo(httpd.db).get("admin")
    with httpd.db.transaction(write=True) as tx:
        tx.execute("UPDATE accounts SET email_verified=1, email=? WHERE id=?",
                   ("admin@example.com", account["id"]))
    httpd.web.open_signup = True
    thread, url = _serve(httpd)
    try:
        yield {"url": url, "httpd": httpd, "admin": admin}
    finally:
        _stop(httpd, thread)


def set_usage(httpd, stored=0, uploaded=0):
    usage = UsageRepo(httpd.db, httpd.default_workspace_id)
    with httpd.db.transaction(write=True) as tx:
        if stored:
            usage.add_stored(stored, tx)
        if uploaded:
            usage.add_upload(uploaded, tx)


def test_team_usage_section_values_and_hidden_without_limits(page, usage_server, tmp_path, monkeypatch):
    page.set_viewport_size({"width": 375, "height": 812})
    set_usage(usage_server["httpd"], stored=1_500_000_000, uploaded=400_000_000)
    sign_in_password(page, usage_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    open_settings(page, "workspace")
    section = page.get_by_role("region", name="Usage", exact=True)
    expect(section).to_be_visible()
    expect(settings_card(page, "workspace")).to_contain_text("plan: free")
    expect(section.get_by_role("meter", name="Storage used")).to_have_attribute("aria-valuenow", "30")
    expect(section.get_by_role("meter", name="Daily uploads used")).to_have_attribute("aria-valuenow", "20")
    expect(section.get_by_role("meter")).to_have_count(3)
    expect(section.get_by_role("heading", name="Usage", exact=True)).to_be_visible()
    expect(section).to_contain_text("1.5 GB of 5.0 GB")
    expect(section).to_contain_text("0.4 GB of 2.0 GB")
    expect(section).to_contain_text("600")
    expect(section).to_contain_text("of 5")
    assert_no_overflow(page)

    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "plain", port=0)
    _, admin = httpd.keys.create("admin", "admin")
    httpd.users.set_password("admin", PASSWORD)
    thread, url = _serve(httpd)
    try:
        page.goto(url)
        sign_in_password(page, {"url": url}, "admin", PASSWORD)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        card = open_settings(page, "workspace")
        expect(card.get_by_text("slug default")).to_be_visible()
        expect(page.get_by_role("region", name="Usage", exact=True)).to_be_hidden()
        assert_no_overflow(page)
    finally:
        _stop(httpd, thread)


def test_usage_banner_at_80_and_100_percent(page, usage_server):
    page.set_viewport_size({"width": 375, "height": 812})
    httpd = usage_server["httpd"]
    set_usage(httpd, stored=4_100_000_000, uploaded=1_640_000_000)
    sign_in_password(page, usage_server, "admin", PASSWORD)
    expect(page.get_by_text("Storage 82% used", exact=True)).to_be_visible()
    expect(page.get_by_text("Daily uploads 82% used", exact=True)).to_be_visible()
    assert_no_overflow(page)

    set_usage(httpd, stored=900_000_000, uploaded=360_000_000)
    page.reload()
    expect(page.get_by_text("Storage limit reached: syncing is paused", exact=True)).to_be_visible()
    expect(page.get_by_text("Daily upload limit reached: resets at 00:00 UTC", exact=True)).to_be_visible()
    assert_no_overflow(page)


def test_no_usage_banner_without_limits(page, tmp_path, monkeypatch):
    page.set_viewport_size({"width": 375, "height": 812})
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    httpd = make_server(tmp_path / "data", port=0)
    _, admin = httpd.keys.create("admin", "admin")
    thread, url = _serve(httpd)
    try:
        sign_in(page, {"url": url, "admin": admin}, admin)
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.locator("#usage-banner")).to_be_hidden()
        expect(page.get_by_text("Storage 82% used", exact=True)).to_have_count(0)
        expect(page.get_by_text("Storage limit reached: syncing is paused", exact=True)).to_have_count(0)
        assert_no_overflow(page)
    finally:
        _stop(httpd, thread)


def test_new_workspace_form_success_and_error(page, signup_ui):
    page.allow_http_errors = True
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in_password(page, signup_ui, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    card = open_settings(page)
    card.get_by_text("New workspace", exact=True).click()
    card.get_by_label("Slug", exact=True).fill("lab")
    card.get_by_label("Name", exact=True).fill("Lab")
    card.get_by_role("button", name="Create", exact=True).click()
    expect(page).to_have_url(re.compile(r"/w/lab/"))
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    assert_no_overflow(page)

    card = open_settings(page)
    card.get_by_text("New workspace", exact=True).click()
    card.get_by_label("Slug", exact=True).fill("lab-two")
    card.get_by_label("Name", exact=True).fill("Lab Two")
    card.get_by_role("button", name="Create", exact=True).click()
    expect(card.get_by_role("alert")).to_have_text("you already have a free workspace")
    assert_no_overflow(page)


def test_new_workspace_item_hidden_when_signup_closed(page, usage_server):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in_password(page, usage_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    card = open_settings(page)
    expect(card.get_by_role("heading", name="Workspaces", exact=True)).to_be_visible()
    expect(card.get_by_text("New workspace", exact=True)).to_have_count(0)
    assert_no_overflow(page)


def test_admin_plan_edit_columns_and_form(page, usage_server):
    page.set_viewport_size({"width": 375, "height": 812})
    sign_in_password(page, usage_server, "admin", PASSWORD)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    open_settings(page)
    page.get_by_role("link", name="Admin", exact=True).click()
    expect(page.get_by_role("heading", name="Admin", exact=True)).to_be_visible()
    table = page.get_by_role("table", name="Workspaces", exact=True)
    expect(table.get_by_role("columnheader", name="Plan", exact=True)).to_be_visible()
    expect(table.get_by_role("columnheader", name="Storage", exact=True)).to_be_visible()
    expect(table.get_by_role("columnheader", name="Uploads today", exact=True)).to_be_visible()
    expect(table.get_by_role("columnheader", name="People", exact=True)).to_be_visible()
    row = table.get_by_role("row").filter(has=page.locator('td[data-label="Slug"]').filter(
        has_text=re.compile(r"^default$")))
    expect(row.locator('td[data-label="Plan"]')).to_have_text("free")
    row.get_by_role("button", name="Plan", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit plan", exact=True)
    expect(dialog).to_be_visible()
    assert_no_overflow(page)
    dialog.get_by_label("Plan", exact=True).select_option("custom")
    dialog.get_by_label("Storage bytes", exact=True).fill("1000")
    dialog.get_by_label("Upload bytes per day", exact=True).fill("2000")
    dialog.get_by_label("Requests per minute", exact=True).fill("30")
    dialog.get_by_label("People", exact=True).fill("9")
    dialog.get_by_role("button", name="Save", exact=True).click()
    expect(row.locator('td[data-label="Plan"]')).to_have_text("custom")
    stored = WorkspaceRepo(usage_server["httpd"].db).get("default")
    assert stored["plan"] == "custom"
    assert json.loads(stored["custom_limits"]) == {
        "storage_bytes": 1000, "upload_bytes_per_day": 2000,
        "requests_per_minute": 30, "people": 9,
    }
    row.get_by_role("button", name="Plan", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit plan", exact=True)
    expect(dialog.get_by_label("Plan", exact=True)).to_have_value("custom")
    expect(dialog.get_by_label("Storage bytes", exact=True)).to_have_value("1000")
    expect(dialog.get_by_label("Upload bytes per day", exact=True)).to_have_value("2000")
    expect(dialog.get_by_label("Requests per minute", exact=True)).to_have_value("30")
    expect(dialog.get_by_label("People", exact=True)).to_have_value("9")
    assert_no_overflow(page)
    dialog.get_by_role("button", name="Save", exact=True).click()
    expect(row.locator('td[data-label="Plan"]')).to_have_text("custom")
    stored = WorkspaceRepo(usage_server["httpd"].db).get("default")
    assert json.loads(stored["custom_limits"]) == {
        "storage_bytes": 1000, "upload_bytes_per_day": 2000,
        "requests_per_minute": 30, "people": 9,
    }
    assert_no_overflow(page)
