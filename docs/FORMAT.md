# AgenticML local store format v1

This is the on-disk contract for the local UI and for `agenticml sync`.
Writers and readers in `agenticml.store` implement this document. A reader
must keep working while a writer is still appending, and a crash must not
make a later append unreadable.

Nothing here is uploaded. Paths below are relative to a root resolved by
`resolve_root`: explicit directory, else `AGENTICML_DIR`, else `~/.agenticml`
(`expanduser` + `resolve`).

## Tree

```
<root>/
  usage.json                     cached total store bytes and computation timestamp
  objects/<hh>/<sha256>          content-addressed bytes, shared by every run
  objects/<hh>/.trash-<sha256>-<pid>  GC candidate awaiting its final mtime check
  lineage/<project-key>.git/     bare shadow Git repository per project
    refs/runs/<run_id>          snapshot commit refs (loose or packed)
    agenticml.lock              init / ref-update flock
  worktrees/<project-key>/<run_id>/  created only by lineage checkout
  runs/<run_id>/
    meta.json
    run.lock                     writer/GC/prune lifetime flock
    events.jsonl
    notes.md
    scalars/index.json
    scalars/<id>.bin             one file per series, <id> is a decimal int
    scalars/<id>.ovw             overview at finish, only if the series has > 65536 points
    scalars/_time.bin            one record per scalar flush
    media/index.json
    sync.json                    sync state and cursor per remote host
```

`<hh>` is the first two hex characters of `<sha256>`. Series ids are small
integers assigned in first-seen order, starting at 0. The filename is the
decimal id with no padding (`0.bin`, `10.bin`). `_time.bin` is not a series
and is not listed in `scalars/index.json`.

Temp files are dotfiles in the destination directory. The name is unique per
process and per thread:

```
.<final-name>.<pid>.<thread-id>.<8 hex>.tmp
```

Object copies that do not yet know their digest use
`objects/.tmp.<pid>.<thread-id>.<8 hex>.tmp`.
Readers never open a name that starts with `.`. Sync must ignore them too,
and must only accept object filenames that match `^[0-9a-f]{64}$` inside a
two-hex directory.

## Identifiers

Run ids and digests are checked before they are joined onto the root. The UI
will pass user-supplied ids in; anything else is rejected with `ValueError`.

| Kind | Pattern |
| --- | --- |
| `run_id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` |
| `sha256` | `^[0-9a-f]{64}$` (lowercase only) |

