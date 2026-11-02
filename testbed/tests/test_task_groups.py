"""Task groups exercise the real dashboard against deterministic stores."""
import json
import re

import pytest

from test_dashboard import (
    _lineage_project, browser, context, home, mock_dashboard, page, row,
)
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.ui]


def _tasks(root, specs, *, parents=None):
    ids = _lineage_project(root, [f"run-{i}" for i in range(len(specs))], parents=parents)
    for run_id, spec in zip(ids, specs):
        path = root / "runs" / run_id / "meta.json"
        meta = json.loads(path.read_text())
        meta.update(spec)
        path.write_text(json.dumps(meta))
    return ids


def _group(page, task):
    return page.locator(f'.task-group[data-task="{task}"]')


def _no_overflow(page):
    assert page.evaluate("""Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth)""") <= 0


@pytest.mark.parametrize("width", [1400, 375])
def test_runs_group_by_task_with_source_summary_and_strip(page, tmp_path, ui_server_factory, width):
    root = tmp_path / "tasks"
    _tasks(root, [
        dict(task="asr", task_source="entry", name="ASR parent"),
        dict(task="imagegen", task_source="marker", name="Image child"),
        dict(task="asr", task_source="entry", name="ASR child"),
        dict(task="", name="loose run"),
    ], parents=(None, 0, 0, None))
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": width, "height": 900})
        home(page, server)
        expect(page.locator(".runs-summary")).to_have_text("4 runs · 1 project · 2 tasks")
        expect(page.locator(".project-meta")).to_have_text("2 tasks · 4 runs")
        # Newest activity first; runs without a task close the card.
        expect(page.locator(".task-name")).to_have_text(["asr", "imagegen", "no task"])
        expect(_group(page, "asr").locator(".task-source")).to_have_text("task · entry")
        expect(_group(page, "imagegen").locator(".task-source")).to_have_text("task · marker")
        expect(_group(page, "").locator(".task-source")).to_have_count(0)
        expect(_group(page, "asr").locator(".task-summary")).to_contain_text("2 runs · last ")
        expect(_group(page, "asr").locator(".run-row")).to_have_count(2)
        # The image child's parent is in another task, so it starts its own strip.
        expect(_group(page, "asr").locator(".task-strip-dot")).to_have_count(2)
        expect(_group(page, "asr").locator(".task-strip-line")).to_have_count(1)
        expect(_group(page, "asr").locator(".task-strip")).to_have_attribute(
            "aria-label", "Lineage of asr: 2 runs, 2 levels")
        expect(_group(page, "imagegen").locator(".task-strip-dot")).to_have_count(1)
        expect(_group(page, "imagegen").locator(".task-strip-line")).to_have_count(0)
        # Δ vs parent still follows the explicit parent across tasks.
        expect(row(page, "Image child").locator(".run-varied")).not_to_have_text("root")
        _no_overflow(page)


def test_strip_opens_a_lane_per_branch(page, tmp_path, ui_server_factory):
    root = tmp_path / "branches"
    _tasks(root, [dict(task="asr")] * 4, parents=(None, 0, 0, 1))
    with ui_server_factory(root) as server:
        home(page, server)
        strip = _group(page, "asr").locator(".task-strip")
        expect(strip).to_have_attribute("aria-label", "Lineage of asr: 4 runs, 3 levels")
        # Three generations wide, two lanes tall.
        assert strip.evaluate("el => [el.style.width, el.style.height]") == ["57px", "21px"]
        tops = strip.locator(".task-strip-dot").evaluate_all("els => els.map(el => el.style.top)")
        assert sorted(set(tops)) == ["11px", "3px"]


