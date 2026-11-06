# Package 8: By-reference artifacts

Nov 6, 2026 · Supervisor plan · Release 2.1 · Coder: Cursor · Size: S–M

Source: [on-prem-roadmap.md](../on-prem-roadmap.md) (2.1, package 8) and
[on-prem-server-plan.md](../on-prem-server-plan.md) section 4.2 (features).
Builds on [7-lineage-replication.md](7-lineage-replication.md), which added
the first feature (`lineage`), the feature-aware idle check and the
kind-scoped `_remember_skip`. Base branch: `dev`. Ships in the single 2.1 PR,
one commit per package.

## Goal

`agenticml sync --artifacts` uploads the bytes of `copy=False` artifacts
whose source file is unchanged since it was logged, so they can be
downloaded from the server dashboard. The mapping from artifact to object
lives in a new allow-listed atomic run file, `artifacts.json`, which only the
server stores. The server advertises the `byref` feature.

Done when a by-reference artifact can be downloaded from the server
dashboard, and a changed or missing source is skipped with one warning.

Out of scope: uploading directories, re-hashing artifacts that changed (they
are skipped, not re-versioned), downloading by-reference artifacts from the
local dashboard (it never serves arbitrary local paths), deleting objects.

## What exists today

- `RunWriter.add_artifact` (`store/writer.py`) appends an `artifact` event:
  `{name, type, metadata, path (absolute), size, mtime (st_mtime float),
  sha256 (null when copy=False), copy, version}`. `version` is 1 + earlier
  events with the same name, so `(name, version)` is unique in a run.
- `RunReader.artifacts()` (`store/reader.py`) returns the artifact events.
  `run_references` / `references_from_bytes` collect referenced object
  digests (only `copy=True` artifacts), used by `gc` and by sync's object
  push.
- The dashboard run page (`ui/static/app.js`, around line 3063) lists
  artifacts (name, version, `latest` chip, type, size) without a download
  link, even for `copy=True` ones. `/api/objects/<sha>?type=...` streams an
  object.
- After package 7: `whoami` returns `features`; sync gets them through
  `_identity(client)`; `_sync_idle`, the "up to date" return and completion in
  `_sync_locked` already consult a per-feature pending check;
  `_remember_skip(state, kind, id, reason)` is scoped by kind.

## Files to touch

- `src/agenticml/server/ingest.py`: allow `artifacts.json` as an atomic file
  (and in `_state` and commit `sizes`), validate it, add `byref` to
  `features`.
- `src/agenticml/store/reader.py`: merge `artifacts.json` into
  `artifacts()`; include its digests in `run_references` so `gc` on a server
  store keeps them.
- `src/agenticml/sync.py`: `--artifacts` behaviour; `src/agenticml/cli.py`:
  the flag on `p_sync`.
- `src/agenticml/ui/static/app.js` (and `app.css` only if needed): a
  Download link for artifacts that have a `sha256`.
- Docs: `docs/PROTOCOL.md` (feature, file format, client steps),
  `docs/FORMAT.md` (`artifacts.json` is server-only, like `remote.json`),
  `docs/SERVER.md` (remove "By-reference artifacts" from unsupported
  features; describe `--artifacts`), `docs/SDK.md` sync flag line.
- Tests: `tests/test_server_byref.py` (new) and a testbed UI test for the
  Download link (`testbed/tests/`, marked `ui`).

## `artifacts.json`

```json
{"version": 1, "artifacts": [
  {"name": "model", "version": 2, "sha256": "<64 hex>", "size": 1234}
]}
```

Server validation (422 `invalid artifacts.json` otherwise): JSON object,
`version == 1`, `artifacts` a list of at most 10,000 objects, each with
exactly these keys: `name` a non-empty string ≤ 1,024 chars, `version` an
int ≥ 1 (not bool), `sha256` valid per `layout.valid_sha`, `size` an int ≥ 0
(not bool); no duplicate `(name, version)`. The atomic body limit (16 MiB)
applies. Objects need not exist yet at PUT time (sync uploads them first
anyway); readers tolerate a missing object.

Readers (`RunReader.artifacts()`): if `artifacts.json` exists in the run
directory and is valid, every artifact event with `copy` false and a matching
`(name, version)` gets `sha256` and `size` from the mapping and
`"byref": true`. Invalid or missing file: events unchanged. A local store
never has this file; if a user creates one by hand it is still only display
metadata, so no extra guard is needed beyond validation.

## Client (`sync --artifacts`)

- The flag is accepted with `--all`, explicit ids, `--watch` and `--follow`
  (pass it through `_follow`). Without it, nothing changes. When the server
  does not list `byref`, print one warning per invocation
  (`server does not support --artifacts; skipping by-reference artifacts`)
  and continue normally.