The first character of a run id is alphanumeric, so `.`, `..`, `.hidden`,
and `-x` cannot pass. `/` and `\` cannot pass either.

`run_dir` and `object_path` then resolve the joined path (`strict=False`)
and require it to stay inside the resolved root. Containment is checked per
path at first use (a later swap of an intermediate directory by a local attacker
is out of scope). The same check is applied to every file path a reader or
writer builds from those directories (`scalars/`, `media/`, series files,
object shards). A symlink at `runs/<id>`, `runs/<id>/scalars`, or
`objects/<hh>` that points outside the root raises `ValueError` before the open
or write. Writer opens of `.bin`, `_time.bin`, and `events.jsonl` use no-follow
semantics (e.g. `O_NOFOLLOW`) to prevent appending through symlinks. A symlink
at an object path is replaced atomically via `os.replace` with the real bytes.

New runs use `YYYYmmddTHHMMSS-<8 lowercase hex>` in UTC (for example
`20261027T021500-ab12cd34`). Any id matching the run-id pattern is accepted
when reopening; the timestamp form is only what `new_run_id` generates.

## `meta.json`

UTF-8 JSON object, keys sorted, indent 2, one trailing newline. Replaced
atomically (see below). Fields:

| Field | Meaning |
| --- | --- |
| `format_version` | `1`. Not changeable after create. |
| `run_id` | Directory name. Not changeable. |
| `project`, `name` | Strings. Default `""`. |
| `task` | String or `null`. |
| `task_source` | `explicit`, `env`, `marker`, `entry`, or `null`. |
| `config` | JSON object. Default `{}`. |
| `tags` | JSON array. Default `[]`. |
| `notes` | String. Default `""`. |
| `group`, `job_type`, `parent_run_id` | String or `null`. |
| `env`, `git` | JSON objects. Default `{}`. |
| `arch` | Optional architecture object; fields below. |
| `lineage` | Optional startup shadow snapshot; fields below. |
| `status` | `running`, `finished`, or `crashed`. |
| `summary` | JSON object. Default `{}`. |
| `created_at` | UTC timestamp, set once at create. |
| `finished_at` | UTC timestamp, or `null` while running. |
| `pid` | Integer pid of the writer process. |
| `hostname` | String or `null`. |

Extra keys from the caller are preserved. Create always forces
`format_version`, `run_id`, `status=running`, `created_at`, `finished_at=null`,
`pid`, and `hostname`, even if the caller passed them.

### Task resolution

A run has a task (e.g. `"asr"`, `"imagegen"`). Lineage graphs and parent inference
are per `(project, task)` instead of per project. Parent inference never crosses
tasks; an explicit `parent_run_id` may cross tasks.

Task resolution evaluates signals at init in order (first match wins, recorded as `task_source`):

| `task_source` | Signal |
| --- | --- |
| `explicit` | `agenticml.init(task="asr")` / `Run(task=...)`. |
| `env` | `AGENTICML_TASK` environment variable. |
| `marker` | Nearest `agenticml.toml` with a top-level `task = "..."`, searched from the entry script's directory (cwd if none) up to and including the project repo root. Must be a regular, non-symlink file of at most 64 KiB; unreadable/invalid files are skipped. |
| `entry` | Entry script's directory relative to the repo root in POSIX form (e.g. `asr/train.py` -> `asr`, `src/tts/train.py` -> `src/tts`). Not used when the script is at the repo root or outside it. |
| `null` | No signal matched; `task` is `null` and the whole project is one stream (previous behaviour). |

Values are stripped; empty, longer than 128 characters, or containing control/format/separator characters (Unicode categories C*, Zl, Zp) are ignored (a warning for explicit/env) and resolution falls through to the next signal. Resume keeps the stored task. Under `agenticml run`, an `init(task=...)` that differs from the launcher run's task logs a warning and the launcher's run is kept.

Older runs without a `task` field: if their `name` is a relative `.py` path with a directory part (the default run name, e.g. `asr/train.py`), the task is read as that directory with source `legacy` (read-time only, nothing is rewritten); otherwise they count as no task.

### `arch`

Optional architecture object recorded on `meta.json` from the first model or loss tracked. The first value wins and is never overwritten:

| Field | Meaning |
| --- | --- |
| `model_class` | `"module.qualname"` string from the first `track_model`. |
| `model_type` | String from a Hugging Face config or `null`. |
| `architectures` | Array of strings from a Hugging Face config or `null`. |
| `loss_class` | `"module.qualname"` string from the first `track_loss`. |

### `lineage`

New runs capture code synchronously at init; resume keeps the existing snapshot.
The user's project and Git index, HEAD, and refs are read-only. A project key is
its lowercase filesystem-safe slug (up to 48 characters, fallback `project`),
then `-` and the first eight hex digits of SHA-256 of the exact project string.
The shadow Git repository is still one per project.
All store paths are containment-checked.

| Field | Meaning |
| --- | --- |
| `version` | `1`. |
| `task` | String or `null`. |
| `commit`, `tree` | Shadow Git object ids. |
| `parent_commit`, `parent_run_id` | Parent snapshot commit and run id, or null. |
| `parent_inference` | `explicit`, `repo_root`, `task`, `project`, or null. |
| `project_key`, `repo_root` | Shadow repository key and absolute source directory. |
| `base_commit`, `base_branch`, `base_dirty` | User Git state, or null outside Git. |
| `files`, `bytes` | Kept file count and total lstat size. |
| `skipped` | `oversized`, `over_total`, and `path_errors` path lists; `over_count` is the number of candidate paths beyond the file-count cap. |
| `error` | Null on success; short diagnostic on failure. |

On failure only `version`, `error`, `parent_run_id`, `parent_inference`,
`project_key`, and `repo_root` are required. Tracking continues without Git.
The shadow commit message gains a `task: ...` line:
`agenticml run <run_id>\n\nproject: <project>\ntask: <task or ''>\nbase_commit: <base_commit or ''>`.

Lineage graphs and parent inference are per `(project, task)` instead of per
project. Parent inference never crosses tasks; an explicit `parent_run_id` may
cross tasks. Parents are explicit (except self-parents), else inferred within
the same project and task: the latest strictly earlier run with the same project,
same task, and same repo root (`repo_root`); else the latest strictly earlier run
with the same project and task (`task` for a non-empty task, `project` when the
task is empty); else no parent. Eligibility requires `(created_at, run_id)`
strictly before the current run, allowing equal timestamps with a smaller run id.
Invalid source paths are skipped. The top-level `parent_run_id` remains
explicit-only. Git ignores, `.agenticmlignore`, hard skip directories/suffixes,
nested Git repositories, and escaping symlinks are excluded. File/total caps are
checked before hashing (defaults 10/100 MiB). Only the first 20,000 candidate
paths are inspected; excess paths are counted without sorting or stat calls.
Individual path errors are skipped and recorded. The entire snapshot shares a
30-second deadline, including lock waits and Git commands. `.agenticmlignore` is
not applied when larger than 1 MiB. The parent ref is resolved inside the
repository lock immediately before `commit-tree` and `update-ref`. If the parent
is still unpublished, the child has no Git parent and `parent_commit` is null;
diff resolves `parent_run_id` live on later calls. Existing commits and metadata
are not rewritten.

CLI: `agenticml lineage ls [--project P] [--task T]` groups output by project,
then `[task]` (`[no task]` for none).

### Dashboard `/api/lineage`

The dashboard endpoint `GET /api/lineage[?project=P]` returns `{nodes: [...], edges: [...]}`:

| Node field | Meaning |
| --- | --- |
| `run_id`, `name`, `project` | Run identifier, run name, and project string. |
| `task` | Task name string (empty string when no task). |
| `task_source` | `explicit`, `env`, `marker`, `entry`, `legacy`, or `null`. |
| `arch` | `arch` object from `meta.json`, or `null`. |
| `arch_changed` | Boolean: true when both own and parent `model_class` are known and different. |
| `cross_task` | Boolean: true when an emitted edge links runs of different tasks. |
| `status`, `created_at` | Effective run status and ISO 8601 UTC creation timestamp. |
| `commit`, `tree` | Shadow Git commit and tree hashes, or `null`. |
| `base_commit`, `base_branch`, `base_dirty` | Base repository Git state, or `null`. |
| `parent_run_id` | Resolved parent run id, or `null`. |
| `parent_inference` | `explicit`, `repo_root`, `task`, `project`, or `null`. |
| `same_tree_as_parent` | Boolean: true when child and parent shadow trees match. |
| `error` | Lineage error diagnostic string, or `null`. |

| Edge field | Meaning |
| --- | --- |
| `from`, `to` | Parent and child run ids. |
| `inferred` | Boolean: true when parent was inferred rather than explicit. |
| `cross_task` | Boolean: true when the emitted edge links runs of different tasks. |

Timestamps are `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC, 6-digit microseconds,
`Z` suffix). The width is fixed so `created_at` sorts lexicographically.
`Z` is the only zone form writers emit. Python 3.9 `fromisoformat` does not
accept `Z`; readers that use it should rewrite the suffix to `+00:00`.

