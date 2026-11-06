# Package 9: Network-filesystem benchmark and tuning

Nov 6, 2026 · Supervisor plan · Release 2.1 · Coder: Cursor · Size: S

Source: [on-prem-roadmap.md](../on-prem-roadmap.md) (2.1, package 9). Base
branch: `dev`. Ships in the single 2.1 PR, one commit per package.

## Goal

Know how the server behaves when its data directory is on EFS or another
network filesystem with several machines syncing, and publish results and
recommended settings in `docs/SERVER.md`.

Done when the results and recommended settings are in `docs/SERVER.md`.

## Constraint: no EFS in this environment

The development machine has no EFS or NFS mount and no root to create one.
So this package:

1. Adds a repeatable server benchmark that an operator can point at any data
   directory (EFS included) with one command.
2. Runs it here on local disk, and with a modelled network-filesystem
   latency (an injected delay on `fsync` and on directory fsyncs in the
   **benchmark's own server process only**), which is where a network
   filesystem's cost concentrates for this write path (every append,
   atomic write and object does `fsync` plus a directory fsync).
3. Publishes those results, labelled as local and modelled, with the
   recommended settings derived from them, and the exact command to
   reproduce on real EFS.
4. Adds a Backlog entry: run the benchmark on a real EFS mount and replace
   the modelled numbers.

No product code changes unless the numbers show a clear problem (for
example, a lock held across an fsync that serializes unrelated runs). If one
shows up, report it to the supervisor with numbers before changing code.

## What exists today

- `scripts/bench.py` (client-side targets) and `scripts/bench_store.py`
  (store reads/writes), stdlib only, temp dirs deleted.
- Server: `agenticml.server.app.make_server(data, host, port, ...)`, port 0
  picks a free port; `KeyStore.create` for keys. Every append / atomic /
  object write does `os.fsync` + `layout.fsync_dir`. Per-run lock
  (`run_lock`) and a global `objects_lock`.
- Client: `agenticml sync` (`sync.cmd_sync`, `sync_run`, `Client`),
  `--follow --interval`, `APPEND_CHUNK = 8 MiB`, heartbeat 60 s.

## Files to touch

- `scripts/bench_server.py` (new).
- `docs/SERVER.md`: a "Network filesystems (EFS)" section.
- `docs/plans/on-prem-roadmap.md` Backlog entry (supervisor adds it; the
  coder writes the entry text in the report).

## `scripts/bench_server.py`

```
python scripts/bench_server.py [--data DIR] [--clients N] [--runs R]
    [--steps S] [--metrics M] [--passes P] [--fsync-delay-ms D] [--quick]
```

- Starts a server in-process on `127.0.0.1:0` with `--data DIR` (default: a
  temp dir, deleted at exit; a given `--data` is used as is and its
  `bench-*` contents are removed at exit). Creates one sync key per client.
- `--fsync-delay-ms D` wraps `os.fsync` (and therefore `layout.fsync_dir`)
  **in the server process only** with `time.sleep(D/1000)` before the real
  call. Clients run in separate processes (`multiprocessing` spawn or
  `subprocess` with `sys.executable`) so they are not slowed.
- Each client process has its own local store (temp dir) with R runs of S
  steps × M metrics written with `RunWriter` (as `bench_store.py` does), then
  runs P passes of `sync --all` against the server, appending a further
  batch of steps to each run between passes to model `--follow` on live
  runs. All clients start together (barrier).
- Measures and prints a table: initial full sync wall time and MB/s
  (aggregate), per-pass incremental sync time p50/p95 per client, commit
  request latency p50/p95 (time the client's `commit` calls via a wrapper
  in the client process), and server-side CPU time if cheap to get.
- `--quick`: small sizes so it finishes in under a minute; used by a smoke
  test.
- Stdlib only (plus the package itself). No network beyond loopback.

## Runs to do and publish

On this machine (record CPU, disk type if known, Python and git versions):

1. Local disk, `--clients 1` and `--clients 8`.
2. Same with `--fsync-delay-ms 5` and `--fsync-delay-ms 20` (low and high
   end of typical EFS commit latency for small writes; state that these are
   assumptions).

From the numbers, recommend:

- `--follow --interval` for many machines on EFS (keep default 30 s, or
  raise), and whether the 60 s heartbeat is enough to stay under the 600 s
  stale threshold with N clients.
- Whether `APPEND_CHUNK` (8 MiB) needs changing (likely not; say why).
- EFS mount options (`nfsvers=4.1`, `hard`, `noresvport`; General Purpose
  vs Elastic throughput) as general guidance, clearly marked as not measured
  here.
- That `server/tmp` and `store` must be on the same filesystem (already
  enforced) and that the server must be a single process (single-writer
  lock; flock on EFS is supported with NFSv4.1).

## Tests

- `tests/test_bench_server.py` (new): run `scripts/bench_server.py --quick`
  in a subprocess with a timeout of 120 s; assert exit 0 and that the output
  contains the table header. Mark it so it does not slow the default suite by
  more than ~30 s (keep `--quick` small).

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.

## Done when

- `scripts/bench_server.py` exists and its quick mode is tested.
- `docs/SERVER.md` has the results (labelled local / modelled), the
  recommended settings, and the command to reproduce on EFS.
- A Backlog entry for the real-EFS run exists; roadmap status for package 9
  is `done` in the 2.1 PR with that caveat stated.

## Addendum (Nov 6, 2026): tuning found by the benchmark

The object phase showed that the global `objects_lock` in
`server/ingest.py` `_object()` is held across the temp file's `fsync`, the
rename and the directory fsync. With a modelled 20 ms fsync, 8 clients saw
object PUT p50 384.96 ms against 51.79 ms for 1 client (7.4x). Separately,
`Client._request` does not retry 429, so a one-shot `agenticml sync` from
several machines behind one address can fail on the login throttle's brief
429 burst (only `--follow` backs off). Both are fixed in this package:

1. **Object writes** (`server/ingest.py` `_object()`, `server/app.py`):
   `fchmod` and `fsync` the request's private temp file before taking any
   lock. Replace the global lock with one lock per object prefix directory
   (`objects/<hh>`, 256 locks, created lazily under a small dict lock like
   `run_lock`) held only for: the unsafe-destination check, the
   already-present check (`utime` on 200), `mkdir`, `os.replace` and the
   directory fsync. A concurrent upload of the same digest still answers 200
   for the second one and its temp file is removed as today. Keep the
   `objects_lock` attribute only if other code uses it (check
   `grep -rn objects_lock src`), else remove it.
2. **Client 429 retry** (`client.py` `_request`): treat 429 like 503:
   retry up to `max_retries`, sleeping `Retry-After` when present and valid
   (capped at 60 s), else the normal backoff. `whoami`'s explicit 429 raise
   then only happens after retries are exhausted. Remove the benchmark's
   runtime patch of `Client._RETRY_STATUSES`.
3. Re-run the 20 ms configuration for 1 and 8 clients and the local 8-client
   one; update the `docs/SERVER.md` tables and text with before/after
   numbers.

Tests: two concurrent PUTs of distinct objects (different prefixes) overlap
their fsyncs (patch `os.fsync` with an event-based barrier that only passes
when both are inside fsync at once, with a timeout); two concurrent PUTs of
the same digest give one 201 and one 200 and one object file; client retries
429 honouring `Retry-After` (fake server or mocked session) and gives up
after `max_retries`. Second read-only review by Codex, since the object write
path is part of the security-sensitive ingest server.
