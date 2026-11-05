# Backlog fixes before the 2.0.0 release

Nov 5, 2026 · Supervisor plan · Release 2.0 · Base branch: `dev`

Source: the Backlog section of [on-prem-roadmap.md](../on-prem-roadmap.md).
All four entries are fixed before `v2.0.0` is tagged. Coders do not edit the
roadmap; the supervisor removes each entry once its task is verified.

| Task | Entry | Files | Coder | Needs |
| --- | --- | --- | --- | --- |
| A | `sync --follow` repeats the same failure line | `sync.py`, `tests/test_sync_follow.py` | Cursor | – |
| B | Lineage tray "Compare N runs" link loses focus | `ui/static/app.js`, `testbed/tests/test_task_groups.py` | Cursor or Codex | – |
| C | Server image installs unpinned runtime dependencies | `docker/Dockerfile`, new `docker/constraints.txt`, `scripts/server_smoke.sh` (if needed), `.github/dependabot.yml` | Codex or Cursor | PR #34 merged |
| D | Unit CI pinned to Ubuntu 24.04 for Python 3.9 | `.github/workflows/test.yml`, `pyproject.toml`, `README.md`, docs that say "3.9" | Codex or Cursor | Decision: drop Python 3.9 |

Conventions: standard library only, no new runtime dependencies, match the
surrounding style, sparse "why" comments. Checks: `pytest -q tests` from the
repo root; the testbed UI suite (`-m ui`) for task B, with zero horizontal
overflow at 375 px.

## A. Repeated failure line in `--follow` and `--watch`

Today `_emit_follow` prints a line whenever `result.failed` is true, so a
failure that is neither permanent nor a network error (an explicit run id
that does not exist, an `OSError` in the store) prints the same line every
`--interval`. `--watch` (the loop in `cmd_sync`) does the same.

1. Add a small helper in `sync.py`, for example `_FailureLog`, keyed by
   run id. It stores the last failure text and the time it was printed
   (`time.monotonic()`, injectable for tests).
2. On a failed result: print the line if the text differs from the last
   one for that run, or if at least `FAILURE_REMINDER = 600.0` seconds have
   passed since it was last printed; then the reminder line is the same line
   with ` (still failing)` appended. Otherwise print nothing.
3. On a non-failed result for a run that had a recorded failure: print the
   normal line (it already prints on status change) and clear the record, so
   the user sees that it cleared.
4. Use it in `_emit_follow` and in the `--watch` loop. Do not change what
   is printed for successful passes, the permanent-failure handling, exit
   codes, or the stderr messages for 401 and protocol errors.
5. Tests (`tests/test_sync_follow.py`): an explicit missing run id over
   several passes prints one failure line; with the clock advanced past 600 s
   it prints one reminder; a run that fails then succeeds prints the failure
   once and the success line once; `--watch` with a failing run prints the
   failure once over several passes. Use the existing fakes in that file.

## B. Lineage tray focus

The tray's `Compare N runs` link in the lineage view (`app.js`, the `el("a",
…)` after the `lv-clear` button) has no `data-focus`, so a re-render drops
keyboard focus to `<body>`.

1. Add `"data-focus": "lv-compare-tray"` (a key not used elsewhere; `lv-compare`
   is already taken by the inspector link).
2. Check every other focusable element the lineage view renders (links,
   buttons, inputs, `tabindex`) for a `data-focus` key, and add stable,
   unique keys where missing. List what you checked.
3. UI test in `testbed/tests/test_task_groups.py` (or the file that already
   tests lineage focus): tick two runs, focus the link, trigger a
   re-render the same way the existing focus tests do (poll or fetch), and
   assert focus is still on the link. One test per newly keyed element is
   not needed; the tray link test is required.

## C. Pinned runtime dependencies in the image

1. Add `docker/constraints.txt` listing every runtime dependency of the
   wheel with exact versions and `--hash` entries (`requests` and its
   dependencies: `charset-normalizer`, `idna`, `urllib3`, `certifi`), for
   Python 3.12 on Linux. Generate it with `pip download` / `pip hash` in
   the pinned base image so the hashes match, and put the command that
   regenerates it in a comment at the top of the file.
2. Dockerfile runtime stage: install with `pip install --no-cache-dir
   --require-hashes -r docker/constraints.txt`, then the wheel with
   `--no-deps`. The build stage copies `docker/constraints.txt` (check
   `.dockerignore` does not exclude it).
3. Dependabot: add a `pip` entry for `/docker` so pinned versions get
   update PRs (Dependabot understands hashed requirements files).
4. Check: `docker build` twice and compare `pip freeze` inside both
   images (identical); smoke script `smoke: OK`.

## D. Drop Python 3.9 (approved by the user, Nov 5, 2026)

1. `pyproject.toml`: `requires-python = ">=3.10"`, remove the 3.9
   classifier, add 3.13 if CI covers it.
2. `.github/workflows/test.yml`: matrix `["3.10", "3.12"]` (or 3.13),
   `runs-on: ubuntu-latest`, remove the pin comment. Confirm with
   `actions/setup-python` that every version in the matrix is available on
   `ubuntu-latest`; if 3.10 is not, use the oldest version that is and say so.
3. Update every doc that says "3.9" (README, website installation page,
   `docs/SDK.md`, AGENTS.md conventions if any). Leave code alone unless a
   3.10+ idiom is needed; no refactors.
4. Check: `pytest -q tests` locally; CI runs on the PR.

## Done when

Each task's tests pass, the full suite is green, Sonnet reviews each task
clean, and the roadmap Backlog section is empty (or holds only entries found
during this work, each with where, problem, fix and done-when).