`effective_status` is **not** stored. `list_runs` adds it on the returned
dict (see below).

## `events.jsonl`

Append-only. Each record is one UTF-8 JSON object, compact separators
(`,` and `:`), no pretty-print, terminated by `\n`. Key order is `t`, `kind`,
then the payload keys. Parsers must use names, not positions. Records are
split on the byte `\n` only. `U+2028` and `U+0085` inside a JSON string
(written with `ensure_ascii=false`) are part of that record, not boundaries.

The writer repairs a torn tail once when it opens or resumes the run.
Later appends in that process do not reread `events.jsonl`.

`t` is a Unix timestamp in seconds (`time.time()`, float).

| `kind` | Payload |
| --- | --- |
| `config_update` | `config`: the full new config object, not a diff. Emitted only when a later `set_config` changes the canonical JSON. Create does not emit one. |
| `code` | `manifest`: array of `{path, sha256, size, content_type, source, ...}`. `size` is bytes. Extra keys on the caller's manifest entry are kept. |
| `artifact` | See below. |
| `resume` | `pid` of the process that reopened the run. |
| `finish` | `status` (`finished` or `crashed`) and `summary`. |

There is no media event. Media lives only in `media/index.json`.

`code` may appear more than once. Readers merge by `path`; the later entry
replaces the earlier one entirely (first-seen path order is kept). A file
passed without a matching manifest row is stored as
`content_type=application/octet-stream`, `source=code`.

Artifact object (also the dict `add_artifact` returns; the event adds `t`
and `kind`):

| Field | Meaning |
| --- | --- |
| `name`, `type` | Caller strings. |
| `metadata` | JSON object. |
| `path` | Absolute path of the source file at log time. |
| `size` | Byte size. For `copy=true`, the number of bytes hashed. |
| `mtime` | `stat.st_mtime` (POSIX seconds, float). |
| `sha256` | Digest when `copy=true`, else `null`. |
| `copy` | Boolean. |
| `version` | `1` + the number of earlier artifact events with the same `name`. |

`copy=false` does not hash and does not write an object. The file can change
later; the record is a reference. `copy=true` snapshots the bytes into
`objects/` under `sha256`. The recorded `path` is still the source path.

## `notes.md`

Optional UTF-8 Markdown text with `\n` line endings, capped at 256 KiB.
One note per run, stored at `<root>/runs/<run_id>/notes.md`. A missing
`notes.md` falls back to the `notes` string in `meta.json` in the dashboard.
An empty `notes.md` means the note was cleared, so that launch note stays
hidden. Clearing the note deletes the file when the run has no launch note.
Notes stay local: `agenticml sync` does not upload them, and `agenticml gc`
deletes them together with the run.

The dashboard is read-only except for notes. Writes use
`PUT /api/runs/<run_id>/notes`, which requires a same-origin request and the
`X-AgenticML-Request: 1` header. Reads use `GET /api/runs/<run_id>/notes`
and `GET /api/notes[?project=P]`. `agenticml ui --read-only` disables note
editing.

## Scalars

### `scalars/index.json`

Atomic JSON, same encoding as `meta.json`:

```json
{
  "version": 1,
  "series": {
    "loss": {"id": 0, "monotonic": true}
  }
}
```

`id` is the series id used in the filename. `monotonic` is true when every
appended step is **greater than or equal to** the previous appended step.
Equal steps stay monotonic. The first step that is strictly less flips the
flag to false permanently. The index is replaced **before** the bytes that
flip it are appended, so a crash cannot leave `monotonic: true` on a file
that already contains a decrease. A reader that sees `false` and data that
is still sorted simply scans; that result is correct.

Keys are arbitrary strings. Non-string keys are dropped.

### `<id>.bin` and `_time.bin`

Little-endian records, no padding, no header:

```
struct "<If"    uint32 step | float32 value     8 bytes
```

Example: step `2`, value `1.0` is the eight bytes `02 00 00 00 00 00 80 3f`.

`<id>.bin` holds the points of that series, in append order (not necessarily
step order). Values are IEEE-754 binary32. Non-finite values (NaN, ±inf) are
stored as-is. A finite value that does not fit in binary32 is stored as a
signed infinity (and the writer warns once for that key).

