# Open tasks

Unfinished work, highest priority first. See [AGENTS.md](AGENTS.md) for how
these are prioritized. When a task is done, delete its entry in the same
change that finishes it.

## Page-wide live region announces every re-render

- **Where:** `src/agenticml/ui/static/index.html` (`<main id="app" aria-live="polite">`).
- **Problem:** the whole app is one polite live region. Screen readers can
  announce every poll repaint, chart refresh, and the notes' 30 s relative-time
  update, not just meaningful status changes.
- **Fix:** drop `aria-live` from `<main>` and give each meaningful status
  (loading/error messages, poll status, save state) its own small live region.
  Check the code viewer and compare page still announce their states.
- **Done when:** `main#app` has no `aria-live`, and a UI test asserts that
  route loading/errors and the notepad save state are each inside a dedicated
  live region while relative-time text is not.

## Polish the oversized-note (`too_large`) notepad gate

- **Where:** `src/agenticml/ui/static/notes.js` (`flush`, unload handlers,
  View-mode ticks, the truncation banner) and
  `testbed/tests/test_dashboard.py::test_truncated_note_warns_until_save_anyway`.
- **Problem:** only reachable when `notes.md` was hand-edited past 256 KiB.
  The test does not cover pagehide/visibilitychange/beforeunload, a route
  change, blur, Ctrl+S or Retry while the gate is closed. View-mode ticks flip
  locally but cannot save. The banner says "Saving will drop…" even on a
  `--read-only` dashboard. Ctrl+S does nothing silently. Separately,
  `renderMarkdown` uses `Array.prototype.at` (Safari 15.4+).
- **Fix:** extend the test to fire those events and assert no PUT (and that
  `beforeunload` is prevented). Disable checkboxes while the gate is closed.
  Use read-only banner text when not writable. Point Ctrl+S at "Save anyway".
  Replace `.at(-1)` with index access.
- **Done when:** the extended test passes and `grep -n '\.at(' notes.js` is empty.

## Bound concurrent dashboard handler threads

- **Where:** `src/agenticml/ui/server.py` (`DashboardServer`, a `ThreadingHTTPServer`).
- **Problem:** there is no cap on handler threads. A client that opens many
  slow connections (slow headers, or slow note PUT bodies within the 15 s
  deadline) can exhaust threads or memory. This predates notes, but notes made
  the server writable.
- **Fix:** cap concurrent handlers (e.g. a semaphore in `process_request`,
  refusing or closing past the limit) and add a header-read deadline.
- **Done when:** a test opens more than the limit of idle connections and the
  server still answers a normal GET promptly.
