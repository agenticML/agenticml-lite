# Package 41: Import runs from TensorBoard, MLflow, W&B and CSV

Nov 28, 2026 · supervisor plan · release 3.4

## Goal

The owner's request (Nov 28, 2026): "give path to tensorboard logs, and
experiment directory, and we port the metrics into the agenticml dashboard",
then "we basically want it to be exported, and save it under agenticml
directory", and "i also want to support multiple exports from mlflow, wandb,
and other stuff".

One command reads another tracker's logs on disk and writes each source run
as a normal, finished v1 run in the local store (`--dir`, else
`AGENTICML_DIR`, else `~/.agenticml`):

```
agenticml import tensorboard ./runs            --project resnet
agenticml import mlflow      ./mlruns          # or ./mlflow.db
agenticml import wandb       ./wandb
agenticml import csv         ./lightning_logs/version_3/metrics.csv
agenticml import wandb       entity/project    # cloud, needs the [wandb] extra
```

After that, imported runs show in `agenticml ui`, `ls`, `show` and `export`
like any other run, and `agenticml sync` uploads them to a server unchanged.
Nothing in the source directory is changed.

Today only live mirroring exists (`integrations/tensorboard.py` copies
`SummaryWriter` calls while a script runs under `agenticml run`). Nothing
reads logs that are already on disk.

## Decisions

Taken by the supervisor on the recommended option (owner may overrule):

