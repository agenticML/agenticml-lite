# Agent instructions

## Open tasks come first

[task.md](task.md) lists unfinished work in priority order.

- At the start of every session, read `task.md` before planning anything else.
- Unfinished tasks in `task.md` take priority over new feature work. If the
  user asks for something new, mention which open tasks remain and, unless the
  user says otherwise, finish the top open task (or the ones the new request
  touches) first.
- Work on tasks in the order listed. Only skip ahead when a task is blocked;
  note why under that task's entry.
- When you finish a task, delete its entry from `task.md` in the same commit
  or PR, and make sure its "Done when" check is satisfied (tests added and
  passing).
- When you leave something unfinished (found in review, out of scope, a
  known bug or follow-up), add it to `task.md` with: where, problem, fix, and
  "done when". Place it by priority, not at the end by default.

## Checks before handing work back

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/` for any dashboard
  change (see `testbed/README.md` to set up the venv).
- Dashboard changes must keep zero horizontal page overflow at 375px.
