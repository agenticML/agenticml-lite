"""End-to-end dashboard tests using Playwright's sync API.

Every test gets a fresh browser context. Console errors, page errors, and HTTP
responses >= 400 from the dashboard fail the test at teardown. The missing-run
route opts out of the 4xx check because that response is the page's error state.
"""
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from agenticml.store.reader import RunReader

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

pytestmark = [pytest.mark.browser, pytest.mark.ui]

TESTBED = Path(__file__).resolve().parents[1]
CONSOLE = Path(sys.executable).parent / "agenticml"
UI_URL = re.compile(r"AgenticML UI: (http://127\.0\.0\.1:\d+)")
RUNS = (
    ("explicit-baseline", "testbed", "finished", "final_loss"),
    ("explicit-highlr", "testbed", "finished", "final_loss"),
    ("zero-code", "testbed", "finished", ""),
    ("crashed", "testbed", "crashed", "_exception"),
    ("other-project", "other-project", "finished", "final_loss"),
)
COMPARE_RUNS = ("explicit-baseline", "explicit-highlr")
# app.js emits raw, low, high, and mean for every compared run. EMA weight 0
# keeps the raw series hidden, so three series per visible run are shown.
SERIES_PER_RUN = 4
SHOWN_PER_RUN = 3
LIVE_TIMEOUT = 12000
# `var uPlot` installs a non-configurable global, so the wrapper has to replace
# the constructor after that script runs and before app.js does `new uPlot`.
UPLOT_HOOK = r"""
(() => {
  if (window.__chartState) return;
  const plots = [];
  window.__plots = plots;
  const wrap = () => {
    const Ctor = window.uPlot;
    if (typeof Ctor !== 'function' || Ctor.__tracksPlots) return false;
    function Tracked(...args) {
      const plot = new Ctor(...args);
      plots.push(plot);
      const destroy = plot.destroy.bind(plot);
      plot.destroy = (...destroyArgs) => {
        const index = plots.indexOf(plot);
        if (index >= 0) plots.splice(index, 1);
        return destroy(...destroyArgs);
      };
      return plot;
    }
    Tracked.__tracksPlots = true;
    Tracked.prototype = Ctor.prototype;
    Object.setPrototypeOf(Tracked, Ctor);
    window.uPlot = Tracked;
    return true;
  };
  const timer = setInterval(() => {
    if (typeof window.uPlot !== 'function') return;
    if (!window.uPlot.__tracksPlots) wrap();
    clearInterval(timer);
  }, 0);
  const plotFor = key => {
    const card = [...document.querySelectorAll('article.chart-card')].find(node => node.dataset.key === key);
    if (!card) return null;
    const plot = plots.find(item => item.root && item.root.isConnected && card.contains(item.root));
    return plot ? {card, plot} : {card, plot: null};
  };
  window.__chartState = key => [...document.querySelectorAll('article.chart-card')]
    .filter(card => !key || card.dataset.key === key)
    .map(card => {
      const plot = plots.find(item => item.root && item.root.isConnected && card.contains(item.root));
      const data = plot && plot.data ? plot.data : [];
      const xs = (data[0] || []).filter(value => Number.isFinite(value));
      const scaleX = plot && plot.scales ? plot.scales.x : null;
      const scaleY = plot && plot.scales ? plot.scales.y : null;
      return {
        key: card.dataset.key,
        hidden: !!card.hidden,
        open: !!card.closest('details')?.open,
        n: xs.length,
        xs,
        xMin: scaleX && Number.isFinite(scaleX.min) ? scaleX.min : null,
        xMax: scaleX && Number.isFinite(scaleX.max) ? scaleX.max : null,
        yDistr: scaleY ? scaleY.distr : null,
        series: plot ? plot.series.slice(1).map((series, index) => {
          const values = data[index + 1] || [];
          return {
            label: series.label || '',
            show: !!series.show,
            finite: values.filter(value => Number.isFinite(value)).length,
          };
        }) : [],
      };
    });
  window.__emaDelta = key => {
    const found = plotFor(key);
    if (!found || !found.plot || !found.plot.data) return null;
    const plot = found.plot;
    const groups = new Map();
    for (let index = 1; index < plot.series.length; index += 1) {
      const label = plot.series[index].label || '';
      const split = label.lastIndexOf(' ');
      if (split <= 0) continue;
      const name = label.slice(0, split);
      const field = label.slice(split + 1);
      if (!groups.has(name)) groups.set(name, {});
      groups.get(name)[field] = {show: !!plot.series[index].show, values: plot.data[index] || []};
    }
    let differ = 0;
    let compared = 0;
    let rawShown = false;
    for (const group of groups.values()) {
      if (!group.raw || !group.mean) continue;
      rawShown = rawShown || group.raw.show;
      const raw = group.raw.values;
      const mean = group.mean.values;
      for (let index = 0; index < raw.length; index += 1) {
        if (Number.isFinite(raw[index]) && Number.isFinite(mean[index])) {
          compared += 1;
          if (raw[index] !== mean[index]) differ += 1;
        }
      }
    }
    return {differ, compared, rawShown};
  };
})();
"""
LOSS_READY = r"""
({steps, visible, total}) => {
  const state = (window.__chartState('train/loss') || [])[0];
  if (!state || state.xs.length !== steps.length) return false;
  if (!state.xs.every((value, index) => value === steps[index])) return false;
  if (state.series.length !== total * 4) return false;
  const shown = state.series.filter(series => series.show);
  if (shown.length !== visible.length * 3) return false;
  if (shown.some(series => series.label.endsWith(' raw'))) return false;
  const names = new Set(shown.map(series => series.label.slice(0, series.label.lastIndexOf(' '))));
  if (names.size !== visible.length || visible.some(name => !names.has(name))) return false;
  return visible.every(name => shown.some(series =>
    series.label === `${name} mean` && series.finite === steps.length));
}
"""