- **D1 Command.** `agenticml import <source> <path>` with sources
  `tensorboard`, `mlflow`, `wandb`, `csv`. The source is named, not guessed;
  a wrong path prints what the source expects (for example "no
  `events.out.tfevents.*` files under ./x").
- **D2 No new required dependencies.** TensorBoard event files, the MLflow
  file store and SQLite database, W&B `.wandb` files and CSV/JSONL are read
  with the standard library (TFRecord framing and a minimal protobuf decoder
  written here). Only W&B *cloud* import needs an optional `[wandb]` extra.
  Reason: `tensorboard`, `mlflow` and `wandb` pull in hundreds of MB, and
  the core install stays `requests` only.
- **D3 Imports are immutable and deduplicated.** The run id is
  `<source start time as YYYYmmddTHHMMSS>-<first 8 hex of
  sha256(source "\0" location "\0" source_run_id)>`, so importing the same
  source run twice gives the same id and the second import skips it.
  `--replace` re-imports, but only a run that has never been synced (no
  `sync.json`); a synced run is append-only on the server, so it is refused
  with a message. Growing a still-written source into an existing run is
  out of scope (Later).
- **D4 Original times are kept.** `created_at`, `finished_at`, the
  `_time.bin` seconds and event `t` come from the source, so the dashboard's
  time axis and run order match the original. The store writer gains
  keyword-only options for this, used only by importers.
- **D5 No code or lineage.** The code on disk at import time is not the code
  the run trained with, so imported runs have no `code` events and no
  `lineage`. Their dashboard code tab shows nothing, as for SDK runs with
  `--no-lineage`.
- **D6 Status.** Imported runs are `finished`, or `crashed` when the source
  says failed / killed / crashed. Source runs still running (MLflow
  `RUNNING`, W&B without an exit record) are skipped with a message;
  `--include-running` imports what is there as `finished` and records
  `source_status`. TensorBoard has no status: a run whose newest event file
  changed in the last 2 minutes gets a "may still be writing" warning and is
  imported.
- **D7 Names.** `project` defaults to the source's experiment or project
  (MLflow experiment name, W&B project, TensorBoard: the logdir's base
  name, CSV: the file's parent directory name); `--project` overrides it
  for every run. `name` is the source run name (TensorBoard: the run
  directory relative to the logdir, as TensorBoard shows it).
- **D8 Summary.** `summary` is the source's own summary only (W&B
  `wandb-summary` / summary records, TensorBoard `add_hparams` metrics).
  Nothing is invented from last values, matching native runs, whose summary
  is user-set.
- **D9 Docs by `sonnet`** (memory: skip Antigravity while Codex and Cursor
  are off).

## Release and parts

New release section **3.4: Import from other trackers**. One PR to `main`,
draft after 41.1 is committed, each part pushed when committed.

| Part | Scope | Depends on |
| --- | --- | --- |
| 41.1 | Import core: writer time options, `importers` package, `agenticml import` CLI, dedupe, dry run, report | – |
| 41.2 | TensorBoard event files: scalars (TF1 and TF2), images, hparams | 41.1 |
| 41.3 | MLflow file store (`mlruns/`) and SQLite (`mlflow.db`) | 41.1 |
| 41.4 | W&B local run directories (`wandb/run-*/run-*.wandb`), media images | 41.1 |
| 41.5 | CSV / JSONL, PyTorch Lightning `CSVLogger`, Hugging Face `trainer_state.json` | 41.1 |
| 41.6 | W&B cloud import through `wandb.Api` (`[wandb]` extra) | 41.4 |
| 41.7 | Docs: guide page, CLI reference, FORMAT.md, README line, facts test | 41.2–41.6 |

41.2–41.5 are independent of each other after 41.1. Coder for all parts:
`sonnet`; the supervisor reviews each diff.

**File ownership (so 41.2–41.5 can be built at the same time).** 41.1 owns
every shared file: `store/writer.py`, `cli.py`, `importers/__init__.py`
(including the registry entries and CLI options of *every* source),
`importers/_protobuf.py`, `importers/_crc32c.py` and `importers/_media.py`.
Each later part adds only its own reader module, its own test file and its
own fixture directory `tests/fixtures/import/<source>/` (with its own
`README.md` holding the generator script and package versions). A part that
finds it needs a change in a shared file reports it as a deviation instead
of editing it. Only 41.6 touches `pyproject.toml`; only 41.7 touches docs.

## Constraints and conventions

- Python 3.10+ (`requires-python`), standard library plus `requests`.
- Readers never write into, lock, or modify the source. SQLite opens
  read-only (`sqlite3.connect("file:<path>?mode=ro", uri=True)`).
- All input is untrusted: cap protobuf nesting (depth 32), reject a TFRecord
  or LevelDB record longer than 64 MiB (skip it with one warning), check
  varint lengths against the remaining buffer, never `pickle`/`eval`, and
  resolve every path a source file names (W&B media paths) inside that run's
  directory (`Path.resolve()` + `is_relative_to`), skipping it otherwise.
- Write through `RunWriter` only; never hand-write store files. Store-format
  rules in `docs/FORMAT.md` stay true (update the doc where 41.1 extends it).
- Steps must fit `0 .. 2**32-1` (`coerce_step`). Values go through the
  writer's own coercion (float32, NaN kept).
- Logging via `logging.getLogger("agenticml.import")`; user-facing lines go to
  stdout from the CLI, one line per run plus a total.
- Follow `cli.py` style: subcommand parser built in `main`, `cmd_import`
  imports its module lazily, `--dir` with the same help text as `ls`.

## 41.1 Import core

Files:

- `src/agenticml/store/writer.py`
- `src/agenticml/importers/__init__.py` (new): `ImportedRun`,
  `ImportOptions`, `SourceReader` protocol, `REGISTRY`,
  `write_imported_run`, `run_import`, `renumber_constant_steps`
- `src/agenticml/importers/_protobuf.py` (new): protobuf wire-format
  decoder (varint, 64-bit, length-delimited, 32-bit; `decode(buf) ->
  dict[int, list[raw]]`, packed and unpacked repeated numerics, signed
  int64 from two's complement, depth cap 32, every length checked against
  the remaining buffer; raises one `ProtobufError`)
- `src/agenticml/importers/_crc32c.py` (new): table-driven CRC-32C
  (Castagnoli) plus `masked_crc32c` for TFRecord; known-answer tests
  (`crc32c(b"123456789") == 0xE3069283`)
- `src/agenticml/importers/_media.py` (new): `sniff_image_type(data) ->
  str | None` from magic bytes (PNG, JPEG, GIF, WEBP)
- `src/agenticml/cli.py`: `import` subparser and `cmd_import`
- `tests/test_import_core.py` (new)

Steps:

1. **Writer time options.** In `RunWriter`:
   - `create(..., created_at: datetime | None = None)`: when given, used
     instead of now for `created_at` and `_created_at_unix`. Everything else
     `create` forces stays forced.
   - `write_scalars(items, *, wall_time: float | None = None)`: when given,
     the `_time.bin` value is `wall_time - _created_at_unix` instead of
     `time.time() - _created_at_unix`. Clamp negative to 0.0.
   - `finish(status, summary=None, *, finished_at: datetime | None = None)`:
     when given, used for `finished_at` and the finish event's `t`.
   - `set_config` / `set_summary` unchanged (create takes config in `meta`).
   Defaults keep today's behaviour exactly; existing tests must pass
   untouched.
2. **Data model** in `importers/__init__.py`:
   ```python
   @dataclass
   class ImportedRun:
       source: str                 # "tensorboard", "mlflow", ...
       location: str               # absolute path or URI of the source store
       source_run_id: str          # stable id inside the source
       name: str
       project: str
       started_at: datetime        # UTC
       ended_at: datetime | None
       status: str                 # "finished" | "crashed" | "running"
       source_status: str | None
       config: dict
       summary: dict
       tags: list[str]
       points: Iterable[tuple[float, int, dict[str, float]]]  # (wall_time, step, {key: value}), source order
       media: Iterable[tuple[float, int, str, bytes, str, str | None]]  # (wall_time, step, key, data, content_type, caption)
       notes: str = ""
   ```
   `points` and `media` are lazy iterators so a large source is streamed,
   not loaded. A reader yields `ImportedRun`s from `discover(path, options)`.
3. **Writing** (`write_imported_run(root, run, *, replace, dry_run)`):
   - Compute the run id (D3). If `runs/<id>/meta.json` exists: skip, unless
     `replace`; with `replace`, refuse when `runs/<id>/sync.json` exists or
     the meta has no `imported_from`; otherwise remove the run directory
     (containment-checked `run_dir`) and continue.
   - `meta` = project, name, config, tags, notes, plus
     `imported_from = {source, location, source_run_id, source_status,
     imported_at, agenticml_version}`. `group`/`job_type` stay null.
   - `RunWriter.create(root, run_id=..., meta=..., created_at=started_at)`.
   - Group consecutive points with the same `(wall_time, step)` into one
     `write_scalars([(step, metrics)], wall_time=...)` call; flush a group
     when the step or wall time changes. Count accepted and dropped points.
   - Step fix: if a series' points all share one step and there are more
     than one, the reader cannot know the order from steps (MLflow
     `log_metric` without `step` logs step 0 every time). Readers that can
     detect this (MLflow, CSV) renumber that series 0..n-1 in time order and
     list the key in `imported_from.renumbered_steps`. Core provides the
     helper; it does not guess for readers that cannot.
   - Media: `writer.add_media(key, step, data, content_type, caption)` in
     source order (the writer thins to 64 per key and downscales).
   - `finish("finished" | "crashed", summary, finished_at=ended_at or last
     wall time)`.
   - On any exception after create: close the writer, remove the partial run
     directory, report the run as failed, continue with the next run.
   - Two imports racing: `create` raising `FileExistsError` counts as skipped.
   - `dry_run`: run the reader fully (counts points and media) but write
     nothing.
4. **CLI**: `agenticml import SOURCE PATH [--dir D] [--project P]
   [--tag T]... [--run GLOB]... [--replace] [--include-running]
   [--dry-run] [--json]`.
   - `--run` filters on source run name or id with `fnmatch` (repeatable,
     any match).
   - `--tag` adds tags to every imported run (plus the automatic tag
     `imported:<source>`).
   - Output per run: `imported <run_id>  <name>  <points> points, <media>
     images` / `skipped <run_id>  <name>  (already imported)` / `failed
     <name>: <reason>`; then `N imported, M skipped, K failed`. `--json`
     prints one JSON object with the same rows.
   - Exit 0 when nothing failed, 1 when any run failed, 2 for a bad path or
     unknown source (argparse `choices`).
   - Unknown or uninstalled source modules: a clear message, never a
     traceback.
   - `REGISTRY` maps every source name (`tensorboard`, `mlflow`, `wandb`,
     `csv`) to its module path (`agenticml.importers.tensorboard`,
     `.mlflow`, `.wandb`, `.table`), imported lazily. Each module exposes
     `discover(path: str, options: ImportOptions) ->
     Iterator[ImportedRun]`. Until a part lands, its module is missing and
     the CLI says "the <source> importer is not available in this version".
   - `ImportOptions` carries every source's options so later parts never
     touch `cli.py`: `project`, `tags`, `run_globs`, `include_running`,
     `step_column`, `time_column` (CSV), plus `warn(message)` for reader
     warnings collected into the report. `--step-column` and
     `--time-column` are parsed in 41.1 and only `csv` uses them; with
     another source they are an error.
   - A reader raising `ImportSourceError` (defined in `__init__.py`) for the
     whole PATH (not found, wrong format) prints its message and exits 2.
     A reader exception while one run's points stream fails that run only.
5. **Tests** (`tests/test_import_core.py`), with a fake in-memory reader:
   - Imported run is readable by `RunReader` with the original
     `created_at`/`finished_at`, status, config, summary, tags,
     `imported_from`, scalars and media.
   - `_time.bin` seconds equal `wall_time - started_at`.
   - Re-import skips (same id); `--replace` rewrites; `--replace` on a run
     with `sync.json` is refused; `--replace` on a native run id is refused.
   - Failure mid-stream leaves no run directory; later runs still import.
   - Dry run writes nothing.
   - Bad steps (negative, > 2**32-1) are dropped and counted.
   - Renumber helper.
   - CLI exit codes and `--json`.
   - Existing writer tests pass unchanged (defaults).

### 41.1 as built (deviations accepted in review)

The code in `src/agenticml/importers/__init__.py` is the API later parts use;
where it differs from the steps above, the code wins:

- `ImportedRun` has two more optional fields: `renumbered_steps` (keys a
  reader renumbered; read after the last point) and `parent_source_run_id`
  (linked to the parent's AgenticML id after the whole import, when the
  parent is in the same import).
- `renumber_constant_steps(points) -> (points, keys)` holds its input in
  memory; call it per run.
- Config, summary, name, project, notes and tags pass through `json_plain`
  (NaN and inf become null, non-str keys become str), as native runs do.
- Points with a non-finite or out-of-range wall time reuse the previous wall
  time; a start before 1970 or after 9999 fails that run.
- A refused `--replace` is a `failed` row (exit 1). A source run still
  running is a `skipped` row unless `--include-running`.
- An interrupted earlier import (an imported run left `running`, its lock
  free) is replaced without `--replace`.
- `_protobuf.decode(buf)` returns a `Message` (dict of field number to raw
  values) with `.child(raw)` (depth-capped), `.first()`, `.last()`, and
  `as_*` / `repeated_*` helpers; `sniff_image_type` returns MIME strings.
- Dry-run lines say `would import`.
- 41.7 documents `imported_from` and the writer options in FORMAT.md.

## 41.2 TensorBoard

Files: `src/agenticml/importers/tensorboard.py` (uses `_protobuf`,
`_crc32c` and `_media` from 41.1), `tests/test_import_tensorboard.py`,
fixtures under `tests/fixtures/import/tensorboard/`.

Discovery: every directory under PATH (PATH itself included) holding files
named `*tfevents*` is one run; its files are read in filename order (the
name embeds the start time). Run name = directory relative to PATH (`.` →
the logdir base name).

Record framing (TFRecord): `uint64 length | uint32 masked_crc32c(length) |
data | uint32 masked_crc32c(data)`, little-endian,
`masked = ((crc >> 15 | crc << 17) + 0xa282ead8) & 0xffffffff`. Check the
length CRC on every record. Check the data CRC by default; a mismatch skips
that record with one warning per file. A short final record (file still
being written) ends the file quietly.

`Event` fields used (verify every number against tensorboardX's bundled
`.proto` files or TensorFlow's `event.proto` / `summary.proto` /
`tensor.proto` before coding, and note the source in a comment):

- `Event`: `wall_time` 1 (double), `step` 2 (int64), `summary` 5.
- `Summary`: `value` 1 (repeated).
- `Summary.Value`: `tag` 1, `simple_value` 2 (float), `image` 4,
  `tensor` 8, `metadata` 9.
- `SummaryMetadata`: `plugin_data` 1 → `PluginData` (`plugin_name` 1,
  `content` 2).
- `Summary.Image`: `height` 1, `width` 2, `encoded_image_string` 4.
- `TensorProto`: `dtype` 1, `tensor_content` 4, `float_val` 5,
  `double_val` 6, `int_val` 7, `string_val` 8, `int64_val` 10, `half_val` 13
  (repeated numeric fields may be packed or not; accept both).

Mapping:

- Scalars: `simple_value`, or a `tensor` with exactly one element of dtype
  float, double, half, bfloat16, int32 or int64 (TF2 `tf.summary.scalar`,
  plugin `scalars`), from `*_val` or `tensor_content`. Key = tag.
- Images: `Summary.Image.encoded_image_string`; or a TF2 `images` plugin
  tensor whose `string_val` is `[width, height, png, png...]` (each image
  becomes one media row; caption null). Content type from the magic bytes
  (PNG, JPEG, GIF, WEBP); other bytes are skipped.
- Hparams (plugin `hparams`, `HParamsPluginData`: `session_start_info` 3 →
  `SessionStartInfo.hparams` 1, a map of `google.protobuf.Value`
  (`number_value` 2, `string_value` 3, `bool_value` 4)): into `config`.
  `torch.utils.tensorboard.add_hparams` writes the hparams into a child
  directory named by a timestamp; when a child directory contains only
  hparams plus the `add_hparams` metric scalars, merge it into its parent
  run (config, and those metrics into `summary`) instead of making a run.
- Histograms, audio, text, graphs, PR curves: skipped and counted
  ("skipped 3 histogram tags").
- `started_at` = first event `wall_time` (the `file_version` event
  counts); `ended_at` = last.
- `source_run_id` = run name; `location` = absolute PATH.

`add_scalars` in torch / tensorboardX writes `logdir/<main>_<sub>/` child
directories; they import as separate runs, as TensorBoard shows them.
Document it.

Fixtures: generate small real event files once with `tensorboardX` and
`torch.utils.tensorboard` in a scratch venv (scalars over 50 steps, one
image, `add_hparams`, `add_scalars`, a histogram, a TF2-style tensor scalar
if `tensorflow`/`tensorboard` can write one; else a hand-encoded record in
the test). Commit the files (keep under 200 KB total) plus
`tests/fixtures/import/tensorboard/README.md` with the generator script and
versions.
Tests also cover: CRC mismatch skips one record, truncated tail, a 2 GiB
claimed length, nesting bomb, empty directory error, two runs in one
logdir, step > 2**32-1 dropped.

## 41.3 MLflow

Files: `src/agenticml/importers/mlflow.py`, `tests/test_import_mlflow.py`,
fixtures under `tests/fixtures/import/mlflow/`.

PATH is a `mlruns` directory, a `.db` SQLite file, or a directory
containing `mlflow.db` or `mlruns/`. MLflow 3.7 made `sqlite:///mlflow.db`
the default backend, and older setups use `./mlruns`, so both are first
class. Remote tracking servers are out of scope (Later).

File store (`mlruns/<experiment_id>/meta.yaml`, runs in
`<experiment_id>/<run_id>/`):

- `meta.yaml`: flat `key: value` lines (`run_name`, `start_time`,
  `end_time` in ms, `status`, `lifecycle_stage`, `experiment_id`). Parse with
  a small flat-YAML reader (quoted and plain scalars, `null`); PyYAML is not
  a dependency.
- `metrics/<key>`: lines `timestamp_ms value step`; keys with `/` are
  nested directories.
- `params/<key>` → `config` (string values, as MLflow stores them).
- `tags/<key>`: `mlflow.runName` (name fallback), `mlflow.note.content` →
  `notes`; other non-`mlflow.` tags → `config["mlflow.tags"]` (not
  AgenticML tags, which are short labels). `mlflow.parentRunId` → when the
  parent is in the same import, set `parent_run_id` to its AgenticML id.
- Skip `.trash/`, experiments and runs with `lifecycle_stage: deleted`.

SQLite (`experiments`, `runs`, `metrics`, `params`, `tags` tables): same
mapping; metrics read per run ordered by `(timestamp, step)`; `is_nan` rows
become NaN. Check the schema at open (`PRAGMA table_info`) and fail with a
clear message if a needed column is missing.

Status: `FINISHED` → finished; `FAILED`, `KILLED` → crashed; `RUNNING`,
`SCHEDULED` → running (D6). `project` = experiment name. Renumber steps per
series when every point has the same step (41.1 helper).

Fixtures: create with real `mlflow` in a scratch venv (one experiment, two
runs, metrics with and without `step`, params, tags, a nested key, one
failed run, one deleted run), once with the file store and once with
SQLite. Commit both (small) with the generator in
`tests/fixtures/import/mlflow/README.md`.

## 41.4 W&B local

Files: `src/agenticml/importers/wandb.py`,
`src/agenticml/importers/_leveldb_log.py`, `tests/test_import_wandb.py`,
fixtures under `tests/fixtures/import/wandb/`.

PATH is a `wandb/` directory (each `run-*` or `offline-run-*` child is a
run) or one run directory. The data is the `run-<id>.wandb` file: a W&B
header followed by LevelDB log-format blocks (32 KiB blocks; record header
`crc32c(4) length(2) type(1)` with types FULL/FIRST/MIDDLE/LAST) holding
protobuf `Record` messages. Before coding, read the header layout in
wandb's `sdk/internal/datastore.py` and the field numbers in
`wandb/proto/wandb_internal.proto` (both MIT), and note the wandb version
in a comment. Records used:

- `run` → name (`display_name`), project, entity, start time, tags, notes.
- `config` → `config` (`value_json` per key; drop W&B's `_wandb` key).
- `history` → one point per record; `_step` is the step, `_timestamp` the
  wall time. Numeric values become scalars. Values whose JSON has
  `"_type": "image-file"` become media read from
  `files/<path>` (contained in the run directory). Other `_type` values
  (histogram, table, video, …) are skipped and counted. Keys starting with
  `_` are not metrics.
- `summary` → `summary` (numeric and string values only).
- `exit` → `ended_at` and status (`exit_code` 0 → finished, else crashed);
  no exit record → running (D6).
- `stats` (system metrics) are skipped in this version (counted).

A header or record the reader does not understand fails that run with
"unsupported .wandb format (wandb <version from wandb-metadata.json>)",
never a partial import. `source_run_id` = the W&B run id.

Fixtures: run real `wandb` in offline mode in a scratch venv (`WANDB_MODE=
offline`, 30 steps, config, summary, one image, one failed run). Commit the
run directories (trim `files/output.log`). Note the wandb version and the
generator in `tests/fixtures/import/wandb/README.md`.

## 41.5 CSV / JSONL and common local loggers

Files: `src/agenticml/importers/table.py`, `tests/test_import_table.py`.

- `csv` source accepts a `.csv`, `.jsonl` file, or a directory searched for
  Lightning `metrics.csv` (one run per `version_*` directory, with
  `hparams.yaml` flat keys into `config`) and Hugging Face
  `trainer_state.json` (`log_history` rows; `step` from `step`, keys other
  than `step`/`epoch` as metrics, `epoch` also kept as a metric).
- CSV/JSONL columns: `--step-column` (default `step`, then `global_step`,
  then `epoch`, else row number), `--time-column` (default `timestamp`,
  `time`, `wall_time`; seconds or ms detected by magnitude; else file mtime
  for every row). Empty cells are skipped (Lightning writes sparse rows).
- Non-numeric columns are ignored and counted.
- `started_at` = first row time; status finished.

## 41.6 W&B cloud

Files: `src/agenticml/importers/wandb_cloud.py`, `pyproject.toml` (`wandb`
extra: `wandb>=0.17`), `tests/test_import_wandb_cloud.py`.

- `agenticml import wandb ENTITY/PROJECT[/RUN_ID]`: a PATH that does not
  exist on disk and matches `^[\w.-]+/[\w.-]+(/[\w.-]+)?$` is a cloud path.
- Uses `wandb.Api()` (the user's own `WANDB_API_KEY` or `wandb login`; we
  never read or store the key), `run.scan_history()` for points, `run.config`,
  `run.summary`, `run.created_at`, `run.state`, and image files via
  `run.file(path).download(root=<temp dir>)`.
- Without the package: "W&B cloud import needs `pip install
  'agenticml-lite[wandb]'`".
- Tests mock `wandb.Api` (inject a fake module into `sys.modules`); no
  network.

## 41.7 Docs

Coder `sonnet`; the supervisor fact-checks against the code.

- `website/src/content/docs/guides/import.md` (new): one section per source
  with the command, what is imported, what is skipped, dedupe and
  `--replace`, then syncing imported runs to a server.
- `website/src/content/docs/reference/configuration.md`: `agenticml import`
  options.
- `docs/FORMAT.md`: `imported_from` meta key; the writer's `created_at`,
  `wall_time`, `finished_at` options (who uses them).
- `README.md`: one line in the features list.
- `website/src/content/docs/releases.md`: 3.4 entry when released.
- `tests/test_website_facts.py`: the guide's source list and flags match
  the CLI parser (`agenticml import --help` choices and options).

## Edge cases (all parts)

- Same source imported from two different paths (copied logdir): different
  `location`, so different ids; documented.
- A source run with no points and no media: imported (config only) with a
  note in the report.
- Duplicate `(step, key)` points: kept in order (the writer allows equal
  steps).
- Steps going backwards (resumed TensorBoard training): kept in append
  order; the writer marks the series non-monotonic.
- Clock skew: a point earlier than `started_at` gets `_time.bin` 0.0.
- Very large logs: points stream; never build a whole series in memory
  (the MLflow and CSV renumber pass may hold one series' points).
- Project or name with characters the dashboard cannot show: stored as is
  (meta strings are free-form).
- Windows paths: use `pathlib`; `location` is `Path.resolve()` as a string.

## Tests and checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- No dashboard code changes, so the UI suite is not required; still open one
  imported run of each source in `agenticml ui` (supervisor) and check the
  charts, image panel and run order.
- Performance (measured, not asserted in CI): 1,000,000 TensorBoard scalar
  points import in under 60 s on the dev machine; record the number in this
  plan.
- `agenticml sync --dry-run` on an imported run reports it as a normal
  finished run.

## Done when

- `agenticml import tensorboard|mlflow|wandb|csv PATH` imports the
  committed fixtures into a fresh store, and each run shows in `agenticml
  ui` with its metrics, images, config and original times.
- Re-running the same import reports every run as skipped.
- W&B cloud import works against the mocked API and prints the install hint
  without the extra.
- All tests pass, the docs page is live in the site build, and the roadmap
  rows for 41 and 41.1–41.7 say done.

## Later (not in this package)

- Remote MLflow tracking servers (REST API with `requests`).
- Appending new points from a source that is still being written
  (`--follow`).
- W&B system metrics (`stats` records) and TensorBoard histograms, once the
  dashboard can show histograms.
- Other trackers on request (Neptune, Comet, Aim, ClearML, Sacred).
