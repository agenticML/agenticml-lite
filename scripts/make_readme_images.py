#!/usr/bin/env python
"""Regenerate the README images.

Seeds a temporary store with a few deterministic training runs, serves it with
the dashboard, and writes:

- docs/assets/dashboard.png (1440 wide, light theme, runs overlaid in compare view,
  cropped below the chart cards)
- docs/assets/social-preview.png (1280x640, for GitHub's social preview)
- docs/assets/demo.gif (the README's animated demo, 960 wide, about 20 s, loops:
  a live run's curves growing, the task's lineage graph, select and compare,
  then hover readouts and smoothing in the metrics workspace)
- docs/assets/demo-lineage.gif (``--only lineage``, a draft that puts the
  lineage graph first: a deeper tree with code snapshots, the best path,
  what changed at each step, then the best path compared; captioned)

Run from the repo root with the testbed venv (it has Playwright and Pillow):

    testbed/.venv/bin/python scripts/make_readme_images.py [--only {all,gif,png,lineage}]

A full run takes about 15 seconds (the GIF is one screenshot per frame).
``--only png`` writes dashboard.png and social-preview.png, ``--only gif``
writes demo.gif only, ``--only lineage`` writes demo-lineage.gif only (from
its own store, seeded with two more runs and real code snapshots).

No network access: fonts and the logo come from files in this repo. Not part
of the test suite.

The GIF is built frame by frame: every frame is a Playwright screenshot, the
dashboard's polling runs on Playwright's fake clock (so which steps a frame
shows never depends on real time), and the frames are joined with Pillow
(no ffmpeg needed). The live run in the first scene is a real run: this
process holds it open, logs more steps between frames, and finishes it
before the other scenes and before dashboard.png. Not pinned: the run
ids, the start date in the run list (the browser runs in UTC), and the wall
time of the live run, so two runs differ in a few pixels of text.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import itertools
import math
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agenticml  # noqa: E402
from agenticml.ui.server import make_server  # noqa: E402

ASSETS = ROOT / "docs" / "assets"
STATIC = ROOT / "src" / "agenticml" / "ui" / "static"
LOGO = ROOT / "website" / "src" / "assets" / "logo-light.svg"
DASHBOARD_PNG = ASSETS / "dashboard.png"
SOCIAL_PNG = ASSETS / "social-preview.png"
DEMO_GIF = ASSETS / "demo.gif"
LINEAGE_GIF = ASSETS / "demo-lineage.gif"

PROJECT = "cifar-resnet"
TASK = "image-classification"
STEPS = 200
DEMO_PORT = 47321
PITCH = (
    "Experiment tracking for machine learning that runs from files on disk. "
    "A W&B-style Python API, a local dashboard, and no server to run."
)

# name -> hyperparameters that shape the curves and show up as the run config.
# "parent" is the run this one was derived from (the lineage graph's edges):
# baseline -> lr-3e-4, baseline -> cosine-warmup -> mixup and wd-5e-4.
RUNS = [
    {
        "name": "baseline", "parent": None, "seed": 11, "lr": 1e-3, "schedule": "constant", "augment": "flip",
        "mixup": 0.0, "weight_decay": 0.0, "tau": 55, "floor": 0.62, "gap": 0.16,
        "tags": ["baseline"], "notes": "Plain AdamW baseline, constant learning rate, no schedule.",
    },
    {
        "name": "lr-3e-4", "parent": "baseline", "seed": 23, "lr": 3e-4, "schedule": "constant", "augment": "flip",
        "mixup": 0.0, "weight_decay": 1e-4, "tau": 70, "floor": 0.50, "gap": 0.08,
        "tags": ["lr-sweep"], "notes": "Lower learning rate, otherwise the baseline.",
    },
    {
        "name": "cosine-warmup", "parent": "baseline", "seed": 37, "lr": 1e-3, "schedule": "cosine", "augment": "flip",
        "mixup": 0.0, "weight_decay": 1e-4, "tau": 62, "floor": 0.36, "gap": 0.05,
        "tags": ["schedule"], "notes": "5% warmup, cosine decay to zero.",
    },
    {
        "name": "mixup", "parent": "cosine-warmup", "seed": 41, "lr": 1.5e-3, "schedule": "cosine", "augment": "flip+mixup",
        "mixup": 0.2, "weight_decay": 1e-4, "tau": 66, "floor": 0.42, "gap": -0.04,
        "tags": ["schedule", "augmentation"], "notes": "Cosine-warmup plus mixup (alpha 0.2).",
    },
    {
        "name": "wd-5e-4", "parent": "cosine-warmup", "seed": 53, "lr": 7e-4, "schedule": "cosine", "augment": "flip",
        "mixup": 0.0, "weight_decay": 5e-4, "tau": 60, "floor": 0.40, "gap": 0.03,
        "tags": ["regularization"], "notes": "Cosine-warmup with stronger weight decay.",
    },
]

# The lineage draft's two extra runs: a third generation (the new best) and a
# branch that diverged and crashed at step ``crash_at``. Parents come first.
LINEAGE_RUNS = RUNS + [
    {
        "name": "mixup-long", "parent": "mixup", "seed": 61, "lr": 1.5e-3, "schedule": "cosine",
        "augment": "flip+mixup", "mixup": 0.2, "weight_decay": 1e-4, "epochs": 30, "tau": 58,
        "floor": 0.30, "gap": -0.05, "tags": ["schedule", "augmentation"],
        "notes": "Mixup, trained for 30 epochs instead of 20.",
    },
    {
        "name": "lr-1e-2", "parent": "baseline", "seed": 71, "lr": 1e-2, "schedule": "constant",
        "augment": "flip", "mixup": 0.0, "weight_decay": 0.0, "tau": 90, "floor": 0.9, "gap": 0.3,
        "crash_at": 60, "tags": ["lr-sweep"], "notes": "Ten times the baseline learning rate: diverged.",
    },
]


def lr_at(spec, step):
    base = spec["lr"]
    if spec["schedule"] == "constant":
        return base
    warmup = STEPS // 20
    if step < warmup:
        return base * (step + 1) / warmup
    progress = (step - warmup) / (STEPS - warmup)
    return base * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * progress)))


def curves(spec):
    """Deterministic, noisy-exponential training curves for one run."""
    rng = random.Random(spec["seed"])
    tau, floor, gap = spec["tau"], spec["floor"], spec["gap"]
    train_noise = 0.0
    val_noise = 0.0
    for step in range(STEPS):
        # AR(1) noise so curves wobble like minibatch loss, not white noise.
        train_noise = 0.6 * train_noise + rng.gauss(0, 0.035)
        val_noise = 0.5 * val_noise + rng.gauss(0, 0.02)
        decay = math.exp(-step / tau)
        late = max(0.0, (step - 0.6 * STEPS) / (0.4 * STEPS))
        train = floor - 0.25 * min(1.0, step / STEPS) + (2.35 - floor) * decay
        train = max(0.05, train * (1 + train_noise))
        val = floor + gap * late + 0.12 + (2.4 - floor - 0.12) * math.exp(-step / (tau * 0.9))
        val = max(0.1, val * (1 + val_noise))
        val_acc = min(0.97, max(0.0, 1.0 - 0.9 * val / 2.3 - 0.02 + rng.gauss(0, 0.004)))
        train_acc = min(0.995, max(0.0, 1.0 - 0.9 * train / 2.3 + 0.02 + rng.gauss(0, 0.006)))
        grad = (0.25 + 1.6 * math.exp(-step / (tau * 0.5))) * (1 + 0.25 * rng.gauss(0, 1))
        if spec["schedule"] == "cosine":
            grad *= 0.55 + 0.45 * lr_at(spec, step) / spec["lr"]
        yield step, {
            "train_loss": round(train, 5),
            "val_loss": round(val, 5),
            "train_acc": round(train_acc, 5),
            "val_acc": round(val_acc, 5),
            "lr": lr_at(spec, step),
            "grad_norm": round(max(0.02, grad), 5),
        }


def train_source(spec) -> str:
    """The run's ``train.py`` for the lineage draft's code snapshots: the
    schedule, mixup and epoch count are code changes; lr and weight decay are
    params only, so a run that changes just those keeps its parent's code."""
    schedule = (
        "    return None  # constant learning rate"
        if spec["schedule"] == "constant"
        else "    return warmup_cosine(optimizer, warmup=0.05, total=steps)"
    )
    batches = (
        f"    return mixup(loader, alpha={spec['mixup']})" if spec["mixup"] else "    return loader"
    )
    return "\n".join([
        '"""ResNet-18 on CIFAR-10."""',
        "from data import loaders, mixup",
        "from model import resnet18, warmup_cosine",
        "",
        f"EPOCHS = {spec.get('epochs', 20)}",
        "",
        "",
        "def schedule(optimizer, steps):",
        schedule,
        "",
        "",
        "def batches(loader):",
        batches,
        "",
    ])


