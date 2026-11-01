"""Metrics workspace: the full-window scalar view on the run page."""
import re

import pytest

from test_dashboard import browser, context, open_run, page, painted
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.ui]


def _open(page, server, name="explicit-highlr"):
    open_run(page, server, name)
    page.get_by_role("button", name="Open in full window ⤢", exact=True).click()
    workspace = page.get_by_role("dialog", name=f"Metrics workspace for {name}")
    expect(workspace).to_be_visible()
    expect(workspace.locator(".ws-card").first).to_be_visible()
    return workspace


def _keys(workspace):
    return workspace.locator(".ws-card").evaluate_all("els => els.map(el => el.dataset.key)")


def _run_lines(canvas):
    return [s for s in painted(canvas)["strokes"] if s["width"] == 2 and len(s["path"]) > 2]


def test_workspace_lists_task_runs_and_overlays_them(page, ui_server):
    workspace = _open(page, ui_server)
    expect(workspace.locator(".ws-crumbs")).to_have_text("agenticml / testbed / dummytrain / explicit-highlr /")
    runs = workspace.get_by_role("complementary", name="Metrics settings").locator(".ws-run")
    # Every run of the task is listed; this run comes first and is shown.
    expect(runs.first.locator(".ws-run-name")).to_have_text("explicit-highlr")
    expect(runs.first.locator(".ws-run-meta")).to_contain_text("this run")
    expect(runs.first.get_by_role("checkbox")).to_be_checked()
    names = runs.locator(".ws-run-name").all_inner_texts()
    assert set(names) == {"explicit-highlr", "explicit-baseline", "zero-code", "crashed"}
    no_scalars = runs.filter(has_text="zero-code")
    expect(no_scalars.get_by_role("checkbox")).to_be_disabled()
    expect(no_scalars.locator(".ws-run-meta")).to_contain_text("no scalars")
    canvas = workspace.locator('.ws-card[data-key="train/loss"] canvas')
    before = len(_run_lines(canvas))
    shown = runs.filter(has=page.get_by_role("checkbox", checked=True)).count()
    # Defaults are this run and its lineage parent; other runs load on tick.
    other = runs.filter(has_text="crashed").get_by_role("checkbox")
    expect(other).not_to_be_checked()
    other.check()
    expect(workspace.locator(".ws-meta")).to_contain_text(f"{shown + 1} runs")
    assert len(_run_lines(workspace.locator('.ws-card[data-key="train/loss"] canvas'))) > before
    # The run filter takes a regex.
    workspace.get_by_role("searchbox", name="Filter runs").fill("^explicit-")
    expect(runs.locator(".ws-run-name")).to_have_text(["explicit-highlr", "explicit-baseline"])
    # Hover snaps to a logged step and lists every shown run.
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
    tip = workspace.locator('.ws-card[data-key="train/loss"] .ws-tip')
    expect(tip).to_be_visible()
    expect(tip).to_contain_text("Smoothed")
    expect(tip.locator(".ws-tip-name").first).to_be_visible()


def test_workspace_tags_columns_cards_and_shared_smoothing(page, ui_server):
    workspace = _open(page, ui_server)
    expect(workspace.locator(".ws-group-name")).to_have_text(["train/", "val/"])
    tags = workspace.get_by_role("searchbox", name="Filter tags")
    tags.fill("val/")
    assert sorted(_keys(workspace)) == ["val/acc", "val/loss"]
    tags.fill("loss$")
    assert sorted(_keys(workspace)) == ["train/loss", "val/loss"]
    tags.fill("nothing-matches")
    expect(workspace.locator(".ws-empty")).to_have_text("No tags match the filter.")
    tags.fill("")
    workspace.get_by_role("button", name="3 columns").click()
    expect(workspace.get_by_role("button", name="3 columns")).to_have_attribute("aria-pressed", "true")
    assert workspace.locator(".ws-grid").first.evaluate(
        "el => getComputedStyle(el).gridTemplateColumns.split(' ').length") == 3
    workspace.get_by_role("button", name="train/ 2 tags", exact=True).click()
    expect(workspace.locator('.ws-group[data-group="train"] .ws-card')).to_have_count(0)
    expect(workspace.locator('.ws-group[data-group="train"] .ws-group-toggle')).to_have_attribute("aria-expanded", "false")
    card = workspace.locator('.ws-card[data-key="val/loss"]')
    card.get_by_role("button", name="Enlarge val/loss").click()
    expect(card).to_have_class(re.compile(r"\bis-wide\b"))
    assert card.evaluate("el => el.getBoundingClientRect().width") > workspace.locator(
        '.ws-card[data-key="val/acc"]').evaluate("el => el.getBoundingClientRect().width") * 2
    log = card.get_by_role("button", name="Log scale for val/loss")
    log.click()
    expect(log).to_have_attribute("aria-pressed", "true")
    painted(card.locator("canvas"))
    # Smoothing is the page's setting; Step is the only axis these runs can use.
    workspace.get_by_label("Smoothing", exact=True).fill("0")
    expect(workspace.get_by_role("button", name="Step", exact=True)).to_have_attribute("aria-pressed", "true")
    with page.expect_download() as info:
        card.get_by_role("button", name="Download val/loss as CSV").click()
    assert info.value.suggested_filename == "val_loss.csv"
    with open(info.value.path(), encoding="utf-8") as handle:
        assert handle.readline().strip() == "run,run_id,step,value,smoothed"
    page.keyboard.press("Escape")
    expect(page.locator(".ws")).to_have_count(0)
    expect(page.get_by_label("EMA smoothing", exact=True)).to_have_value("0")
    expect(page.get_by_role("button", name="Open in full window ⤢", exact=True)).to_be_focused()


def test_chart_card_opens_workspace_enlarged(page, ui_server):
    open_run(page, ui_server, "explicit-highlr")
    page.locator('.chart-card[data-key="val/acc"]').click()
    workspace = page.locator(".ws")
    expect(workspace.locator('.ws-card[data-key="val/acc"]')).to_have_class(re.compile(r"\bis-wide\b"))
    expect(workspace.get_by_role("button", name="Shrink val/acc")).to_be_focused()
    for _ in range(30):
        page.keyboard.press("Tab")
        assert page.evaluate("document.querySelector('.ws').contains(document.activeElement)")
    workspace.get_by_role("button", name="Close metrics workspace").click()
    expect(workspace).to_have_count(0)
    assert page.evaluate("!document.documentElement.classList.contains('has-dialog')")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_workspace_fits_mobile(page, ui_server, theme):
    page.emulate_media(color_scheme=theme)
    page.set_viewport_size({"width": 375, "height": 812})
    workspace = _open(page, ui_server)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert workspace.evaluate("el => el.scrollWidth <= el.clientWidth")
    # Settings stack under the charts.
    main = workspace.locator(".ws-main").bounding_box()
    side = workspace.locator(".ws-side").bounding_box()
    assert side["y"] >= main["y"] + main["height"] - 1