A point is dropped, not stored, when:

- the step is not an integer in `0 .. 2**32-1` (bools are not steps; numpy
  integer scalars are; floats and numeric strings are not), or
- `float(value)` fails, including `OverflowError` (for example `10**1000`),
  or the value is a `str` / `bytes` (so `"1.0"` is dropped even though
  `float("1.0")` would succeed). Bool values are kept as `0.0` / `1.0`.
  A value that becomes a Python float but does not fit in binary32 is not
  dropped; it is stored as a signed infinity (see above).

The first dropped point for a key logs one warning. Later drops of that key
on the same writer instance do not. The set is not persisted across resume.

`_time.bin` has one record per `write_scalars` call that stored at least one
point. `step` is the maximum accepted step in that call. `value` is seconds
since `created_at`, as float32. Calls that are empty or whose points were
all dropped do not append a time record. This is one sample per flush, not
per point.

Writers do not fsync these files on the append path. Durability of a crash
is the torn-tail rule, not `fsync`. Handles are unbuffered so a reader in
another thread sees bytes as soon as `write` returns. `write_scalars` is
all-or-nothing on disk: if an error occurs while appending series or
`_time.bin`, touched files are truncated back to their pre-batch sizes and
in-memory state is restored before re-raising.

At most 256 series file handles are kept open. The first 256 series that
receive points keep a handle. Later series are opened, appended, and closed
on each call. `_time.bin` has its own handle and does not count toward 256.

### `<id>.ovw`

Written by `finish`, one overview per series, built by replaying `<id>.bin`
(torn tail ignored) through the overview rules below. Replaced atomically
and fsynced. Not read while `status` is `running`.

`finish` writes `<id>.ovw` only when the series has strictly more than 65,536
points (`OVERVIEW_MIN_POINTS = 65536`). Below or at that threshold, no `.ovw`
is written; the reader builds the overview from `<id>.bin` on demand.
Any stale `<id>.ovw` for a series that is now at or below the threshold is
removed only if its `<id>.bin` exists.

A later prune (`sync --prune`) may delete `<id>.bin` only for series that have
a valid `.ovw` whose point count equals the `.bin` complete-record count,
after a run is complete (finalized and fully synced) for the host.
`store.gc.prunable_series(root, run_id)` returns only matching series of a
finished/crashed run. `prune_run(root, run_id, keys, expected_fingerprint=None)` takes `run.lock`,
checks the optional fingerprint against `run_fingerprint` (sync passes its saved
`source_hash`, and a mismatch raises `ValueError("run changed since sync; not pruning")`),
revalidates all requested keys before deleting any file, and rejects a held
lock or a key with a missing, corrupt, or mismatched overview with `ValueError`.
Small series keep their `<id>.bin` (at most 65,536 records * 8 bytes =
524,288 bytes, ≤ 512 KB each). The `scalars/` directory is fsynced after the `.bin`
files are removed, and `"pruned": true` is recorded in `sync.json`.
Metadata, events, `scalars/index.json`, overviews, and the media index are preserved.
The local UI continues to serve the run from `.ovw` files.

`finish` does not replace an existing `<id>.ovw` when `<id>.bin` is absent.
That keeps a pruned series from being rewritten as an empty overview.
`resume` raises `ValueError` with the message `run <id> was pruned after
sync; it cannot be resumed` when any series has a `.ovw` and no `.bin`.

On a `finished` or `crashed` run, a valid `.ovw` is authoritative for
overview queries when the `.bin` complete-record count equals the overview
total. A missing or unreadable overview is ignored and the reader rebuilds
from the `.bin`. If the `.bin` is missing (`sync --prune` deletes it after
the overview is durable), the reader serves that series from the `.ovw`
alone. Queries that would have been raw, and queries that pass `start` /
`end`, then return the overview as well, with `mode` `overview` (the range
is not re-applied; the raw points are gone). If the `.bin` exists but its
complete-record count disagrees with the overview total, the `.bin` is the
source of truth: the reader logs a warning and rebuilds from it.

## Overview bytes

A series chart stores at most `max_buckets` buckets (writers use 2048).
Width `W` is a power of two, starting at 1. Point `step` belongs to bucket
`step // W`. When a step `>= max_buckets * W` arrives, `W` doubles and
bucket `2i` is merged with `2i+1` into `i`, repeating until the new step
fits. Empty buckets are not stored.

Merge of two buckets:

| Field | Rule |
| --- | --- |
| `step_min`, `vmin` | min |
| `step_max`, `vmax` | max |
| `vsum` | float64 sum, left bucket then right |
| `count` | sum of counts |
| `last`, `seq` | taken from the bucket with the greater `seq` |

`last` is the finite value most recently **appended** into that step range,
not the value at `step_max`. `seq` is the 1-based count of finite points at
the moment that value was appended. Non-finite values increment `nonfinite`
and `point_count` and do not enter a bucket, so they do not move `seq`.

`vsum` matches a direct group-by for values whose partial sums are exact in
float64 (integers inside the 53-bit mantissa). Very large magnitudes can
differ in the last bits from a fully interleaved sum; bounds, counts, and
`last` do not.

The file is little-endian, standard size (no alignment padding).

