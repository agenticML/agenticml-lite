# MLflow import fixtures

Written by real MLflow, then scrubbed of machine-specific values. Used by
`tests/test_import_mlflow.py`.

- `filestore/mlruns/`: the file store (`MLFLOW_TRACKING_URI=file:<dir>/mlruns`).
- `sqlite/mlflow.db`: the SQLite backend (`sqlite:///<dir>/mlflow.db`).

Both hold the same data: experiment `resnet` with the runs `baseline` (params,
tags, a note, nested key `train/loss`, a NaN metric, parent of `child`),
`child` (nested run), `no-steps` (`log_metric` without `step`, so every point
has step 0), `broken` (failed), `gone` (deleted) and `live` (never ended, so
RUNNING); plus a deleted experiment `old`. The run ids are random, so the tests
look runs up by name.

## Tool versions

- mlflow 3.18.0 (installed with `uv pip install mlflow` into a scratch venv,
  Python 3.12). MLflow 3.18 refuses to create a file store unless
  `MLFLOW_ALLOW_FILE_STORE=true`; the generator sets it.
- `mlflow` is only needed to regenerate the fixtures. The importer and its
  tests never import it.

## Regenerate

```
uv venv /tmp/venv-mlflow
uv pip install --python /tmp/venv-mlflow/bin/python mlflow
/tmp/venv-mlflow/bin/python tests/fixtures/import/mlflow/generate.py /tmp/mlflow-out
cp -r /tmp/mlflow-out/filestore /tmp/mlflow-out/sqlite tests/fixtures/import/mlflow/
```

`generate.py` (next to this file) does the whole job, including the scrub. The
timestamps are the time of generation, so a regenerated fixture needs no test
change except the year/date checked in `test_file_store_original_times_are_kept`.

## What the scrub changes

- Replaces absolute paths (`artifact_location`, `artifact_uri`) with
  `file:///mlruns/...`, the user name with `user`, and the source script name
  with `train.py`; removes the `mlflow.source.git.*` tags.
- Removes the empty `artifacts/` directories (git does not keep empty
  directories).
- SQLite only: drops every table the importer does not read (traces, models,
  registry, jobs, ...), keeping `alembic_version`, `experiments`, `runs`,
  `metrics`, `params` and `tags`, then runs `VACUUM`. The database is about
  80 KB.
