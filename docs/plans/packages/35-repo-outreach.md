# Package 35: repo outreach (README, community files, dashboard link)

Status: in review (plan Nov 19, 2026; built and reviewed the same day). Background and numbers:
[outreach plan](../outreach.md).

## Goal

Make the GitHub repo turn visitors into users and stars, and point the
PyPI users who never visit GitHub at the repo. Nothing here changes
tracking, sync or server behaviour.

Two parts:

- **35.1** README rework, comparison table, dashboard screenshot and
  social preview image (with the script that makes them), community files,
  PyPI metadata. Docs and assets, plus one small script.
- **35.2** A GitHub link in the dashboard top bar and one line from
  `agenticml ui` at startup.

Out of scope (owner steps, see "Owner steps" at the end): repo topics,
uploading the social preview, turning on Discussions, labels, and every
launch post.

## Constraints and conventions

- Plain, factual wording, in the style of the current docs. No hype words
  ("blazing", "revolutionary", emoji walls). Short sentences.
- Never drop a fact from the README without making sure it is on the docs
  site (`website/src/content/docs/`), in `docs/*.md`, or in
  `CONTRIBUTING.md`. If a removed section's facts are missing there, add
  them to the right docs page in this package.
- `tests/test_website_facts.py` reads `README.md`; keep it passing (read
  what it checks before editing).
- Images in the README use absolute URLs
  (`https://raw.githubusercontent.com/agenticML/agenticml-lite/main/docs/assets/<file>`)
  so they also render on PyPI. They show up once the files reach `main`.
- Dashboard changes: keep zero horizontal page overflow at 375px; run the
  UI tests.
- No new runtime dependencies. The image script may use Playwright, which
  the testbed venv already has.

## 35.1 README, images, community files, metadata

### Files

- `README.md` (rewrite the top, cut reference sections)
- `docs/assets/dashboard.png`, `docs/assets/social-preview.png` (new)
- `scripts/make_readme_images.py` (new)
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (new)
- `.github/ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml`,
  `config.yml`, `.github/pull_request_template.md` (new)
- `pyproject.toml` (keywords, classifiers)
- docs site pages, only if a fact removed from the README is missing there

### README layout, in this order

1. `# agenticml-lite`, then one line: "Experiment tracking for machine
   learning that runs from files on disk. A W&B-style Python API, a local
   dashboard, and no server to run."
2. Badges on one line: PyPI version, Python versions (from PyPI), license
   (MIT), the `Tests` workflow status, docs link. shields.io and the GitHub
   workflow badge only.
3. The dashboard screenshot (`docs/assets/dashboard.png`), alt text
   describing it.
4. A short paragraph (2–3 sentences): runs are files, `agenticml ui`
   shows them, sync to your own server or the hosted service preview when
   you want to share.
5. `## Quickstart`: `pip install agenticml-lite`, the existing Python
   quickstart (trim to init / log / finish), then
   `agenticml run train.py` and `agenticml ui` with one line each.
6. `## Features`: 6–8 bullets, each one fact we can back up from the code
   or docs: files on disk; zero-code `agenticml run`; runtime code
   snapshots and code diffs between runs; PyTorch component detection;
   TensorBoard mirroring; system and GPU metrics; media and artifacts;
   self-hosted server with workspaces, roles and audit log; hosted
   service preview at app.agenticml.xyz; about 3 µs per `log()` call
   (from the docs site's current figure).
7. `## How it compares`: the table below, verbatim, with the note under it.
8. `## Share runs with a team`: 4–6 lines on `agenticml server serve`,
   `agenticml login` / `agenticml sync`, and the hosted service, each
   linking to its docs page.
9. `## Documentation`: a link list to the docs site sections (quickstart,
   zero-code, Python SDK, dashboard, distributed, storage, configuration,
   server install / keys / sync / backups / upgrade, hosted, releases),
   plus `docs/SDK.md`, `docs/FORMAT.md`, `docs/PROTOCOL.md`,
   `docs/SERVER.md`, `docs/THREAT_MODEL.md`.
10. `## Contributing`: one line pointing at `CONTRIBUTING.md`, and "If
    agenticml-lite is useful to you, a star on GitHub helps others find
    it."
11. `## License`: MIT.

Everything else in today's README (command forms table, viewing, editing
ended runs, where data lives, disk, upload, on-prem server, configuration,
performance, distributed/resume/disabled, development, releasing) moves
out: user facts to the docs site or `docs/*.md` (check each one is there),
development and releasing to `CONTRIBUTING.md`. Keep the performance
table in the docs site (reference/storage or a new reference/performance
page) if it is not there yet.

