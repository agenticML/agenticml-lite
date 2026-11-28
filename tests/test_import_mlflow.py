"""MLflow importer: a real file store and a real SQLite database, plus hostile input.

The fixtures under ``tests/fixtures/import/mlflow`` were written by MLflow itself
(see the README there). Hostile cases are built from scratch in ``tmp_path``.
"""

import hashlib
import json
import math
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from agenticml import cli
from agenticml.importers import ImportOptions, ImportSourceError, run_import
from agenticml.importers import mlflow as mlflow_importer
from agenticml.importers.mlflow import parse_flat_yaml
from agenticml.store import RunReader

FIXTURES = Path(__file__).parent / "fixtures" / "import" / "mlflow"
FILE_STORE = FIXTURES / "filestore" / "mlruns"
SQLITE_DB = FIXTURES / "sqlite" / "mlflow.db"


def do_import(store, source, **kwargs):
    options = kwargs.pop("options", None) or ImportOptions()
    return run_import(store, "mlflow", str(source), options, **kwargs)


def by_name(report):
    return {row.name: row for row in report.rows}


def meta_of(store, row):
    return RunReader(store, row.run_id).meta()


def series(store, row, key):
    return RunReader(store, row.run_id).read_records(key)[0]


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


# --- a hand-made file store for hostile inputs ----------------------------

START_MS = 1_700_000_000_000


def make_store(root, runs, *, experiment="exp", exp_id="1", exp_extra=""):
    """``runs`` maps run id -> dict(meta=..., metrics={key: text}, params={}, tags={})."""
    exp = Path(root) / "mlruns" / exp_id
    exp.mkdir(parents=True)
    (exp / "meta.yaml").write_text(
        f"experiment_id: '{exp_id}'\nlifecycle_stage: active\nname: {experiment}\n{exp_extra}"
    )
    for run_id, spec in runs.items():
        run = exp / run_id
        meta = {
            "run_id": run_id,
            "run_name": run_id,
            "start_time": START_MS,
            "end_time": START_MS + 5000,
            "status": 3,
            "lifecycle_stage": "active",
        }
        meta.update(spec.get("meta", {}))
        run.mkdir()
        (run / "meta.yaml").write_text(
            "".join(f"{key}: {value}\n" for key, value in meta.items() if value is not None)
        )
        for folder in ("metrics", "params", "tags"):
            for key, text in spec.get(folder, {}).items():
                path = run / folder / key
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return Path(root) / "mlruns"


def lines(*rows):
    return "".join(f"{ts} {value} {step}\n" for ts, value, step in rows)


# --- flat YAML -------------------------------------------------------------


def test_flat_yaml_scalars():
    text = (
        "artifact_uri: file:///x/y\n"
        "end_time: 1795859733550\n"
        "entry_point_name: ''\n"
        "experiment_id: '984938799057253277'\n"
        "lifecycle_stage: active\n"
        "run_name: it's \"fine\"\n"
        "quoted: 'it''s'\n"
        'double: "tab\\there \\u00e9 \\x41"\n'
        "status: 3\n"
        "ratio: 0.5\n"
        "missing: null\n"
        "tilde: ~\n"
        "flag: true\n"
        "tags: []\n"
        "extra: {}\n"
        "comment: value # trailing\n"
        "colon: a: b\n"
    )
    meta = parse_flat_yaml(text)
    assert meta["artifact_uri"] == "file:///x/y"
    assert meta["end_time"] == 1795859733550 and isinstance(meta["end_time"], int)
    assert meta["entry_point_name"] == ""
    assert meta["experiment_id"] == "984938799057253277"
    assert meta["run_name"] == 'it\'s "fine"'
    assert meta["quoted"] == "it's"
    assert meta["double"] == "tab\there \u00e9 A"
    assert meta["status"] == 3 and meta["ratio"] == 0.5
    assert meta["missing"] is None and meta["tilde"] is None
    assert meta["flag"] is True
    assert meta["tags"] == [] and meta["extra"] == {}
    assert meta["comment"] == "value"
    assert meta["colon"] == "a: b"


def test_flat_yaml_folded_and_block_values():
    text = (
        "run_name: first line\n"
        "  second line\n"
        "quoted: 'one\n"
        "  two\n"
        "\n"
        "  three'\n"
        "nested:\n"
        "  - a\n"
        "  - b\n"
        "after: 1\n"
    )
    meta = parse_flat_yaml(text)
    assert meta["run_name"] == "first line second line"
    assert meta["quoted"] == "one two\nthree"
    assert meta["nested"] is None
    assert meta["after"] == 1


def test_flat_yaml_never_resolves_anchors_tags_or_aliases():
    text = (
        "a: &x [1, 2, 3]\n"
        "b: *x\n"
        "c: !!python/object/apply:os.system ['echo hi']\n"
        "d: !!binary aGVsbG8=\n"
    )
    meta = parse_flat_yaml(text)
    assert meta["b"] == "*x"
    assert isinstance(meta["a"], str) and isinstance(meta["c"], str)
    assert all(not isinstance(value, (list, dict)) for value in meta.values())