Header, 40 bytes, `struct <4sHHIIQQQ`:

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | 4 bytes | magic `AMLO` |
| 4 | uint16 | version `2` |
| 6 | uint16 | reserved, writers store `0` |
| 8 | uint32 | `max_buckets` |
| 12 | uint32 | `bucket_count` (number of records after the header) |
| 16 | uint64 | `width` (`W`) |
| 24 | uint64 | `point_count` (finite + non-finite) |
| 32 | uint64 | `nonfinite` |

Then `bucket_count` records of 36 bytes, `struct <IIIIfffd`, sorted by
`index` ascending:

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | uint32 | `index` (`step // width`, and `< max_buckets`) |
| 4 | uint32 | `step_min` |
| 8 | uint32 | `step_max` |
| 12 | uint32 | `count` (at least 1) |
| 16 | float32 | `vmin` |
| 20 | float32 | `vmax` |
| 24 | float32 | `last` |
| 28 | float64 | `vsum` |

`step_min // width` and `step_max // width` both equal `index`. The file
length is exactly `40 + 36 * bucket_count`. `mean` is not stored; it is
`vsum / count`.

Stored bucket bounds `vmin`, `vmax`, and `last` are binary32 (float32).
To guarantee that stored bucket bounds enclose all true values, writers
use directed rounding: after packing to float32, if the float32 `vmin` is
greater than the true `vmin` it is stepped down by one float32 ULP; if
the float32 `vmax` is less than the true `vmax` it is stepped up by one
float32 ULP. This preserves the invariant `vmin <= vmax` while ensuring
`stored_vmin <= true_vmin <= true_vmax <= stored_vmax`.

`from_bytes` rejects a file whose buckets are not semantically valid:
`vmin`, `vmax`, `vsum`, and `last` must be finite; `vmin` must not exceed
`vmax`; `count` must be at least 1; `step_min` must not exceed `step_max`;
each step must fall in that bucket's index; indexes must be strictly
increasing (no duplicates, no unsorted order). Version must match `2`
(older versions such as `1` are rejected, prompting readers to rebuild from
`.bin`). The sum of bucket `count` fields must equal `point_count - nonfinite`
(non-finite points are not stored in buckets). A reader that cannot parse the
overview rebuilds from `<id>.bin`. If that file is missing too, `series`
returns an empty result and logs a warning.

Downsample (read time, not a file) merges that sorted list into at most
`max_points` contiguous groups. Group `i` of `m` groups over `n` buckets
covers indexes `[floor(i*n/m), floor((i+1)*n/m))`. The group's mean is
`sum(vsum) / sum(count)`, not the mean of means.

`from_bytes` rebuilds `seq` as cumulative count across buckets. For non-monotonic
series where later points may land in earlier buckets, reconstructed `seq`
values reflect bucket index order rather than the original append order; as a
result, `last` after `downsample()` may differ for non-monotonic series.

## `media/index.json`

Atomic JSON object. Keys are media keys. Each value is an array of rows, in
log order. New rows have an integer `id`, starting at 0, unique and increasing
across all media keys in the run. IDs survive thinning unchanged. The writer
recovers the next ID as max retained ID + 1 on resume; every key retains its
newest entry, so thinning cannot discard the highest assigned ID. A failed
index write rolls back the row without consuming an ID. Legacy rows without
`id` remain readable; they are not renumbered. `RunReader.media()` remains a
dict of key → list of rows.

At most `MEDIA_MAX_PER_KEY = 64` entries per key are retained in `media/index.json`
(overridable via `AGENTICML_MEDIA_MAX_PER_KEY`). When an additional entry arrives
(e.g., the 65th), the index is thinned evenly by step: every other entry is dropped,
keeping the first logged entry and the newest entry (the newly added one is always kept),
so coverage stays spread across the run. Dropped entries are removed from the index;
their underlying blobs remain in `objects/` until swept by garbage collection.

Before storing media bytes, if Pillow is installed and the content type is `image/*`,
the image is downscaled if its longer side exceeds `MEDIA_MAX_PX = 1024`
(overridable via `AGENTICML_MEDIA_MAX_PX`). The downscaled image is re-encoded as PNG,
or WEBP if supported and smaller. Without Pillow, images are stored as-is.

```json
{
  "pred": [
    {"id": 0, "step": 3, "sha256": "<64 hex>", "content_type": "image/png", "caption": "first"}
  ]
}
```

`caption` may be `null`. `step` is an integer (not a bool) and is not
range-checked; media is not a `<If` record. The bytes live in `objects/`
under `sha256`. The same bytes logged twice share one object and produce
two rows.

## Objects

`put_bytes` / `put_file` hash with SHA-256. `put_file` reads the source in
1 MiB chunks and hashes while copying into a temp file under `objects/`,
then `os.replace`s into `objects/<hh>/<sha256>`. The temp file is fsynced
before the name is published, so a name that exists refers to the full
contents. If the digest is already present the temp file is deleted and the
existing inode has its `mtime` refreshed with `os.utime`. This refresh also
applies when another writer publishes the digest during the copy, extending
the GC grace period for reused bytes until their new reference is committed.

A crash before replace leaves a dotfile temp and no new name. An unreferenced
object (crash after publish, before the event or media row) is harmless:
digests are shared, and a later put of the same bytes finds it. Sync should
not delete objects just because one run's manifest is mid-write.