def start_run(spec, root: Path, parent_id: str | None = None, snapshots: bool = False):
    return agenticml.Run(
        PROJECT,
        spec["name"],
        task=TASK,
        parent_run_id=parent_id,
        config={
            "model": "resnet18",
            "optimizer": "adamw",
            "lr": spec["lr"],
            "schedule": spec["schedule"],
            "weight_decay": spec["weight_decay"],
            "augment": spec["augment"],
            "mixup_alpha": spec["mixup"],
            "batch_size": 128,
            "epochs": spec.get("epochs", 20),
            # The lineage draft keeps one seed, so the config diffs show only
            # the changes that matter (the curves still use spec["seed"]).
            "seed": 42 if snapshots else spec["seed"],
        },
        tags=spec["tags"],
        notes=spec["notes"],
        dir=str(root),
        system_metrics=False,
        auto_track=False,
        track_source=False,
        capture_env=False,
        # The lineage draft snapshots the work directory's train.py.
        lineage=snapshots,
        # The background flusher is off the clock: frames flush by hand.
        flush_interval=3600,
    )


class LiveRun:
    """A run this process holds open: ``advance`` logs the next steps of its
    curves and writes them (and a heartbeat) to disk at once, so a frame sees
    exactly the steps logged so far. ``finish`` seals it like any other run."""

    def __init__(self, run, spec):
        self.run = run
        self.steps = curves(spec)
        self.val_loss: list[float] = []
        self.finished = False

    def advance(self, count: int) -> None:
        for step, metrics in itertools.islice(self.steps, count):
            self.run.log(metrics, step=step)
            self.val_loss.append(metrics["val_loss"])
        # _flush is private: there is no public "write now" and the
        # background flusher is off the clock (flush_interval is huge).
        self.run._flush()
        self.run.writer.heartbeat()

    def finish(self) -> None:
        self.advance(STEPS)  # whatever is left
        finish_run(self.run, self.val_loss)
        self.finished = True