def test_flat_yaml_plain_scalars_follow_yaml_1_1():
    meta = parse_flat_yaml("a: 1e-4\nb: 1.5e3\nc: .inf\nd: -.inf\nname: 1.50\nrun_name: 007\n")
    assert meta["a"] == "1e-4"
    assert meta["b"] == 1500.0
    assert meta["c"] == math.inf and meta["d"] == -math.inf
    # name-like fields keep the text that was written
    assert meta["name"] == "1.50" and meta["run_name"] == "007"


def test_experiment_and_run_named_like_a_number_keep_their_names(tmp_path):
    root = make_store(
        tmp_path / "src",
        {"r1": {"meta": {"run_name": "1e-4"}}},
        experiment="1e-4",
    )
    report = do_import(tmp_path / "s", root)
    assert [row.name for row in report.rows] == ["1e-4"]
    assert meta_of(tmp_path / "s", report.rows[0])["project"] == "1e-4"


def test_flat_yaml_long_space_padded_lines_are_fast():
    import time

    start = time.monotonic()
    for text in (
        "a" + " " * 200_000 + "b\n",
        "a" + " " * 200_000 + "x\n",
        "'" + "a" * 200_000 + "\n",
        '"' + "\\" * 200_001 + "\n",
        "k" + ":" * 200_000 + "\n",
        "-" + " " * 200_000 + "\n",
    ):
        assert isinstance(parse_flat_yaml(text), dict)
    assert time.monotonic() - start < 5


def test_flat_yaml_keys():
    meta = parse_flat_yaml(
        "plain key : 1\n'quoted: key': 2\n\"dq\\tkey\": 3\n-dash: 4\nnocolon\na:b: 5\nend:\n"
    )
    assert meta == {"plain key": 1, "quoted: key": 2, "dq\tkey": 3, "-dash": 4, "end": None}


def test_flat_yaml_garbage_does_not_raise():
    for blob in ("\x00\x01\x02", "::::", "- - -", "key", "'unterminated: 1", ": no key", "k: \"\\"):
        assert isinstance(parse_flat_yaml(blob), dict)
    assert parse_flat_yaml("k: " + "x" * 100_000)["k"] == "x" * 100_000


# --- real file store -------------------------------------------------------


def test_file_store_fixture_imports_every_active_run(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE)
    rows = by_name(report)
    # deleted run, deleted experiment and the still-running run are not imported
    assert sorted(
        (row.name, row.status) for row in report.rows
    ) == [
        ("baseline", "imported"),
        ("broken", "imported"),
        ("child", "imported"),
        ("live", "skipped"),
        ("no-steps", "imported"),
    ]
    assert rows["live"].reason == "still running; use --include-running"
    assert report.failed == 0
    store = tmp_path / "s"
    base = meta_of(store, rows["baseline"])
    assert base["project"] == "resnet"
    assert base["status"] == "finished"
    assert base["config"]["lr"] == "0.1" and base["config"]["layers"] == "18"
    assert base["config"]["optimizer"] == "sgd"
    assert base["config"]["mlflow.tags"] == {"team": "vision"}
    assert base["notes"] == "first baseline run"
    assert "imported:mlflow" in base["tags"]
    assert base["imported_from"]["source"] == "mlflow"
    assert base["imported_from"]["source_status"] == "FINISHED"
    assert base["imported_from"]["location"] == str(FILE_STORE.resolve())
    assert base["summary"] == {}
    assert base["finished_at"] > base["created_at"]
    # nested key, plain key and a NaN
    assert series(store, rows["baseline"], "train/loss")[0] == (0, 1.0)
    assert [step for step, _ in series(store, rows["baseline"], "accuracy")] == [0, 1, 2, 3, 4]
    nan_points = series(store, rows["baseline"], "nan_metric")
    assert len(nan_points) == 1 and math.isnan(nan_points[0][1])
    assert "mlflow.tags" not in meta_of(store, rows["child"])["config"]


def test_file_store_failed_run_is_crashed(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE)
    broken = meta_of(tmp_path / "s", by_name(report)["broken"])
    assert broken["status"] == "crashed"
    assert broken["imported_from"]["source_status"] == "FAILED"


def test_file_store_metrics_without_step_are_renumbered(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE)
    row = by_name(report)["no-steps"]
    assert series(tmp_path / "s", row, "loss") == [(0, 3.0), (1, 2.0), (2, 1.0)]
    assert meta_of(tmp_path / "s", row)["imported_from"]["renumbered_steps"] == ["loss"]
    # a series that already had steps is left alone
    base = meta_of(tmp_path / "s", by_name(report)["baseline"])
    assert "renumbered_steps" not in base["imported_from"]


def test_file_store_original_times_are_kept(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE)
    row = by_name(report)["no-steps"]
    run_dir = FILE_STORE / "924854014875798061" / "3cc9c2b53a3149209256b06f6450a3a0"
    meta = parse_flat_yaml((run_dir / "meta.yaml").read_text())
    stored = meta_of(tmp_path / "s", row)
    assert stored["created_at"].startswith("2026-11-28T")
    first_ts = int((run_dir / "metrics" / "loss").read_text().split()[0])
    seconds = dict(RunReader(tmp_path / "s", row.run_id).time_index())
    assert seconds[0] == pytest.approx((first_ts - meta["start_time"]) / 1000.0, abs=1e-3)