Directory entries are not fsynced on each put. Process crash cannot publish
a torn object (the bytes are in the page cache before the name exists, and
were fsynced). A power loss in the instant after publish can still drop the
new directory entry; the object then looks like the put never happened.

## Atomic files and append files

Atomic (temp in the same directory, `flush`, `os.replace`):

- `meta.json`
- `scalars/index.json`
- `media/index.json`
- `scalars/<id>.ovw`

`os.replace` on one filesystem is atomic. A reader opens either the previous
inode or the new one, never a half-written JSON or overview. `finish` also
fsyncs the temp file before replace for `meta.json` and `*.ovw`. Other meta
and index updates are flushed to the page cache but not fsynced: a concurrent
reader on the same machine sees the full new file; a power loss can lose the
last update and keep the previous file.

Append-only, no per-write fsync:

- `events.jsonl`
- `scalars/<id>.bin`
- `scalars/_time.bin`

`finish` fsyncs the scalar files, overviews, `events.jsonl`, the `scalars/`
directory (after the `.ovw` renames), the `media/` directory (so the media
index name is durable), and the run directory (after `meta.json` is
replaced). If a directory fsync fails, finish still completes (the files
were already fsynced) and logs a warning.

## Crash and torn tails

A failed event append (including a partial write followed by ENOSPC) truncates
`events.jsonl` to its pre-append size before propagating the error. A later
event therefore begins at the original record boundary. If that rollback's
`ftruncate` also fails, the writer clears its healed flag: the next append
must heal the remaining tail before writing, even within the same open writer.

| When the process dies | What is on disk | What a reader does |
| --- | --- | --- |
| Middle of a `.bin` / `_time.bin` write | A prefix of the write, length maybe not a multiple of 8 | Uses `size - size%8` bytes. Ignores the partial tail. Does not truncate. |
| Next append after that crash | Writer truncates the file to a multiple of 8, then appends | Records stay aligned. The torn fragment is gone. |
| Middle of an event line | Partial last line, no trailing `\n` | If the last line is not valid JSON, ignore it with no warning. An unparseable **earlier** line is skipped and a warning is logged. |
| Next open or resume | If the tail is valid JSON missing `\n`, writer adds `\n`. If it does not parse, writer truncates back to the previous `\n`. This heal runs once per open, not on every later append. | The glued-line failure cannot happen. |
| Middle of an atomic replace | Dotfile temp; previous file intact | Reader opens the previous file. |
| `finish` after `.ovw` files exist, before `meta.json` says finished | Status still `running` | Reader ignores `.ovw` and rebuilds from `.bin`. |
| `finish` after meta is `finished` / `crashed`, before the `finish` event | Status is terminal; event may be absent | Trust `meta.json`. |
| Exit without `finish` | `status` stays `running` | `list_runs` reports `crashed` when the lifetime lock is free and its heartbeat is at least 120 seconds old (below). |

A `.bin` whose size is a multiple of 8 has no torn tail. Readers never
repair files. Only the writer truncates, and only a torn tail, and only
before its own next append.

`read_records(key, offset)` returns complete records with `offset <= byte < new_offset`,
and `new_offset` is a multiple of 8 at or before the complete prefix.
Sync must persist `new_offset`, not the raw file size (the raw size includes
a torn tail the cursor must not step into). `offset` itself must be a
non-negative multiple of 8.

Each sync pass snapshots every series' complete `.bin` length before its
first request and reads only those prefixes. New keys and appended records
wait for the next invocation (or `--watch` pass), so continuous training cannot
keep a pass running forever. Each scalar round loads at most 200,000 records
total across all keys.

## Who may write

`RunWriter.create` and `resume` acquire an exclusive, non-blocking
`fcntl.flock` on `<run_dir>/run.lock` before reading or changing run state.
They hold the descriptor for the writer's lifetime, releasing it after a
successful `finish`, on `close`, on failed create/resume, or at process exit.
`close` releases ownership without marking the run finished. A locked create or
resume retries ten times over about 0.5 seconds (a reader's shared lock probe
may be brief), then raises `ValueError("run <id> is open in another process")`.
`layout.try_lock_run(run_dir)` uses the same file and returns a handle or
`None` if held; `layout.unlock_run(handle)` releases it. GC and prune use
these helpers to coordinate with resumed writers. Windows uses no-op locks
because `fcntl` is unavailable. The lock file itself is persistent: its
presence does not mean the run is locked.

Each lock handle records its owner pid. `unlock_run` issues `LOCK_UN` only
in that process; elsewhere it just closes the descriptor, and already closed
handles are safe. A single module-level `os.register_at_fork` child hook closes
all inherited run-lock handles tracked in a `WeakSet`, without unlocking.
Thus child close, garbage collection, or normal exit cannot unlock the parent,
and a surviving worker cannot keep a dead parent's run locked. Inherited
writers do not retain ownership; children must acquire their own run lock
before writing.

Threads in the writer process share a separate threading lock. `RunReader`
is thread-safe: several threads may call `series` on one reader while the
writer appends. Its overview cache is locked, so the same appended bytes are
folded in once. Two writer processes on the same run are not supported.