def finish_run(run, val_loss, status: str = "finished") -> None:
    # The dashboard ranks a task by the first loss or accuracy key of the run
    # summaries, so keep val_loss alone: the lowest val_loss is the "best" run.
    # It is the mean of the last 10 steps, not the (noisy) final step.
    run.summary["val_loss"] = round(sum(val_loss[-10:]) / len(val_loss[-10:]), 5)
    run.finish(status=status)


def seed_store(root: Path, live: str | None = None, runs=RUNS, snapshots: bool = False):
    """Write the runs. The run named ``live`` is left open and unfinished,
    with no steps logged: the caller drives it through the returned
    ``LiveRun`` (None when ``live`` is not given). With ``snapshots``, each
    run snapshots a ``train.py`` written for it in the current directory."""
    ids = {}
    live_run = None
    for spec in runs:
        if snapshots:
            Path("train.py").write_text(train_source(spec))
        run = start_run(spec, root, ids.get(spec["parent"]), snapshots=snapshots)
        ids[spec["name"]] = run.run_id
        if spec["name"] == live:
            live_run = LiveRun(run, spec)
            continue
        crash_at = spec.get("crash_at")
        val_loss = []
        for step, metrics in curves(spec):
            if step == crash_at:
                break
            run.log(metrics, step=step)
            val_loss.append(metrics["val_loss"])
        finish_run(run, val_loss, "crashed" if crash_at else "finished")
    return ids, live_run


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def scroll_compare_to_charts(page) -> None:
    # The compare view is a full-window overlay with its own scroll: put the
    # overlaid curves at the top, as dashboard.png does.
    page.evaluate(
        "() => { const body = document.querySelector('.focus-body');"
        " const top = document.querySelector('.metrics-panel .toolbar') || document.querySelector('.chart-card');"
        " body.scrollTop += top.getBoundingClientRect().top - 100; }"
    )