def _ignored_browser_noise(text):
    lowered = text.lower()
    return "favicon" in lowered or "err_aborted" in lowered


def _stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc.stdout:
        proc.stdout.close()


class _UIServer:
    def __init__(self, root):
        self.proc = subprocess.Popen(
            [str(CONSOLE), "ui", "--dir", str(root), "--port", "0"],
            cwd=TESTBED,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.lines = queue.Queue()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def url(self):
        try:
            line = self.lines.get(timeout=15)
        except queue.Empty:
            raise AssertionError("UI did not announce a URL within 15s") from None
        if line is None:
            raise AssertionError("UI exited before announcing its URL")
        match = UI_URL.search(line)
        if not match:
            raise AssertionError(f"unexpected UI output: {line!r}")
        return match.group(1)

    def stop(self):
        _stop_process(self.proc)
        self.thread.join(timeout=2)

    def __enter__(self):
        try:
            self.address = self.url()
        except Exception:
            self.stop()
            raise
        return self.address

    def __exit__(self, *exc):
        self.stop()


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
    ctx = browser.new_context(
        viewport={"width": 1400, "height": 1000},
        locale="en-US",
        color_scheme="light",
    )
    ctx.route("**/favicon.ico", lambda route: route.fulfill(status=204, body=""))
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def page(context):
    tab = context.new_page()
    tab.add_init_script(UPLOT_HOOK)
    state = {"problems": [], "allow_http_errors": False}

    def on_console(msg):
        if msg.type != "error" or _ignored_browser_noise(msg.text):
            return
        state["problems"].append(f"console: {msg.text}")

    def on_pageerror(exc):
        state["problems"].append(f"pageerror: {exc}")

    def on_response(response):
        if response.status < 400 or _ignored_browser_noise(response.url):
            return
        state["problems"].append(f"HTTP {response.status} {response.url}")

    def on_request_failed(request):
        failure = request.failure or ""
        if _ignored_browser_noise(failure) or _ignored_browser_noise(request.url):
            return
        state["problems"].append(f"failed: {request.url} {failure}")

    tab.on("console", on_console)
    tab.on("pageerror", on_pageerror)
    tab.on("response", on_response)
    tab.on("requestfailed", on_request_failed)
    tab.allow_http_errors = lambda: state.__setitem__("allow_http_errors", True)
    yield tab
    problems = list(state["problems"])
    if state["allow_http_errors"]:
        def allowed(item):
            # Chromium logs the same 4xx responses the page turns into an error.
            return item.startswith("HTTP 4") or item.startswith("console: Failed to load resource:")

        other = [item for item in problems if not allowed(item)]
        assert not other, "unexpected browser errors:\n" + "\n".join(other)
        assert any(item.startswith("HTTP 4") for item in problems), (
            "expected the missing run route to receive an HTTP 4xx"
        )
    else:
        assert not problems, "browser errors:\n" + "\n".join(problems)
    tab.close()


def open_home(page, url):
    page.goto(url)
    expect(page.get_by_role("heading", name="Experiments", exact=True)).to_be_visible()


def run_row(page, name):
    return page.locator("tbody tr").filter(
        has=page.get_by_role("link", name=name, exact=True)
    )


def loss_steps(ui_server, name):
    series = RunReader(ui_server.root, ui_server.runs[name]).series("train/loss")
    assert series["mode"] == "raw", series["mode"]
    steps = [point[0] for point in series["points"]]
    assert steps and steps == sorted(set(steps))
    return steps


def chart_state(page, key="train/loss"):
    return page.evaluate("(key) => (window.__chartState(key) || [])[0] || null", key)


def response_matches(response, *, kind, run_id=None, key=None, bounds=None):
    if not response.ok:
        return False
    parts = urlsplit(response.url)
    tail = parts.path.rsplit("/", 1)[-1]
    if tail != kind:
        return False
    if run_id is not None and f"/{run_id}/" not in parts.path:
        return False
    if kind != "scalars":
        return True
    query = parse_qs(parts.query)
    if key is not None and key not in query.get("key", []):
        return False
    has_bounds = "start" in query and "end" in query
    if bounds is not None and has_bounds != bounds:
        return False
    return True


def expect_api(page, action, timeout=15000, **match):
    with page.expect_response(lambda response: response_matches(response, **match), timeout=timeout) as caught:
        action()
    return caught.value


def quiet(page):
    page.wait_for_load_state("networkidle", timeout=15000)


def require_plot_hook(page):
    page.wait_for_function(
        "() => typeof window.uPlot === 'function' && window.uPlot.__tracksPlots === true",
        timeout=15000,
    )


def wait_for_loss_plot(page, steps, visible, total=None):
    page.locator('article.chart-card[data-key="train/loss"]').scroll_into_view_if_needed()
    require_plot_hook(page)
    page.wait_for_function(
        LOSS_READY,
        arg={"steps": steps, "visible": list(visible), "total": total or len(visible)},
        timeout=15000,
    )
    expect(page.locator(".poll-status")).to_have_text("Up to date")
    quiet(page)


def open_compare(page, url):
    open_home(page, url)
    expect(page.get_by_role("button", name="Compare selected (0)")).to_be_disabled()
    page.get_by_role("checkbox", name="Select explicit-baseline", exact=True).check()
    page.get_by_role("checkbox", name="Select explicit-highlr", exact=True).check()
    button = page.get_by_role("button", name="Compare selected (2)")
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_role("heading", name="Compare 2 runs", exact=True)).to_be_visible()


