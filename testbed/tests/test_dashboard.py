"""Browser coverage for the local workspace, metrics, and code snapshots.

Canvas instrumentation observes real draw calls without adding test APIs to the
application. Every browser console/page/network error fails fixture teardown.
"""
import json
import re

import pytest
from agenticml.store import RunWriter
from agenticml.store.reader import RunReader

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

pytestmark = [pytest.mark.browser, pytest.mark.ui]
LIVE_TIMEOUT = 16000
COMPARE_RUNS = ("explicit-baseline", "explicit-highlr")

CANVAS_HOOK = """
(() => {
  const p = CanvasRenderingContext2D.prototype;
  const clear = p.clearRect, begin = p.beginPath, move = p.moveTo;
  const line = p.lineTo, stroke = p.stroke, text = p.fillText;
  p.clearRect = function(...args) {
    this.canvas.__draw = {strokes: [], labels: []};
    return clear.apply(this, args);
  };
  p.beginPath = function(...args) { this.__path = []; this.__moves = 0; return begin.apply(this, args); };
  p.moveTo = function(x, y) { this.__path?.push([x, y]); this.__moves++; return move.call(this, x, y); };
  p.lineTo = function(x, y) { this.__path?.push([x, y]); return line.call(this, x, y); };
  p.stroke = function(...args) {
    this.canvas.__draw?.strokes.push({path: this.__path, moves: this.__moves, color: this.strokeStyle,
      alpha: this.globalAlpha, width: this.lineWidth, dash: this.getLineDash()});
    return stroke.apply(this, args);
  };
  p.fillText = function(value, ...args) {
    this.canvas.__draw?.labels.push(String(value));
    return text.call(this, value, ...args);
  };
})();
"""


@pytest.fixture(scope="module")
def browser():
    manager = sync_playwright().start()
    try:
        launched = manager.chromium.launch()
    except Exception as exc:
        manager.stop()
        pytest.skip(f"Chromium cannot launch: {exc}")
    try:
        yield launched
    finally:
        launched.close()
        manager.stop()