### Comparison table (use verbatim)

| | agenticml-lite | Weights & Biases | MLflow | TensorBoard | Aim | Trackio |
| --- | --- | --- | --- | --- | --- | --- |
| License | MIT (client and server) | Client MIT, server commercial | Apache-2.0 | Apache-2.0 | Apache-2.0 | MIT |
| Local dashboard, no account or server setup | Yes | No | Yes | Yes | Yes | Yes |
| Self-hosted team server | Yes, open source, with workspaces, roles and audit log | Commercial license | Yes | No | Yes | Yes |
| Hosted option | Yes (public preview) | Yes | Through managed providers | No | – | Hugging Face Spaces |
| Track a script without editing it | `agenticml run train.py` | – | – | – | – | – |

Note under the table: "Checked November 2026. If something here is wrong
or out of date, please open an issue." The last row uses "–" for the
others on purpose: we make no claim about them.

### Images (`scripts/make_readme_images.py`)

- Seeds a temporary store (`AGENTICML_DIR` or the store-root option the
  SDK uses; read `store/layout.py`) with 4–6 realistic runs of one
  project, e.g. `cifar-resnet` with names like `baseline`, `lr-3e-4`,
  `cosine-warmup`, `mixup`: smooth, plausible `train/loss`, `val/loss`,
  `val/accuracy`, `lr` curves over ~200 steps (deterministic: fixed
  seeds), configs, tags, and a summary. Use the public SDK only.
- Starts the dashboard in-process with `agenticml.ui.server.make_server`
  on port 0 in a thread, opens it in Playwright Chromium at 1440×900,
  light theme, selects the runs so the charts view shows several curves,
  and saves `docs/assets/dashboard.png`. Keep it under ~400 KB
  (`device_scale_factor` 1, PNG).