def test_collapse_expand_and_show_more(page, tmp_path, ui_server_factory):
    root = tmp_path / "many"
    _tasks(root, [dict(task="asr")] * 7 + [dict(task="tts")])
    with ui_server_factory(root) as server:
        home(page, server)
        asr = _group(page, "asr")
        expect(asr.locator(".run-row")).to_have_count(5)
        asr.get_by_role("button", name="Show 2 more", exact=True).click()
        expect(asr.locator(".run-row")).to_have_count(7)
        asr.get_by_role("button", name="Show latest 5 only", exact=True).click()
        expect(asr.locator(".run-row")).to_have_count(5)
        toggle = page.get_by_role("button", name="Collapse asr", exact=True)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        toggle.click()
        expanded = page.get_by_role("button", name="Expand asr", exact=True)
        expect(expanded).to_have_attribute("aria-expanded", "false")
        expect(expanded).to_be_focused()
        expect(asr.locator(".task-body")).to_have_count(0)
        expect(_group(page, "tts").locator(".run-row")).to_have_count(1)
        page.get_by_role("button", name="Collapse all", exact=True).click()
        expect(page.locator(".task-body")).to_have_count(0)
        page.get_by_role("button", name="Expand all", exact=True).click()
        expect(page.locator(".task-body")).to_have_count(2)


def test_task_filter_and_cross_task_selection(page, tmp_path, ui_server_factory):
    root = tmp_path / "filter"
    _tasks(root, [
        dict(task="asr", name="asr one"), dict(task="asr", name="asr two"),
        dict(task="imagegen", name="image one"),
    ], parents=(None, 0, None))
    with ui_server_factory(root) as server:
        home(page, server)
        page.get_by_role("searchbox", name="Filter runs").fill("task:image")
        expect(page.locator(".task-name")).to_have_text(["imagegen"])
        page.get_by_role("searchbox", name="Filter runs").fill("")
        expect(page.locator(".task-name")).to_have_text(["imagegen", "asr"])
        bar = page.get_by_test_id("selection-bar")
        page.get_by_role("checkbox", name="Select asr one", exact=True).check()
        page.get_by_role("checkbox", name="Select asr two", exact=True).check()
        expect(bar.locator(".selection-hint")).to_be_visible()
        expect(bar.locator(".selection-warn")).to_be_hidden()
        page.get_by_role("checkbox", name="Select image one", exact=True).check()
        expect(bar.locator(".selection-warn")).to_have_text("Different tasks — Δ isn't lineage-based")
        expect(bar.locator(".selection-hint")).to_be_hidden()
        expect(bar.get_by_role("button", name="Compare runs", exact=True)).to_be_enabled()


@pytest.mark.parametrize("width", [1400, 375])
def test_task_metadata_is_literal_text(page, tmp_path, ui_server_factory, width):
    root = tmp_path / "xss"
    task = "<img src=x onerror=window.__xss=1>"
    name = "<svg onload=window.__xss=1>"
    _tasks(root, [
        dict(task=task, task_source="<b>explicit</b>"),
        dict(task=task, name=name),
    ])
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": width, "height": 900})
        home(page, server)
        expect(page.locator(".task-name")).to_have_text([task])
        expect(page.locator(".task-source")).to_have_text("task · <b>explicit</b>")
        expect(page.locator(f".task-strip-dot").nth(1)).to_have_attribute("title", name)
        expect(page.get_by_role("link", name=name, exact=True)).to_have_text(name)
        expect(page.locator("[onerror], [onload]")).to_have_count(0)
        assert page.evaluate("typeof window.__xss === 'undefined'")
        _no_overflow(page)


def test_task_change_on_poll_regroups_and_keeps_focus(page, ui_server):
    runs = [
        dict(run_id="a", name="parent", project="demo", status="running", keys=[],
             task="asr", task_source="explicit", created_at="2026-10-01"),
        dict(run_id="b", name="child", project="demo", status="finished", keys=[],
             task="asr", task_source="explicit", created_at="2026-10-02"),
    ]
    mock_dashboard(page, runs)
    page.clock.install()
    home(page, ui_server)
    expect(page.locator(".task-name")).to_have_text(["asr"])
    page.get_by_role("checkbox", name="Select child", exact=True).focus()
    page.keyboard.press("Space")
    runs[1]["task"] = "imagegen"
    page.clock.fast_forward(5000)
    expect(page.locator(".task-name")).to_have_text(["imagegen", "asr"])
    checkbox = page.get_by_role("checkbox", name="Select child", exact=True)
    expect(checkbox).to_be_focused()
    expect(checkbox).to_be_checked()
    expect(row(page, "child")).to_have_attribute("data-picked", "true")