@pytest.fixture
def context(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US", color_scheme="light")
    ctx.route("**/favicon.ico", lambda route: route.fulfill(status=204, body=""))
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    tab = context.new_page()
    tab.add_init_script(CANVAS_HOOK)
    problems = []
    tab.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
    tab.on("console", lambda message: problems.append(f"console: {message.text}") if message.type == "error" else None)
    tab.on("response", lambda response: problems.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
    tab.on("requestfailed", lambda request: problems.append(f"failed: {request.url}: {request.failure}") if "ERR_ABORTED" not in (request.failure or "") else None)
    tab.allow_http_errors = False
    yield tab
    if tab.allow_http_errors:
        problems = [problem for problem in problems if not problem.startswith(("HTTP 4", "HTTP 503", "console: Failed to load resource:"))]
    assert not problems, "browser errors:\n" + "\n".join(problems)
    tab.close()


def home(page, server):
    page.goto(server.url)
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.locator(".poll-status")).not_to_be_empty()


def row(page, name):
    return page.locator(".run-row").filter(has=page.get_by_role("link", name=name, exact=True))


def open_run(page, server, name="explicit-baseline"):
    page.goto(f"{server.url}#/run/{server.runs[name]}")
    expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()


def compare(page, server):
    home(page, server)
    for name in COMPARE_RUNS:
        page.get_by_role("checkbox", name=f"Select {name}", exact=True).check()
    page.get_by_role("button", name="Compare runs", exact=True).click()
    expect(page.get_by_role("heading", name="Compare runs", exact=True)).to_be_visible()
    expect(page.locator('.chart-card[data-key="train/loss"]')).to_be_visible()


def loss(page):
    return page.locator('.chart-card[data-key="train/loss"]')


def painted(canvas):
    expect(canvas).to_be_visible()
    page = canvas.page
    page.wait_for_function("el => el.width > 0 && !!el.__draw && el.__draw.strokes.some(s => s.width === 2 && s.path.length > 1)", arg=canvas.element_handle())
    return canvas.evaluate("el => el.__draw")


def test_home_lists_every_run(page, ui_server):
    home(page, ui_server)
    expect(page.locator(".run-row")).to_have_count(5)
    expect(page.locator(".poll-status")).to_have_text("5 runs")
    expect(page.get_by_text("5 runs · grouped by project", exact=True)).to_be_visible()
    for name, status, project in (("explicit-baseline", "finished", "testbed"), ("explicit-highlr", "finished", "testbed"), ("zero-code", "finished", "testbed"), ("crashed", "crashed", "testbed"), ("other-project", "finished", "other-project")):
        run = row(page, name)
        expect(run).to_be_visible()
        expect(run.locator(".chip")).to_have_text(status)
        assert run.evaluate("el => el.closest('[data-project]').dataset.project") == project
    data = page.request.get(f"{ui_server.url}/api/runs").json()
    for project in {run["project"] for run in data}:
        siblings = sorted((run for run in data if run["project"] == project), key=lambda run: run["created_at"], reverse=True)
        key = None
        for run in siblings:
            numeric = [key for key, value in run["summary"].items() if isinstance(value, (int, float)) and not isinstance(value, bool)]
            if numeric:
                key = next((key for key in numeric if re.search("loss|acc", key, re.I)), numeric[0])
                break
        if key:
            for run in siblings:
                expect(row(page, run["name"]).locator(".run-number").first).to_have_attribute("title", key)


def test_project_filter_and_search(page, ui_server):
    home(page, ui_server)
    page.get_by_role("tab", name="testbed", exact=True).click()
    expect(page.locator(".run-row")).to_have_count(4)
    page.get_by_role("tab", name="other-project", exact=True).click()
    expect(page.locator(".run-row")).to_have_count(1)
    page.get_by_role("tab", name="All", exact=True).click()
    search = page.get_by_label("Filter runs")
    for query, names in (("Zero-Code", ["zero-code"]), ("FIXTURE", ["explicit-baseline", "explicit-highlr", "crashed", "other-project"]), ("baseline", ["explicit-baseline", "crashed", "other-project"]), ("highlr", ["explicit-highlr"])):
        search.fill(query)
        expect(page.locator(".run-row")).to_have_count(len(names))
        for name in names:
            expect(row(page, name)).to_be_visible()
    search.fill(ui_server.runs["zero-code"])
    expect(page.locator(".run-row")).to_have_count(1)
    search.fill("")
    expect(page.locator(".run-row")).to_have_count(5)


@pytest.mark.parametrize("width", [375, 1400])
def test_selection_bar_and_compare_navigation(page, ui_server, width):
    page.set_viewport_size({"width": width, "height": 1000})
    home(page, ui_server)
    bar = page.get_by_test_id("selection-bar")
    expect(bar).to_be_visible()
    expect(bar).to_have_text("Select runs to compare", use_inner_text=True)
    expect(bar.get_by_role("button", name="Compare runs")).to_be_hidden()
    placeholder_box = bar.bounding_box()
    checkbox = page.get_by_role("checkbox", name="Select explicit-baseline", exact=True)
    original = checkbox.element_handle()
    second = page.get_by_role("checkbox", name="Select explicit-highlr", exact=True)
    position = second.bounding_box()
    checkbox.check()
    assert original.evaluate("el => el.isConnected && el === document.activeElement")
    assert second.bounding_box() == position
    expect(bar).to_contain_text("1 run selected")
    assert bar.bounding_box() == placeholder_box
    expect(bar.get_by_role("button", name="Compare runs")).to_be_disabled()
    expect(page).not_to_have_url(re.compile(r"#/run/"))
    page.get_by_role("checkbox", name="Select explicit-highlr", exact=True).check()
    expect(bar).to_contain_text("2 runs selected")
    expect(bar.get_by_role("button", name="Compare runs")).to_be_enabled()
    bar.get_by_role("button", name="Clear").click()
    expect(bar).to_have_text("Select runs to compare", use_inner_text=True)
    expect(bar.get_by_role("button", name="Compare runs")).to_be_hidden()
    assert bar.bounding_box() == placeholder_box
    assert second.bounding_box() == position
    expect(page.get_by_role("checkbox", checked=True)).to_have_count(0)
    compare(page, ui_server)
    assert page.url.index(ui_server.runs["explicit-baseline"]) < page.url.index(ui_server.runs["explicit-highlr"])
    expect(page.get_by_test_id("selected-runs")).to_contain_text("explicit-baseline")
    expect(page.get_by_role("heading", name="Params & metrics comparison")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Params that differ \(\d+\)"))).to_be_visible()
    expect(page.locator(".compare-focus pre")).to_contain_text('"explicit-highlr"')
    page.get_by_role("button", name="Exit focus").click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


def test_run_facts_parameters_environment_artifacts_and_media(page, ui_server):
    open_run(page, ui_server)
    expect(page.locator(".detail > .inline .chip").first).to_have_text("finished")
    identity = page.get_by_test_id("identity")
    for label in ("Run id", "Project", "Created", "Finished", "Duration", "Host", "Git commit"):
        expect(identity.locator("dt").filter(has_text=re.compile(f"^{label}$"))).to_be_visible()
    expect(identity).to_contain_text(ui_server.runs["explicit-baseline"])
    expect(identity).to_contain_text("testbed")
    assert all(value != "—" for value in identity.locator("dd").all_text_contents()[:6])
    params = page.locator('[data-card="Parameters"]')
    expect(params.locator("dt")).to_contain_text(["batch_size", "epochs", "lr"])
    params.get_by_label("Search Parameters").fill("batch_size")
    expect(params.locator("dt").filter(has_text=re.compile("^lr$"))).to_be_hidden()
    expect(params.locator("dt").filter(has_text=re.compile("^batch_size$"))).to_be_visible()
    params.get_by_label("Search Parameters").fill("")
    environment = page.locator('[data-card="Environment"]')
    expect(environment).to_contain_text("python_version")
    expect(environment).to_contain_text("hostname")
    environment.get_by_text("Packages", exact=True).click()
    expect(environment.locator("details")).to_have_js_property("open", True)
    artifacts = page.locator('[data-card="Artifacts"]')
    expect(artifacts.locator("[data-artifact]")).to_have_count(2)
    expect(artifacts).to_contain_text("checkpoint")
    expect(artifacts).to_contain_text("model")
    image = page.locator(".media-strip img")
    expect(image).to_have_count(1)
    expect(image).to_have_attribute("alt", "decision boundary")
    image.scroll_into_view_if_needed()
    page.wait_for_function("() => document.querySelector('.media-strip img')?.naturalWidth > 0")
    image.focus()
    image.press("Enter")
    dialog = page.locator("#lightbox")
    expect(dialog).to_be_visible()
    page.wait_for_function("() => document.querySelector('#lightbox img')?.naturalWidth > 0")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(dialog).to_be_hidden()
    image.click()
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


def test_code_tabs_find_wrap_and_full_window(page, ui_server):
    open_run(page, ui_server)
    viewer = page.get_by_test_id("code-inline-view")
    model = viewer.locator('.code-file[data-path$="model.py"]')
    expect(model).to_be_visible()
    expect(viewer.locator('.code-file[data-path$="train.py"]')).to_be_visible()
    expect(viewer.locator('.code-file[data-path$="data.py"]')).to_be_visible()
    expect(viewer.get_by_role("tab", name="train.py", exact=True)).to_be_visible()
    model.click()
    expect(viewer.locator(".code-lines")).to_contain_text("class LogisticRegression")
    search = viewer.get_by_label("Find in file", exact=True)
    search.fill("self")
    expect(viewer.locator(".find-count")).to_have_text(re.compile(r"1 of [1-9]\d*"))
    search.press("Enter")
    expect(viewer.locator(".find-count")).to_have_text(re.compile(r"2 of \d+"))
    search.press("Shift+Enter")
    expect(viewer.locator(".find-count")).to_have_text(re.compile(r"1 of \d+"))
    viewer.get_by_role("button", name="Next match", exact=True).click()
    expect(viewer.locator(".find-count")).to_have_text(re.compile(r"2 of \d+"))
    viewer.get_by_role("button", name="Previous match", exact=True).click()
    viewer.get_by_role("button", name="Wrap", exact=True).click()
    expect(viewer.locator(".code-lines")).to_have_class(re.compile(r"wrap"))
    viewer.locator('.code-file[data-path$="train.py"]').click()
    expect(viewer.get_by_role("tab", name="train.py", exact=True)).to_be_visible()
    expect(viewer.get_by_role("tab", name="model.py", exact=True)).to_be_visible()
    viewer.get_by_role("button", name=re.compile(r"^Close .*train.py$")).click()
    expect(viewer.get_by_role("tab", name="train.py", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Open full window ⤢").click()
    full = page.locator(".code-full")
    expect(full).to_be_visible()
    expect(full.get_by_label("Code outline")).to_be_visible()
    expect(full.locator(".code-crumb")).to_contain_text("model.py")
    page.keyboard.press("Escape")
    expect(full).to_have_count(0)
    viewer.locator(".code-lines").click()
    expect(full).to_be_visible()
    full.get_by_role("button", name="Close · Esc").click()


def test_canvas_charts_compare_smoothing_filter_and_focus(page, ui_server):
    compare(page, ui_server)
    legend = page.locator(".legend")
    expect(legend).to_contain_text("explicit-baseline")
    expect(legend).to_contain_text("explicit-highlr")
    colors = legend.locator(".dot").evaluate_all("nodes => nodes.map(el => getComputedStyle(el).backgroundColor)")
    assert len(set(colors)) == 2
    canvas = loss(page).locator("canvas")
    before = painted(canvas)
    paths = [stroke for stroke in before["strokes"] if stroke["width"] == 2 and len(stroke["path"]) > 2]
    assert len(paths) == 4 and sum(stroke["alpha"] == .22 for stroke in paths) == 2
    expect(page.get_by_label("Smoothing", exact=True)).to_have_value("0.6")
    page.get_by_label("Smoothing", exact=True).fill("0")
    after = painted(canvas)
    assert len([s for s in after["strokes"] if s["width"] == 2 and len(s["path"]) > 2]) == 2
    assert [s["path"] for s in paths if s["alpha"] == 1] != [s["path"] for s in after["strokes"] if s["width"] == 2 and len(s["path"]) > 2]
    page.get_by_label("Ignore outliers", exact=True).uncheck()
    page.get_by_label("Ignore outliers", exact=True).check()
    all_keys = page.locator(".chart-card").evaluate_all("nodes => nodes.map(el => el.dataset.key)")
    assert {"train/loss", "train/lr", "val/loss", "val/acc"} <= set(all_keys)
    for query in ("train/loss", "VAL/", "loss"):
        page.get_by_label("Filter tags", exact=True).fill(query)
        expected = sorted(key for key in all_keys if query.lower() in key.lower())
        assert sorted(page.locator(".chart-card").evaluate_all("nodes => nodes.map(el => el.dataset.key)")) == expected
    page.get_by_label("Filter tags", exact=True).fill("")
    loss(page).click()
    overlay = page.locator(".chart-focus")
    expect(overlay).to_be_visible()
    expect(overlay.get_by_role("heading", name="train/loss", exact=True)).to_be_visible()
    full_canvas = overlay.locator("canvas")
    painted(full_canvas)
    box = full_canvas.bounding_box()
    page.mouse.move(box["x"] + box["width"] * .5, box["y"] + 100)
    expect(overlay.get_by_test_id("hover-table")).to_contain_text("Step · hover")
    assert any(s["dash"] == [3, 3] for s in full_canvas.evaluate("el => el.__draw.strokes"))
    page.keyboard.press("Escape")
    expect(overlay).to_have_count(0)
    expect(page.locator(".compare-focus")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()


def test_ema_matches_hosted_bias_correction(page, ui_server):
    home(page, ui_server)
    result = page.evaluate("async () => (await import('/static/charts.js')).emaSmooth([1, 2, 4], .6)")
    assert result == pytest.approx([1, 1.625, 2.836734693877551])


def test_theme_cycles_persists_and_repaints_chart(page, ui_server):
    open_run(page, ui_server)
    canvas = loss(page).locator("canvas")
    before = painted(canvas)
    theme = page.get_by_role("button", name="Change color theme")
    expect(theme).to_have_text("Theme: system")
    theme.click()
    expect(theme).to_have_text("Theme: light")
    theme.click()
    expect(theme).to_have_text("Theme: dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert painted(canvas)["strokes"][0]["color"] != before["strokes"][0]["color"]
    assert page.evaluate("localStorage.getItem('agenticml-theme')") == "dark"
    page.reload()
    expect(theme).to_have_text("Theme: dark")
    theme.click()
    expect(theme).to_have_text("Theme: system")


def test_back_and_forward_and_route_cancellation(page, ui_server):
    home(page, ui_server)
    page.get_by_role("link", name="explicit-baseline", exact=True).click()
    expect(page.get_by_role("heading", name="explicit-baseline", exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role("heading", name="explicit-baseline", exact=True)).to_be_visible()
    page.get_by_role("link", name="← All runs").click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    page.evaluate("id => { location.hash = '#/run/' + id; setTimeout(() => { location.hash = '#/'; }, 5); }", ui_server.runs["explicit-highlr"])
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    expect(page.locator(".code-full, .chart-focus")).to_have_count(0)


@pytest.mark.parametrize(("run_id", "status"), [("unknown-valid-id", 404), (".hidden", 400)])
def test_bad_run_route_shows_error(page, ui_server, run_id, status):
    page.allow_http_errors = True
    page.goto(f"{ui_server.url}#/run/{run_id}")
    expect(page.locator("#app .error")).to_contain_text(str(status))
    expect(page.locator("#app .error")).to_have_count(1)


def test_empty_store_and_friendly_compare(page, tmp_path, ui_server_factory):
    root = tmp_path / "empty"
    root.mkdir()
    with ui_server_factory(root) as server:
        home(page, server)
        expect(page.get_by_text("No runs found. Start a local experiment to see it here.", exact=True)).to_be_visible()
        expect(page.locator(".poll-status")).to_have_text("0 runs")
        page.goto(f"{server.url}#/compare")
        expect(page.get_by_text("Select two or more runs from Runs to compare.", exact=True)).to_be_visible()


def test_single_run_compare_is_friendly(page, ui_server):
    page.goto(f"{ui_server.url}#/compare?runs={ui_server.runs['explicit-baseline']}")
    expect(page.get_by_text("Select two or more runs from Runs to compare.", exact=True)).to_be_visible()


def test_no_project_group_is_first_and_escaped(page, tmp_path, ui_server_factory):
    root = tmp_path / "groups"
    malicious = '<img src=x onerror="window.__xss=1">'
    for name, project in ((malicious, ""), ("missing-project", None), ("assigned", "z-project")):
        meta = {"name": name, "tags": [malicious], "config": {"literal": malicious}}
        if project is not None:
            meta["project"] = project
        writer = RunWriter.create(root, meta=meta)
        writer.finish("finished")
    with ui_server_factory(root) as server:
        home(page, server)
        expect(page.locator(".project-card")).to_have_count(2)
        expect(page.locator(".project-card").first.locator(".project-head")).to_contain_text("No project")
        expect(page.locator('.project-card[data-project=""] .run-row')).to_have_count(2)
        expect(page.get_by_text("no project — pass project= or run inside a repo", exact=True)).to_be_visible()
        page.get_by_role("tab", name="No project", exact=True).click()
        expect(page.locator(".run-row")).to_have_count(2)
        page.get_by_role("link", name=malicious, exact=True).click()
        expect(page.get_by_role("heading", name=malicious, exact=True)).to_be_visible()
        expect(page.locator('[data-card="Parameters"]')).to_contain_text(malicious)
        assert page.locator("#app img").count() == 0
        assert page.evaluate("window.__xss") is None


def live_run(root):
    import agenticml
    run = agenticml.init("live-project", "live-run", dir=root, flush_interval=.01, system_metrics=False, auto_track=False, track_source=False, capture_env=False)
    return agenticml, run


def test_live_run_finishes_without_reload(page, tmp_path, ui_server_factory):
    root = tmp_path / "live"
    agenticml, run = live_run(root)
    try:
        with ui_server_factory(root) as server:
            home(page, server)
            expect(row(page, "live-run").locator(".chip")).to_have_text("running")
            expect(page.locator(".poll-status")).to_have_text("Live · refreshes every 5 s")
            page.evaluate("window.__stayed = true")
            agenticml.finish()
            expect(row(page, "live-run").locator(".chip")).to_have_text("finished", timeout=LIVE_TIMEOUT)
            expect(page.locator(".poll-status")).to_have_text("1 runs")
            assert page.evaluate("window.__stayed")
    finally:
        agenticml.finish()


def test_live_chart_retains_controls_and_open_focus(page, tmp_path, ui_server_factory):
    root = tmp_path / "live-chart"
    agenticml, run = live_run(root)
    try:
        for step, value in enumerate((.9, .6, .4)):
            agenticml.log({"train/loss": value}, step=step)
        run._flush()
        with ui_server_factory(root) as server:
            page.goto(f"{server.url}#/run/{run.run_id}")
            expect(loss(page).locator("[data-points]")).to_have_attribute("data-points", "3")
            page.get_by_label("Smoothing", exact=True).fill("0.9")
            page.get_by_label("Filter tags", exact=True).fill("train/")
            original_canvas = loss(page).locator("canvas").element_handle()
            loss(page).click()
            agenticml.log({"train/loss": .2, "train/extra": 1.25}, step=3)
            agenticml.log({"train/loss": .1}, step=4)
            run._flush()
            expect(loss(page).locator("[data-points]")).to_have_attribute("data-points", "5", timeout=LIVE_TIMEOUT)
            assert original_canvas.evaluate("el => el.isConnected")
            expect(page.locator('.chart-card[data-key="train/extra"] [data-points]')).to_have_attribute("data-points", "1")
            expect(page.locator(".chart-focus")).to_be_visible()
            expect(page.locator(".chart-focus [data-testid=hover-table]")).to_contain_text("0.1")
            page.keyboard.press("Escape")
            expect(page.get_by_label("Smoothing", exact=True)).to_have_value("0.9")
            expect(page.get_by_label("Filter tags", exact=True)).to_have_value("train/")
            agenticml.finish()
            expect(page.locator(".detail > .inline .chip").first).to_have_text("finished", timeout=LIVE_TIMEOUT)
            expect(page.locator(".poll-status")).to_have_text("Up to date")
    finally:
        agenticml.finish()


def write_snapshot(root, name, project, source, created):
    """Use the store contract directly so baseline order and bytes are deterministic."""
    writer = RunWriter.create(root, meta={"name": name, "project": project})
    writer.set_meta_fields(created_at=created)
    writer.add_code([{"path": path} for path in source], [(path, text.encode()) for path, text in source.items()])
    writer.finish("finished")
    return writer.run_id


def test_code_baseline_modified_marker_lazy_fetch_and_diff(page, tmp_path, ui_server_factory):
    root = tmp_path / "baseline"
    common = "unchanged = True\n"
    old = write_snapshot(root, "baseline", "project", {"main.py": "def train():\n    return 1\n", "same.py": common}, "2024-01-01T00:00:00Z")
    # A newer same-project run without code and another project must be skipped.
    write_snapshot(root, "empty", "project", {}, "2024-01-02T00:00:00Z")
    write_snapshot(root, "foreign", "elsewhere", {"main.py": "foreign\n"}, "2024-01-03T00:00:00Z")
    current = write_snapshot(root, "current", "project", {"main.py": "def train():\n    return 2\n", "same.py": common}, "2024-01-04T00:00:00Z")
    objects = []
    page.on("request", lambda request: objects.append(request.url) if "/api/objects/" in request.url else None)
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{current}")
        viewer = page.get_by_test_id("code-inline-view")
        expect(viewer.locator('[data-path="main.py"] [data-modified=true]')).to_have_text("M")
        expect(viewer.locator('[data-path="same.py"] [data-modified=false]')).not_to_have_text("M")
        expect(viewer.locator(".code-line.changed")).to_contain_text("return 2")
        assert len(objects) == 2  # Current and baseline main.py; same.py stays lazy.
        viewer.locator('[data-path="same.py"]').click()
        expect(viewer.locator(".code-lines")).to_contain_text(common.strip())
        assert len(objects) == 3  # The identical baseline blob is never fetched.
        page.get_by_role("button", name="View diff", exact=True).click()
        full = page.locator(".code-full")
        expect(full.get_by_text("Differs from baseline", exact=True)).to_be_visible()
        expect(full.locator(".code-crumb")).to_have_text("same.py")
        full.locator('[data-path="main.py"]').click()
        expect(full.locator(".code-outline")).to_contain_text("was return 1")
        page.keyboard.press("Escape")
        expect(full).to_have_count(0)


@pytest.mark.parametrize("width", [360, 375])
def test_mobile_pages_have_no_horizontal_scroll(page, ui_server, width):
    page.set_viewport_size({"width": width, "height": 812})
    home(page, ui_server)
    expect(page.locator(".run-row")).to_have_count(5)
    for route in ("", f"#/run/{ui_server.runs['explicit-baseline']}", f"#/compare?runs={','.join(ui_server.runs[name] for name in COMPARE_RUNS)}"):
        page.goto(ui_server.url + route)
        page.wait_for_load_state("networkidle")
        overflow = page.evaluate("Math.max(document.documentElement.scrollWidth-document.documentElement.clientWidth, document.body.scrollWidth-document.body.clientWidth)")
        assert overflow <= 0, f"{route} scrolls horizontally by {overflow}px"


def test_metrics_chunk_keys_and_overview_points(page, tmp_path, ui_server_factory):
    root = tmp_path / "many-metrics"
    writer = RunWriter.create(root, meta={"name": "many", "project": "stress"})
    writer.write_scalars([(step, {"dense/loss": float(step)}) for step in range(5105)])
    writer.write_scalars([(0, {f"tags/{index:03}": index / 10 for index in range(201)})])
    writer.finish("finished")
    from urllib.parse import parse_qs, urlsplit
    queries = []
    page.on("request", lambda request: queries.append(parse_qs(urlsplit(request.url).query)) if "/scalars?" in request.url else None)
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{writer.run_id}")
        expect(page.locator(".chart-card")).to_have_count(202)
        expect(page.get_by_text("202 / 202 tags", exact=True)).to_be_visible()
        assert len(queries) == 2
        assert all(len(query["key"]) <= 200 for query in queries)
        assert len({key for query in queries for key in query["key"]}) == 202
        dense = page.locator('.chart-card[data-key="dense/loss"]')
        points = int(dense.locator("[data-points]").get_attribute("data-points"))
        assert 1 < points <= 1000
        values = RunReader(root, writer.run_id).series("dense/loss", max_points=1000)
        assert values["mode"] == "overview" and points == len(values["points"])
        last_mean = values["points"][-1][4]
        assert dense.locator(".chart-values").inner_text().strip() == f"{last_mean:,.4f}".rstrip("0").rstrip(".")
        painted(dense.locator("canvas"))


def test_canvas_resizes_for_device_pixels_and_viewport(page, context, ui_server):
    open_run(page, ui_server)
    canvas = loss(page).locator("canvas")
    painted(canvas)
    old = canvas.evaluate("el => ({width: el.width, css: el.getBoundingClientRect().width})")
    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_function("({canvas, width}) => canvas.width !== width", arg={"canvas": canvas.element_handle(), "width": old["width"]})
    size = canvas.evaluate("el => ({width: el.width, css: el.getBoundingClientRect().width, dpr: devicePixelRatio})")
    assert size["width"] == int(size["css"] * size["dpr"])
    assert painted(canvas)["labels"]


def test_code_preview_limits_and_escaping(page, tmp_path, ui_server_factory):
    root = tmp_path / "preview-limits"
    text = '<script>window.__xss=1</script>\n'
    run_id = write_snapshot(root, "limits", "preview", {"main.py": text, "large.py": "x" * (1024 * 1024 + 1)}, "2024-01-01T00:00:00Z")
    with ui_server_factory(root) as server:
        objects = []
        page.on("request", lambda request: objects.append(request.url) if "/api/objects/" in request.url else None)
        page.goto(f"{server.url}#/run/{run_id}")
        viewer = page.get_by_test_id("code-inline-view")
        viewer.locator('[data-path="main.py"]').click()
        expect(viewer.locator(".code-lines")).to_contain_text(text.strip())
        assert page.evaluate("window.__xss") is None
        assert viewer.locator("script").count() == 0
        count = len(objects)
        viewer.locator('[data-path="large.py"]').click()
        expect(viewer.locator(".code-lines")).to_contain_text("Too large: code previews are limited to 1 MB.")
        assert len(objects) == count


def test_code_checks_actual_blob_size_and_sixty_file_cap(page, tmp_path, ui_server_factory):
    root = tmp_path / "blob-limit"
    source = {f"file{index:02}.py": "x = 1\n" for index in range(65)}
    source["file00.py"] = "x" * (1024 * 1024 + 1)
    run_id = write_snapshot(root, "limits", "preview", source, "2024-01-01T00:00:00Z")
    # Simulate a stale size in the manifest without changing the object bytes.
    events_path = root / "runs" / run_id / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    for event in events:
        if event.get("kind") == "code":
            for file in event["manifest"]:
                file["size"] = 1
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        viewer = page.get_by_test_id("code-inline-view")
        expect(viewer.locator(".code-file")).to_have_count(60)
        expect(viewer.locator(".code-file-limit")).to_have_text("Showing 60 of 65 files")
        expect(viewer.locator(".code-lines")).to_contain_text("Too large: code previews are limited to 1 MB.")


def test_chart_errors_and_truncation_are_visible(page, ui_server):
    page.route("**/scalars?*", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"series": {"loss": {"mode": "raw", "points": [[0, 1], [1, .5]]}}, "truncated": True})))
    open_run(page, ui_server)
    expect(page.get_by_text("Some metric keys were truncated by the server.", exact=True)).to_be_visible()
    expect(page.locator('.chart-card[data-key="loss"]')).to_be_visible()
    page.unroute("**/scalars?*")
    page.route("**/scalars?*", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"test failure"}'))
    page.allow_http_errors = True
    page.reload()
    expect(page.locator(".metrics-panel .error")).to_contain_text("503")
    expect(page.get_by_role("heading", name="explicit-baseline", exact=True)).to_be_visible()


def test_varied_progress_headline_delta_and_best_metric(page, tmp_path, ui_server_factory):
    root = tmp_path / "table-facts"
    ids = []
    for index, (name, summary) in enumerate((("older", {"train/loss": .8, "quality": .5}), ("different-key", {"quality": .7}), ("newer", {"train/loss": .4, "quality": .9}))):
        writer = RunWriter.create(root, meta={"name": name, "project": "project", "config": {"lr": index + 1, "epochs": 5}})
        writer.set_meta_fields(created_at=f"2024-01-0{index + 1}T00:00:00Z")
        writer.write_scalars([(1500, {"loss": .4})])
        writer.finish("finished", summary=summary)
        ids.append(writer.run_id)
    with ui_server_factory(root) as server:
        home(page, server)
        expect(row(page, "newer").locator(".run-varied")).to_have_text("lr=3")
        expect(row(page, "newer").locator(".run-progress")).to_have_text("1.5k")
        expect(row(page, "newer").locator('.run-number[title="train/loss"]')).to_have_text("0.4")
        expect(row(page, "newer").locator(".run-delta")).to_have_text("-0.4")
        expect(row(page, "newer").locator(".run-delta")).to_have_class(re.compile(r"\bok\b"))
        # Skip the intervening run with a different headline key.
        expect(page.locator(".run-columns")).to_contain_text("train/loss")
        page.goto(f"{server.url}#/compare?runs={ids[0]},{ids[2]}")
        table = page.locator(".compare-focus table").last
        loss_row = table.locator("tr").filter(has=page.get_by_role("cell", name="train/loss", exact=True))
        expect(loss_row.locator(".ok")).to_have_text("0.4")
        quality_row = table.locator("tr").filter(has=page.get_by_role("cell", name="quality", exact=True))
        expect(quality_row.locator(".ok")).to_have_text("0.9")
        expect(table).to_contain_text("Params that differ (1)")
        assert "epochs" not in table.inner_text()


def test_theme_storage_denied_and_system_change(page, context, ui_server):
    page.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('storage denied'); }});")
    home(page, ui_server)
    expect(page.get_by_role("button", name="Change color theme")).to_have_text("Theme: system")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_role("button", name="Change color theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_abandoned_run_detail_uses_effective_status(page, tmp_path, ui_server_factory):
    import os
    root = tmp_path / "abandoned"
    writer = RunWriter.create(root, meta={"name": "abandoned", "project": "project"})
    writer.close()
    os.utime(root / "runs" / writer.run_id / "run.lock", (0, 0))
    with ui_server_factory(root) as server:
        status = page.request.get(f"{server.url}/api/runs").json()[0]["status"]
        assert status in ("crashed", "stale")
        page.goto(f"{server.url}#/run/{writer.run_id}")
        expect(page.locator(".detail > .inline .chip").first).to_have_text(status)
        expect(page.locator(".poll-status")).to_have_text("Up to date")


def mock_dashboard(page, runs):
    """Mock only store data; exercise the actual modules, routes, and DOM."""
    from urllib.parse import parse_qs, unquote, urlsplit

    def respond(route):
        path = urlsplit(route.request.url).path
        if path == "/api/runs":
            body = runs
        elif path == "/api/lineage":
            project = parse_qs(urlsplit(route.request.url).query, keep_blank_values=True)["project"][0]
            body = {"nodes": [run for run in runs if (run.get("project") or "") == project], "edges": []}
        else:
            parts = path[len("/api/runs/"):].split("/")
            run_id = unquote(parts[0])
            run = next(run for run in runs if run["run_id"] == run_id)
            suffix = parts[1] if len(parts) > 1 else ""
            body = {"scalars": {"series": {}}, "media": {}, "code": []}.get(suffix, run)
        route.fulfill(content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", respond)


@pytest.mark.parametrize("branch", ["claude/experiments-view-redesign", "branch/" + "long-name" * 80])
def test_git_dirty_chip_wraps_only_when_needed(page, ui_server, branch):
    mock_dashboard(page, [{"run_id": "git-run", "name": "git run", "status": "finished", "keys": [],
                           "git": {"git_commit": "51c5eb5f12345678", "git_branch": branch, "is_dirty": True}}])
    page.goto(f"{ui_server.url}#/run/git-run")
    value = page.get_by_test_id("identity").locator("dd").filter(has_text="51c5eb5f")
    expect(value).to_contain_text(branch)
    dirty = value.get_by_text("dirty", exact=True)
    expect(dirty).to_be_visible()
    branch_box = value.locator(".git-identity > span").nth(1).bounding_box()
    chip_box = dirty.bounding_box()
    if branch.startswith("claude/"):
        assert chip_box["x"] > branch_box["x"]
        assert chip_box["y"] < branch_box["y"] + branch_box["height"]
    else:
        assert chip_box["y"] >= branch_box["y"] + branch_box["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


@pytest.mark.parametrize("run_id", ["literal%", "a%2Fb", "a&b?c,d"])
def test_run_route_decodes_exactly_once(page, ui_server, run_id):
    from urllib.parse import quote
    run = {"run_id": run_id, "name": "encoded run", "status": "finished", "keys": []}
    mock_dashboard(page, [run])
    page.goto(f"{ui_server.url}#/run/{quote(run_id, safe='')}")
    expect(page.get_by_role("heading", name="encoded run", exact=True)).to_be_visible()
    expect(page.get_by_test_id("identity")).to_contain_text(run_id)


def test_compare_round_trips_punctuation_in_repeated_parameters(page, ui_server):
    from urllib.parse import parse_qs, urlsplit
    ids = ["literal%", "a%2Fb", "a&b?c,d"]
    runs = [{"run_id": run_id, "name": f"run {index}", "status": "finished", "keys": []} for index, run_id in enumerate(ids)]
    mock_dashboard(page, runs)
    home(page, ui_server)
    for index in range(3):
        page.get_by_role("checkbox", name=f"Select run {index}", exact=True).check()
    page.get_by_role("button", name="Compare runs", exact=True).click()
    expect(page.get_by_test_id("selected-runs")).to_contain_text("run 2")
    assert parse_qs(urlsplit(page.url).fragment.split("?", 1)[1])["runs"] == ids
    page.reload()
    expect(page.get_by_test_id("selected-runs")).to_contain_text("run 2")


def test_project_headline_uses_chronology_and_skips_missing_values(page, ui_server):
    runs = [
        {"run_id": "middle", "created_at": "2024-01-02", "summary": {"quality": .7}},
        {"run_id": "old", "created_at": "2024-01-01", "summary": {"loss": .8}},
        {"run_id": "new", "created_at": "2024-01-03", "summary": {"loss": .4}},
        {"run_id": "same-date", "created_at": "2024-01-03", "summary": {"loss": .6}},
        {"run_id": "invalid", "created_at": "invalid", "summary": {}},
    ]
    for run in runs:
        run.update(name=run["run_id"], project="test", status="finished", keys=[])
    mock_dashboard(page, runs)
    home(page, ui_server)
    assert page.locator(".run-row").evaluate_all("nodes => nodes.map(el => el.dataset.runId)") == ["new", "same-date", "middle", "old", "invalid"]
    expect(row(page, "middle").locator(".run-number").first).to_have_text("—")
    expect(row(page, "middle").locator(".run-delta")).to_have_text("—")
    expect(row(page, "new").locator(".run-delta")).to_have_text("-0.4")
    expect(row(page, "same-date").locator(".run-delta")).to_have_text("-0.2")
    expect(row(page, "invalid").locator(".run-duration")).to_have_text("—")


@pytest.mark.parametrize("width", [375, 1400])
def test_long_values_are_contained_and_short_subtitles_have_titles(page, ui_server, width):
    page.set_viewport_size({"width": width, "height": 1000})
    long = "/outputs/" + "nested-directory/" * 80
    project = "project" * 80
    runs = [{"run_id": str(index), "name": f"run {index}", "status": "finished", "keys": [], "project": project,
             "config": {"output": long + str(index), **{f"parameter{i}": f"short-{index}" for i in range(10)}},
             "summary": {"best_val_accuracy_metric_with_a_long_name": index}, "env": {"packages": long}, "tags": [long]}
            for index in range(2)]
    mock_dashboard(page, runs)
    home(page, ui_server)
    subtitle = row(page, "run 0").locator(".run-varied")
    assert long not in subtitle.inner_text()
    assert subtitle.get_attribute("title") == subtitle.inner_text()
    assert subtitle.evaluate("el => el.scrollWidth > el.clientWidth && getComputedStyle(el).whiteSpace === 'nowrap'")
    header = page.locator(".run-columns .run-number").first
    expect(header).to_have_attribute("title", "best_val_accuracy_metric_with_a_long_name")
    for route in ("", "#/run/0", "#/compare?runs=0&runs=1"):
        page.goto(ui_server.url + route)
        if "run/0" in route:
            expect(page.locator(".subline")).to_have_attribute("title", re.compile("parameter"))
            assert long not in page.locator(".subline").inner_text()
            expect(page.locator('[data-card="Parameters"]')).to_contain_text(long)
            page.get_by_text("Packages", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), page.evaluate("[...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > innerWidth).map(el => [el.tagName, el.className, Math.round(el.getBoundingClientRect().width)]).slice(0, 20)")


def test_live_poll_skips_unchanged_scalars_and_refreshes_media(page, tmp_path, ui_server_factory):
    root = tmp_path / "live-media"
    agenticml, run = live_run(root)
    try:
        agenticml.log({"train/loss": .5}, step=0)
        agenticml.log({"train/loss": .4}, step=1)
        run._flush()
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        with ui_server_factory(root) as server:
            page.goto(f"{server.url}#/run/{run.run_id}")
            painted(loss(page).locator("canvas"))
            canvas = loss(page).locator("canvas").element_handle()
            # Inject a new media response after initial render; no scalar step/key changes.
            media = {"live image": [{"step": 1, "sha256": "a" * 64, "content_type": "image/png", "caption": "new image"}]}
            page.route("**/media", lambda route: route.fulfill(content_type="application/json", body=json.dumps(media)))
            page.route("**/api/objects/**", lambda route: route.fulfill(content_type="image/png", body=bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")))
            expect(page.locator(".media-strip img")).to_have_attribute("alt", "new image", timeout=LIVE_TIMEOUT)
            assert sum("/scalars?" in url for url in requests) == 1
            assert sum(url.endswith("/media") for url in requests) >= 2
            assert canvas.evaluate("el => el.isConnected")
    finally:
        agenticml.finish()


def test_code_failed_load_can_retry(page, tmp_path, ui_server_factory):
    root = tmp_path / "retry-code"
    run_id = write_snapshot(root, "retry", "p", {"main.py": "print('retried')"}, "2024-01-01")
    attempts = []
    page.allow_http_errors = True
    with ui_server_factory(root) as server:
        def respond(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.fulfill(status=503, body="unavailable")
            else:
                route.continue_()
        page.route("**/api/objects/**", respond)
        page.goto(f"{server.url}#/run/{run_id}")
        viewer = page.get_by_test_id("code-inline-view")
        expect(viewer.locator(".code-lines")).to_contain_text("503")
        viewer.locator('[data-path="main.py"]').click()
        expect(viewer.locator(".code-lines")).to_contain_text("retried")
        assert len(attempts) == 2


def test_code_stream_limit_cancels_without_content_length(page, tmp_path, ui_server_factory):
    root = tmp_path / "stream-code"
    run_id = write_snapshot(root, "stream", "p", {"main.py": "small"}, "2024-01-01")
    page.add_init_script("""(() => {
      const original = window.fetch;
      window.fetch = (url, options) => {
        if (!String(url).startsWith('/api/objects/')) return original(url, options);
        window.streamChunks = 0; window.streamCancelled = false;
        return Promise.resolve(new Response(new ReadableStream({
          pull(controller) { window.streamChunks++; controller.enqueue(new Uint8Array(65536)); },
          cancel() { window.streamCancelled = true; }
        })));
      };
    })();""")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        expect(page.get_by_test_id("code-inline-view").locator(".code-lines")).to_contain_text("limited to 1 MB")
        assert page.evaluate("streamCancelled && streamChunks <= 18")


def test_oversized_baseline_keeps_current_source_visible(page, tmp_path, ui_server_factory):
    root = tmp_path / "large-baseline"
    write_snapshot(root, "large baseline", "p", {"main.py": "x" * (1024 * 1024 + 1)}, "2024-01-01")
    run_id = write_snapshot(root, "current", "p", {"main.py": "print('current file')"}, "2024-01-02")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        viewer = page.get_by_test_id("code-inline-view")
        expect(viewer.locator(".code-lines")).to_contain_text("current file")
        expect(viewer.locator(".code-status")).to_contain_text("Diff skipped: baseline unavailable.")
        expect(viewer.locator(".code-status")).to_contain_text("1 MB")
        expect(viewer.locator(".code-line.changed")).to_have_count(0)


def test_file_cap_sorts_and_retains_config_and_entry(page, tmp_path, ui_server_factory):
    root = tmp_path / "file-order"
    source = {"z/train.py": "print('entry')", "z/config.yaml": "lr: 1", **{f"a/{i:03}.py": "pass" for i in reversed(range(65))}}
    run_id = write_snapshot(root, "order", "p", source, "2024-01-01")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        viewer = page.get_by_test_id("code-inline-view")
        expect(viewer.locator(".code-file")).to_have_count(60)
        expect(viewer.locator(".code-file-limit")).to_have_text("Showing 60 of 67 files")
        paths = viewer.locator(".code-file").evaluate_all("nodes => nodes.map(el => el.dataset.path)")
        assert paths == sorted(paths)
        assert paths[:58] == [f"a/{i:03}.py" for i in range(58)]
        assert paths[-2:] == ["z/config.yaml", "z/train.py"]
        expect(viewer.get_by_role("tab")).to_have_text(["config.yaml", "train.py"])
        expect(viewer.locator(".code-lines")).to_contain_text("lr: 1")


def test_full_code_preserves_file_and_line_and_caps_change_list(page, tmp_path, ui_server_factory):
    root = tmp_path / "code-position"
    write_snapshot(root, "named baseline", "p", {"config.yaml": "a: 1", "train.py": "\n".join(f"old_{i}" for i in range(500))}, "2024-01-01")
    run_id = write_snapshot(root, "current", "p", {"config.yaml": "a: 1", "train.py": "\n".join(f"new_{i}" for i in range(500))}, "2024-01-02")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        viewer = page.get_by_test_id("code-inline-view")
        expect(viewer.locator(".code-status")).to_contain_text("Click code to open full window")
        viewer.get_by_role("tab", name="train.py", exact=True).click()
        viewer.get_by_label("Find in file", exact=True).fill("new_300")
        expect(viewer.locator(".find-count")).to_have_text("1 of 1")
        page.get_by_role("button", name="Open full window ⤢").click()
        full = page.get_by_test_id("code-full-view")
        expect(full.locator(".code-crumb")).to_have_text("train.py")
        expect(full.locator(".code-outline")).to_contain_text("Differs from named baseline")
        expect(full.locator(".change-item")).to_have_count(200)
        expect(full.locator(".code-more")).to_have_text("and 301 more")
        expect(full.get_by_text("Nothing to outline", exact=True)).to_have_css("padding-left", "14px")
        page.wait_for_function("() => document.querySelector('[data-testid=code-full-view] .code-lines').scrollTop > 0")
        assert full.locator('.code-line[data-line="301"]').evaluate("el => { const a=el.getBoundingClientRect(), b=el.parentElement.getBoundingClientRect(); return a.top >= b.top && a.bottom <= b.bottom; }")
        page.keyboard.press("Escape")
        viewer.locator('.code-line[data-line="301"]').click()
        expect(full.locator(".code-crumb")).to_have_text("train.py")
        expect(full.locator('.code-line[data-line="301"]')).to_be_in_viewport()


def test_identical_code_has_no_diff_heading_and_baseline_suffix(page, tmp_path, ui_server_factory):
    root = tmp_path / "same-code"
    old = write_snapshot(root, "", "p", {"main.py": "pass"}, "2024-01-01")
    same = write_snapshot(root, "same", "p", {"main.py": "pass"}, "2024-01-02")
    current = write_snapshot(root, "different", "p", {"main.py": "changed"}, "2024-01-03")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{same}")
        expect(page.locator(".code-status")).to_contain_text("Click code to open full window")
        page.get_by_role("button", name="Open full window ⤢").click()
        expect(page.locator(".code-outline")).not_to_contain_text("Differs from")
        page.keyboard.press("Escape")
        # Remove the intermediate run from baseline candidates to exercise fallback naming.
        page.route("**/api/runs", lambda route: route.fulfill(content_type="application/json", body=json.dumps([
            {"run_id": old, "created_at": "2024-01-01", "project": "p", "name": ""},
            {"run_id": current, "created_at": "2024-01-03", "project": "p", "name": "different"},
        ])))
        page.goto(f"{server.url}#/run/{current}")
        page.get_by_role("button", name="View diff", exact=True).click()
        expect(page.locator(".code-outline")).to_contain_text(f"Differs from {old.rsplit('-', 1)[-1]}")


def test_large_chart_null_gaps_and_cached_hover(page, ui_server):
    home(page, ui_server)
    result = page.evaluate("""async () => {
      const {MetricChart} = await import('/static/charts.js');
      const canvas = document.createElement('canvas');
      canvas.style.cssText = 'width:600px;height:300px';
      document.body.append(canvas);
      const points = Array.from({length:150000}, (_, i) => ({step:i, value:i === 75000 ? null : i % 100}));
      const chart = new MetricChart(canvas, {runs:[{id:'large'}], metrics:{large:{loss:points}}, activeMetric:'loss', smoothing:.6});
      const layout = {...chart.layout};
      const before = canvas.__draw.labels.length;
      let sorts = 0;
      const sort = Array.prototype.sort;
      Array.prototype.sort = function(...args) { sorts++; return sort.apply(this,args); };
      try { chart.move({clientX:canvas.getBoundingClientRect().left+300}); }
      finally { Array.prototype.sort = sort; }
      const after = canvas.__draw.labels.length;
      chart.destroy(); canvas.remove();
      return {layout, sorts, before, after};
    }""")
    assert result["layout"]["minStep"] == 0 and result["layout"]["maxStep"] == 149999
    assert result["sorts"] == 0 and result["before"] == result["after"]
    # A small gapped series should have two independent stroke segments.
    points = [[0, .8], [1, .6], [2, None], [3, .4], [4, .2]]
    page.route("**/scalars?*", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"series": {"train/loss": {"mode": "raw", "points": points}}})))
    open_run(page, ui_server)
    expect(loss(page).locator("[data-points]")).to_have_attribute("data-points", "5")
    draw = painted(loss(page).locator("canvas"))
    assert all(stroke["moves"] == 2 for stroke in draw["strokes"] if stroke["width"] == 2 and len(stroke["path"]) == 4)
    assert len([stroke for stroke in draw["strokes"] if stroke["width"] == 2 and len(stroke["path"]) == 4]) == 2


def test_baseline_discovery_is_bounded_parallel_and_chooses_nearest(page, tmp_path, ui_server_factory):
    root = tmp_path / "parallel-baseline"
    for day in range(1, 7):
        write_snapshot(root, f"baseline {day}", "p", {"main.py": "old"} if day <= 4 else {}, f"2024-01-0{day}")
    run_id = write_snapshot(root, "current", "p", {"main.py": "new"}, "2024-01-07")
    page.add_init_script("""(() => {
      const original = window.fetch;
      window.manifestActive = 0; window.manifestPeak = 0;
      window.fetch = async (url, options) => {
        if (!String(url).endsWith('/code')) return original(url, options);
        window.manifestActive++;
        window.manifestPeak = Math.max(window.manifestPeak, window.manifestActive);
        try {
          const response = await original(url, options);
          await new Promise(resolve => setTimeout(resolve, 50));
          return response;
        } finally { window.manifestActive--; }
      };
    })();""")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        page.get_by_role("button", name="View diff", exact=True).click()
        expect(page.locator(".code-outline")).to_contain_text("Differs from baseline 4")
        assert page.evaluate("manifestPeak") == 4


def test_code_content_length_is_rejected_before_reading(page, tmp_path, ui_server_factory):
    root = tmp_path / "length-limit"
    run_id = write_snapshot(root, "length", "p", {"main.py": "small"}, "2024-01-01")
    page.add_init_script("""(() => {
      const original = window.fetch;
      window.bodyRead = false; window.downloadAborted = false;
      window.fetch = (url, options) => {
        if (!String(url).startsWith('/api/objects/')) return original(url, options);
        options.signal.addEventListener('abort', () => { window.downloadAborted = true; });
        return Promise.resolve({ok:true, headers:new Headers({'Content-Length':'2000000'}),
          get body() { window.bodyRead = true; throw new Error('body must not be read'); }});
      };
    })();""")
    with ui_server_factory(root) as server:
        page.goto(f"{server.url}#/run/{run_id}")
        expect(page.locator(".code-lines")).to_contain_text("limited to 1 MB")
        assert page.evaluate("!bodyRead && downloadAborted")


def test_lineage_graph_selects_and_opens_run(page, tmp_path, ui_server_factory):
    root = tmp_path / "lineage-ui"
    names = ("root-run", "child-run", "leaf-run")
    writers = []
    for name in names:
        writer = RunWriter.create(root, meta={"name": name, "project": "demo", "tags": ["graph"]})
        writer.finish("finished")
        writers.append(writer)
    ids = [writer.run_id for writer in writers]
    stamps = (
        "2026-10-27T00:00:00.000000Z",
        "2026-10-27T00:01:00.000000Z",
        "2026-10-27T00:02:00.000000Z",
    )
    parents = (None, ids[0], ids[1])
    inferences = (None, "explicit", "repo_root")
    shared_tree = "ab" * 20
    for index, run_id in enumerate(ids):
        path = root / "runs" / run_id / "meta.json"
        meta = json.loads(path.read_text())
        meta["created_at"] = stamps[index]
        meta["parent_run_id"] = parents[index]
        meta["lineage"] = {
            "version": 1,
            "commit": f"{index + 1:040x}",
            "tree": shared_tree if index else "cd" * 20,
            "parent_commit": None if index == 0 else f"{index:040x}",
            "parent_run_id": parents[index],
            "parent_inference": inferences[index],
            "project_key": "demo-0123abcd",
            "repo_root": "/tmp/demo",
            "base_commit": "abc1234",
            "base_branch": "main",
            "base_dirty": index == 2,
            "error": None,
        }
        path.write_text(json.dumps(meta))
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": 375, "height": 812})
        home(page, server)
        expect(page.locator(".run-row")).to_have_count(3)
        expect(page.locator(".lineage-node")).to_have_count(3)
        expect(page.locator(".lineage-edge")).to_have_count(2)
        expect(page.locator(".lineage-edge.is-inferred")).to_have_count(1)
        suffix = ids[1].rsplit("-", 1)[-1]
        node_label = page.locator(".lineage-node", has_text="child-run")
        visible_id = node_label.locator(".lineage-meta").evaluate(
            "el => el.childNodes[0] ? el.childNodes[0].nodeValue : el.textContent"
        )
        assert visible_id.split(" · ", 1)[0] == suffix
        assert ids[1] in node_label.get_attribute("aria-label")
        assert ids[1] in node_label.locator("title").first.text_content()
        overflow = page.evaluate(
            "Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth, document.body.scrollWidth - document.body.clientWidth)"
        )
        assert overflow <= 0, f"lineage view scrolls horizontally by {overflow}px"
        page.reload()
        expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        expect(page.locator(".lineage-node")).to_have_count(3)
        node = page.locator(".lineage-node", has_text="child-run")
        node.click()
        expect(page.get_by_test_id("selection-bar")).to_contain_text("1 run selected")
        expect(node).to_have_attribute("data-picked", "true")
        expect(page.locator(".lineage-detail")).to_contain_text("agenticml lineage checkout")
        overflow = page.evaluate(
            "Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth, document.body.scrollWidth - document.body.clientWidth)"
        )
        assert overflow <= 0, f"lineage detail scrolls horizontally by {overflow}px"
        node.press("Enter")
        expect(page.get_by_role("heading", name="child-run", exact=True)).to_be_visible()


def _lineage_project(root, names=("root-run", "child-run", "leaf-run")):
    writers = []
    for name in names:
        writer = RunWriter.create(root, meta={"name": name, "project": "demo", "tags": ["graph"]})
        writer.finish("finished")
        writers.append(writer)
    ids = [writer.run_id for writer in writers]
    stamps = (
        "2026-10-27T00:00:00.000000Z",
        "2026-10-27T00:01:00.000000Z",
        "2026-10-27T00:02:00.000000Z",
    )
    parents = (None, ids[0], ids[1])
    inferences = (None, "explicit", "repo_root")
    shared_tree = "ab" * 20
    for index, run_id in enumerate(ids):
        path = root / "runs" / run_id / "meta.json"
        meta = json.loads(path.read_text())
        meta["created_at"] = stamps[index]
        meta["parent_run_id"] = parents[index]
        meta["lineage"] = {
            "version": 1,
            "commit": f"{index + 1:040x}",
            "tree": shared_tree if index else "cd" * 20,
            "parent_commit": None if index == 0 else f"{index:040x}",
            "parent_run_id": parents[index],
            "parent_inference": inferences[index],
            "project_key": "demo-0123abcd",
            "repo_root": "/tmp/demo",
            "base_commit": "abc1234",
            "base_branch": "main",
            "base_dirty": index == 2,
            "error": None,
        }
        path.write_text(json.dumps(meta))
    return ids


def test_split_runs_page_works_when_storage_throws(page, tmp_path, ui_server_factory):
    page.add_init_script("""
      Storage.prototype.getItem = function () { throw new Error("denied"); };
      Storage.prototype.setItem = function () { throw new Error("denied"); };
    """)
    root = tmp_path / "lineage-storage"
    writer = RunWriter.create(root, meta={"name": "solo", "project": "demo", "tags": []})
    writer.finish("finished")
    with ui_server_factory(root) as server:
        home(page, server)
        expect(page.locator(".lineage-graph")).to_be_visible()
        expect(page.locator(".lineage-node")).to_have_count(1)
        expect(page.locator(".run-row")).to_have_count(1)
        page.reload()
        expect(page.locator(".lineage-node")).to_have_count(1)
        expect(page.locator(".run-row")).to_have_count(1)


@pytest.mark.parametrize("width", [375, 650, 900, 1001, 1440])
def test_split_runs_layout_and_shared_selection(page, tmp_path, ui_server_factory, width):
    root = tmp_path / "lineage-split"
    ids = _lineage_project(root)
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": width, "height": 900})
        home(page, server)
        left = page.locator(".runs-pane")
        right = page.locator(".lineage-body")
        expect(left).to_be_visible()
        expect(right.locator(".lineage-graph")).to_be_visible()
        expect(page.get_by_role("tab", name="Lineage", exact=True)).to_have_count(0)
        expect(page.get_by_role("tab", name="List", exact=True)).to_have_count(0)
        a, b = left.bounding_box(), right.bounding_box()
        assert abs(a["width"] - b["width"]) <= 1
        if width > 1000:
            assert a["x"] < b["x"]
            assert abs(a["y"] - b["y"]) <= 1
        else:
            assert abs(a["x"] - b["x"]) <= 1
            assert a["y"] + a["height"] <= b["y"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert left.evaluate("el => el.scrollWidth <= el.clientWidth")
        node = page.locator(f'.lineage-node[data-run-id="{ids[0]}"]')
        check = page.get_by_role("checkbox", name="Select root-run", exact=True)
        check.check()
        expect(node).to_have_attribute("data-picked", "true")
        node.press("Space")
        expect(check).not_to_be_checked()
        expect(node).to_have_attribute("data-picked", "false")
        expect(page.locator(".lineage-detail")).to_contain_text(ids[0])
        if width == 1440:
            run_row = row(page, "root-run")
            run_row.hover()
            expect(node).to_have_class(re.compile(r"\bis-linked\b"))
            node.hover()
            expect(run_row).to_have_class(re.compile(r"\bis-linked\b"))
            page.get_by_role("heading", name="Runs", exact=True).hover()
            expect(run_row).not_to_have_class(re.compile(r"\bis-linked\b"))


def test_split_runs_search_keeps_unmatched_project_graphs(page, ui_server):
    home(page, ui_server)
    expect(page.locator(".lineage-node")).to_have_count(5)
    page.evaluate("window.keptGraphs = [...document.querySelectorAll('.lineage-graph')]")
    page.get_by_label("Filter runs").fill("explicit-highlr")
    expect(page.locator(".run-row")).to_have_count(1)
    expect(page.locator(".project-card")).to_have_count(2)
    expect(page.locator('.project-card[data-project="other-project"] .runs-empty')).to_have_text("No runs match this filter.")
    expect(page.locator(".lineage-node.is-dim")).to_have_count(4)
    assert page.evaluate("keptGraphs.every(graph => graph.isConnected)")
    page.get_by_label("Filter runs").fill("no-such-run")
    expect(page.locator(".run-row")).to_have_count(0)
    expect(page.locator(".project-card")).to_have_count(2)
    expect(page.locator(".runs-empty")).to_have_count(2)
    expect(page.locator(".empty")).to_have_text("No runs match this filter.")
    expect(page.locator(".lineage-node.is-dim")).to_have_count(5)
    page.get_by_role("tab", name="testbed", exact=True).click()
    expect(page.locator(".project-card")).to_have_count(1)
    expect(page.locator(".lineage-node")).to_have_count(4)
    page.get_by_label("Filter runs").fill("")
    expect(page.locator(".run-row")).to_have_count(4)
    expect(page.locator(".lineage-node.is-dim")).to_have_count(0)


@pytest.mark.parametrize("retry", ["poll", "reenter"])
def test_failed_lineage_refresh_retries_idle_project(page, ui_server, retry):
    runs = [
        {"run_id": "idle", "name": "old run", "project": "demo", "status": "finished", "keys": []},
        {"run_id": "live", "name": "live run", "project": "other", "status": "running", "keys": []},
    ]
    mock_dashboard(page, runs)
    attempts = []

    def lineage(route):
        attempts.append(route.request.url)
        if len(attempts) == 2:
            route.fulfill(status=503, body="unavailable")
        else:
            route.fulfill(content_type="application/json", body=json.dumps({"nodes": [runs[0]], "edges": []}))

    page.route("**/api/lineage?project=demo", lineage)
    page.allow_http_errors = True
    page.clock.install()
    home(page, ui_server)
    page.get_by_role("tab", name="demo", exact=True).click()
    node = page.locator('.lineage-node[data-run-id="idle"]')
    expect(node).to_contain_text("old run")
    page.evaluate("window.keptGraph = document.querySelector('.lineage-graph')")
    runs[0]["name"] = "updated run"
    page.clock.fast_forward(5000)
    expect(row(page, "updated run")).to_be_visible()
    page.wait_for_function("() => !document.querySelector('.project-card').dataset.signature")
    assert len(attempts) == 2
    assert page.evaluate("keptGraph === document.querySelector('.lineage-graph')")
    expect(node).to_contain_text("old run")
    if retry == "poll":
        page.clock.fast_forward(5000)
    else:
        page.get_by_role("tab", name="other", exact=True).click()
        page.get_by_role("tab", name="demo", exact=True).click()
    expect(node).to_contain_text("updated run")
    assert len(attempts) == 3


@pytest.mark.parametrize("source", ["run-row", "lineage-node"])
def test_hover_link_survives_poll_filter_and_detached_mouseout(page, ui_server, source):
    mock_dashboard(page, [{"run_id": "hover", "name": "hover run", "project": "demo", "status": "running", "keys": []}])
    page.clock.install()
    home(page, ui_server)
    node = page.locator(".lineage-node")
    run_row = page.locator(".run-row")
    expect(node).to_be_visible()
    page.locator(f".{source}").hover()
    linked = re.compile(r"\bis-linked\b")
    expect(node).to_have_class(linked)
    expect(run_row).to_have_class(linked)
    page.evaluate("window.oldRow = document.querySelector('.run-row')")
    page.clock.fast_forward(5000)
    page.wait_for_function("() => !oldRow.isConnected")
    expect(node).to_have_class(linked)
    expect(run_row).to_have_class(linked)
    page.get_by_label("Filter runs").fill("run")
    expect(node).to_have_class(linked)
    expect(run_row).to_have_class(linked)
    # Remove the event source during dispatch, then link its replacement before
    # the old mouseout reaches the card's delegated handler.
    run_row.evaluate("""old => {
      old.addEventListener('mouseout', () => {
        const fresh = old.cloneNode(true);
        old.replaceWith(fresh);
        fresh.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
      }, {once: true});
      old.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
    }""")
    expect(node).to_have_class(linked)
    expect(run_row).to_have_class(linked)
    page.get_by_role("heading", name="Runs", exact=True).hover()
    expect(node).not_to_have_class(linked)
    expect(run_row).not_to_have_class(linked)


def test_lineage_rebuild_keeps_focus_scroll_and_detail(page, tmp_path, ui_server_factory):
    root = tmp_path / "lineage-focus"
    ids = _lineage_project(root)
    root_meta = root / "runs" / ids[0] / "meta.json"
    meta = json.loads(root_meta.read_text())
    meta["status"] = "running"
    root_meta.write_text(json.dumps(meta))
    (root / "runs" / ids[0] / "run.lock").write_bytes(b"")
    child = ids[1]
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": 375, "height": 812})
        home(page, server)
        expect(page.locator(".poll-status")).to_contain_text("refreshes every 5 s")
        node = page.locator(f'.lineage-node[data-run-id="{child}"]')
        expect(node).to_be_visible()
        expect(page.locator(".lineage-detail")).to_contain_text("Select a run")
        node.focus()
        left = page.locator(".lineage-scroll").evaluate(
            "el => { el.scrollLeft = Math.min(180, el.scrollWidth); return el.scrollLeft; }"
        )
        assert left > 0, "lineage graph did not overflow its scroller"
        path = root / "runs" / child / "meta.json"
        updated = json.loads(path.read_text())
        updated["name"] = "child-renamed"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated))
        temporary.replace(path)
        expect(node).to_contain_text("child-renamed", timeout=12000)
        expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", child)
        assert page.locator(".lineage-scroll").evaluate("el => el.scrollLeft") == left
        page.locator(".lineage-node:focus").click()
        expect(page.locator(".lineage-detail")).to_contain_text("agenticml lineage checkout")
        expect(page.locator(".lineage-detail")).to_contain_text(child)


def test_split_runs_poll_updates_list_and_preserves_lineage(page, tmp_path, ui_server_factory):
    root = tmp_path / "lineage-poll"
    ids = _lineage_project(root)
    path = root / "runs" / ids[0] / "meta.json"
    meta = json.loads(path.read_text())
    meta.update(status="running", summary={"loss": .5})
    path.write_text(json.dumps(meta))
    (path.parent / "run.lock").write_bytes(b"")
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "/api/lineage?" in request.url else None)
    page.clock.install()
    with ui_server_factory(root) as server:
        page.set_viewport_size({"width": 375, "height": 812})
        home(page, server)
        node = page.locator(f'.lineage-node[data-run-id="{ids[1]}"]')
        node.press("Space")
        detail = page.locator(".lineage-detail")
        expect(detail).to_contain_text(ids[1])
        left = page.locator(".lineage-scroll").evaluate(
            "el => { el.scrollLeft = 180; return el.scrollLeft; }"
        )
        assert left > 0
        page.evaluate("""() => {
          window.keptCard = document.querySelector('.project-card');
          window.keptPane = document.querySelector('.lineage-body');
          window.keptGraph = document.querySelector('.lineage-graph');
          window.keptNode = document.activeElement;
          window.keptDetail = document.querySelector('.lineage-detail').firstElementChild;
          window.oldRow = document.querySelector('.run-row');
        }""")
        assert len(requests) == 1
        meta["summary"]["loss"] = .25
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(meta))
        temporary.replace(path)
        page.clock.fast_forward(5000)
        expect(row(page, "root-run").locator('.run-number[title="loss"]')).to_have_text("0.25")
        assert page.evaluate("""keptCard === document.querySelector('.project-card') &&
          keptPane === document.querySelector('.lineage-body') &&
          keptGraph === document.querySelector('.lineage-graph') &&
          keptNode === document.activeElement && keptNode.isConnected &&
          keptDetail === document.querySelector('.lineage-detail').firstElementChild &&
          !oldRow.isConnected""")
        assert len(requests) == 1
        assert page.locator(".lineage-scroll").evaluate("el => el.scrollLeft") == left
        expect(detail).to_contain_text(ids[1])
        # Running projects still refresh their lineage at the longer interval.
        page.clock.fast_forward(10000)
        page.wait_for_function("() => keptGraph !== document.querySelector('.lineage-graph')")
        assert len(requests) == 2
        expect(page.locator(".lineage-node:focus")).to_have_attribute("data-run-id", ids[1])
        assert page.locator(".lineage-scroll").evaluate("el => el.scrollLeft") == left
        assert page.evaluate("keptDetail === document.querySelector('.lineage-detail').firstElementChild")
        expect(detail).to_contain_text(ids[1])