def open_loss_plot(page, ui_server):
    open_compare(page, ui_server.url)
    steps = loss_steps(ui_server, "explicit-baseline")
    assert steps == loss_steps(ui_server, "explicit-highlr")
    wait_for_loss_plot(page, steps, COMPARE_RUNS)
    return steps


def run_checkbox(page, name):
    return page.locator(".run-list label").filter(
        has=page.get_by_role("link", name=name, exact=True)
    ).locator("input[type=checkbox]")


def loss_card(page):
    return page.locator('article.chart-card[data-key="train/loss"]')


def drag_plot(page, start=0.18, end=0.62):
    overlay = loss_card(page).locator(".u-over")
    overlay.scroll_into_view_if_needed()
    expect(overlay).to_be_visible()
    box = overlay.bounding_box()
    assert box is not None and box["width"] > 100 and box["height"] > 40, box
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * start, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * end, y, steps=24)
    page.mouse.up()


def assert_narrow_bounds(url, steps):
    query = parse_qs(urlsplit(url).query)
    assert "train/loss" in query.get("key", []), query
    start = int(query["start"][0])
    end = int(query["end"][0])
    assert steps[0] <= start <= end <= steps[-1], (start, end, steps[0], steps[-1])
    assert end - start < steps[-1] - steps[0], (start, end, steps)
    return start, end


def x_span(state):
    assert state["xMin"] is not None and state["xMax"] is not None, state
    span = state["xMax"] - state["xMin"]
    assert span > 0, state
    return span


def metric_keys(page, visible_only):
    return page.locator("article.chart-card").evaluate_all(
        """(nodes, visibleOnly) => nodes
            .filter(node => !visibleOnly || !node.hidden)
            .map(node => node.dataset.key)
            .sort()""",
        visible_only,
    )


def train_section(page):
    return page.locator("details.section").filter(
        has=page.locator("summary", has_text=re.compile(r"^train$"))
    )


def test_home_lists_every_run(page, ui_server):
    open_home(page, ui_server.url)
    expect(page.locator(".poll-status")).to_have_text("5 runs")
    expect(page.locator("tbody tr")).to_have_count(len(RUNS))
    for name, project, status, summary_text in RUNS:
        row = run_row(page, name)
        expect(row).to_be_visible()
        expect(row.get_by_role("link", name=name, exact=True)).to_be_visible()
        expect(row.locator("td").nth(2)).to_have_text(project)
        badge = row.locator(".badge")
        expect(badge).to_have_text(status)
        expect(badge).to_have_class(re.compile(rf"\b{status}\b"))
        summary = row.locator(".summary")
        expect(summary).to_have_count(1)
        if summary_text:
            expect(summary).to_contain_text(summary_text)


