# Agent instructions

## The roadmap comes first

[docs/plans/on-prem-roadmap.md](docs/plans/on-prem-roadmap.md) lists planned
work in release order, with a status per work package and a backlog. Detailed
plans sit next to it in `docs/plans/`.

- At the start of every session, read the roadmap before planning anything else.
- Unfinished packages in the current release, and backlog entries, take
  priority over new feature work. If the user asks for something new, mention
  what remains open and, unless the user says otherwise, finish the next open
  package (or the ones the new request touches) first.
- **Plan before building.** Every package starts with a planning task for the
  supervisor: write `docs/plans/packages/<id>-<slug>.md` (goal, files to
  touch, step-by-step approach, constraints and conventions, edge cases,
  tests and checks, done when), based on the package's section in the
  release plan and on the current code. No coder starts until that plan exists
  and the package's status is `plan ready`. The coder gets the plan file as its
  task and reports any deviation instead of changing the approach. The plan
  file is committed in the package's PR.
- Work on packages in their dependency order. Only skip ahead when one is
  blocked; set its status to `blocked: <reason>`.
- Update a package's status in the same PR that changes it, and make sure its
  "Done when" check is satisfied (tests added and passing).
- Keep progress visible: give each part of a multi-part package its own
  roadmap sub-row, and push the branch with a draft PR as soon as the first
  package is committed, then push every later package or part as it is
  committed (see the roadmap's "Status" section).
- When you leave something unfinished (found in review, out of scope, a
  known bug or follow-up), add it to the roadmap's Backlog with: where,
  problem, fix, and "done when". Place it by priority, not at the end by
  default.
- macOS is not a CI target; do not track macOS-only test failures.

## Checks before handing work back

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/` for any dashboard
  change (see `testbed/README.md` to set up the venv).
- Dashboard changes must keep zero horizontal page overflow at 375px.