def test_file_store_links_the_parent_run(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE)
    rows = by_name(report)
    assert meta_of(tmp_path / "s", rows["child"])["parent_run_id"] == rows["baseline"].run_id
    assert meta_of(tmp_path / "s", rows["baseline"]).get("parent_run_id") is None


def test_include_running_imports_the_live_run_as_finished(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE, options=ImportOptions(include_running=True))
    live = by_name(report)["live"]
    assert live.status == "imported"
    meta = meta_of(tmp_path / "s", live)
    assert meta["status"] == "finished"
    assert meta["imported_from"]["source_status"] == "RUNNING"


def test_reimport_is_skipped_and_ids_do_not_depend_on_the_path_form(tmp_path):
    store = tmp_path / "s"
    first = do_import(store, FILE_STORE)
    assert first.imported == 4
    again = do_import(store, FILE_STORE.parent)  # the directory that contains mlruns/
    assert again.imported == 0 and again.skipped == 5
    assert {row.run_id for row in again.rows if row.status == "skipped" and row.name != "live"} == {
        row.run_id for row in first.rows if row.status == "imported"
    }


def test_file_store_source_is_not_modified(tmp_path):
    source = tmp_path / "src"
    shutil.copytree(FIXTURES / "filestore", source)
    before = tree_hash(source)
    do_import(tmp_path / "s", source / "mlruns", options=ImportOptions(include_running=True))
    assert tree_hash(source) == before


# --- real SQLite -----------------------------------------------------------


def test_sqlite_fixture_imports_every_active_run(tmp_path):
    report = do_import(tmp_path / "s", SQLITE_DB)
    assert sorted((row.name, row.status) for row in report.rows) == [
        ("baseline", "imported"),
        ("broken", "imported"),
        ("child", "imported"),
        ("live", "skipped"),
        ("no-steps", "imported"),
    ]
    rows = by_name(report)
    store = tmp_path / "s"
    base = meta_of(store, rows["baseline"])
    assert base["project"] == "resnet"
    assert base["imported_from"]["location"] == str(SQLITE_DB.resolve())
    assert base["notes"] == "first baseline run"
    assert meta_of(store, rows["broken"])["status"] == "crashed"
    nan_points = series(store, rows["baseline"], "nan_metric")
    assert len(nan_points) == 1 and math.isnan(nan_points[0][1])
    assert series(store, rows["no-steps"], "loss") == [(0, 3.0), (1, 2.0), (2, 1.0)]
    assert meta_of(store, rows["no-steps"])["imported_from"]["renumbered_steps"] == ["loss"]
    assert meta_of(store, rows["child"])["parent_run_id"] == rows["baseline"].run_id


def test_sqlite_file_is_opened_read_only_and_not_modified(tmp_path):
    db = tmp_path / "mlflow.db"
    shutil.copy(SQLITE_DB, db)
    before = db.read_bytes()
    stat_before = db.stat()
    do_import(tmp_path / "s", db, options=ImportOptions(include_running=True))
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert sorted(path.name for path in tmp_path.iterdir()) == ["mlflow.db", "s"]


def test_file_store_and_sqlite_give_the_same_runs(tmp_path):
    options = ImportOptions(include_running=True)
    file_report = do_import(tmp_path / "f", FILE_STORE, options=options)
    sql_report = do_import(tmp_path / "q", SQLITE_DB, options=ImportOptions(include_running=True))
    file_rows, sql_rows = by_name(file_report), by_name(sql_report)
    assert sorted(file_rows) == sorted(sql_rows)
    for name in file_rows:
        a = RunReader(tmp_path / "f", file_rows[name].run_id)
        b = RunReader(tmp_path / "q", sql_rows[name].run_id)
        ma, mb = a.meta(), b.meta()
        for field in ("project", "name", "status", "config", "notes", "tags"):
            assert ma[field] == mb[field], (name, field)
        for field in ("source", "source_status"):
            assert ma["imported_from"][field] == mb["imported_from"][field]
        assert ma["imported_from"].get("renumbered_steps") == mb["imported_from"].get(
            "renumbered_steps"
        )
        assert sorted(a.keys()) == sorted(b.keys())
        for key in a.keys():
            pa, pb = a.read_records(key)[0], b.read_records(key)[0]
            assert [step for step, _ in pa] == [step for step, _ in pb]
            for (_, x), (_, y) in zip(pa, pb):
                assert (math.isnan(x) and math.isnan(y)) or x == y