def test_project_filter_limits_rows(page, ui_server):
    open_home(page, ui_server.url)
    select = page.get_by_label("Project filter")
    expect(select.locator("option")).to_have_text(["All projects", "other-project", "testbed"])

    select.select_option(label="testbed")
    expect(page.locator("tbody tr")).to_have_count(4)
    expect(page.get_by_role("link", name="other-project", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="explicit-baseline", exact=True)).to_be_visible()

    select.select_option(label="other-project")
    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.get_by_role("link", name="other-project", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="crashed", exact=True)).to_have_count(0)

    select.select_option(label="All projects")
    expect(page.locator("tbody tr")).to_have_count(5)


def test_search_filters_by_name_and_tag(page, ui_server):
    open_home(page, ui_server.url)
    search = page.get_by_label("Filter runs")

    search.fill("Zero-Code")
    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.get_by_role("link", name="zero-code", exact=True)).to_be_visible()

    # "fixture" is a tag, not part of any run name. zero-code is launched
    # without --explicit, so the script's tags are not stored on that run.
    search.fill("FIXTURE")
    expect(page.get_by_role("link", name="zero-code", exact=True)).to_have_count(0)
    for name in ("explicit-baseline", "explicit-highlr", "crashed", "other-project"):
        expect(page.get_by_role("link", name=name, exact=True)).to_be_visible()
    expect(page.locator("tbody tr")).to_have_count(4)

    # "baseline" matches crashed and other-project only through their tags.
    search.fill("baseline")
    expect(page.get_by_role("link", name="crashed", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="other-project", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="explicit-baseline", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="explicit-highlr", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="zero-code", exact=True)).to_have_count(0)

    search.fill("highlr")
    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.get_by_role("link", name="explicit-highlr", exact=True)).to_be_visible()

    search.fill("")
    expect(page.locator("tbody tr")).to_have_count(5)


def test_compare_selected_updates_and_navigates(page, ui_server):
    open_compare(page, ui_server.url)
    url = page.url
    baseline = ui_server.runs["explicit-baseline"]
    highlr = ui_server.runs["explicit-highlr"]
    assert "#/compare?" in url
    assert url.index(baseline) < url.index(highlr)


def test_run_page_shows_facts_config_code_media_and_artifacts(page, ui_server):
    open_home(page, ui_server.url)
    page.get_by_role("link", name="explicit-baseline", exact=True).click()
    expect(page.get_by_role("heading", name="explicit-baseline", exact=True)).to_be_visible()
    expect(page).to_have_url(re.compile(r"#/run/"))
    expect(page.locator("#app > .badge")).to_have_text("finished")

    facts = page.locator(".run-facts")
    expect(facts).to_be_visible()
    text = facts.inner_text()
    assert "testbed" in text and "finished" in text
    assert re.search(r"Created .*\d", text)
    assert re.search(r"Finished .*\d", text)
    assert re.search(r"Duration .+ s", text)
    assert "Host —" not in text and "Created —" not in text and "Finished —" not in text
    assert re.search(r"Git (?:[0-9a-f]{8}|—)", text)

    config = page.locator("section.card").filter(
        has=page.get_by_role("heading", name="Config", exact=True)
    )
    expect(config.get_by_role("cell", name="lr", exact=True)).to_be_visible()
    expect(config.get_by_role("cell", name="epochs", exact=True)).to_be_visible()
    config.get_by_label("Search Config").fill("batch_size")
    expect(config.get_by_role("cell", name="batch_size", exact=True)).to_be_visible()
    expect(config.get_by_role("cell", name="lr", exact=True)).to_be_hidden()
    expect(config.get_by_role("cell", name="epochs", exact=True)).to_be_hidden()
    config.get_by_label("Search Config").fill("")
    expect(config.get_by_role("cell", name="lr", exact=True)).to_be_visible()

    environment = page.locator("section.card").filter(
        has=page.get_by_role("heading", name="Environment", exact=True)
    )
    expect(environment.get_by_role("cell", name="python_version", exact=True)).to_be_visible()
    expect(environment.get_by_role("cell", name="hostname", exact=True)).to_be_visible()
    expect(environment.get_by_text("Packages", exact=True)).to_be_visible()

    heading = page.get_by_role("heading", name=re.compile(r"^Code · \d+ files?$"))
    expect(heading).to_be_visible()
    file_count = int(re.search(r"(\d+)", heading.inner_text()).group(1))
    tree = page.get_by_role("navigation", name="Code files")
    for _ in range(8):
        closed = tree.locator("details:not([open]) > summary")
        if closed.count() == 0:
            break
        closed.first.click()
    else:
        raise AssertionError("code tree did not fully expand")
    expect(tree.get_by_role("button")).to_have_count(file_count)
    for filename in ("model.py", "train.py", "data.py"):
        expect(tree.get_by_role("button", name=filename, exact=True)).to_be_visible()
    tree.get_by_role("button", name="model.py", exact=True).click()
    expect(page.locator(".code-view")).to_contain_text("class LogisticRegression")

    image = page.locator(".media-strip img")
    expect(image).to_have_count(1)
    expect(image).to_have_attribute("alt", "decision boundary")
    image.scroll_into_view_if_needed()
    page.wait_for_function(
        """() => {
          const img = document.querySelector('.media-strip img');
          return !!img && img.naturalWidth > 0;
        }""",
        timeout=10000,
    )
    assert image.evaluate("node => node.naturalWidth") > 0
    image.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    page.wait_for_function(
        """() => {
          const img = document.querySelector('#lightbox img');
          return !!img && img.naturalWidth > 0;
        }""",
        timeout=10000,
    )
    assert dialog.locator("img").evaluate("node => node.naturalWidth") > 0
    dialog.get_by_role("button", name="Close").click()
    expect(dialog).to_be_hidden()

    artifacts = page.locator("section.card").filter(
        has=page.get_by_role("heading", name="Artifacts", exact=True)
    )
    expect(artifacts.locator("tbody tr")).to_have_count(2)
    expect(artifacts.get_by_role("cell", name="checkpoint", exact=True)).to_have_count(2)
    expect(artifacts.get_by_role("cell", name="model", exact=True)).to_have_count(2)


