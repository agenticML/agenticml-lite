# Package 35: repo outreach (README, community files, dashboard link)

Status: 35.1 and 35.2 done (#90, released in 3.3.0); 35.3 in review (Nov 20, 2026). Background and numbers:
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

## 35.3 README demo GIF

Added Nov 20, 2026 (owner: "i wanna update the image on the github
readme, we should update it for a gif, or a small video"). Status: in
review.

### Decision: an animated GIF, not a video

- A GIF in Markdown (`![alt](raw URL)`) autoplays and loops on GitHub
  **and** on PyPI, which renders the same README. Most visitors arrive from
  PyPI (about 300 downloads a month against 8 GitHub visitors in 14 days).
- An MP4 or WebM stored in the repo does not play on either: GitHub plays
  video only from `github.com/user-attachments` uploads (made by hand in the
  web editor, outside the repo, so the script can't regenerate it), and PyPI
  strips `<video>`.
- No ffmpeg on the machine. Playwright's bundled ffmpeg only writes VP8
  WebM. So the script takes Playwright screenshots frame by frame and puts
  them together with Pillow, which the testbed venv already has
  (`testbed[test]` pins `Pillow>=10` and `playwright==1.62.0`). No new
  dependency anywhere.

### Files

- `scripts/make_readme_images.py`: add the GIF capture; keep making
  `dashboard.png` and `social-preview.png` (the social preview embeds
  `dashboard.png`).
- `docs/assets/demo.gif` (new, generated).
- `README.md`: line 7, the image becomes the GIF. Keep the absolute
  `https://raw.githubusercontent.com/agenticML/agenticml-lite/main/docs/assets/demo.gif`
  URL (needed for PyPI; `tests/test_website_facts.py` exempts
  `docs/assets/`). New alt text describing what the GIF shows.
- `docs/plans/on-prem-roadmap.md`: row 35.3 status.
- `docs/assets/dashboard.png` and `social-preview.png` are regenerated by
  the same run. Commit them only if they changed in a way a person would
  notice. If nothing about them changed, `git checkout` them so the diff
  carries no noise.

### Storyboard (about 17–19 s, loops)

Same seeded project as the screenshot (`cifar-resnet`, the five runs in
`RUNS`), light theme. The viewport is 1280×800 at `device_scale_factor` 1.
Read `src/agenticml/ui/static/app.js` (`home`, `runPage`, `comparePage`,
the runs-page poll at "Live · refreshes every 5 s") and adapt the selectors
and steps to what the UI really does. Report any scene you had to change.

1. **Live training (about 4.5 s).** The page opens on the run list of
   `cifar-resnet`. Four runs are finished. The fifth (`wd-5e-4`) is still
   `running` and gains about 20 steps between frames, so its curves visibly
   grow while the page shows it as live. If the run list has no charts, open
   that run's page (`#/run/<id>`) for this scene, so the growing curves are
   the subject. Make the poll deterministic: call `page.clock.install()`
   before the first `goto`, then after each batch of logged steps call
   `page.clock.run_for(5000)` (or the poll interval you find) and wait for
   the charts to redraw before taking the frame. No real-time sleeps that
   decide what ends up in a frame. Finish the run at the end of the scene so
   it shows as finished.
2. **Lineage graph (about 4.5 s).** Added Nov 20, 2026 (owner: "i wanna
   include lineage graph into the gif"). On the run list, the cursor clicks
   the task's "Lineage ⤢" button (`openLineageView` in `app.js`). The
   lineage dialog opens with the task's tree: generation columns, one box
   per run with its "changed vs parent" line and metric, and "Highlight
   best path" on. Hold so the tree can be read. Then the cursor clicks the
   `mixup` box, and the inspector shows that run. Then close the dialog
   with its close button or Esc. The seeded runs need real parent links
   for this. See "Lineage seeding" below.
3. **Select and compare (about 3.5 s).** Back on the run list, tick the runs
   (one tick per frame or two, with the fake cursor moving to each) and
   click Compare. This lands on the overlaid-curves view of today's
   screenshot.
4. **Explore (about 4 s).** Move the cursor along a loss chart so the hover
   readout or crosshair updates. Then drag the EMA smoothing slider (for
   example from 0 to 0.6) to show the curves smoothing. If either control
   does not exist or behaves differently, pick the nearest real interaction
   and report it.
5. **Hold (about 1.5 s)** on the final compare view before the loop
   restarts.

Lineage seeding:

- Give every run the same explicit `task` (for example
  `"image-classification"`). Give the runs explicit parents with the SDK's
  `parent_run_id` so the tree branches: `baseline` is the root,
  `lr-3e-4` and `cosine-warmup` are children of `baseline`, and `mixup`
  and `wd-5e-4` are children of `cosine-warmup`. Read `run.py` (the
  `parent_run_id`, `lineage` and `task` arguments) and `ui/server.py`
  (`_stored_parent`, `_lineage_graph`) to check that the parents survive
  with the options the script uses. If `lineage=False` drops
  `parent_run_id`, use whatever the SDK offers publicly to keep it, and
  say which.
- Code snapshots are optional. If `lineage=True` works deterministically
  from a temporary project directory (a small `train.py` that changes
  between runs, so the inspector shows a code change vs the parent), use
  it. Do not snapshot this repository's own code. If it brings in commit
  hashes, timestamps or flakiness that show in the frames, keep
  `lineage=False` with explicit parents and report the choice.
- The dashboard's best-run badge and "Highlight best path" should land on
  a sensible run (the lowest `val_loss`). Check what metric the view ranks
  by and make sure the seeded summaries agree.
- `dashboard.png` and the social preview may change slightly because of
  the task or parent data. That is fine if they still look right.

Fake cursor: headless screenshots show no mouse pointer. Inject a small
fixed-position arrow (inline SVG, `pointer-events: none`, high `z-index`,
with a short click pulse) and move it with `page.mouse.move(..., steps=n)`,
taking a frame at each step, so viewers can follow the clicks. Add it with
`add_init_script` (or after each navigation) so it survives route changes.
It must never cover the thing being clicked.

### Encoding

- One PNG screenshot per frame, held in memory. Each frame gets its own
  duration (short for motion, longer for holds). Scenes move at about
  8–12 fps.
- Output width 960–1280 px (choose one and give the reason, considering
  that GitHub shows README images at most about 880–1000 px wide). If you
  downscale, use LANCZOS before quantizing.
- Use one shared palette for all frames, built from a sample of frames
  (for example `Image.quantize` on a strip of frames, then quantize each
  frame with that palette, `dither=Image.Dither.NONE` or FLOYDSTEINBERG,
  whichever looks cleaner on the charts). A different palette per frame
  makes the flat UI flicker. `save_all=True`, `loop=0`, `optimize=True`,
  `disposal` chosen so the frames don't flicker, per-frame `duration` list.
- Size: at most 5 MB, aim for 3 MB or less (GitHub's image proxy and slow
  connections). If it is too big: fewer frames, a smaller width, or longer
  holds instead of more frames.
- Deterministic apart from tiny pixel differences: fixed seeds, the
  `page.clock`-controlled polling, no `Date.now()`-dependent content in the
  frames (relative times like "3 s ago" must not change between runs. Freeze
  the clock if they show).

### Script shape

- New function `capture_demo(url, ids, root)` (or similar), called from
  `main()` while the server is up. It needs to write the live run into the
  store during the capture, so seed four finished runs first, then create
  `wd-5e-4` without finishing it, and log its steps from the capture loop.
  `capture_dashboard` still needs all five finished for `dashboard.png`, so
  order the work so the PNG is taken after the live run finishes, or seed a
  second store. Keep it simple and say which you chose.
- The module docstring lists the third output and how long a run takes.
  It still prints every path it wrote.
- Optional `--only {all,gif,png}` flag, only if it keeps `main()` simple.
- No network access, as today.

### Edge cases

- Chart animation or transitions: wait until the canvases have drawn (reuse
  the "every canvas has pixels" check) before each frame that follows a data
  change.
- The poll status text ("Live · refreshes every 5 s") must not show a
  stale or "Loading" state in a frame.
- A focus ring or hover state left over from the scripted clicks must not
  stay on screen (blur as the PNG capture does).
- The run list at this viewport must not overflow horizontally.
- Watch for UI defects that show up in the frames. For example, in the
  current `dashboard.png` the label "Clip outliers on y-axis" reads
  "Clipoutlierson y-axis", although `app.js:2585` has the spaces. Do not
  fix dashboard code in this package. Report each such defect with the
  file and line, and the supervisor adds it to the Backlog.

### Tests and checks

- `testbed/.venv/bin/python scripts/make_readme_images.py` from the repo
  root runs cleanly (the worktree may have no `testbed/.venv`. Use the main
  checkout's `testbed/.venv/bin/python` with this worktree's script path:
  the script puts its own `src/` first on `sys.path`).
- Look at the GIF yourself: extract about 8 evenly spaced frames to PNGs in
  a scratch directory with Pillow and view them, plus the first and last
  frames. Check for no empty charts, no "Loading", the cursor visible, the
  live run growing, readable text, and no palette banding or flicker.
- Report the frame count, total duration, width and file size.
- `testbed/.venv/bin/python -m pytest -q tests` passes (README facts
  tests).

### Done when (35.3)

`docs/assets/demo.gif` exists and is under 5 MB. It shows the five scenes
(lineage graph included)
and loops cleanly. The README shows it with absolute raw URL and alt text.
The script makes all three images in one run. Tests pass, and the roadmap
row is updated.

### As built (35.3, Nov 20, 2026)

Deviations from the storyboard above, all found by reading `app.js` and
looking at the frames:

- **Scene 1** is the live run's own page (`#/run/<id>`), scrolled past the
  header, because the run list has no charts. The curves grow from step 23
  to 199 over eight polls on Playwright's fake clock (`page.clock`), then the
  run is finished by this process. The scroll stops at the sticky smoothing
  toolbar, so its label stays in view. The "running" chip
  is scrolled out of view; only the growing curves and "@ step N" labels
  show that the run is live.
- **Scene 2** (lineage) is as planned: the Lineage button, the dialog with
  "Highlight best path", a click on `cosine-warmup` and then `mixup`
  (the best run), then Close. Seeding uses `lineage=False` with explicit
  `parent_run_id` and `task`, which the dashboard shows as "task · explicit".
- **Scene 4** uses the metrics workspace, not a compare chart: the compare
  view's compact charts have no hover readout. The cursor sweeps the
  `train_loss` readout, then drags Smoothing 0.60 to 0 and up to 0.90.
- Width 960 px (GitHub shows README images up to about 880–1000 px, so 960
  stays sharp on high-DPI screens), 121 frames, 18.7 s, 1.33 MB.
- The viewport is 1280x800; the GIF is 960x600.
- Not pinned: run ids, the start date in the run list (the browser runs in
  UTC), and the wall time of the live run ("1 s" against "<1 s"), so two
  generations differ in a few pixels of text. The server port is fixed
  (47321) unless it is taken. The polling and the steps in each frame are
  deterministic.
- `--only {all,gif,png}` was added.

The first GIF showed three dashboard defects (squeezed word gaps in small
sans text, a literal "null" in the run page header, and "code changed" in
the lineage inspector for runs without snapshots). They were fixed in the
same PR ([plan](fix-demo-gif-ui-defects.md)), and the three images were
regenerated from the fixed dashboard.

### Lineage draft (Nov 21, 2026)

Owner, after seeing `demo.gif`: the lineage graph shows for about one
frame; "i want to highlight the concept of lineage graph, create one more
draft of the gif that we can use". A second GIF, `docs/assets/demo-lineage.gif`,
made next to `demo.gif`. The README keeps `demo.gif` until the owner picks
one.

**Goal.** The GIF makes the lineage idea readable: every run records the run
it came from, the tree shows how an experiment evolved over generations, and
each step shows what changed (params and code) and what it did to the
metric. Then the best path is compared.

**Files.** `scripts/make_readme_images.py` (new `--only lineage` choice and
the scenes), `docs/assets/demo-lineage.gif` (new, generated), this section
and the roadmap row 35.3. No dashboard, server or README change.

**Seeding (draft only, its own temporary store).**

- The five runs of `demo.gif` plus two: `mixup-long` (child of `mixup`,
  gen 3, 30 epochs, the new best) and `lr-1e-2` (child of `baseline`, last
  step 59, status `crashed`). The tree then has 7 runs, 4 generations, 4
  branches and one failed run.
- One config seed (42) in this draft, so the config diffs show only the
  changes that matter (the curves keep each run's own seed).
- Real code snapshots: `lineage=True`. Before each run the script writes
  `train.py` in the work directory with that run's change (schedule, mixup,
  weight decay, epochs), so the inspector shows "1 file changed · train.py"
  next to the param diff. `wd-5e-4` keeps its parent's code ("code
  unchanged", a param-only change).
- `demo.gif`, `dashboard.png` and `social-preview.png` keep the current
  seeding and do not change.

**Storyboard (about 18–22 s, loops).** A short caption pill at the bottom
names each beat (the draft only; injected like the cursor).

1. Live run, short: the same page as `demo.gif`'s scene 1 with fewer polls
   (about 3 s). Caption: "A run is training".
2. Run list, click "Lineage ⤢". The view is fitted ("Fit") so the root is in
   view, and holds with the best path lit. Caption: "Every run records the
   run it came from".
3. Untick then tick "Highlight best path": the path from the root to the
   best run lights up. Caption: "The best path: from the root run to the
   best one".
4. Walk the path: click `baseline` (root config, baseline snapshot),
   `cosine-warmup`, `mixup`, `mixup-long`, holding about 1.2 s each so the
   inspector's config diff, code row and Δ vs parent / Δ vs root can be
   read. Caption: "What each step changed, and what it did to val_loss".
5. Click the crashed `lr-1e-2`: a dead branch stays in the record. Caption:
   "Dead ends stay in the record".
6. "Best path" quick select, then "Compare 4 runs" from the tray: the
   compare view overlays the path's curves. Caption: "Compare the best
   path".

As built (Nov 21, 2026): 78 frames, 21.9 s, 0.56 MB. The caption pill sits
at the bottom right, over the lineage footnote, so it hides neither the
legend nor the tray. Reviewed by Sonnet (read-only); its four findings
(plan wording, a stricter inspector wait, the final cursor spot) are fixed.

**Constraints.** Same recording rules as `demo.gif` (fake clock, one
screenshot per frame, one palette, 960 wide). Under 3 MB. No change to
`demo.gif` output for `--only all|gif|png`.

**Edge cases.** The 4-lane tree must fit the 1280×800 viewport with the
inspector open (check the frames; zoom out one step if it does not). The
crashed run must not be picked as best. `train.py` must not land in the
store's run code (`track_source=False` already).

**Checks.** Run `--only lineage`. View the frames for each beat: the tree is
fully in view, captions don't hide the controls, the inspector text is
readable at 960 px, no "Loading" or empty chart. Run `--only gif` and confirm
`demo.gif` still renders. `tests/test_website_facts.py` passes.

**Done when.** `demo-lineage.gif` exists, the owner has seen it, and the
choice between the two GIFs is the owner's.

**Owner's pick (Nov 21, 2026): "Use lineage draft".** README line 7 now
shows `demo-lineage.gif`, with alt text for the lineage story. `demo.gif`
and its `--only gif` mode stay as the first cut; nothing links to it.

## Branch and release

One PR to `main` (the working branch) with the plan, script, GIF, README and
roadmap changes. The README on GitHub's front page and on PyPI changes when it
is merged; PyPI shows it after the next release. Merging is the owner's step.

## Owner steps (not code)

- Repo topics: `experiment-tracking`, `mlops`, `machine-learning`,
  `deep-learning`, `pytorch`, `tensorboard`, `wandb-alternative`,
  `self-hosted`, `local-first`, `python`.
- Upload `docs/assets/social-preview.png` in Settings → General → Social
  preview (GitHub has no API for it).
- Turn on Discussions; add a few `good first issue` issues.
- Launch steps in the [outreach plan](../outreach.md), after the README is
  on `main`.