def capture_dashboard(url: str, run_ids: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    query = urlencode([("selection", "1")] + [("runs", run_id) for run_id in run_ids])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=1, color_scheme="light"
        )
        # The dashboard reads this key on load; "light" ignores the OS setting.
        context.add_init_script("localStorage.setItem('agenticml-theme', 'light')")
        page = context.new_page()
        page.goto(f"{url}#/compare?{query}")
        page.wait_for_selector('.chart-card[data-key="train_loss"]')
        # Every chart canvas has drawn pixels, and nothing says "Loading".
        page.wait_for_function(
            "() => !document.body.innerText.includes('Loading')"
            " && document.querySelectorAll('.chart-card canvas').length >= 6"
            " && [...document.querySelectorAll('.chart-card canvas')].every(c => {"
            "const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
            "for (let i = 3; i < d.length; i += 4) if (d[i]) return true; return false;})"
        )
        scroll_compare_to_charts(page)
        # The compare overlay focuses its "Exit focus" button on open: drop the
        # focus ring and any hover state before the capture.
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.mouse.move(0, 0)
        page.wait_for_timeout(600)
        # Stop below the second row of chart cards (plus padding), so the
        # empty "Code & artifacts" strip stays out of the picture.
        bottom = page.evaluate(
            "() => Math.max(...[...document.querySelectorAll('.chart-card')].slice(0, 6)"
            ".map(c => c.getBoundingClientRect().bottom))"
        )
        height = min(900, math.ceil(bottom) + 24)
        page.screenshot(path=str(DASHBOARD_PNG), clip={"x": 0, "y": 0, "width": 1440, "height": height})
        browser.close()


def social_html() -> str:
    fonts = (
        "@font-face{font-family:'Instrument Sans';font-weight:400 700;"
        f"src:url({data_url(STATIC / 'InstrumentSans.woff2', 'font/woff2')}) format('woff2')}}"
        "@font-face{font-family:'JetBrains Mono';font-weight:400 700;"
        f"src:url({data_url(STATIC / 'JetBrainsMono.woff2', 'font/woff2')}) format('woff2')}}"
    )
    logo = data_url(LOGO, "image/svg+xml")
    shot = data_url(DASHBOARD_PNG, "image/png")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{fonts}