def test_compare_charts_render_and_stay_healthy(page, ui_server):
    open_compare(page, ui_server.url)
    expect(page.locator("details.section > summary").filter(has_text=re.compile(r"^train$"))).to_be_visible()
    expect(page.locator("details.section > summary").filter(has_text=re.compile(r"^val$"))).to_be_visible()
    legend = page.locator(".run-list")
    swatches = legend.locator(".swatch")
    expect(swatches).to_have_count(2)
    expect(legend.get_by_role("link", name="explicit-baseline", exact=True)).to_be_visible()
    expect(legend.get_by_role("link", name="explicit-highlr", exact=True)).to_be_visible()
    colors = swatches.evaluate_all(
        "nodes => nodes.map(node => getComputedStyle(node).backgroundColor)"
    )
    assert all(color not in ("", "transparent", "rgba(0, 0, 0, 0)") for color in colors)

    steps = loss_steps(ui_server, "explicit-baseline")
    assert steps == loss_steps(ui_server, "explicit-highlr")
    wait_for_loss_plot(page, steps, COMPARE_RUNS)
    state = chart_state(page)
    assert state["xs"] == steps
    assert len(state["series"]) == SERIES_PER_RUN * len(COMPARE_RUNS)
    assert sum(series["show"] for series in state["series"]) == SHOWN_PER_RUN * len(COMPARE_RUNS)

    expect_api(
        page,
        lambda: run_checkbox(page, "explicit-highlr").uncheck(),
        kind="scalars",
        run_id=ui_server.runs["explicit-baseline"],
        key="train/loss",
        bounds=False,
    )
    wait_for_loss_plot(page, steps, ["explicit-baseline"], total=len(COMPARE_RUNS))
    hidden = chart_state(page)
    assert hidden["xs"] == steps
    assert len(hidden["series"]) == SERIES_PER_RUN * len(COMPARE_RUNS)
    assert sum(series["show"] for series in hidden["series"]) == SHOWN_PER_RUN
    assert not any(
        series["show"] and series["label"].startswith("explicit-highlr ")
        for series in hidden["series"]
    )

    expect_api(
        page,
        lambda: page.get_by_role("combobox", name="X axis").select_option("time"),
        kind="time",
        run_id=ui_server.runs["explicit-baseline"],
    )
    page.wait_for_function(
        r"""
        (previous) => {
          const state = (window.__chartState('train/loss') || [])[0];
          if (!state || !state.n) return false;
          const shown = state.series.filter(series => series.show);
          const names = new Set(shown.map(series => series.label.slice(0, series.label.lastIndexOf(' '))));
          if (names.size !== 1 || !names.has('explicit-baseline')) return false;
          if (!shown.some(series => series.label === 'explicit-baseline mean' && series.finite > 0)) return false;
          if (state.xs.length !== previous.length) return true;
          return state.xs.some((value, index) => value !== previous[index]);
        }
        """,
        arg=steps,
        timeout=15000,
    )
    expect(page.locator(".poll-status")).to_have_text("Up to date")
    assert chart_state(page)["xs"] != steps


