# Open tasks

Unfinished work, highest priority first. See [AGENTS.md](AGENTS.md) for how
these are prioritized. When a task is done, delete its entry in the same
change that finishes it.

## Deliver the note PUT 408 before closing on macOS

- **Where:** `src/agenticml/ui/server.py` (the `TimeoutError` branch of the
  notes PUT that sends 408) and
  `tests/test_ui.py::test_put_notes_trickled_body_hits_absolute_deadline`.
- **Problem:** the server closes the socket right after the 408 while the
  client is still trickling body bytes. Closing with unread input makes macOS
  send a reset, so the client gets `ConnectionResetError` and never sees the
  408. The test fails on every macOS run.
- **Fix:** after the 408, `shutdown(SHUT_WR)` and drain input for a short,
  bounded time (lingering close) before closing, without letting the drain
  hold a connection slot past the existing deadlines.
- **Done when:** `pytest -q tests` passes repeatedly on macOS and in CI.

## Send `task` and `arch` when syncing a run

- **Where:** `src/agenticml/sync.py` (`CONFIG_FIELDS` and the `/init` payload).
- **Problem:** runs now record `task`, `task_source`, and `arch` in
  `meta.json`, but sync uploads neither, so a remote server cannot group
  lineage by task.
- **Fix:** once the server accepts them, add `task`, `task_source`, and `arch`
  to the synced fields (keep older servers working if they reject unknown
  keys).
- **Done when:** a sync test asserts the three fields reach the payload and an
  older-server response does not fail the sync.
- **Blocked:** the remote server's `/init` schema is not in this repo, so we
  cannot tell yet whether it accepts these fields. Deferred until the cloud
  deployment work.
