"""Task lanes exercise the real dashboard against deterministic stores."""
import json

import pytest

from test_dashboard import (
    _lineage_project, browser, context, home, mock_dashboard, page,
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


def _node(page, run_id):
    return page.locator(f'.lineage-node[data-run-id="{run_id}"]')


def _no_overflow(page):
    assert page.evaluate("""Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth)""") <= 0


@pytest.mark.parametrize("width", [1400, 375])
def test_task_lanes_cross_parent_arch_and_keyboard(page, tmp_path, ui_server_factory, width):
    root = tmp_path / "tasks"
    ids = _tasks(root, [
        dict(task="asr", task_source="entry", name="ASR parent",
             arch={"model_class": "models.Encoder", "model_type": "encoder"}),
        dict(task="imagegen", task_source="marker", name="Image child",
             arch={"model_class": "models.Diffusion", "model_type": "diffusion"}),
        dict(task="asr", task_source="entry", name="ASR child",
             arch={"model_class": "models.Encoder", "model_type": "encoder"}),
    ], parents=(None, 0, 0))
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": width, "height": 900})
        home(page, server)
        expect(page.locator(".lineage-lane-title")).to_have_text(["asr", "imagegen"])
        expect(page.locator(".lineage-lane-source")).to_have_text(["from asr/", "agenticml.toml"])
        expect(page.locator(".lineage-graph")).to_have_count(2)
        for task in ("asr", "imagegen"):
            lane = page.get_by_role("region", name=task, exact=True)
            expect(lane).to_have_class("lineage-lane")
            assert lane.get_attribute("aria-labelledby") == lane.locator("h3").get_attribute("id")
        marker_ids = page.locator(".lineage-graph marker").evaluate_all("els => els.map(el => el.id)")
        assert len(marker_ids) == len(set(marker_ids)) == 2
        for graph in page.locator(".lineage-graph").all():
            marker_id = graph.locator("marker").get_attribute("id")
            for edge in graph.locator(".lineage-edge").all():
                expect(edge).to_have_attribute("marker-end", f"url(#{marker_id})")
        expect(page.locator(".lineage-edge")).to_have_count(1)
        expect(page.locator(".lineage-edge")).to_have_attribute("data-from", ids[0])
        expect(page.locator(".lineage-edge")).to_have_attribute("data-to", ids[2])
        cross = _node(page, ids[1]).locator(".lineage-cross-task")
        expect(cross).to_contain_text("from asr")
        expect(cross.locator("title")).to_contain_text(f"ASR parent ({ids[0]})")
        arch = _node(page, ids[1]).locator(".lineage-arch-changed")
        expect(arch).to_contain_text("arch changed")
        expect(arch.locator("title")).to_have_text("models.Encoder → models.Diffusion")
        _node(page, ids[0]).focus()
        page.keyboard.press("ArrowDown")
        expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", ids[2])
        page.keyboard.press("ArrowDown")
        expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", ids[1])
        page.keyboard.press("Space")
        expect(_node(page, ids[1])).to_have_attribute("data-picked", "true")
        facts = page.locator(".lineage-detail .lineage-facts")
        for text in ("ASR parent", "Model class", "models.Diffusion", "Model type", "diffusion"):
            expect(facts).to_contain_text(text)
        page.keyboard.press("ArrowUp")
        expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", ids[2])
        _no_overflow(page)


def test_single_no_task_keeps_original_graph(page, tmp_path, ui_server_factory):
    root = tmp_path / "unassigned"
    _tasks(root, [dict(task=""), dict(task="")])
    with ui_server_factory(root) as server:
        home(page, server)
        expect(page.locator(".lineage-scroll > .lineage-graph")).to_have_count(1)
        expect(page.locator(".lineage-lane-head")).to_have_count(0)
        expect(page.locator(".lineage-node")).to_have_count(2)
        expect(page.locator(".lineage-edge")).to_have_count(1)


def test_task_source_hints_no_task_and_long_mobile_lanes(page, tmp_path, ui_server_factory):
    root = tmp_path / "sources"
    long_task = "very-long-task/" * 8
    _tasks(root, [
        dict(task="", task_source=None), dict(task="asr", task_source="explicit"),
        dict(task="tts", task_source="env"), dict(task="old", task_source="legacy"),
        *[dict(task=long_task, task_source="entry",
               arch={"model_class": "models." + "HugeClass" * 24, "model_type": "x" * 256})
          for _ in range(6)],
    ], parents=(None,) * 10)
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": 375, "height": 812})
        home(page, server)
        expect(page.locator(".lineage-lane-title")).to_have_text([long_task, "old", "tts", "asr", "No task"])
        expect(page.locator(".lineage-lane-source")).to_have_text([f"from {long_task}/", "legacy", "env", "explicit"])
        scroller = page.locator(".lineage-lane-scroll").first
        assert scroller.evaluate("el => el.scrollWidth > el.clientWidth && getComputedStyle(el).overflowX === 'auto'")
        _no_overflow(page)
        page.locator(".lineage-node").first.click()
        expect(page.locator(".lineage-facts")).to_contain_text("HugeClass")
        _no_overflow(page)


def test_single_no_task_preserves_badge_spacing(page, tmp_path, ui_server_factory):
    root = tmp_path / "badge-spacing"
    ids = _tasks(root, [dict(task="")] * 3)
    path = root / "runs" / ids[2] / "meta.json"
    meta = json.loads(path.read_text())
    meta["lineage"]["error"] = "snapshot failed"
    path.write_text(json.dumps(meta))
    with ui_server_factory(root) as server:
        home(page, server)
        node = _node(page, ids[2])
        expect(node.locator(".lineage-error")).to_be_visible()
        shape = node.locator("rect")
        expect(shape).to_have_attribute("height", "64")
        x, y = float(shape.get_attribute("x")), float(shape.get_attribute("y"))
        for selector, offset in ((".lineage-dirty", 28), (".lineage-same", 66), (".lineage-error", 126.4)):
            badge = node.locator(selector)
            assert float(badge.get_attribute("x")) == pytest.approx(x + offset)
            assert float(badge.get_attribute("y")) == y + 54


@pytest.mark.parametrize("width", [1400, 375])
def test_lane_metadata_is_literal_text(page, tmp_path, ui_server_factory, width):
    root = tmp_path / "xss"
    task = "<img src=x onerror=window.__xss=1>"
    name = "<svg onload=window.__xss=1>"
    model_class = "<img src=x onerror=window.__xss=1>"
    ids = _tasks(root, [
        dict(task=task, task_source="explicit", arch={"model_class": "models.Safe"}),
        dict(task="child", name=name, arch={"model_class": model_class}),
    ])
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": width, "height": 900})
        home(page, server)
        expect(page.get_by_role("region", name=task, exact=True).locator("h3")).to_have_text(task)
        node = _node(page, ids[1])
        expect(node.locator(".lineage-name > title")).to_have_text(name)
        expect(page.get_by_role("link", name=name, exact=True)).to_have_text(name)
        expect(node.locator(".lineage-cross-task > title")).to_contain_text(f"from {task}:")
        expect(node.locator(".lineage-arch-changed > title")).to_have_text(f"models.Safe → {model_class}")
        node.click()
        expect(page.locator(".lineage-facts").get_by_text(model_class, exact=True)).to_be_visible()
        expect(page.locator("[onerror], [onload]")).to_have_count(0)
        assert page.evaluate("typeof window.__xss === 'undefined'")
        _no_overflow(page)


def test_task_only_poll_change_rebuilds_lanes_and_keeps_focus(page, ui_server):
    runs = [
        dict(run_id="a", name="parent", project="demo", status="running", keys=[],
             task="asr", task_source="explicit", created_at="2026-10-01"),
        dict(run_id="b", name="child", project="demo", status="finished", keys=[],
             task="asr", task_source="explicit", created_at="2026-10-02",
             arch={"model_class": "models.Original", "model_type": "encoder"}),
    ]
    mock_dashboard(page, runs)
    page.route("**/api/runs/b/lineage", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"files": [], "commit": "abcdef"})))
    page.clock.install()
    home(page, ui_server)
    expect(page.locator(".lineage-lane-title")).to_have_text(["asr"])
    _node(page, "b").focus()
    page.keyboard.press("Space")
    expect(page.locator(".lineage-detail")).to_contain_text("abcdef")
    runs[1]["task"] = "imagegen"
    page.clock.fast_forward(5000)
    expect(page.locator(".lineage-lane-title")).to_have_text(["imagegen", "asr"])
    expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", "b")
    expect(_node(page, "b")).to_have_attribute("data-picked", "true")
    expect(page.locator(".lineage-detail")).to_contain_text("abcdef")
    page.wait_for_function("() => !document.querySelector('.project-card').dataset.detailPending")
    page.evaluate("window.keptGraph = document.querySelector('.lineage-graph'); window.keptDetail = document.querySelector('.lineage-detail').firstElementChild; window.keptRow = document.querySelector('.run-row')")
    page.clock.fast_forward(5000)
    page.wait_for_function("() => !keptRow.isConnected")
    assert page.evaluate("keptGraph === document.querySelector('.lineage-graph') && keptDetail === document.querySelector('.lineage-detail').firstElementChild")
    # Architecture-only changes also refresh the open detail strip.
    runs[1]["arch"]["model_class"] = "models.Updated"
    page.clock.fast_forward(5000)
    expect(page.locator(".lineage-facts")).to_contain_text("models.Updated")
    expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", "b")