def test_zoom_drag_narrows_range_and_reset_clears_bounds(page, ui_server):
    steps = open_loss_plot(page, ui_server)
    before = chart_state(page)
    full_span = x_span(before)

    response = expect_api(page, lambda: drag_plot(page), kind="scalars", key="train/loss", bounds=True)
    start, end = assert_narrow_bounds(response.url, steps)
    page.wait_for_function(
        r"""
        ({count, start, end, span}) => {
          const state = (window.__chartState('train/loss') || [])[0];
          if (!state || !state.n || state.xMin == null || state.xMax == null) return false;
          const width = state.xMax - state.xMin;
          const last = state.xs[state.xs.length - 1];
          return state.n < count && state.n > 0 && width > 0 && width < span * 0.85
            && state.xs[0] >= start && last <= end;
        }
        """,
        arg={"count": len(steps), "start": start, "end": end, "span": full_span},
        timeout=15000,
    )
    zoomed = chart_state(page)
    assert zoomed["n"] < len(steps)
    assert x_span(zoomed) < full_span * 0.85
    quiet(page)

    expect_api(
        page,
        lambda: loss_card(page).locator(".chart-host").dblclick(),
        kind="scalars",
        key="train/loss",
        bounds=False,
    )
    page.wait_for_function(
        r"""
        (steps) => {
          const state = (window.__chartState('train/loss') || [])[0];
          if (!state || state.xMin == null || state.xMax == null) return false;
          return state.xs.length === steps.length
            && state.xs.every((value, index) => value === steps[index]);
        }
        """,
        arg=steps,
        timeout=15000,
    )
    restored = chart_state(page)
    assert restored["xs"] == steps
    assert x_span(restored) > x_span(zoomed)
    quiet(page)

    time_before_xs = restored["xs"]
    expect_api(
        page,
        lambda: page.get_by_role("combobox", name="X axis").select_option("time"),
        kind="time",
    )
    page.wait_for_function(
        r"""
        (previous) => {
          const state = (window.__chartState('train/loss') || [])[0];
          if (!state || !state.n || state.xMin == null || state.xMax == null) return false;
          if (!state.series.some(series => series.show && series.finite > 0)) return false;
          if (state.xs.length !== previous.length) return true;
          return state.xs.some((value, index) => value !== previous[index]);
        }
        """,
        arg=time_before_xs,
        timeout=15000,
    )
    timed = chart_state(page)
    assert timed["xs"] != steps
    time_span = x_span(timed)
    quiet(page)

    response = expect_api(page, lambda: drag_plot(page), kind="scalars", key="train/loss", bounds=True)
    assert_narrow_bounds(response.url, steps)
    page.wait_for_function(
        r"""
        (span) => {
          const state = (window.__chartState('train/loss') || [])[0];
          if (!state || !state.n || state.xMin == null || state.xMax == null) return false;
          return state.xMax - state.xMin < span * 0.85;
        }
        """,
        arg=time_span,
        timeout=15000,
    )
    time_zoomed = chart_state(page)
    assert x_span(time_zoomed) < time_span * 0.85
    quiet(page)

    expect_api(
        page,
        lambda: page.get_by_role("button", name="Reset zoom").click(),
        kind="scalars",
        key="train/loss",
        bounds=False,
    )
    page.wait_for_function(
        r"""
        (span) => {
          const state = (window.__chartState('train/loss') || [])[0];
          if (!state || !state.n || state.xMin == null || state.xMax == null) return false;
          return state.xMax - state.xMin > span * 0.9;
        }
        """,
        arg=time_span,
        timeout=15000,
    )
    assert x_span(chart_state(page)) > x_span(time_zoomed)


def test_ema_smoothing_changes_plotted_values(page, ui_server):
    steps = open_loss_plot(page, ui_server)
    series = RunReader(ui_server.root, ui_server.runs["explicit-baseline"]).series("train/loss")
    assert len({point[1] for point in series["points"]}) > 1
    initial = page.evaluate("() => window.__emaDelta('train/loss')")
    assert initial["compared"] == len(steps) * len(COMPARE_RUNS)
    assert initial["differ"] == 0
    assert initial["rawShown"] is False

    slider = page.get_by_label("EMA smoothing")
    slider.fill("0.9")
    expect(page.locator("label").filter(has=slider)).to_contain_text("0.90")
    page.wait_for_function(
        r"""
        (count) => {
          const delta = window.__emaDelta('train/loss');
          return !!delta && delta.compared === count && delta.differ > 0 && delta.rawShown === true;
        }
        """,
        arg=len(steps) * len(COMPARE_RUNS),
        timeout=15000,
    )