def test_directory_with_mlflow_db_and_directory_with_mlruns(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    shutil.copy(SQLITE_DB, folder / "mlflow.db")
    assert do_import(tmp_path / "a", folder).imported == 4
    folder2 = tmp_path / "proj2"
    shutil.copytree(FIXTURES / "filestore", folder2)
    assert do_import(tmp_path / "b", folder2).imported == 4
    # the db file and its directory are the same source: same ids
    first = do_import(tmp_path / "c", folder / "mlflow.db")
    second = do_import(tmp_path / "c", folder)
    assert first.imported == 4 and second.imported == 0


def test_mlflow_db_wins_when_both_exist_and_warns(tmp_path):
    folder = tmp_path / "proj"
    shutil.copytree(FIXTURES / "filestore", folder)
    shutil.copy(SQLITE_DB, folder / "mlflow.db")
    options = ImportOptions()
    report = do_import(tmp_path / "s", folder, options=options)
    assert report.imported == 4
    assert any("both mlflow.db and mlruns" in warning for warning in report.warnings)
    location = meta_of(tmp_path / "s", report.rows[0])["imported_from"]["location"]
    assert location.endswith("mlflow.db")


def test_sqlite_artifact_root_next_to_the_db_is_not_a_file_store(tmp_path):
    folder = tmp_path / "proj"
    (folder / "mlruns" / "1" / "abc" / "artifacts").mkdir(parents=True)
    (folder / "mlruns" / "1" / "abc" / "artifacts" / "model.pkl").write_bytes(b"x")
    shutil.copy(SQLITE_DB, folder / "mlflow.db")
    report = do_import(tmp_path / "s", folder)
    assert report.imported == 4 and not report.warnings


# --- errors for the whole path ---------------------------------------------


def test_path_errors(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ImportSourceError, match="no MLflow data under"):
        do_import(tmp_path / "s", empty)
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    with pytest.raises(ImportSourceError, match="not an MLflow store"):
        do_import(tmp_path / "s", notes)
    with pytest.raises(ImportSourceError, match="does not exist"):
        do_import(tmp_path / "s", tmp_path / "missing")


def test_sqlite_without_mlflow_tables_or_columns_fails_clearly(tmp_path):
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE things (x INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(ImportSourceError, match="not an MLflow tracking database"):
        do_import(tmp_path / "s", other)

    old = tmp_path / "old.db"
    shutil.copy(SQLITE_DB, old)
    con = sqlite3.connect(old)
    con.execute("ALTER TABLE runs RENAME COLUMN start_time TO started")
    con.commit()
    con.close()
    with pytest.raises(ImportSourceError, match="'runs' lacks column start_time"):
        do_import(tmp_path / "s", old)


def test_not_a_sqlite_file_named_db(tmp_path):
    fake = tmp_path / "fake.db"
    fake.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(ImportSourceError):
        do_import(tmp_path / "s", fake)


def test_sqlite_file_without_db_suffix_is_detected_by_its_header(tmp_path):
    odd = tmp_path / "tracking.data"
    shutil.copy(SQLITE_DB, odd)
    assert do_import(tmp_path / "s", odd).imported == 4


def test_sqlite_text_that_is_not_utf8_does_not_break_the_import(tmp_path):
    db = tmp_path / "mlflow.db"
    shutil.copy(SQLITE_DB, db)
    con = sqlite3.connect(db)
    con.text_factory = bytes
    con.execute("UPDATE runs SET name = ? WHERE name = 'child'", (b"ch\xff\xfeld",))
    con.execute("UPDATE params SET value = ? WHERE key = 'seed'", (b"\xc3(",))
    con.commit()
    con.close()
    report = do_import(tmp_path / "s", db)
    assert report.failed == 0 and report.imported == 4
    assert any("ch" in row.name and "ld" in row.name for row in report.rows)


def test_sqlite_nulls_and_odd_types_in_metrics_are_skipped_not_fatal(tmp_path):
    db = tmp_path / "mlflow.db"
    shutil.copy(SQLITE_DB, db)
    con = sqlite3.connect(db)
    run = con.execute("SELECT run_uuid FROM runs WHERE name = 'broken'").fetchone()[0]
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute(
        "INSERT INTO metrics (key, value, timestamp, run_uuid, step, is_nan) VALUES "
        "('bad', 'oops', 1795859740200, ?, 1, 0)",
        (run,),
    )
    con.execute(
        "INSERT INTO metrics (key, value, timestamp, run_uuid, step, is_nan) VALUES "
        "('bad_ts', 1.5, 'later', ?, 1, 0)",
        (run,),
    )
    con.commit()
    con.close()
    options = ImportOptions()
    report = do_import(tmp_path / "s", db, options=options)
    assert report.failed == 0
    assert any("skipped 2 malformed metric lines" in warning for warning in report.warnings)


# --- hostile file stores -----------------------------------------------------


def test_malformed_metric_lines_are_skipped_with_one_warning(tmp_path):
    good = lines((START_MS + 1000, 1.0, 0), (START_MS + 2000, 2.0, 1))
    bad = "garbage\n1\nabc 1 2\n1000 nope 3\n1000 1.5 x\n\n" + "9" * 5000 + "\n"
    mixed = bad + good + "1500 7 8 9 10 11 12\n"
    root = make_store(tmp_path / "src", {"r1": {"metrics": {"loss": mixed}}})
    options = ImportOptions()
    report = do_import(tmp_path / "s", root, options=options)
    assert report.imported == 1 and report.failed == 0
    points = series(tmp_path / "s", report.rows[0], "loss")
    assert [value for _, value in points] == [7.0, 1.0, 2.0]  # 1500 ms sorts first
    assert len([w for w in report.warnings if "malformed" in w]) == 1
    assert "skipped 6 malformed metric lines" in report.warnings[0]


def test_two_and_five_field_metric_lines(tmp_path):
    text = "1700000001000 1.5\n1700000002000 2.5 4 model-1 data 1\n"
    root = make_store(tmp_path / "src", {"r1": {"metrics": {"m": text}}})
    report = do_import(tmp_path / "s", root)
    # step 0 then 4: not constant, nothing renumbered
    assert series(tmp_path / "s", report.rows[0], "m") == [(0, 1.5), (4, 2.5)]


def test_metric_values_nan_inf_and_huge_steps(tmp_path):
    text = lines(
        (START_MS + 1, "nan", 0),
        (START_MS + 2, "inf", 1),
        (START_MS + 3, "-inf", 2),
        (START_MS + 4, 5, -1),
        (START_MS + 5, 6, 2**40),
        (START_MS + 6, 7, 3),
    )
    root = make_store(tmp_path / "src", {"r1": {"metrics": {"m": text}}})
    report = do_import(tmp_path / "s", root)
    row = report.rows[0]
    assert row.status == "imported" and row.dropped_points == 2 and row.points == 4
    values = series(tmp_path / "s", row, "m")
    assert math.isnan(values[0][1]) and values[1][1] == math.inf and values[2][1] == -math.inf
    assert values[3] == (3, 7.0)


def test_unsorted_timestamps_are_merged_in_time_order(tmp_path):
    a = lines((START_MS + 3000, 3.0, 2), (START_MS + 1000, 1.0, 0), (START_MS + 2000, 2.0, 1))
    b = lines((START_MS + 1500, 10.0, 0), (START_MS + 2500, 20.0, 1))
    root = make_store(tmp_path / "src", {"r1": {"metrics": {"a": a, "b": b}}})
    report = do_import(tmp_path / "s", root)
    reader = RunReader(tmp_path / "s", report.rows[0].run_id)
    # a is sorted by time on import, so its steps are 0, 1, 2 again
    assert reader.read_records("a")[0] == [(0, 1.0), (1, 2.0), (2, 3.0)]
    assert reader.read_records("b")[0] == [(0, 10.0), (1, 20.0)]
    times = [seconds for _, seconds in reader.time_index()]
    assert times == sorted(times)


def test_long_series_stream_across_batches(tmp_path):
    n = mlflow_importer._BATCH_LINES * 2 + 17
    a = lines(*((START_MS + i * 2, float(i), i) for i in range(n)))
    b = lines(*((START_MS + i * 2 + 1, float(-i), i) for i in range(n)))
    root = make_store(tmp_path / "src", {"r1": {"metrics": {"a": a, "b": b}}})
    report = do_import(tmp_path / "s", root)
    assert report.rows[0].points == 2 * n
    reader = RunReader(tmp_path / "s", report.rows[0].run_id)
    assert reader.read_records("a", 0, n)[0][-1] == (n - 1, float(n - 1))
    assert reader.read_records("b", 0, n)[0][5] == (5, -5.0)


def test_batch_size_shrinks_as_the_series_count_grows(tmp_path, monkeypatch):
    seen = []
    original = mlflow_importer._read_metric_file

    def spy(path, stats, batch_lines=mlflow_importer._BATCH_LINES):
        seen.append(batch_lines)
        return original(path, stats, batch_lines)

    monkeypatch.setattr(mlflow_importer, "_read_metric_file", spy)
    monkeypatch.setattr(mlflow_importer, "_BATCH_BUDGET", 6400)
    monkeypatch.setattr(mlflow_importer, "_BATCH_LINES", 2048)
    few = {f"m{i}": lines((START_MS + 1, 1.0, 0), (START_MS + 2, 2.0, 1)) for i in range(4)}
    many = {f"m{i}": lines((START_MS + 1, 1.0, 0), (START_MS + 2, 2.0, 1)) for i in range(400)}
    root = make_store(tmp_path / "src", {"few": {"metrics": few}, "many": {"metrics": many}})
    report = do_import(tmp_path / "s", root)
    assert report.imported == 2
    assert max(seen) == 6400 // 4  # four series: a big batch each
    assert min(seen) == mlflow_importer._MIN_BATCH_LINES  # 6400 // 400 = 16, floored at 64


def test_run_with_one_series_of_equal_steps_and_one_with_steps(tmp_path):
    root = make_store(
        tmp_path / "src",
        {
            "r1": {
                "metrics": {
                    "flat": lines((START_MS + 1, 1.0, 0), (START_MS + 2, 2.0, 0)),
                    "single": lines((START_MS + 1, 9.0, 0)),
                    "real": lines((START_MS + 1, 1.0, 0), (START_MS + 2, 2.0, 5)),
                }
            }
        },
    )
    report = do_import(tmp_path / "s", root)
    store, row = tmp_path / "s", report.rows[0]
    assert series(store, row, "flat") == [(0, 1.0), (1, 2.0)]
    assert series(store, row, "single") == [(0, 9.0)]
    assert series(store, row, "real") == [(0, 1.0), (5, 2.0)]
    assert meta_of(store, row)["imported_from"]["renumbered_steps"] == ["flat"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinks_are_never_followed(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text(lines((START_MS + 1, 666.0, 0)))
    outside = tmp_path / "outside"
    make_store(outside, {"hidden": {"metrics": {"loss": lines((START_MS + 1, 1.0, 0))}}})
    root = make_store(
        tmp_path / "src",
        {
            "r1": {
                "metrics": {"real": lines((START_MS + 1, 1.0, 0))},
                "params": {"p": "1"},
            }
        },
    )
    run = root / "1" / "r1"
    os.symlink(secret, run / "metrics" / "linked")
    os.symlink(secret, run / "params" / "linked_param")
    os.symlink(outside / "mlruns" / "1" / "hidden", root / "1" / "hidden_run")
    os.symlink(outside / "mlruns" / "1", root / "linked_experiment")
    os.symlink(secret, run / "meta2.yaml")
    report = do_import(tmp_path / "s", root)
    assert [row.name for row in report.rows] == ["r1"]
    reader = RunReader(tmp_path / "s", report.rows[0].run_id)
    assert reader.keys() == ["real"]
    assert reader.meta()["config"] == {"p": "1"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs fifos")
def test_a_fifo_in_the_store_does_not_block(tmp_path):
    root = make_store(
        tmp_path / "src",
        {"r1": {"metrics": {"real": lines((START_MS + 1, 1.0, 0))}, "params": {"p": "1"}}},
    )
    run = root / "1" / "r1"
    os.mkfifo(run / "metrics" / "pipe")
    os.mkfifo(run / "params" / "pipe_param")
    report = do_import(tmp_path / "s", root)
    assert report.imported == 1
    assert RunReader(tmp_path / "s", report.rows[0].run_id).keys() == ["real"]


def test_huge_meta_yaml_fails_that_run_only(tmp_path):
    root = make_store(
        tmp_path / "src",
        {"big": {"meta": {"junk": "x" * (2 << 20)}}, "ok": {"metrics": {"m": lines((START_MS, 1, 0))}}},
    )
    report = do_import(tmp_path / "s", root)
    rows = by_name(report)
    assert sorted(rows) == ["big", "ok"]
    assert rows["ok"].status == "imported"
    assert rows["big"].status == "failed" and "meta.yaml" in rows["big"].reason
    assert not any("no active MLflow runs" in w for w in report.warnings)
    assert report.failed == 1


def test_unreadable_meta_yaml_fails_that_run(tmp_path):
    root = make_store(tmp_path / "src", {"bad": {}, "ok": {}})
    meta = root / "1" / "bad" / "meta.yaml"
    os.chmod(meta, 0)
    try:
        if os.access(meta, os.R_OK):
            pytest.skip("permissions are not enforced (running as root)")
        report = do_import(tmp_path / "s", root)
    finally:
        os.chmod(meta, 0o644)
    rows = by_name(report)
    assert rows["bad"].status == "failed" and rows["ok"].status == "imported"


def test_huge_experiment_meta_yaml_is_reported_not_dropped_silently(tmp_path):
    root = make_store(tmp_path / "src", {"lost": {}}, exp_id="1")
    (root / "1" / "meta.yaml").write_text("name: exp\n" + "# " + "x" * (2 << 20) + "\n")
    second = make_store(tmp_path / "src2", {"ok": {}}, exp_id="2")
    shutil.copytree(second / "2", root / "2")
    report = do_import(tmp_path / "s", root)
    assert [row.name for row in report.rows] == ["ok"]
    assert any("experiment meta.yaml is unreadable or larger than" in w for w in report.warnings)


def test_unreadable_experiment_meta_yaml_is_reported(tmp_path):
    root = make_store(tmp_path / "src", {"lost": {}}, exp_id="1")
    meta = root / "1" / "meta.yaml"
    os.chmod(meta, 0)
    try:
        if os.access(meta, os.R_OK):
            pytest.skip("permissions are not enforced (running as root)")
        report = do_import(tmp_path / "s", root)
    finally:
        os.chmod(meta, 0o644)
    assert any("experiment meta.yaml is unreadable" in w for w in report.warnings)


def test_directory_without_meta_yaml_is_not_a_run(tmp_path):
    root = make_store(tmp_path / "src", {"ok": {}})
    (root / "1" / "not_a_run").mkdir()
    report = do_import(tmp_path / "s", root)
    assert [row.name for row in report.rows] == ["ok"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinked_metrics_params_and_tags_directories_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    for folder, name, text in (
        ("m", "loss", lines((START_MS + 1, 666.0, 0))),
        ("p", "id_rsa", "PRIVATE KEY"),
        ("t", "secret", "TAG"),
    ):
        (outside / folder).mkdir(parents=True)
        (outside / folder / name).write_text(text)
    root = make_store(tmp_path / "src", {"r1": {}})
    run = root / "1" / "r1"
    os.symlink(outside / "m", run / "metrics")
    os.symlink(outside / "p", run / "params")
    os.symlink(outside / "t", run / "tags")
    report = do_import(tmp_path / "s", root)
    assert report.imported == 1
    reader = RunReader(tmp_path / "s", report.rows[0].run_id)
    assert reader.keys() == []
    assert reader.meta()["config"] == {}


def test_a_huge_param_value_is_ignored_not_loaded(tmp_path):
    root = make_store(
        tmp_path / "src",
        {"r1": {"params": {"big": "y" * (2 << 20), "small": "v"}}},
    )
    report = do_import(tmp_path / "s", root)
    assert meta_of(tmp_path / "s", report.rows[0])["config"] == {"small": "v"}


def test_deeply_nested_metric_keys_are_capped(tmp_path):
    deep = "/".join(["d"] * (mlflow_importer._MAX_WALK_DEPTH + 4))
    ok = "a/b/c"
    root = make_store(
        tmp_path / "src",
        {"r1": {"metrics": {deep: lines((START_MS, 1, 0)), ok: lines((START_MS, 2, 0))}}},
    )
    report = do_import(tmp_path / "s", root)
    assert RunReader(tmp_path / "s", report.rows[0].run_id).keys() == ["a/b/c"]


def test_status_names_numbers_and_unknown_values(tmp_path):
    runs = {
        "r_int_failed": {"meta": {"status": 4}},
        "r_killed": {"meta": {"status": "KILLED"}},
        "r_str_ok": {"meta": {"status": "FINISHED"}},
        "r_scheduled": {"meta": {"status": 2}},
        "r_unknown": {"meta": {"status": "WEIRD"}},
        "r_no_status": {"meta": {"status": None}},
    }
    root = make_store(tmp_path / "src", runs)
    report = do_import(tmp_path / "s", root, options=ImportOptions(include_running=True))
    rows = by_name(report)
    store = tmp_path / "s"
    assert meta_of(store, rows["r_int_failed"])["status"] == "crashed"
    assert meta_of(store, rows["r_killed"])["status"] == "crashed"
    assert meta_of(store, rows["r_str_ok"])["status"] == "finished"
    assert meta_of(store, rows["r_scheduled"])["imported_from"]["source_status"] == "SCHEDULED"
    assert meta_of(store, rows["r_unknown"])["status"] == "finished"
    assert meta_of(store, rows["r_no_status"])["status"] == "finished"
    assert any("unknown MLflow status" in warning for warning in report.warnings)


def test_run_without_a_usable_start_time_is_a_failed_run(tmp_path):
    root = make_store(
        tmp_path / "src",
        {
            "no_start": {"meta": {"start_time": None, "end_time": None}},
            "absurd": {"meta": {"start_time": 10**30, "end_time": None}},
            "ok": {},
        },
        exp_extra="",
    )
    report = do_import(tmp_path / "s", root)
    rows = by_name(report)
    assert sorted(rows) == ["absurd", "no_start", "ok"]
    assert rows["ok"].status == "imported"
    for name in ("absurd", "no_start"):
        assert rows[name].status == "failed" and "no usable start time" in rows[name].reason
    assert not any("no active MLflow runs" in w for w in report.warnings)


def test_missing_start_time_falls_back_to_the_end_time_or_experiment_time(tmp_path):
    root = make_store(
        tmp_path / "src",
        {"r1": {"meta": {"start_time": None, "end_time": START_MS + 9000}}},
        exp_extra=f"creation_time: {START_MS - 5000}\n",
    )
    report = do_import(tmp_path / "s", root)
    assert meta_of(tmp_path / "s", report.rows[0])["created_at"].startswith("2023-11-14T22:13:29")


def test_end_time_before_start_time_is_ignored(tmp_path):
    root = make_store(tmp_path / "src", {"r1": {"meta": {"end_time": START_MS - 1000}}})
    report = do_import(tmp_path / "s", root)
    assert report.imported == 1


def test_run_name_falls_back_to_the_tag_then_the_run_id(tmp_path):
    root = make_store(
        tmp_path / "src",
        {
            "r_tag": {"meta": {"run_name": None}, "tags": {"mlflow.runName": "from-tag"}},
            "r_none": {"meta": {"run_name": None}},
            "r_empty": {"meta": {"run_name": "''"}, "tags": {"mlflow.runName": "tag2"}},
        },
    )
    names = {row.name for row in do_import(tmp_path / "s", root).rows}
    assert names == {"from-tag", "r_none", "tag2"}


def test_tags_notes_and_params_mapping(tmp_path):
    root = make_store(
        tmp_path / "src",
        {
            "r1": {
                "params": {"lr": "0.1", "nested/param": "x"},
                "tags": {
                    "mlflow.note.content": "my note\nsecond line",
                    "mlflow.user": "someone",
                    "mlflow.source.type": "LOCAL",
                    "owner": "me",
                    "a/b": "c",
                },
            }
        },
    )
    report = do_import(tmp_path / "s", root)
    meta = meta_of(tmp_path / "s", report.rows[0])
    assert meta["notes"] == "my note\nsecond line"
    assert meta["config"] == {
        "lr": "0.1",
        "nested/param": "x",
        "mlflow.tags": {"a/b": "c", "owner": "me"},
    }
    assert meta["tags"] == ["imported:mlflow"]


def test_parent_outside_the_import_is_not_linked(tmp_path):
    root = make_store(tmp_path / "src", {"r1": {"tags": {"mlflow.parentRunId": "someone-else"}}})
    report = do_import(tmp_path / "s", root)
    assert report.imported == 1
    assert meta_of(tmp_path / "s", report.rows[0]).get("parent_run_id") is None


def test_parent_that_is_the_run_itself_is_ignored(tmp_path):
    root = make_store(tmp_path / "src", {"r1": {"tags": {"mlflow.parentRunId": "r1"}}})
    report = do_import(tmp_path / "s", root)
    assert report.imported == 1
    assert meta_of(tmp_path / "s", report.rows[0]).get("parent_run_id") is None


def test_deleted_runs_and_experiments_and_trash_are_skipped(tmp_path):
    root = make_store(
        tmp_path / "src",
        {"keep": {}, "dropped": {"meta": {"lifecycle_stage": "deleted"}}},
    )
    gone = root / "2"
    gone.mkdir()
    (gone / "meta.yaml").write_text("experiment_id: '2'\nlifecycle_stage: deleted\nname: gone\n")
    (gone / "r").mkdir()
    (gone / "r" / "meta.yaml").write_text(f"start_time: {START_MS}\nstatus: 3\nlifecycle_stage: active\n")
    trash = root / ".trash" / "3" / "r"
    trash.mkdir(parents=True)
    (trash / "meta.yaml").write_text(f"start_time: {START_MS}\nstatus: 3\nlifecycle_stage: active\n")
    (root / "models").mkdir()
    report = do_import(tmp_path / "s", root)
    assert [row.name for row in report.rows] == ["keep"]


def test_store_without_runs_warns(tmp_path):
    root = make_store(tmp_path / "src", {})
    report = do_import(tmp_path / "s", root)
    assert report.rows == []
    assert any("no active MLflow runs" in warning for warning in report.warnings)


def test_experiment_without_a_name_gets_a_placeholder_project(tmp_path):
    root = make_store(tmp_path / "src", {"r1": {}}, experiment="''", exp_id="7")
    report = do_import(tmp_path / "s", root)
    assert meta_of(tmp_path / "s", report.rows[0])["project"] == "experiment 7"


def test_project_option_overrides_the_experiment_name(tmp_path):
    report = do_import(tmp_path / "s", FILE_STORE, options=ImportOptions(project="mine"))
    assert {meta_of(tmp_path / "s", row)["project"] for row in report.rows if row.run_id and row.status == "imported"} == {"mine"}


def test_same_run_in_two_stores_gets_two_ids(tmp_path):
    one = make_store(tmp_path / "one", {"r1": {}})
    two = make_store(tmp_path / "two", {"r1": {}})
    a = do_import(tmp_path / "s", one)
    b = do_import(tmp_path / "s", two)
    assert a.imported == 1 and b.imported == 1
    assert a.rows[0].run_id != b.rows[0].run_id


def test_metrics_file_that_disappears_midway_does_not_crash_the_run(tmp_path, monkeypatch):
    root = make_store(tmp_path / "src", {"r1": {"metrics": {"m": lines((START_MS, 1, 0))}}})
    real = mlflow_importer._open_regular

    def flaky(path):
        if str(path).endswith("metrics/m") or str(path).endswith("metrics\\m"):
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(mlflow_importer, "_open_regular", flaky)
    report = do_import(tmp_path / "s", root)
    assert report.imported == 1 and report.rows[0].points == 0


# --- CLI ---------------------------------------------------------------------


def test_cli_imports_the_file_store_and_the_database(tmp_path, capsys):
    store = tmp_path / "s"
    assert cli.main(["import", "mlflow", str(FILE_STORE), "--dir", str(store), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["imported"] == 4 and report["skipped"] == 1 and report["failed"] == 0
    assert cli.main(["import", "mlflow", str(FILE_STORE), "--dir", str(store)]) == 0
    assert "0 imported, 5 skipped, 0 failed" in capsys.readouterr().out
    assert cli.main(["import", "mlflow", str(SQLITE_DB), "--dir", str(tmp_path / "q"), "--include-running"]) == 0
    assert "5 imported, 0 skipped, 0 failed" in capsys.readouterr().out


def test_cli_run_filter_and_project(tmp_path, capsys):
    store = tmp_path / "s"
    assert (
        cli.main(
            ["import", "mlflow", str(SQLITE_DB), "--dir", str(store), "--run", "base*", "--project", "p", "--json"]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in report["runs"]] == ["baseline"]
    assert meta_of(store, type("R", (), {"run_id": report["runs"][0]["run_id"]}))["project"] == "p"


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    store = tmp_path / "s"
    assert cli.main(["import", "mlflow", str(SQLITE_DB), "--dir", str(store), "--dry-run"]) == 0
    assert "would import" in capsys.readouterr().out
    assert not (store / "runs").exists() or not list((store / "runs").iterdir())


def test_cli_bad_paths_exit_two(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["import", "mlflow", str(empty), "--dir", str(tmp_path / "s")]) == 2
    assert "no MLflow data under" in capsys.readouterr().err
    assert cli.main(["import", "mlflow", str(tmp_path / "nope"), "--dir", str(tmp_path / "s")]) == 2
    capsys.readouterr()
