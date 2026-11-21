# Fix: dashboard defects seen in the README demo GIF

Nov 21, 2026 · in review (see Resume notes) · ships in the package 35.3 PR
([#96](https://github.com/agenticML/agenticml-lite/pull/96))

## Goal

Fix the three dashboard defects that the 35.3 demo GIF showed (roadmap
Backlog, "Dashboard UI defects seen in the README demo GIF"). Then
regenerate the README images so the GIF and `dashboard.png` show the fixed
dashboard. Owner: "Yes, fix them here".

## Findings (supervisor, verified)

1. **Squeezed word gaps in small sans text.** "Clip outliers on y-axis"
   reads "Clipoutlierson y-axis", and the same happens to "Tick two or more
   runs in this task to compare them." and "Highlight best path". The
   self-hosted `InstrumentSans.woff2` is not broken: its space is 0.2 em
   (2.4 px at 12 px), the same as the Google Fonts build at every size. But
   0.2 em is narrow for this face. Chromium on Linux (which owners and CI
   use) rounds glyph positions, and kerning against the space takes more
   off, so at `--fs-sm` (12 px) a space draws as about 1.3 px. A
   `--font-render-hinting` flag doesn't fix it. A word-spacing of
   `0.08em` does, measured in a 12 px and 14 px render: the gaps read
   normally and the text is not visibly loose.
2. **Literal "null" in the run page header.** `drawFacts` in `app.js`
   (about line 3644) calls the native `title.replaceChildren(...)` with
   `null` for the owner chip (always null in local mode) and for the parent
   link (null for root runs). The `el()` helper filters out null children
   (`children.filter((child) => child != null)`), but native
   `replaceChildren` turns `null` into the text "null".
3. **Lineage inspector says "code changed" with no snapshots.** In the
   lineage view inspector (about lines 2028–2052), the code row is
   `!parent ? "baseline snapshot" : node.same_tree_as_parent ? "code
   unchanged" : … : "code changed"`. The server sets `same_tree_as_parent`
   to false when either run has no `tree` (`server.py` about line 806), so
   runs recorded with `lineage=False` show "code changed", and a root
   without a snapshot shows "baseline snapshot". It also fetches
   `/lineage` for runs that have no snapshot.

## Files to touch

- `src/agenticml/ui/static/app.css`: word spacing (1).
- `src/agenticml/ui/static/app.js`: `drawFacts` and any other
  `replaceChildren` call that can get null or false (2); the inspector's
  code row (3).
- `testbed/tests/test_dashboard.py` (or the test module that already covers
  the same page): new UI tests.
- `docs/assets/demo.gif`, `docs/assets/dashboard.png`,
  `docs/assets/social-preview.png`: regenerated with
  `scripts/make_readme_images.py`.
- `docs/plans/on-prem-roadmap.md`: Backlog entry to done, plus a
  "Maintenance fixes" bullet with this plan's link.
- `docs/plans/packages/35-repo-outreach.md`: in the 35.3 "As built"
  section, drop the statements about these defects being in the GIF.

## Approach

1. **Word spacing.** In `app.css`, add `word-spacing: 0.08em` to the
   `html, body` rule, with a short comment giving the reason (finding 1). It
   inherits, so it must be switched off for monospace text. Code views, diff
   views and aligned mono columns must keep one-character spaces. Add
   `word-spacing: normal` to every rule that sets the mono family
   (`var(--font-mono)` or `var(--font-mono-stack)`, in a `font` shorthand
   or `font-family`), or to one shared selector that provably covers them
   all. Check `code.js` and `codediff.js` for elements styled mono through
   classes, `<pre>` or `<code>`, and include `pre, code, kbd, samp`.
   Canvas text in `charts.js` is not affected. Leave it alone.
2. **No "null" text.** In `drawFacts`, filter out nullish and false
   children before `replaceChildren` (for example
   `title.replaceChildren(...[...].filter(Boolean))`, or build with
   `el()`). Then grep every `replaceChildren(` in `app.js`, `code.js` and
   `codediff.js`. Fix each call whose arguments can be `null`, `undefined`
   or `false` (conditional `? … : null` and `&&` children) the same way.
   Leave calls that can't get such values unchanged. List in the hand-back
   which calls you changed.
3. **Inspector code row.** Using the lineage node fields (`tree`, and the
   parent node's `tree` from `entry.byId`):
   - root run: "baseline snapshot" when `node.tree`, else "no snapshot";
   - child, both trees present: as today ("code unchanged", the changed
     files, the error, or "code changed");
   - child, its own tree missing: "no snapshot";
   - child with a tree whose parent has none: "parent has no snapshot".
   Fetch `${apiRun(runId)}/lineage` only when both trees exist and differ.
   Check the run list's `changeNote` (about line 3680), which already
   returns null without trees, and keep it that way.

## Constraints and conventions

- No change to the server, the store or the API. Front-end only.
- Keep zero horizontal page overflow at 375 px (the wider word gaps must not
  push anything over). The existing mobile overflow tests must pass.
- Plain wording in UI strings, like the existing ones.
- The main checkout's `testbed/.venv` has `agenticml` installed in editable
  mode from the **main checkout's** `src/`, not this worktree's. Make sure
  the UI tests serve this worktree's code: set
  `PYTHONPATH=<worktree>/src` and check what the test server imports (print
  `agenticml.__file__` from the test process, and check that a dashboard
  served in a test returns this worktree's `app.css`, for example by
  grepping the served CSS for `word-spacing`). If the fixtures start a
  subprocess that drops `PYTHONPATH`, report it and find another way. Do
  not install anything into the venv.

## Edge cases

- Mono elements inside sans containers (badges, ids, `.mono` spans, metric
  values) keep `normal`.
- `replaceChildren()` with no arguments (clearing) is fine. Don't touch it.
- Inspector for a run with a snapshot whose parent is outside the view or
  missing: unchanged behaviour unless it hits a branch above.

## Tests and checks

- New UI tests (`-m ui`):
  - the computed `word-spacing` of the compare toolbar's "Clip outliers"
    label is greater than 0, and that of a code line in the code viewer
    (or another mono element on the same page) is `0px`/`normal`;
  - a local root run's page header text has no "null" (a run with a parent
    as well, to check both branches);
  - lineage view inspector for a child run seeded with `lineage=False` and
    an explicit `parent_run_id` shows "no snapshot", not "code changed".
    Seed with the existing fixtures' style.
- `cd testbed && PYTHONPATH=<worktree>/src .venv/bin/python -m pytest -q -m ui`
  (the main checkout's venv, run from the worktree's `testbed/`, or point
  pytest at the worktree's test files): all pass, including the 375 px
  overflow tests.
- `testbed/.venv/bin/python -m pytest -q tests` from the worktree root
  (the 8 server test files that need `boto3`/`jwt` may be ignored, as
  before; say so).
- Regenerate: `<main venv python> scripts/make_readme_images.py` from the
  worktree root. Then view frames 0, about 25, about 40 and about 85 of
  the GIF plus `dashboard.png`: "Clip outliers on y-axis" reads with
  spaces; the GIF is still under 3 MB.

## Done when

All three defects are fixed with tests that fail before the fix and pass
after it. The UI and full test runs pass. The images are regenerated and
show the fixes. The roadmap Backlog entry is marked done with this plan
linked.

## Resume notes (Nov 21, 2026, paused by the owner)

State at the pause, all pushed to #96 (draft):

- Built by Sonnet. Code and visual reviewers checked it, Sonnet fixed their
  four minor findings (form controls now inherit the word spacing; `· gen N`
  in the lineage inspector stays on one line; comment wording; hosted
  375px tests can't run locally), and a recheck passed.
- The recheck found one more case of finding 3: the run page's change note
  said "code changed" when the run has a snapshot and its parent has none.
  Fixed in `runPage` (`parentTree === null` now gives "parent has no
  snapshot"), with
  `test_dashboard.py::test_run_page_change_note_says_parent_has_no_snapshot`
  (the coder saw it fail before the fix). The supervisor read the change
  and ran `test_dashboard.py` and `test_task_groups.py`: 100 passed.

Left to do before the PR is marked ready:

- Run the whole `-m ui` suite and `pytest -q tests` once more after that
  last fix (the last full runs, 301 UI passed and 3714 passed, came before
  it).
- The hosted dashboard UI tests (`test_server_oidc_dashboard.py`,
  `test_server_firebase_dashboard.py`, `test_server_signin_theme_dashboard.py`)
  need `jwt` and the firebase extra, which the local testbed venv lacks: let
  CI cover their 375px overflow checks.
- The README images don't show the run-page change note case, so they
  need no regeneration for it.