def test_log_y_scale_changes_distribution(page, ui_server):
    open_loss_plot(page, ui_server)
    assert chart_state(page)["yDistr"] == 1
    toggle = page.get_by_label("Logarithmic y axis")
    toggle.check()
    page.wait_for_function(
        r"""
        () => {
          const state = (window.__chartState('train/loss') || [])[0];
          return !!state && state.yDistr === 3 && state.n > 0
            && state.series.some(series => series.show && series.finite > 0);
        }
        """,
        timeout=15000,
    )
    toggle.uncheck()
    page.wait_for_function(
        r"""
        (steps) => {
          const state = (window.__chartState('train/loss') || [])[0];
          return !!state && state.yDistr === 1 && state.xs.length === steps.length
            && state.xs.every((value, index) => value === steps[index]);
        }
        """,
        arg=loss_steps(ui_server, "explicit-baseline"),
        timeout=15000,
    )


def test_metric_filter_shows_exact_cards(page, ui_server):
    open_compare(page, ui_server.url)
    expect(loss_card(page)).to_be_visible()
    all_keys = metric_keys(page, False)
    assert {"train/loss", "train/lr", "val/loss", "val/acc"} <= set(all_keys)
    search = page.get_by_label("Filter metric keys")

    def expect_exact(query):
        search.fill(query)
        expected = sorted(key for key in all_keys if query.lower() in key.lower())
        assert metric_keys(page, True) == expected
        for key in all_keys:
            card = page.locator(f'article.chart-card[data-key="{key}"]')
            if key in expected:
                expect(card).to_be_visible()
            else:
                expect(card).to_be_hidden()

    expect_exact("train/loss")
    expect_exact("VAL/")
    expect_exact("loss")
    search.fill("")
    assert metric_keys(page, True) == sorted(all_keys)
    expect(loss_card(page)).to_be_visible()


def test_reopening_section_reloads_plots(page, ui_server):
    steps = open_loss_plot(page, ui_server)
    section = train_section(page)
    expect(section).to_have_count(1)
    expect(section).to_have_js_property("open", True)
    summary = section.locator("> summary")
    summary.click()
    expect(section).to_have_js_property("open", False)
    expect(loss_card(page)).to_be_hidden()
    quiet(page)

    response = expect_api(page, lambda: summary.click(), kind="scalars", key="train/loss", bounds=False)
    assert response.ok
    expect(section).to_have_js_property("open", True)
    wait_for_loss_plot(page, steps, COMPARE_RUNS)
    assert chart_state(page)["xs"] == steps


def test_theme_cycles_and_persists(page, ui_server):
    open_home(page, ui_server.url)
    theme = page.get_by_role("button", name="Change color theme")
    expect(theme).to_have_text("Theme: system")

    def applied():
        return page.evaluate(
            """() => ({
              stored: localStorage.getItem('agenticml-theme'),
              theme: document.documentElement.dataset.theme,
              bg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
            })"""
        )

    initial = applied()
    assert initial["stored"] is None
    assert initial["theme"] == "light"
    assert initial["bg"] == "#f7f8f8"

    theme.click()
    expect(theme).to_have_text("Theme: light")
    assert applied() == {"stored": "light", "theme": "light", "bg": "#f7f8f8"}

    theme.click()
    expect(theme).to_have_text("Theme: dark")
    assert applied() == {"stored": "dark", "theme": "dark", "bg": "#0b0e0f"}

    page.reload()
    expect(theme).to_have_text("Theme: dark")
    assert applied()["stored"] == "dark"
    assert applied()["theme"] == "dark"
    assert applied()["bg"] == "#0b0e0f"


