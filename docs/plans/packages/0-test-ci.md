# Package 0: Test CI

Nov 3, 2026 · Supervisor plan · Release 2.0 · Coder: Codex · Size: S

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), section 7,
package 0. Roadmap: [on-prem-roadmap.md](../on-prem-roadmap.md).

## Goal

Every push to `main` and every pull request runs the repo's test suites on
GitHub Actions (Linux), so later packages are merged on a green check instead
of on local runs only. CI runs the same commands `AGENTS.md` asks agents to run.

## Baseline (measured Nov 3, 2026 on Linux, before any change)

| Suite | Command | Result |
| --- | --- | --- |
| Root, Python 3.9.25 | `python -m pytest -q tests` | 1251 passed, 9 skipped, 130 s |
| Root, Python 3.12.13 | `testbed/.venv/bin/python -m pytest -q tests` | 1251 passed, 9 skipped, 117 s |
| Testbed, all, Python 3.12 | `.venv/bin/python -m pytest -q` from `testbed/` | 250 passed, 106 s |
| Testbed, UI only | `.venv/bin/python -m pytest -q -m ui` from `testbed/` | 136 passed, 80 s |

The testbed's browser tests (`tests/test_dashboard.py`, marked `browser` and
`ui`) need Playwright 1.62.0 with Chromium. They include the 375px overflow
checks.

## Files to touch

| Path | Change |
| --- | --- |
| `.github/workflows/test.yml` | New |
| `docs/plans/on-prem-roadmap.md` | Package 0 row: Plan column links this file; Status `in review` |

Nothing under `src/`, `tests/` or `testbed/` changes. If a test fails only on
the CI runner, do not edit or skip it in this package; report it (see "Edge
cases").

## Approach

1. Create `.github/workflows/test.yml` named `Tests`.
2. Triggers: `push` to `main`, `pull_request` (any base branch),
   `workflow_dispatch`.
3. `permissions: contents: read` at the top level.
4. `concurrency`: group `tests-${{ github.workflow }}-${{ github.ref }}`,
   `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`, so a new
   push to a PR cancels the old run but `main` runs are never cancelled.
5. Job `unit` (name `Unit tests (Python ${{ matrix.python }})`):
   - `runs-on: ubuntu-latest`, `timeout-minutes: 20`.
   - `strategy.fail-fast: false`, `matrix.python: ["3.9", "3.12"]` (lowest
     supported per `requires-python`, and the version the testbed uses).
   - Steps: `actions/checkout@v7`; `actions/setup-python@v7` with the matrix
     version, `cache: pip`, `cache-dependency-path: pyproject.toml`;
     `python -m pip install --upgrade pip`; `python -m pip install -e '.[dev]'`;
     `python -m pytest -q tests`.
6. Job `testbed` (name `Testbed and dashboard UI`):
   - `runs-on: ubuntu-latest`, `timeout-minutes: 30`, Python 3.12.
   - Mirror `testbed/README.md` and `AGENTS.md` exactly, using a venv at
     `testbed/.venv` so the commands match what agents run locally:
     - `python -m venv testbed/.venv`
     - `testbed/.venv/bin/python -m pip install --upgrade pip`
     - `testbed/.venv/bin/python -m pip install -e '.[dev]' playwright==1.62.0 -e './testbed[test]'`
   - Cache Playwright browsers with `actions/cache@v6`: path
     `~/.cache/ms-playwright`, key `playwright-1.62.0-${{ runner.os }}`.
   - `testbed/.venv/bin/python -m playwright install --with-deps chromium`
     (always run it: on a cache hit it only installs the system packages).
   - Fail fast if Chromium cannot launch, because
     `testbed/tests/test_dashboard.py` skips every browser test (about 114,
     including the 375px checks) when launch fails, which would leave the job
     green: a step that starts Playwright, launches and closes Chromium, and
     exits non-zero on any error.
   - Run from `testbed/` (`working-directory: testbed`):
     `.venv/bin/python -m pytest -q -rs` (`-rs` prints the reason for every
     skip in the log). This is the whole testbed suite (CLI,
     API, smoke script and the `ui` + `browser` dashboard tests), a superset of
     the `-m ui` check in `AGENTS.md`, for about 25 s more.
7. Pin actions to major tags as the existing workflows do (`docs.yml` already
   uses `@v7`). Do not add third-party actions.
8. Update the roadmap row for package 0 (see "Files to touch").

## Constraints and conventions

- Match the style of `.github/workflows/publish.yml` and `docs.yml`: two-space
  YAML, short step names, a comment only where a choice is not obvious.
- No secrets, no write permissions, no `pull_request_target`.
- Do not change the test suites, `pyproject.toml` or `testbed/pyproject.toml`.
- macOS and Windows are not CI targets (`AGENTS.md`).

## Edge cases

- **testbed dependency on `agenticml-lite`:** the single `pip install` line
  installs the root package editable alongside `./testbed[test]`, so pip
  resolves `agenticml-lite` to the local checkout, not PyPI. Verify with
  `testbed/.venv/bin/python -m pip show agenticml-lite` (location must be the
  checkout) while testing locally.
- **Sandboxed HOME in the testbed:** `testbed/tests/conftest.py` sets
  `PLAYWRIGHT_BROWSERS_PATH` from the real home before sandboxing, so the
  default browser path works. Do not set it in the workflow.
- **Timing-sensitive tests flaking on shared runners:** do not add retries or
  skips. If a test fails on CI but passes locally, report the test name and
  log excerpt; the supervisor adds it to the roadmap backlog.
- **Python 3.9:** the baseline shows 3.9 passes today; if the job fails on 3.9 only, report it rather than dropping 3.9.

## Checks (coder, before handing back)

1. Validate the workflow: `uvx --from actionlint-py actionlint .github/workflows/test.yml`
   (no findings).
2. Run each job's commands locally on Linux in a throwaway venv under the
   scratchpad (not the repo), following the steps above exactly:
   - root suite on Python 3.9 and 3.12;
   - testbed suite from `testbed/` with the venv built as in step 6 (use a
     scratch venv path and adjust only the path).
3. Report: the final file, actionlint output, and the pass/fail counts.

## Done when

- `actionlint` is clean and the local runs of both jobs' commands pass.
- After the PR is pushed: both `unit` jobs and the `testbed` job are green on
  the PR, and the workflow is green on `main` after merge. The roadmap row moves
  to `done (<PR link>)` in a follow-up commit or the merge PR.