- Renders `docs/assets/social-preview.png` at 1280×640 from an inline HTML
  string: name, the one-line pitch, `pip install agenticml-lite`, and a
  cropped copy of the dashboard screenshot; uses the logo from
  `website/src/assets/logo-light.svg` and the dashboard's fonts from
  `src/agenticml/ui/static/`. Under 1 MB (GitHub's limit).
- Prints both paths. Run with
  `testbed/.venv/bin/python scripts/make_readme_images.py`. Not part of
  the test suite; no network access.
- Look at both images yourself (open the PNGs) before handing back: no
  empty charts, no "Loading", no overflow.

### Community files

- `CONTRIBUTING.md`: dev setup (from today's README "Development" and
  `testbed/README.md`), the two test commands from `AGENTS.md`, the 375px
  rule for dashboard changes, branch flow (PRs to `dev`; `main` gets
  releases), releasing (from today's README "Releasing"), and "Security
  issues: see SECURITY.md".
- `CODE_OF_CONDUCT.md`: Contributor Covenant 2.1, contact
  `gaurav.singh@agenticml.xyz` (the author address already in
  `pyproject.toml`).
- Issue forms: bug report (version, OS, Python, local / self-hosted /
  hosted, steps, expected, actual, logs), feature request (problem,
  proposal, alternatives). `config.yml`: blank issues on, a contact link
  to the docs site. PR template: summary, tests run, dashboard 375px
  checked (if UI).

### PyPI metadata (`pyproject.toml`)

- `keywords`: `experiment-tracking`, `mlops`, `machine-learning`,
  `deep-learning`, `pytorch`, `tensorboard`, `wandb-alternative`,
  `self-hosted`, `local-first` (drop `agentic`).
- `Development Status :: 4 - Beta` (3.0 has a server and a hosted
  service; "2 - Pre-Alpha" undersells it).
- Add `Topic :: Scientific/Engineering :: Artificial Intelligence` and
  `Python :: 3.13` only if CI runs 3.13 (check `.github/workflows/test.yml`;
  otherwise leave it out).

### Done when (35.1)

- README follows the layout; every removed fact is findable on the docs
  site, in `docs/*.md` or in `CONTRIBUTING.md` (list where each went in
  the hand-back).
- Both images exist, within size limits, and look right.
- `testbed/.venv/bin/python -m pytest -q tests` passes.
- The docs site still builds (`npm run build` in `website/`, if
  `node_modules` can be installed; otherwise say so) when docs pages were
  touched.

## 35.2 Dashboard link and `agenticml ui` line

### Files

- `src/agenticml/ui/static/index.html`, `app.css` (and `app.js` only if
  needed)
- `src/agenticml/ui/server.py` (`serve`)
- tests: `tests/` for the startup line, `testbed/tests/test_dashboard.py`
  for the link

### Approach

1. Top bar: before the Theme button, a ghost link
   `<a id="nav-github" class="btn btn-ghost btn-sm" href="https://github.com/agenticML/agenticml-lite" target="_blank" rel="noopener">`
   with an inline GitHub mark SVG (`aria-hidden`) and the text "GitHub"
   and `aria-label="agenticml-lite on GitHub"`. Same dashboard is served
   by the self-hosted and hosted server; showing it there is fine (the
   project is open source).
2. At narrow widths, hide the text and keep the icon so the top bar does
   not wrap or overflow at 375px (check what the top bar already does at
   that width first).
3. `serve()`: right after the existing `AgenticML UI: {url}` line, print
   one line: `Docs: https://agenticml.github.io/agenticml-lite/ · Star us
   on GitHub: https://github.com/agenticML/agenticml-lite`. Keep the
   `AgenticML UI:` line unchanged (`testbed/tests/conftest.py` parses it).
   Only `agenticml ui`; not the server commands.

### Edge cases

- Dark theme: the icon uses `currentColor`.
- No network: it is a plain link; nothing is fetched.
- Read-only mode: no difference.

### Tests and checks

- A unit test that `serve` prints the docs/GitHub line after the URL line
  (follow how existing tests start the UI server, or test the print in
  isolation).
- A UI test: `#nav-github` is visible with the right href, `target`,
  `rel`; at 375px the page has no horizontal overflow (reuse the existing
  overflow helper).
- `testbed/.venv/bin/python -m pytest -q tests` and, from `testbed/`,
  `.venv/bin/python -m pytest -q -m ui`.

### Done when (35.2)

Link and line present, tests added and passing, 375px overflow still zero.

## Branch and release

One PR to `dev` with both parts. The README on GitHub's front page changes
only when this reaches `main`; after review, a docs-only PR to `main`
with 35.1's files (like 29.6) is the quickest way to make it visible.
Merging to `main` is the owner's step.

## Owner steps (not code)

- Repo topics: `experiment-tracking`, `mlops`, `machine-learning`,
  `deep-learning`, `pytorch`, `tensorboard`, `wandb-alternative`,
  `self-hosted`, `local-first`, `python`.
- Upload `docs/assets/social-preview.png` in Settings → General → Social
  preview (GitHub has no API for it).
- Turn on Discussions; add a few `good first issue` issues.
- Launch steps in the [outreach plan](../outreach.md), after the README is
  on `main`.