`finish` is idempotent in the process that called it: a second call returns
without changing status, summary, or events. `status` passed to `finish`
must be `finished` or `crashed`. After finish, further writes raise
`RunFinishedError` (a `RuntimeError` subclass exported from `agenticml.store`).
`resume` of a finished run sets `status` back to `running`, sets
`finished_at` to null, refreshes `pid` and `hostname`, leaves `created_at`
alone, and appends `resume`. `last_step` on the writer is the max step across
series after that reopen (last record only, when `monotonic` is still true).
`resume` raises `ValueError` (`run <id> was pruned after sync; it cannot be
resumed`) when any series has a `.ovw` and no `.bin`.

Create raises `FileExistsError` if `meta.json` already exists for that id.
`resume(..., must=True)` raises `FileNotFoundError` when the run directory
is absent; `must=False` returns `None` so the caller can `create(run_id=...)`.
A directory that exists without `meta.json` is not a run and raises
`FileNotFoundError` either way. A path that exists and is not a directory
raises `NotADirectoryError`.

## How to read

`list_runs(root)` reads only each run's `meta.json`. It skips names that fail
`valid_run_id`, entries that are not real directories (symlinks are not
followed), symlinked `meta.json` files (a symlink at `meta.json` is refused
and skipped), and meta that is missing or not a JSON object (or fails to parse,
such as exceeding integer digit limits or recursion depth). The `run_id` comes
from the directory name (if `meta.json` contains a mismatched `run_id`, a
warning is logged and the directory name is used). Results are sorted
by `created_at` descending, then `run_id` descending. Runs with a missing
`created_at` sort last.

The writer refreshes the mtime of `run.lock` on create/resume and every flush
interval, even without metrics or system sampling. For stored
`status == "running"`, `effective_status` is `running` if the lock is held OR
the heartbeat is less than 120 seconds old. A free lock alone does not prove
death: NFS `local_lock=flock|all` can hide another client's lock. Only a free
lock AND a heartbeat at least 120 seconds old yield `crashed`. If locking is
unsupported (Windows, ENOLCK, ENOSYS, EOPNOTSUPP) or a probe fails, an old
heartbeat yields `stale`; a fresh heartbeat still yields `running`. Missing
heartbeats count as old; an unreadable heartbeat is treated conservatively as
running. Bad lock files are logged once per run and probes never break listing.
Readers probe without creating files or rewriting metadata. Sync takes the run
lock and rechecks metadata and liveness before finalizing, retaining the lock
through the request. It finalizes dead runs as `crashed`, preserving summary
and adding `_exit_reason: "process died"`. Stale runs are never finalized.
Stored `finished` and `crashed` statuses remain terminal.

`series(key, max_points=1000, start=None, end=None)`:

- `start` and `end` are inclusive. Omit either side to leave it open.
  `start > end` is an empty raw result.
- If the selected records number `<= max_points`, `mode` is `raw` and
  `points` is `[[step, value], ...]` in **file order** (not sorted by step).
- Otherwise `mode` is `overview` and `points` is
  `[[step_min, step_max, vmin, vmax, mean, last], ...]`, with at most
  `max_points` buckets after downsample.
- `total` is the number of selected records, including non-finite values.
  `nonfinite` counts those. Raw points include NaN and inf. Overview
  buckets do not.
- A full-series read (`start` and `end` both omitted) of a `finished` or
  `crashed` run loads `<id>.ovw` when it parses and its point total equals
  the `.bin` complete-record count. That file is authoritative for the
  overview. A running run, a bad or missing overview, or a `.bin` whose
  record count disagrees (a warning is logged) folds `<id>.bin` and caches
  the overview with the byte offset so the next call reads only new
  complete records.
- If `<id>.bin` is missing and `<id>.ovw` parses, `series` answers from the
  overview alone. That includes a query with `start` / `end`, and a query
  small enough that it would have been `raw`. `mode` is `overview`.
- When `<id>.bin` is present, a range does not use that cached full-series
  overview (a bucket can contain steps outside the range). The reader stats
  the `.bin` size first, then reads `monotonic` from `index.json`. A flag
  that is still true means the prefix up to that size is sorted: the writer
  clears the flag before it appends an out-of-order record. On a monotonic
  series the range is a binary search (`os.pread` of the uint32 step) over
  the fixed-width records, O(log n) plus a read of the selected slice. On a
  non-monotonic series the file is scanned. Both return the same multiset of
  points; file order can differ, and overview `last` follows each file's
  append order. Bounds, min, max, count, and (for exactly representable
  values) mean match.
- A terminal overview is cached under `(status, .ovw size, .ovw mtime_ns,
  .bin size)`. Any change drops the entry, so resume, a rewritten `.ovw`,
  or a pruned `.bin` is not served from the previous cache. A running
  series still extends its incremental overview by byte offset while the
  file only grows. Each reader keeps at most 32 cached series in LRU order;
  a terminal series retains only its parsed `.ovw`, replacing any incremental
  overview built while it was running.

Unknown series keys raise `KeyError` from `series` and `read_records`.
`media(key)` returns `[]` when the key has no rows. `media()` with no key
returns the whole index object.

`code_manifest()` is the merged code list. `artifacts()` is the artifact
events in order. `time_index()` is `[(step, seconds), ...]` from `_time.bin`
with a torn tail dropped. `events()` applies the torn-line rules above.

## Storage Usage and Garbage Collection

### `usage.json` and Budget Check