def _scored(root, values, parents, extra=None):
    """One "asr" task whose runs logged best_val_acc."""
    extra = extra or {}
    specs = [dict(task="asr", task_source="entry", name=f"run-{i}",
                  summary={"best_val_acc": value}, **extra.get(i, {}))
             for i, value in enumerate(values)]
    return _tasks(root, specs, parents=parents)


def _open_lineage(page, task="asr"):
    page.get_by_role("button", name=f"Open lineage of {task}", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    return dialog


@pytest.mark.parametrize("width", [1440, 375])
def test_lineage_view_graph_best_path_and_inspector(page, tmp_path, ui_server_factory, width):
    root = tmp_path / "lineage-view"
    ids = _scored(root, [0.5, 0.6, 0.7, 0.65, 0.9], parents=(None, 0, 0, 1, 1))
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": width, "height": 900})
        home(page, server)
        dialog = _open_lineage(page)
        expect(dialog).to_have_attribute("aria-label", "Lineage of demo/asr")
        expect(dialog.locator(".lv-meta")).to_have_text(
            "5 runs · 3 generations · 3 branches · best run-4 (val_acc 0.9)")
        expect(dialog.locator(".lv-gen")).to_have_text(["root", "gen 1", "gen 2"])
        # Focus starts on the best run; the best path runs root → run-1 → run-4.
        expect(page.locator(".lv-node.is-focused .lv-box")).to_be_focused()
        expect(page.locator(".lv-node.is-focused")).to_have_attribute("data-run-id", ids[4])
        best_path = page.locator(".lv-node.is-best-path").evaluate_all("els => els.map(el => el.dataset.runId)")
        assert sorted(best_path) == sorted([ids[0], ids[1], ids[4]])
        expect(page.locator(f'.lv-node[data-run-id="{ids[4]}"] .lv-badge')).to_have_text("★ best")
        # The subtree holding the best run keeps the top lane.
        top = lambda i: page.locator(f'.lv-node[data-run-id="{ids[i]}"]').evaluate("el => parseFloat(el.style.top)")
        assert top(0) == top(1) == top(4) < top(2)
        inspector = dialog.get_by_role("complementary", name="Selected run")
        expect(inspector.locator(".lv-insp-sub")).to_have_text(f"run {ids[4][-8:]} · child of run-1 · gen 2")
        expect(inspector.locator(".lv-stats dd")).to_have_text(["0.9", "+0.3", "+0.4"])
        expect(inspector.get_by_role("link", name="Compare with parent")).to_have_attribute(
            "href", f"#/compare?runs={ids[1]}&runs={ids[4]}")
        # Children move the focus; the root shows its own config.
        page.locator(f'.lv-node[data-run-id="{ids[0]}"] .lv-box').click()
        expect(inspector.locator(".lv-insp-sub")).to_have_text(f"run {ids[0][-8:]} · root of asr · gen 0")
        expect(inspector.locator(".lv-kid")).to_have_count(2)
        inspector.locator(".lv-kid").filter(has_text="run-2").click()
        expect(page.locator(".lv-node.is-focused")).to_have_attribute("data-run-id", ids[2])
        dialog.get_by_role("checkbox", name="Highlight best path").uncheck()
        expect(page.locator(".lv-node.is-best-path")).to_have_count(0)
        expect(page.locator(".lv-edge.is-best")).to_have_count(0)
        dialog.get_by_role("searchbox", name="Find run or param").fill("run-3")
        expect(page.locator(".lv-node:not(.is-dim)")).to_have_count(1)
        _no_overflow(page)
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(page.get_by_role("button", name="Open lineage of asr", exact=True)).to_be_focused()


def test_lineage_tray_shares_selection_with_table(page, tmp_path, ui_server_factory):
    root = tmp_path / "lineage-tray"
    ids = _scored(root, [0.5, 0.6, 0.7, 0.9], parents=(None, 0, 0, 1))
    with ui_server_factory(root) as server:
        home(page, server)
        page.get_by_role("checkbox", name="Select run-2", exact=True).check()
        dialog = _open_lineage(page)
        tray = dialog.locator(".lv-tray")
        expect(tray).to_contain_text("Tick two or more runs in this task to compare them.")
        expect(dialog.get_by_role("checkbox", name="Select run-2 for comparison")).to_be_checked()
        dialog.get_by_role("button", name="Best path", exact=True).click()
        expect(tray.locator(".lv-tray-count")).to_have_text("3 selected in asr")
        expect(tray.locator(".lv-chip-name")).to_have_text(["run-0", "run-1", "run-3"])
        compare = tray.get_by_role("link", name="Compare 3 runs")
        expect(compare).to_have_attribute("href", f"#/compare?selection=1&runs={ids[0]}&runs={ids[1]}&runs={ids[3]}")
        dialog.get_by_role("button", name="Remove run-1 from comparison").click()
        expect(tray.locator(".lv-chip-name")).to_have_text(["run-0", "run-3"])
        # Family of the focused best run: itself and its parent.
        dialog.get_by_role("button", name="run-3 + parent & children").click()
        expect(tray.locator(".lv-chip-name")).to_have_text(["run-1", "run-3"])
        dialog.get_by_role("checkbox", name="Include in comparison").uncheck()
        expect(tray.locator(".lv-chip-name")).to_have_text(["run-1"])
        dialog.get_by_role("button", name="Close lineage").click()
        expect(page.get_by_test_id("selection-bar")).to_contain_text("1 run selected")
        expect(row(page, "run-1")).to_have_attribute("data-picked", "true")
        _open_lineage(page).get_by_role("button", name="Clear", exact=True).click()
        page.keyboard.press("Escape")
        expect(page.locator(".run-row[data-picked=true]")).to_have_count(0)
        _open_lineage(page).get_by_role("button", name="Best path", exact=True).click()
        page.get_by_role("link", name="Compare 3 runs").click()
        expect(page.get_by_role("heading", name="Compare 3 runs")).to_be_visible()
        expect(page.locator(".lv-dialog")).to_have_count(0)
        assert page.evaluate("!document.documentElement.classList.contains('has-dialog')")


def test_lineage_view_traps_focus_and_flags_failures(page, tmp_path, ui_server_factory):
    root = tmp_path / "lineage-failed"
    ids = _scored(root, [0.5, 0.6], parents=(None, 0), extra={1: {"status": "crashed"}})
    with ui_server_factory(root) as server:
        home(page, server)
        dialog = _open_lineage(page)
        node = page.locator(f'.lv-node[data-run-id="{ids[1]}"]')
        expect(node).to_have_class(re.compile(r"\bis-failed\b"))
        expect(node.locator(".lv-badge")).to_have_text("failed")
        node.locator(".lv-box").click()
        expect(dialog.locator(".callout-warn")).to_contain_text("Run failed")
        for _ in range(40):
            page.keyboard.press("Tab")
            assert page.evaluate("document.querySelector('.lv-dialog').contains(document.activeElement)")
        # The backdrop closes the dialog; the page behind never scrolled.
        page.mouse.click(3, 3)
        expect(page.get_by_role("dialog")).to_have_count(0)


def test_lineage_view_follows_polls(page, ui_server):
    runs = [
        dict(run_id="a", name="parent", project="demo", status="running", keys=[], task="asr",
             task_source="explicit", created_at="2026-10-01", summary={"loss": 1.0}),
    ]
    mock_dashboard(page, runs)
    page.clock.install()
    home(page, ui_server)
    dialog = _open_lineage(page)
    expect(dialog.locator(".lv-node")).to_have_count(1)
    find = dialog.get_by_role("searchbox", name="Find run or param")
    find.focus()
    runs.append(dict(run_id="b", name="child", project="demo", status="running", keys=[], task="asr",
                     task_source="explicit", created_at="2026-10-02", summary={"loss": 0.5},
                     parent_run_id="a"))
    page.clock.fast_forward(5000)
    expect(dialog.locator(".lv-node")).to_have_count(2)
    expect(dialog.locator(".lv-meta")).to_contain_text("2 runs · 2 generations · 1 branch · 2 running")
    expect(find).to_be_focused()
    runs.clear()
    page.clock.fast_forward(5000)
    expect(page.get_by_role("dialog")).to_have_count(0)