*{{box-sizing:border-box;margin:0}}
body{{width:1280px;height:640px;overflow:hidden;background:#f6f8fb;
  font-family:'Instrument Sans',sans-serif;color:#14213d;position:relative}}
.left{{position:absolute;left:64px;top:64px;width:520px}}
.logo{{height:52px;display:block}}
h1{{font-size:50px;line-height:1.08;margin-top:44px;letter-spacing:-0.02em;font-weight:700}}
p{{font-size:24px;line-height:1.4;margin-top:24px;color:#41506b}}
.install{{position:absolute;left:64px;bottom:64px;background:#14213d;color:#fff;
  font-family:'JetBrains Mono',monospace;font-size:26px;padding:16px 26px;border-radius:12px}}
.install b{{color:#7fb2ff;font-weight:400}}
.shot{{position:absolute;left:640px;top:96px;width:720px;height:480px;border-radius:14px;
  overflow:hidden;background:#fff;
  box-shadow:0 18px 50px rgba(20,33,61,.22),0 0 0 1px rgba(20,33,61,.10)}}
.shot img{{display:block;width:1080px;margin:0}}
</style></head><body>
<div class="left">
  <img class="logo" src="{logo}" alt="agenticML">
  <h1>Track experiments from files on disk</h1>
  <p>A W&amp;B-style Python API, a local dashboard, no server to run.</p>
</div>
<div class="install"><b>$</b> pip install agenticml-lite</div>
<div class="shot"><img src="{shot}" alt=""></div>
</body></html>"""


def capture_social() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
        page.set_content(social_html())
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.screenshot(path=str(SOCIAL_PNG))
        browser.close()


# --- demo GIF -------------------------------------------------------------

VIEW = {"width": 1280, "height": 800}
# Output width: the README shows the image at most ~900 px wide on GitHub and
# PyPI, so 960 keeps text sharp there and the file small. LANCZOS downscale.
GIF_WIDTH = 960
MOVE_MS = 90  # one frame of cursor motion (about 11 fps)
LIVE_FIRST = 24  # steps of the live run before the first frame
LIVE_BATCH = 14  # steps it gains between frames

# Headless screenshots show no pointer: draw one (and a ring while a button is
# down) that follows the real mouse events. Transitions and animations are off
# so a frame never catches one half-way.
PAGE_INIT = """
(() => {
  const css = document.createElement('style');
  css.textContent = '*,*::before,*::after{transition:none!important;animation:none!important;'
    + 'scroll-behavior:auto!important;caret-color:transparent!important}'
    + '#demo-cursor{position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;'
    + 'pointer-events:none;display:none}'
    + '#demo-cursor svg{position:absolute;left:0;top:0;width:22px;height:22px;'
    + 'filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}'
    + '#demo-cursor i{position:absolute;left:-15px;top:-15px;width:30px;height:30px;border-radius:50%;'
    + 'border:2px solid #2f6fed;background:rgba(47,111,237,.18);display:none}'
    + '#demo-cursor.down i{display:block}'
    + '#demo-caption{position:fixed;right:40px;bottom:22px;'
    + 'z-index:2147483646;pointer-events:none;display:none;white-space:nowrap;'
    + 'background:#14213d;color:#fff;font:600 17px/1.2 "Instrument Sans",sans-serif;'
    + 'padding:10px 20px;border-radius:999px;box-shadow:0 6px 20px rgba(20,33,61,.28)}';
  const cursor = document.createElement('div');
  cursor.id = 'demo-cursor';
  cursor.innerHTML = '<i></i><svg viewBox="0 0 22 22"><path d="M2 1.5v16l4.4-4 3 6.5 2.8-1.3-3-6.4 6-.4z" '
    + 'fill="#fff" stroke="#14213d" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const caption = document.createElement('div');
  caption.id = 'demo-caption';
  const mount = () => { document.head.append(css); document.body.append(cursor, caption); };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', mount) : mount();
  const put = (e) => { cursor.style.display = 'block';
    cursor.style.transform = `translate(${e.clientX}px,${e.clientY}px)`; };
  addEventListener('mousemove', put, true);
  addEventListener('mousedown', (e) => { put(e); cursor.classList.add('down'); }, true);
  addEventListener('mouseup', () => cursor.classList.remove('down'), true);
})();
"""

CANVASES_DRAWN = (
    "() => !document.body.innerText.includes('Loading')"
    " && [...document.querySelectorAll('canvas')].every(c => {"
    "const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
    "for (let i = 3; i < d.length; i += 4) if (d[i]) return true; return false;})"
)


class Reel:
    """Records the page, one screenshot per frame, with the fake cursor."""

    def __init__(self, page):
        self.page = page
        self.frames: list[list] = []  # [png bytes, duration ms]
        self.at = (0.0, 0.0)

    def settle(self, canvases: bool = False) -> None:
        # The fake clock never runs on its own: let timers and animation
        # frames queued by the last action fire before the screenshot.
        self.page.clock.run_for(60)
        if canvases:
            self.page.wait_for_function(CANVASES_DRAWN)

    def shot(self, ms: int, tick: bool = True, canvases: bool = False) -> None:
        if tick:
            self.settle(canvases)
        png = self.page.screenshot()
        if self.frames and self.frames[-1][0] == png:
            self.frames[-1][1] += ms
        else:
            self.frames.append([png, ms])

    def put(self, x: float, y: float) -> None:
        self.page.mouse.move(x, y)
        self.at = (x, y)

    def glide(self, x: float, y: float, steps: int = 8, **kw) -> None:
        x0, y0 = self.at
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in and out
            self.put(x0 + (x - x0) * t, y0 + (y - y0) * t)
            self.shot(MOVE_MS, **kw)

    def point(self, selector: str, fx: float = 0.4, fy: float = 0.4):
        box = self.page.locator(selector).first.bounding_box()
        return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy

    def click(self, ms: int = 160, **kw) -> None:
        self.page.mouse.down()
        self.shot(ms, **kw)
        self.page.mouse.up()

    def hold(self, ms: int, **kw) -> None:
        self.shot(ms, **kw)

    def caption(self, text: str | None) -> None:
        """Show ``text`` in the caption pill (None hides it) from the next frame."""
        self.page.evaluate(
            "text => { const c = document.getElementById('demo-caption');"
            " c.textContent = text || ''; c.style.display = text ? 'block' : 'none'; }",
            text,
        )


def scene_live(reel: Reel, url: str, ids, live: LiveRun, batch: int = LIVE_BATCH,
               caption: str | None = None) -> None:
    """1. The live run's page: its curves grow while it is `running`."""
    page = reel.page
    live.advance(LIVE_FIRST)
    page.goto(f"{url}#/run/{ids['wd-5e-4']}")
    page.wait_for_selector(".chart-card canvas")
    # Scroll past the header and facts (host, commit, start time) to the
    # charts, whose "@ step N" labels show the run advancing. The toolbar
    # is sticky, so its "Clip outliers" label (rendered without spaces, see
    # the Backlog) stays in view.
    page.evaluate("() => window.scrollTo(0, document.querySelector('.card.toolbar').getBoundingClientRect().top"
                  " + scrollY - 72)")
    if caption:
        reel.caption(caption)
    reel.shot(400, canvases=True)
    while len(live.val_loss) + batch < STEPS:
        live.advance(batch)
        # One poll of the page's 5 s refresh timer, then put Date back to the
        # real time so the run's "wall time" stays honest.
        page.clock.run_for(5000)
        page.clock.set_system_time(int(time.time() * 1000))
        page.wait_for_function(
            f"() => document.body.innerText.includes('@ step {len(live.val_loss) - 1}')"
        )
        reel.shot(260, tick=False, canvases=True)
    live.finish()
    page.clock.run_for(5000)
    page.clock.set_system_time(int(time.time() * 1000))
    page.wait_for_function(f"() => document.body.innerText.includes('@ step {STEPS - 1}')")
    reel.shot(400, tick=False, canvases=True)


def scene_lineage(reel: Reel) -> None:
    """2. Run list, the task's Lineage view, the mixup run in its inspector."""
    page = reel.page
    page.evaluate("() => { location.hash = '#/'; }")
    page.wait_for_selector(".run-row")
    page.wait_for_function(
        "() => document.querySelector('.task-strip') && !document.body.innerText.includes('Loading')"
    )
    page.evaluate("() => scrollTo(0, 0)")
    reel.put(640, 300)
    reel.shot(300)
    reel.glide(*reel.point("text=Lineage ⤢", 0.92, 0.85), steps=7)
    reel.click(160)
    page.wait_for_selector(".lv-node")
    reel.hold(900)
    # The view opens on the best run (mixup). Click its parent first so the
    # click on mixup visibly changes the inspector.
    for name, wait in (("cosine-warmup", 500), ("mixup", 700)):
        reel.glide(*reel.point(f'.lv-node:has-text("{name}") .lv-box', 0.5, 0.6), steps=5)
        reel.click(150)
        reel.hold(wait)
    reel.glide(*reel.point('button[aria-label="Close lineage"]', 0.35, 0.35), steps=6)
    reel.click(120)
    page.wait_for_selector(".lv-dialog", state="detached")
    page.evaluate("document.activeElement && document.activeElement.blur()")
    reel.hold(300)


def scene_compare(reel: Reel, ids) -> None:
    """3. Tick the runs, click Compare runs."""
    page = reel.page
    for name in reversed([spec["name"] for spec in RUNS]):  # top row first
        reel.glide(*reel.point(f'.run-row[data-run-id="{ids[name]}"] .check', 0.5, 0.5), steps=4)
        reel.click(130)
    reel.glide(*reel.point('button:has-text("Compare runs")', 0.3, 0.3), steps=7)
    reel.click(150)
    page.wait_for_selector('.chart-card[data-key="train_loss"]')
    scroll_compare_to_charts(page)
    page.evaluate("document.activeElement && document.activeElement.blur()")
    reel.hold(400, canvases=True)


def scene_explore(reel: Reel) -> None:
    """4. Open the train_loss chart in the metrics workspace (the compare
    view's charts are compact and have no hover readout), sweep its hover
    readout across the curves, then drag Smoothing down to 0 and up to 0.9."""
    page = reel.page
    reel.glide(*reel.point('.chart-card[data-key="train_loss"]', 0.5, 0.55), steps=7)
    reel.click(150)
    page.wait_for_selector('.ws-card[data-key="train_loss"] canvas')
    page.evaluate("document.activeElement && document.activeElement.blur()")
    # Put the enlarged chart at the top of the workspace.
    page.evaluate("() => document.querySelector('.ws-card[data-key=\"train_loss\"]').scrollIntoView({block: 'start'})")
    reel.hold(350, canvases=True)
    box = page.locator('.ws-card[data-key="train_loss"] .ws-chart canvas').first.bounding_box()
    y = box["y"] + box["height"] * 0.5
    reel.glide(box["x"] + box["width"] * 0.15, y, steps=6)
    for fx in (0.22, 0.34, 0.46, 0.58, 0.7, 0.82):
        reel.put(box["x"] + box["width"] * fx, y)
        reel.shot(MOVE_MS * 2)
    reel.hold(200)
    slider = page.locator('input[aria-label="Smoothing"]').first.bounding_box()
    thumb = 8  # half a thumb: a range input's value spans width - thumb

    def at(value):
        return slider["x"] + thumb + (slider["width"] - 2 * thumb) * value / 0.99

    cy = slider["y"] + slider["height"] / 2
    reel.glide(at(0.6), cy, steps=6)
    page.mouse.down()
    reel.shot(100)
    for i in range(1, 6):
        reel.put(at(0.6 * (1 - i / 5)), cy)
        reel.shot(MOVE_MS, canvases=True)
    reel.hold(300)
    for i in range(1, 8):
        reel.put(at(0.9 * i / 7), cy)
        reel.shot(MOVE_MS, canvases=True)
    page.mouse.up()
    reel.hold(800, canvases=True)


def scene_lineage_story(reel: Reel, ids) -> None:
    """Lineage draft, 2. The task's Lineage view: the tree, the best path
    switched off and on, a walk down the path with the inspector showing what
    changed at each generation, then the crashed branch."""
    page = reel.page
    node = lambda name: f'.lv-node[data-run-id="{ids[name]}"] .lv-box'  # noqa: E731
    page.evaluate("() => { location.hash = '#/'; }")
    page.wait_for_selector(".run-row")
    page.wait_for_function(
        "() => document.querySelector('.task-strip') && !document.body.innerText.includes('Loading')"
    )
    page.evaluate("() => scrollTo(0, 0)")
    reel.caption("Every run records the run it came from")
    reel.put(640, 300)
    reel.shot(500)
    reel.glide(*reel.point("text=Lineage ⤢", 0.92, 0.85), steps=7)
    reel.click(160)
    page.wait_for_selector(".lv-node")
    # The view opens scrolled to the best run (gen 3): fit the whole tree,
    # root included, in the canvas.
    # A DOM click, so the real mouse (and the drawn cursor) stays put.
    page.locator('.lv-zoom button:has-text("Fit")').dispatch_event("click")
    page.evaluate("document.activeElement && document.activeElement.blur()")
    reel.hold(1600)
    # "Highlight best path" off, then on: the root-to-best chain lights up.
    reel.caption("The best path: from the root run to the best one")
    reel.glide(*reel.point("label.lv-best", 0.08, 0.5), steps=6)
    reel.click(140)
    reel.hold(700)
    reel.click(140)
    reel.hold(1300)
    # Walk the path: the inspector shows the params and code each step changed
    # and what that did to val_loss.
    reel.caption("What each step changed, and what it did to val_loss")
    for name in ("baseline", "cosine-warmup", "mixup", "mixup-long"):
        reel.glide(*reel.point(node(name), 0.5, 0.6), steps=5)
        reel.click(150)
        # The inspector shows this run, and a changed tree's file list (it
        # reads "code changed" until the fetch lands) is in.
        page.wait_for_function(
            "name => document.querySelector('.lv-insp-name')?.textContent === name"
            " && !document.querySelector('.lv-inspector').innerText.includes('code changed')",
            arg=name,
        )
        reel.hold(1500)
    reel.caption("Dead ends stay in the record")
    reel.glide(*reel.point(node("lr-1e-2"), 0.5, 0.6), steps=6)
    reel.click(150)
    reel.hold(1400)


def scene_compare_path(reel: Reel) -> None:
    """Lineage draft, 3. Quick-select the best path, compare it."""
    page = reel.page
    reel.caption("Compare the best path")
    reel.glide(*reel.point('[data-focus="lv-pick-path"]', 0.5, 0.5), steps=7)
    reel.click(150)
    # The tray grows chips and a Compare button, so the buttons shift under
    # the cursor: keep this hold short.
    reel.hold(300)
    reel.glide(*reel.point('[data-focus="lv-compare-tray"]', 0.4, 0.5), steps=6)
    reel.click(150)
    page.wait_for_selector('.chart-card[data-key="train_loss"]')
    scroll_compare_to_charts(page)
    page.evaluate("document.activeElement && document.activeElement.blur()")
    reel.put(1240, 700)  # out of the way, and clear of the caption
    reel.hold(2400, canvases=True)


def encode_gif(frames, path: Path) -> None:
    from PIL import Image

    width = GIF_WIDTH
    height = round(VIEW["height"] * width / VIEW["width"])
    images = [
        Image.open(io.BytesIO(png)).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        for png, _ in frames
    ]
    # One palette for every frame, built from a sample of them, so the flat
    # UI colours don't flicker from frame to frame.
    sample = images[:: max(1, len(images) // 24)]
    strip = Image.new("RGB", (width, height * len(sample)))
    for i, image in enumerate(sample):
        strip.paste(image, (0, i * height))
    palette = strip.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    paletted = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    paletted[0].save(
        path,
        save_all=True,
        append_images=paletted[1:],
        duration=[ms for _, ms in frames],
        loop=0,
        optimize=True,
        disposal=1,
    )


def capture_demo(url: str, ids, live: LiveRun, story: str = "demo") -> None:
    """Record ``demo.gif`` (story "demo") or ``demo-lineage.gif`` ("lineage")."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport=VIEW, device_scale_factor=1, color_scheme="light", timezone_id="UTC"
        )
        context.add_init_script("localStorage.setItem('agenticml-theme', 'light')")
        context.add_init_script(PAGE_INIT)
        page = context.new_page()
        # Polling runs on a fake clock: the page only refreshes when we say.
        page.clock.install(time=int(time.time() * 1000))
        reel = Reel(page)
        if story == "lineage":
            path = LINEAGE_GIF
            scene_live(reel, url, ids, live, batch=44, caption="A run is training")
            scene_lineage_story(reel, ids)
            scene_compare_path(reel)
        else:
            path = DEMO_GIF
            scene_live(reel, url, ids, live)
            scene_lineage(reel)
            scene_compare(reel, ids)
            scene_explore(reel)
        browser.close()
    encode_gif(reel.frames, path)
    total = sum(ms for _, ms in reel.frames)
    print(f"{path.name}: {len(reel.frames)} frames, {total / 1000:.1f} s, "
          f"{path.stat().st_size / 1e6:.2f} MB", file=sys.stderr)


@contextlib.contextmanager
def seeding_env(work: Path):
    """Run from an empty directory (no git repo, so no commit or branch lands
    in the runs) and under a neutral host name."""
    old = Path.cwd()
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)
    try:
        with mock.patch("socket.gethostname", return_value="gpu-box"):
            yield
    finally:
        os.chdir(old)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=["all", "gif", "png", "lineage"], default="all")
    only = parser.parse_args().only
    ASSETS.mkdir(parents=True, exist_ok=True)
    lineage = only == "lineage"
    with tempfile.TemporaryDirectory(prefix="agenticml-readme-") as tmp:
        root = Path(tmp) / "store"
        with seeding_env(Path(tmp) / "work"):
            ids, live = seed_store(
                root,
                live="wd-5e-4" if only != "png" else None,
                runs=LINEAGE_RUNS if lineage else RUNS,
                snapshots=lineage,
            )
        # A fixed port, so the header reads the same in every run. If it is
        # taken, any free port will do (the header then shows that one).
        try:
            server = make_server(root, host="127.0.0.1", port=DEMO_PORT, read_only=True)
        except OSError:
            server = make_server(root, host="127.0.0.1", port=0, read_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            if live is not None:
                # Leaves every run, the live one included, finished.
                capture_demo(url, ids, live, story="lineage" if lineage else "demo")
            if only in ("all", "png"):
                capture_dashboard(url, [ids[spec["name"]] for spec in RUNS])
        finally:
            if live is not None and not live.finished:
                # A failed capture: seal the open run before the store goes,
                # or its exit handler prints errors on top of the real one.
                with contextlib.suppress(Exception):
                    live.finish()
            server.shutdown()
            server.server_close()
    if only in ("all", "png"):
        capture_social()
        print(DASHBOARD_PNG)
        print(SOCIAL_PNG)
    if only in ("all", "gif"):
        print(DEMO_GIF)
    if lineage:
        print(LINEAGE_GIF)


if __name__ == "__main__":
    main()