The store maintains a cached `<root>/usage.json` file:

```json
{
  "bytes": 104857600,
  "computed_at": 1700000000.0
}
```

- `check_budget(root)` reads `<root>/usage.json`. If missing or older than 1 day
  (86,400 seconds), it starts a daemon to walk directory trees with `os.scandir`
  (summing `st_size` without hashing), update the cache atomically, and return
  immediately. A subsequent check can warn from the refreshed cache.
- `finish` adds the run directory's size delta to the cached total. The cache
  also stores per-run sizes in `runs` to avoid counting resumed runs twice;
  this update preserves `computed_at` so a full refresh still runs daily.
- If total usage exceeds `AGENTICML_MAX_BYTES` (default 5 GiB, 5,368,709,120 bytes),
  it returns a warning string suggesting `agenticml du`, `agenticml gc`, or
  `agenticml sync --prune`.
- Unsynced data is never deleted automatically.

### Garbage Collection (`collect`)

`agenticml gc` and `agenticml.store.collect` clean up storage:

- **Run selection**:
  - Eligible runs must have terminal or abandoned status: `finished`, `crashed`, or
    `stale`. Active runs (`running`) are **never** deleted.
  - GC takes `run.lock` before deciding on or deleting each run, then re-reads
    its status. A held lock skips the run with an "in use" message and lists
    it in the report's `in_use` field, including a locked stale run.
  - By default, only fully-synced runs (where a host in `sync.json` has
    `complete: true` and `source_hash` equal to `store.gc.run_fingerprint(root, run_id)`)
    are selected. `--include-unsynced` allows selecting unsynced runs and is
    required for stale runs, whose heartbeat must be at least 120 seconds old.
  - `--keep-last N`: Protects the N newest runs per project from deletion.
  - `--older-than DAYS`: Protects runs newer than the specified age.
  - Symlinks are never followed during run directory traversal or deletion.
- Deleting a run also removes its shadow `refs/runs/<id>` and default checkout
  at `<root>/worktrees/<project-key>/<run_id>`. Custom `--path` directories are
  user-owned: GC never deletes them and reports their paths in `worktrees_kept`
  and CLI output, even if the checkout has new commits or edits. Each checkout
  records its run id in `<repo>/worktrees/<name>/agenticml-run`, independently of
  HEAD. Existing checkout destinations are accepted only when registered at
  the requested run's commit. Dry-run previews removals and kept paths.
  Non-dry-run GC also retries cleanup of orphan `refs/runs/<id>` whose run
  directories no longer exist, and logs cleanup failures for later retry.
  Shadow object storage is retained; parent commits may still be reachable.
  `usage()` includes `lineage_total` and `worktrees_total` bytes in `total`.
- **Mark-and-sweep object sweep**:
  - Referenced objects are determined by taking the union of digests across all
    surviving runs: `media/index.json` sha256s, code manifest sha256s, and copied
    artifact sha256s from `events.jsonl`. This extraction is shared with
    `RunReader.referenced_shas()` and raises on unreadable input. It retains
    references from all code events, including superseded paths.
  - The sweep fails closed: if any surviving run's metadata, events, media index,
    or inline code manifest is unreadable, missing, or corrupt, no objects are
    deleted. This includes run directories omitted by `list_runs` due to bad
    metadata. The warning and report's `sweep_error` name the run; completed
    run deletions are still reported. A corrupt torn event tail also aborts
    the sweep, even though display readers tolerate it.
  - Unreferenced objects in `objects/` are swept.
  - **1-hour grace period**: Unreferenced objects whose modification time (`mtime`)
    is 1 hour old or less are kept, protecting newly written objects that have not yet
    been committed to an event line or index file by a concurrent process.
    Dedup refreshes this timestamp. After an initial re-stat, GC renames each
    candidate to `objects/<hh>/.trash-<sha256>-<pid>` and re-stats the renamed
    inode. If its mtime is within the grace window, GC renames it back; if a
    writer has already recreated the original name, GC deletes only the trash
    copy. Otherwise GC unlinks the old trash file. A writer encountering the
    missing original publishes fresh bytes, including when GC renames it
    between the writer's existence check and dedup `utime`.
  - Stale `.trash-*` regular files older than the grace window are removed
    during the sweep; fresh trash and symlinks are preserved. Object listings
    and object usage totals ignore trash names. Cleanup contributes to the
    report's object deletion count and freed bytes, including dry-run estimates.
  - Empty shard directories (`objects/<hh>`) are cleaned up.
  - Dry-run mode calculates counts and freed bytes without deleting files.

### Sync source fingerprint

`store.gc.run_fingerprint(root, run_id) -> str` is shared by sync and GC.
It returns the hex SHA-256 of canonical JSON (sorted keys, compact separators,
UTF-8 without ASCII escaping) containing, in order: meta status, summary,
config; the `scalars/index.json` series dictionary; sorted `(filename, size)`
pairs for every scalar `.bin` (including `_time.bin`); `events.jsonl` size;
and `media/index.json` size. A missing sized file is represented by null.
It excludes `sync.json`, overviews, lock files, and mtimes. Pruning changes
the fingerprint, so sync must save the fingerprint again after pruning.
Missing or mismatched `source_hash` is never considered fully synced, even
when `complete` is true.
