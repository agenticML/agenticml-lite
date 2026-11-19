#!/usr/bin/env python
"""Regenerate the README images.

Seeds a temporary store with a few deterministic training runs, serves it with
the dashboard, and writes:

- docs/assets/dashboard.png (1440 wide, light theme, runs overlaid in compare view,
  cropped below the chart cards)
- docs/assets/social-preview.png (1280x640, for GitHub's social preview)

Run from the repo root with the testbed venv (it has Playwright):

    testbed/.venv/bin/python scripts/make_readme_images.py

No network access: fonts and the logo come from files in this repo. Not part
of the test suite.
"""

from __future__ import annotations

import base64
import math
import random
import sys
import tempfile
import threading
from pathlib import Path
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

PROJECT = "cifar-resnet"
STEPS = 200
PITCH = (
    "Experiment tracking for machine learning that runs from files on disk. "
    "A W&B-style Python API, a local dashboard, and no server to run."
)

# name -> hyperparameters that shape the curves and show up as the run config.
RUNS = [
    {
        "name": "baseline", "seed": 11, "lr": 1e-3, "schedule": "constant", "augment": "flip",
        "mixup": 0.0, "weight_decay": 0.0, "tau": 55, "floor": 0.62, "gap": 0.16,
        "tags": ["baseline"], "notes": "Plain AdamW baseline, constant learning rate, no schedule.",
    },
    {
        "name": "lr-3e-4", "seed": 23, "lr": 3e-4, "schedule": "constant", "augment": "flip",
        "mixup": 0.0, "weight_decay": 1e-4, "tau": 70, "floor": 0.50, "gap": 0.08,
        "tags": ["lr-sweep"], "notes": "Lower learning rate, otherwise the baseline.",
    },
    {
        "name": "cosine-warmup", "seed": 37, "lr": 1e-3, "schedule": "cosine", "augment": "flip",
        "mixup": 0.0, "weight_decay": 1e-4, "tau": 62, "floor": 0.36, "gap": 0.05,
        "tags": ["schedule"], "notes": "5% warmup, cosine decay to zero.",
    },
    {
        "name": "mixup", "seed": 41, "lr": 1.5e-3, "schedule": "cosine", "augment": "flip+mixup",
        "mixup": 0.2, "weight_decay": 1e-4, "tau": 66, "floor": 0.42, "gap": -0.04,
        "tags": ["schedule", "augmentation"], "notes": "Cosine-warmup plus mixup (alpha 0.2).",
    },
    {
        "name": "wd-5e-4", "seed": 53, "lr": 7e-4, "schedule": "cosine", "augment": "flip",
        "mixup": 0.0, "weight_decay": 5e-4, "tau": 60, "floor": 0.40, "gap": 0.03,
        "tags": ["regularization"], "notes": "Cosine-warmup with stronger weight decay.",
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


def seed_store(root: Path) -> dict[str, str]:
    ids = {}
    for spec in RUNS:
        run = agenticml.Run(
            PROJECT,
            spec["name"],
            config={
                "model": "resnet18",
                "optimizer": "adamw",
                "lr": spec["lr"],
                "schedule": spec["schedule"],
                "weight_decay": spec["weight_decay"],
                "augment": spec["augment"],
                "mixup_alpha": spec["mixup"],
                "batch_size": 128,
                "epochs": 20,
                "seed": spec["seed"],
            },
            tags=spec["tags"],
            notes=spec["notes"],
            dir=str(root),
            system_metrics=False,
            auto_track=False,
            track_source=False,
            capture_env=False,
            lineage=False,
        )
        last = {}
        for step, metrics in curves(spec):
            run.log(metrics, step=step)
            last = metrics
        for key in ("train_loss", "val_loss", "val_acc"):
            run.summary[key] = last[key]
        run.finish()
        ids[spec["name"]] = run.run_id
    return ids


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


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
        # The compare view is a full-window overlay with its own scroll: put the
        # overlaid curves at the top so the charts fill the screenshot.
        page.evaluate(
            "() => { const body = document.querySelector('.focus-body');"
            " const top = document.querySelector('.metrics-panel .toolbar') || document.querySelector('.chart-card');"
            " body.scrollTop += top.getBoundingClientRect().top - 100; }"
        )
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


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agenticml-readme-") as tmp:
        root = Path(tmp)
        ids = seed_store(root)
        server = make_server(root, host="127.0.0.1", port=0, read_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            capture_dashboard(url, [ids[spec["name"]] for spec in RUNS])
        finally:
            server.shutdown()
            server.server_close()
    capture_social()
    print(DASHBOARD_PNG)
    print(SOCIAL_PNG)


if __name__ == "__main__":
    main()