- Candidates: artifact events in the snapshot's `events.jsonl` bytes with
  `copy` false, a string `path`, int `size`, numeric `mtime`, string `name`,
  int `version`.
- For each candidate, keyed `"<name>:v<version>"`:
  - Already in `state["byref"]` with the same `path`, `size`, `mtime`: reuse
    its `sha256` (no re-hash).
  - Already skipped (`kind == "byref"`): leave it skipped.
  - Else `os.stat(path)` (follow symlinks; it is the user's own file). If
    missing, not a regular file, or `st_size`/`st_mtime` differ from the
    event: warning `artifact <name>:v<n>: source changed or missing`,
    `_remember_skip(state, "byref", key, reason)`. That is the "one warning":
    remembered skips are not warned again on later passes.
  - Else hash the file (stream, `hashlib.sha256`), then stat again; if it
    changed during hashing, skip as above.
- Upload: `client.missing(shas)` then `client.put_object(sha, handle,
  size)` streaming from the source path. 422 `hash mismatch` (file changed
  during upload) → skip as above. 413 → skip with reason `too large`.
  Transient errors propagate (the run stays incomplete, retried next pass).
  Record `state["byref"][key] = {path, size, mtime, sha256}` after success.
- Then PUT `artifacts.json` (sorted by name, version) as an atomic file with
  the same cursor logic as other atomic files (`state["files"]
  ["artifacts.json"]` sha256), include its size in the commit `sizes`. Only
  put it when it has at least one entry. Push it after the other atomic files
  and before `commit`.
- Completion and idleness: with `--artifacts` and the feature listed, a run
  is pending while any candidate is neither in `state["byref"]` (with a
  matching entry in the last pushed `artifacts.json`) nor skipped. Use the
  same mechanism package 7 added for lineage, so a run completed earlier
  without `--artifacts` becomes non-idle once, uploads, then is idle again.
- `--dry-run --artifacts` counts candidates that would be uploaded (no
  hashing, no server calls) and adds them to `objects`.
- 404 recovery (`missing_retried`): also forget `artifacts.json` cursor so
  it is resent; keep `state["byref"]` (objects are content-addressed).
- `run_fingerprint` and local files are unchanged; `gc` on the client is
  unaffected.

## Dashboard

- In the artifact list, add a link `Download` (class matching other inline
  links) for every artifact with a valid 64-hex `sha256`:
  `href=/api/objects/<sha>?type=application/octet-stream`,
  `download=<basename of artifact.path, else name>`. This also gives
  `copy=True` artifacts a download link locally, which is intended.
- Keep the row from overflowing at 375px (long names already wrap; the link
  must not force a horizontal scroll).

## Constraints and conventions

- Server stays standard-library only. No new endpoints: `artifacts.json`
  goes through the existing atomic route and objects through the existing
  object routes.
- 2.0 clients and servers keep working: the client only writes
  `artifacts.json` when the server lists `byref`.
- Match surrounding style. Keep docs edits short.

## Edge cases to test

1. End to end against a real server (port 0): log a `copy=False` artifact,
   `sync --artifacts`, then the server's `/api/runs/<id>` lists the artifact
   with `sha256` and `byref: true`, and `/api/objects/<sha>` (with an admin
   session, as other server web tests do) returns the original bytes.
2. Source modified after logging (size or mtime): one warning, skipped,
   run completes; a second pass prints no warning and uploads nothing.
3. Source deleted: same as 2.
4. Without `--artifacts`: no `artifacts.json`, behaviour unchanged.
5. Server without `byref` (monkeypatch features): one warning, no upload.
6. Backfill: run synced complete without `--artifacts`; next
   `sync --all --artifacts` uploads and puts `artifacts.json`; third pass is
   idle.
7. Re-run with unchanged source reuses the cached hash (patch the hash helper
   or count reads).
8. Server validation: bad JSON, wrong version, duplicate entry, bad sha,
   bool version, too many entries → 422; valid file accepted and listed in
   `/state`.
9. `run_references` on the server store includes the byref digests, so
   `gc` would not delete them.
10. File changed between hash and upload (simulate 422 hash mismatch):
    skipped with one warning.
11. UI (testbed, `-m ui`): Download link present for a `copy=True` artifact
    on the local dashboard, absent for `copy=False` without mapping; no
    horizontal overflow at 375px.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.

## Done when

- The tests above pass, including the end-to-end download and the
  one-warning skip.
- Docs updated; roadmap status for package 8 is `done` in the 2.1 PR.
