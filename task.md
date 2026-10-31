# Open tasks

Unfinished work, highest priority first. See [AGENTS.md](AGENTS.md) for how
these are prioritized. When a task is done, delete its entry in the same
change that finishes it.

## Busy dashboard connections can still fill every handler slot

- **Where:** `src/agenticml/ui/server.py` (`DashboardServer.process_request`,
  `_MAX_CONNECTIONS`).
- **Problem:** the connection cap evicts only connections still waiting for
  headers. A client that sends complete headers and then stalls (a slow note
  PUT body for up to 15 s, or not reading a response for up to the 60 s socket
  timeout) holds a "busy" slot. Enough of them, re-opened as they expire, make
  every new request get a 503.
- **Fix:** bound "busy" time too, e.g. a total per-request deadline for the
  response write, a lower cap on concurrent PUT bodies, or a per-client-address
  share of the slots.
- **Done when:** a test fills every slot with stalled PUT bodies or non-reading
  clients and a normal GET still answers within a few seconds.

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
