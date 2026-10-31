# Open tasks

Unfinished work, highest priority first. See [AGENTS.md](AGENTS.md) for how
these are prioritized. When a task is done, delete its entry in the same
change that finishes it.

## Fix failing dashboard connection-limit tests

- **Where:** `tests/test_ui_limits.py`
  (`test_nonreading_clients_are_evicted_when_full`,
  `test_slow_writer_is_not_evicted_below_capacity`).
- **Problem:** both fail on `main` (as of e13dd25, macOS) with "server did not
  reach the expected state"; the full suite also showed a few other
  intermittent failures in the same run.
- **Fix:** find whether the eviction logic or the test's wait/timing is wrong
  and correct that side.
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