def test_back_and_forward_between_routes(page, ui_server):
    open_home(page, ui_server.url)
    page.get_by_role("link", name="explicit-baseline", exact=True).click()
    expect(page.get_by_role("heading", name="explicit-baseline", exact=True)).to_be_visible()
    expect(page).to_have_url(re.compile(r"#/run/"))

    page.go_back()
    expect(page.get_by_role("heading", name="Experiments", exact=True)).to_be_visible()
    expect(page).not_to_have_url(re.compile(r"#/run/"))

    page.go_forward()
    expect(page.get_by_role("heading", name="explicit-baseline", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Config", exact=True)).to_be_visible()

    page.get_by_role("link", name="Charts").click()
    expect(page.get_by_role("button", name="Reset zoom")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#/compare\?"))
    expect(page.locator(".poll-status")).to_have_text("Up to date")

    page.go_back()
    expect(page.get_by_role("heading", name="Config", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Reset zoom")).to_have_count(0)

    page.go_forward()
    expect(page.get_by_role("button", name="Reset zoom")).to_be_visible()
    expect(page.locator(".poll-status")).to_have_text("Up to date")


@pytest.mark.parametrize(("run_id", "status_code"), [
    ("unknown-valid-id", 404),
    (".hidden", 400),
])
def test_bad_run_route_shows_error(page, ui_server, run_id, status_code):
    page.allow_http_errors()
    page.goto(f"{ui_server.url}#/run/{run_id}")
    error = page.locator("#app .error")
    expect(error).to_be_visible()
    expect(error).to_contain_text(str(status_code))
    expect(page.get_by_role("heading", name="Experiments")).to_have_count(0)
    expect(page.locator("#app .error")).to_have_count(1)


def test_empty_store_shows_no_runs(page, tmp_path):
    root = tmp_path / "empty-store"
    root.mkdir()
    with _UIServer(root) as url:
        open_home(page, url)
        expect(page.get_by_text(
            "No runs found. Start a local experiment to see it here.",
            exact=True,
        )).to_be_visible()
        expect(page.locator(".poll-status")).to_have_text("0 runs")


def _open_live_run(root):
    import agenticml

    return agenticml, agenticml.init(
        "live-project",
        "live-run",
        dir=root,
        flush_interval=0.01,
        system_metrics=False,
        auto_track=False,
        track_source=False,
        capture_env=False,
    )


def test_live_run_finishes_without_reload(page, tmp_path):
    root = tmp_path / "live-store"
    root.mkdir()
    agenticml, _run = _open_live_run(root)
    try:
        with _UIServer(root) as url:
            open_home(page, url)
            badge = run_row(page, "live-run").locator(".badge")
            expect(badge).to_have_text("running")
            expect(page.locator(".poll-status")).to_have_text("Live · refreshes every 5 s")
            page.evaluate("() => { window.__stayed = true; }")
            agenticml.finish()
            expect(badge).to_have_text("finished", timeout=LIVE_TIMEOUT)
            expect(page.locator(".poll-status")).to_have_text("1 runs")
            assert page.evaluate("() => window.__stayed === true")
    finally:
        agenticml.finish()


def test_live_chart_shows_new_points_and_metric(page, tmp_path):
    root = tmp_path / "live-chart"
    root.mkdir()
    agenticml, run = _open_live_run(root)
    try:
        for step, loss in enumerate((0.9, 0.6, 0.4)):
            agenticml.log({"train/loss": loss}, step=step)
        run._flush()
        with _UIServer(root) as url:
            page.set_viewport_size({"width": 1400, "height": 1400})
            page.goto(f"{url}#/compare?runs={run.run_id}")
            expect(page.get_by_role("heading", name="live-run", exact=True)).to_be_visible()
            expect(page.locator(".poll-status")).to_have_text("Live · refreshes every 5 s", timeout=15000)
            require_plot_hook(page)
            loss_card(page).scroll_into_view_if_needed()
            page.wait_for_function(
                r"""
                () => {
                  const state = (window.__chartState('train/loss') || [])[0];
                  return !!state && state.n === 3
                    && state.series.some(series => series.label.endsWith(' mean') && series.show && series.finite === 3);
                }
                """,
                timeout=15000,
            )
            page.evaluate("() => { window.__stayed = true; }")
            agenticml.log({"train/loss": 0.2, "train/extra": 1.25}, step=3)
            agenticml.log({"train/loss": 0.1}, step=4)
            run._flush()
            page.wait_for_function(
                r"""
                () => {
                  const loss = (window.__chartState('train/loss') || [])[0];
                  const extra = (window.__chartState('train/extra') || [])[0];
                  return !!loss && loss.n === 5
                    && loss.series.some(series => series.show && series.label.endsWith(' mean') && series.finite === 5)
                    && !!extra && extra.n === 1 && !extra.hidden
                    && extra.series.some(series => series.show && series.label.endsWith(' mean') && series.finite === 1);
                }
                """,
                timeout=LIVE_TIMEOUT,
            )
            extra = chart_state(page, "train/extra")
            assert extra["n"] == 1
            assert chart_state(page)["n"] == 5
            assert page.evaluate("() => window.__stayed === true")
    finally:
        agenticml.finish()


def test_mobile_home_has_no_horizontal_scroll(page, ui_server):
    page.set_viewport_size({"width": 375, "height": 812})
    open_home(page, ui_server.url)
    expect(page.locator("tbody tr")).to_have_count(5)
    expect(page.get_by_label("Filter runs")).to_be_visible()
    overflow = page.evaluate(
        """() => {
          const nodes = [document.documentElement, document.body];
          return Math.max(...nodes.map(node => node.scrollWidth - node.clientWidth));
        }"""
    )
    assert overflow <= 0, f"home page scrolls horizontally by {overflow}px"
