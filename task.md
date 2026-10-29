# Open tasks

Unfinished work, highest priority first. See [AGENTS.md](AGENTS.md) for how
these are prioritized. When a task is done, delete its entry in the same
change that finishes it.

## 1. Refresh the lineage detail strip when the graph repaints

- **Where:** `src/agenticml/ui/static/app.js`, `paintLineage()` (the
  `card.dataset.detailFor !== focusId` check) and `showLineageDetail()` /
  `renderDetail()`.
- **Problem:** the detail strip under the lineage graph is only re-fetched when
  a different node is focused. After the graph repaints (lineage signature
  change, or the 15 s refresh for running projects), the open strip keeps
  showing the parent link, commit fallback and dirty flag from when the node
  was clicked. Pre-existing; found in review of PR #4.
- **Fix:** re-call `showLineageDetail` when the painted node's payload differs
  from the one the strip was rendered from (keep the strip open, no flash of
  "Loading changes…" if nothing changed).
- **Done when:** a Playwright test in `testbed/tests/test_dashboard.py` changes
  a focused node's parent/commit between polls and asserts the strip updates.
  The existing focus/scroll-preservation test may need its DOM-identity
  assertion relaxed for the strip.

## 2. Lay out the lineage graph top-to-bottom in the split view

- **Where:** `layoutLineage()` and `renderLineageSvg()` in
  `src/agenticml/ui/static/app.js`; `.lineage-*` rules in `app.css`.
- **Problem:** the graph is laid out left-to-right. Since the Runs page became
  a split view (list left, lineage right), the graph lives in a half-width,
  tall pane, so long chains of runs need horizontal scrolling while most of
  the pane's height is empty.
- **Fix:** lay generations out top-to-bottom (edges flowing downward), so the
  graph fits the pane's shape and reads in the same direction as the list.
  Keep keyboard navigation, selection, dimming and focus/scroll preservation.
- **Done when:** a linear chain of ~8 runs fits the pane at 1440px without
  horizontal scrolling, 375px still has no page-level horizontal overflow,
  and the existing lineage tests pass (update scroll assertions from
  `scrollLeft` to `scrollTop` where needed).

## 3. Retry a failed lineage fetch for idle projects

- **Where:** `ensureLineage()` and `refresh()` in
  `src/agenticml/ui/static/app.js`.
- **Problem:** after a failed `/api/lineage` fetch, the cached signature is
  invalidated so the next `draw()` refetches. But `refresh()` only re-polls
  while some run is `running`, so for an all-idle workspace the retry only
  happens on the next user action (filter keystroke, project tab switch).
- **Fix:** schedule a bounded retry (e.g. backoff up to 30 s) for a project
  whose lineage fetch errored, independent of the runs poll.
- **Done when:** a test fails `/api/lineage` once with no running runs and
  asserts the graph appears without any user interaction.

## 4. Evict lineage cache entries for projects no longer shown

- **Where:** `lineageCache` in `home()` in `src/agenticml/ui/static/app.js`.
- **Problem:** when a project disappears from `/api/runs`, its `lineageCache`
  entry (and the detached card it references) is never deleted. No visible
  bug, since paints are gated on `isConnected`, but it leaks for the life of
  the page.
- **Fix:** in `draw()`, delete cache entries whose project key is not in the
  current `projects` list.
- **Done when:** covered by a unit-level check or a short Playwright test that
  removes a project between polls; existing tests pass.
