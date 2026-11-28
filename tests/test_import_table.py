"""CSV / JSONL, Lightning metrics.csv and Hugging Face trainer_state.json importer."""

import json
import os
import shutil
from pathlib import Path

import pytest

from agenticml import cli
from agenticml.importers import ImportOptions, ImportSourceError, run_import
from agenticml.importers import table
from agenticml.store import RunReader, list_runs

FIXTURES = Path(__file__).parent / "fixtures" / "import" / "table"
MTIME = 1700000000.0  # a fixed "file modification time" for the copied fixtures


@pytest.fixture()
def src(tmp_path):
    """A writable copy of the fixtures with a fixed modification time."""
    target = tmp_path / "src"
    shutil.copytree(FIXTURES, target, ignore=shutil.ignore_patterns("*.py", "*.md"))
    for path in target.rglob("*"):
        if path.is_file():
            os.utime(path, (MTIME, MTIME))
    return target


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "store"


def do_import(store, path, **kwargs):
    options = kwargs.pop("options", None) or ImportOptions()
    report = run_import(store, "csv", str(path), options, **kwargs)
    return report, options


def reader_for(store, row):
    return RunReader(store, row.run_id)


def points(reader, key):
    return [(step, value) for step, value in reader.series(key)["points"]]


def seconds(reader):
    return [(step, secs) for step, secs in reader.time_index()]


def by_name(report):
    return {row.name: row for row in report.rows}


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


# --- Lightning ------------------------------------------------------------


def test_lightning_directory_imports_one_run_per_version(src, store):
    report, options = do_import(store, src / "lightning")
    rows = by_name(report)
    assert sorted(rows) == ["lightning_logs/version_0", "lightning_logs/version_1"]
    assert report.imported == 2 and report.failed == 0
    meta = reader_for(store, rows["lightning_logs/version_0"]).meta()
    assert meta["name"] == "lightning_logs/version_0" and meta["project"] == "lightning_logs"
    assert meta["status"] == "finished"
    assert meta["imported_from"]["source"] == "csv"
    assert "imported:csv" in meta["tags"]
    # hparams.yaml: flat keys only; the nested list is skipped and reported
    assert meta["config"] == {
        "batch_size": 32,
        "hidden_dim": 128,
        "lr": 0.001,
        "optimizer": "adam",
        "use_bn": True,
        "dropout": None,
    }
    assert any("hparams.yaml" in w and "skipped" in w for w in options.warnings)


def test_lightning_sparse_rows_skip_empty_cells(src, store):
    report, _ = do_import(store, src / "lightning" / "lightning_logs" / "version_0" / "metrics.csv")
    (row,) = report.rows
    reader = reader_for(store, row)
    assert points(reader, "train_loss") == [
        (9, pytest.approx(2.1)),
        (19, pytest.approx(1.8)),
        (29, pytest.approx(1.4)),
        (39, pytest.approx(1.1)),
        (59, pytest.approx(0.85)),
        (69, pytest.approx(0.6)),
    ]
    assert points(reader, "val_loss") == [
        (24, pytest.approx(1.9)),
        (49, pytest.approx(1.2)),
        (74, pytest.approx(0.82)),
    ]
    # epoch is a metric of its own; the step column is not
    assert points(reader, "epoch")[0] == (9, 0.0)
    assert "step" not in reader.keys()
    assert row.points == 6 * 2 + 3 * 2 + 9  # train_acc+train_loss, val_acc+val_loss, epoch
    assert row.dropped_points == 0


def test_lightning_without_a_time_column_uses_the_file_mtime(src, store):
    report, options = do_import(store, src / "lightning" / "lightning_logs" / "version_0" / "metrics.csv")
    (row,) = report.rows
    reader = reader_for(store, row)
    assert reader.meta()["created_at"].startswith("2023-11-14T22:13:20")
    assert {secs for _step, secs in seconds(reader)} == {0.0}
    assert any("no time column" in w and "modification time" in w for w in options.warnings)
    assert not any("new run" in w for w in options.warnings)


def test_touching_a_metrics_file_does_not_import_it_again(src, store):
    path = src / "lightning" / "lightning_logs" / "version_0" / "metrics.csv"
    first, _ = do_import(store, path)
    (row,) = first.rows
    assert row.status == "imported"
    os.utime(path, (MTIME + 86400, MTIME + 86400))
    again, _ = do_import(store, path)
    (second,) = again.rows
    assert second.status == "skipped" and second.run_id == row.run_id
    assert len(list_runs(store)) == 1


def test_same_file_by_directory_or_by_path_is_the_same_run(src, store):
    first, _ = do_import(store, src / "lightning")
    second, _ = do_import(store, src / "lightning" / "lightning_logs" / "version_1" / "metrics.csv")
    (row,) = second.rows
    assert row.status == "skipped"
    assert row.run_id in {r.run_id for r in first.rows}
    again, _ = do_import(store, src / "lightning")
    assert again.skipped == 2 and again.imported == 0


def test_single_lightning_file_names_the_version_and_experiment(src, store):
    report, _ = do_import(store, src / "lightning" / "lightning_logs" / "version_1" / "metrics.csv")
    (row,) = report.rows
    meta = reader_for(store, row).meta()
    assert row.name == "version_1"
    assert meta["name"] == "version_1" and meta["project"] == "lightning_logs"
    assert meta["config"] == {}  # no hparams.yaml next to it


def test_hparams_yaml_is_never_evaluated(src, store):
    version = src / "lightning" / "lightning_logs" / "version_1"
    write(
        version / "hparams.yaml",
        "lr: 0.5\n"
        "evil: !!python/object/apply:os.system ['echo hi']\n"
        "anchor: &a 1\n"
        "ref: *a\n"
        "block: |\n  line one\n  line two\n"
        "nested:\n  inner: 1\n  deeper:\n    x: 2\n"
        "quoted: 'it''s'\n"
        "dq: \"a\\tb\"\n"
        "url: http://example.com:8080/x # comment\n"
        "huge: 123456789012345678901234567890\n",
    )
    report, _ = do_import(store, version / "metrics.csv")
    config = reader_for(store, report.rows[0]).meta()["config"]
    assert config["lr"] == 0.5
    assert config["quoted"] == "it's"
    assert config["dq"] == "a\tb"
    assert config["url"] == "http://example.com:8080/x"
    assert config["huge"] == "123456789012345678901234567890"
    assert "evil" not in config and "anchor" not in config and "block" not in config
    assert "nested" not in config and "inner" not in config


def test_parse_flat_yaml_units():
    parsed, skipped = table.parse_flat_yaml(
        "---\n# comment\na: 1\nb: -2.5\nc: 1.0e-05\nd: ~\ne:\nf: yes\ng: 'x'\n"
        "list:\n- 1\n- 2\nmap:\n  k: v\n\"q key\": 3\nflow: [1, 2]\nbad line\n"
    )
    assert parsed == {"a": 1, "b": -2.5, "c": 1e-05, "d": None, "e": None, "f": "yes", "g": "x",
                      "q key": 3, "flow": [1, 2]}
    assert skipped == 3  # one per skipped entry: list, map, "bad line" (not their items)
    assert table.parse_flat_yaml("") == ({}, 0)
    big, skipped = table.parse_flat_yaml("".join(f"k{i}: {i}\n" for i in range(5000)))
    assert len(big) == 1000 and skipped == 4000


# --- Hugging Face trainer_state.json ----------------------------------------


def test_hf_output_directory_imports_the_directorys_own_state(src, store):
    report, _ = do_import(store, src / "hf")
    rows = by_name(report)
    # checkpoint copies of bert-finetune are not separate runs; the checkpoint-only directory
    # yields its newest checkpoint
    assert sorted(rows) == ["bert-finetune", "only-checkpoints"]
    reader = reader_for(store, rows["bert-finetune"])
    meta = reader.meta()
    assert meta["project"] == "hf" and meta["name"] == "bert-finetune"
    assert meta["config"] == {
        "train_batch_size": 8,
        "max_steps": 30,
        "num_train_epochs": 3,
        "logging_steps": 10,
        "eval_steps": 10,
        "save_steps": 10,
    }
    assert [step for step, _ in points(reader, "loss")] == [10, 20, 30]
    assert [step for step, _ in points(reader, "eval_loss")] == [10, 20]
    assert points(reader, "epoch") == [(10, 1.0), (10, 1.0), (20, 2.0), (20, 2.0), (30, 3.0), (30, 3.0)]
    assert points(reader, "train_runtime") == [(30, pytest.approx(31.5))]
    assert "step" not in reader.keys()
    newest = reader_for(store, rows["only-checkpoints"])
    assert [step for step, _ in points(newest, "loss")] == [10, 20]
    assert newest.meta()["imported_from"]["location"].endswith("checkpoint-20/trainer_state.json")


def test_hf_single_file_without_time_uses_mtime(src, store):
    report, options = do_import(store, src / "hf" / "bert-finetune" / "trainer_state.json")
    (row,) = report.rows
    reader = reader_for(store, row)
    assert row.name == "bert-finetune"
    assert {secs for _step, secs in seconds(reader)} == {0.0}
    assert reader.meta()["created_at"].startswith("2023-11-14T22:13:20")
    assert any("no time column" in w for w in options.warnings)


def test_hf_rejects_files_without_a_log_history(src, store):
    bad = write(src / "bad" / "trainer_state.json", json.dumps({"epoch": 1}))
    with pytest.raises(ImportSourceError, match="log_history"):
        do_import(store, bad)
    invalid = write(src / "bad2" / "trainer_state.json", "{not json")
    with pytest.raises(ImportSourceError, match="not valid JSON"):
        do_import(store, invalid)
    deep = write(src / "bad3" / "trainer_state.json", "[" * 100000)
    with pytest.raises(ImportSourceError):
        do_import(store, deep)


def test_hf_hostile_log_history_rows(src, store):
    state = {
        "log_history": [
            "text",
            None,
            {"loss": 1.0},  # no step
            {"step": -3, "loss": 2.0},  # negative step: dropped by the core
            {"step": 2.5, "loss": 3.0},  # not a whole number
            {"step": 4, "loss": "x", "tag": "a", "nested": {"a": 1}, "ok": 1, "flag": True},
            {"step": 5, "loss": float("nan")},
        ]
    }
    path = write(src / "odd" / "trainer_state.json", json.dumps(state))
    report, options = do_import(store, path)
    (row,) = report.rows
    assert row.status == "imported"
    reader = reader_for(store, row)
    assert points(reader, "ok") == [(4, 1.0)]
    assert [step for step, _ in points(reader, "loss")] == [5]
    assert reader.series("loss")["points"][0][1] != reader.series("loss")["points"][0][1]  # NaN kept
    assert row.dropped_points == 1  # the negative step
    assert any("without a usable step" in w for w in options.warnings)
    assert any("not a JSON object" in w for w in options.warnings)
    assert any("non-numeric" in w for w in options.warnings)


def test_hf_ignores_step_and_time_column_options_with_a_warning(src, store):
    path = src / "hf" / "bert-finetune" / "trainer_state.json"
    report, options = do_import(store, path, options=ImportOptions(step_column="epoch"))
    assert report.imported == 1
    assert any("do not apply" in w for w in options.warnings)


# --- plain CSV and JSONL ------------------------------------------------------


def test_plain_csv_uses_step_and_timestamp_columns(src, store):
    report, options = do_import(store, src / "plain" / "train.csv")
    (row,) = report.rows
    reader = reader_for(store, row)
    meta = reader.meta()
    assert row.name == "train" and meta["project"] == "plain"
    assert meta["created_at"] == "2023-11-14T22:13:20.000000Z"
    assert points(reader, "loss")[0] == (0, 1.0)
    assert [secs for _step, secs in seconds(reader)] == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert meta["finished_at"] == "2023-11-14T22:14:00.000000Z"
    assert "split" not in reader.keys() and "timestamp" not in reader.keys()
    assert any("1 non-numeric column(s): 'split'" in w for w in options.warnings)
    assert not any("no time column" in w for w in options.warnings)


def test_jsonl_flattens_nested_keys_and_reads_millisecond_times(src, store):
    report, options = do_import(store, src / "plain" / "train.jsonl")
    (row,) = report.rows
    reader = reader_for(store, row)
    assert sorted(reader.keys()) == ["lr", "train/loss"]
    assert points(reader, "train/loss")[1] == (1, 0.5)
    assert reader.meta()["created_at"] == "2023-11-14T22:13:20.000000Z"
    assert [secs for _step, secs in seconds(reader)] == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert any("non-numeric column(s): 'note'" in w for w in options.warnings)


def test_step_column_default_order_and_row_number_fallback(tmp_path, store):
    a = write(tmp_path / "a.csv", "epoch,global_step,loss\n0,100,1\n0,200,2\n")
    b = write(tmp_path / "b.csv", "epoch,loss\n5,1\n6,2\n")
    c = write(tmp_path / "c.csv", "loss\n1\n2\n3\n")
    out = {}
    for key, path in (("a", a), ("b", b), ("c", c)):
        report, _ = do_import(store, path)
        out[key] = points(reader_for(store, report.rows[0]), "loss")
    assert [s for s, _ in out["a"]] == [100, 200]  # global_step beats epoch
    assert [s for s, _ in out["b"]] == [5, 6]  # epoch is the step when nothing better exists
    assert [s for s, _ in out["c"]] == [0, 1, 2]  # row number


def test_explicit_step_and_time_columns(tmp_path, store):
    path = write(tmp_path / "x.csv", "it,sec,step,loss\n7,1700000100,0,1\n8,1700000160,0,2\n")
    options = ImportOptions(step_column="it", time_column="sec")
    report, _ = do_import(store, path, options=options)
    reader = reader_for(store, report.rows[0])
    assert points(reader, "loss") == [(7, 1.0), (8, 2.0)]
    assert [secs for _s, secs in seconds(reader)] == [0.0, 60.0]
    assert points(reader, "step") == [(7, 0.0), (8, 0.0)]  # the unused step column is a metric


def test_explicit_columns_are_matched_case_insensitively_and_checked(tmp_path, store):
    path = write(tmp_path / "x.csv", "Step,Loss\n1,2\n")
    report, _ = do_import(store, path, options=ImportOptions(step_column="step"))
    assert points(reader_for(store, report.rows[0]), "Loss") == [(1, 2.0)]
    with pytest.raises(ImportSourceError, match="no column 'nope'"):
        do_import(store, path, options=ImportOptions(step_column="nope"))
    with pytest.raises(ImportSourceError, match="no column 'when'"):
        do_import(store, path, options=ImportOptions(time_column="when"))
    jsonl = write(tmp_path / "x.jsonl", '{"step": 1, "loss": 2}\n')
    with pytest.raises(ImportSourceError, match="no column 'nope'"):
        do_import(store, jsonl, options=ImportOptions(step_column="nope"))


def test_step_and_time_columns_only_apply_to_csv_source(tmp_path, store):
    with pytest.raises(ImportSourceError, match="only apply to the csv source"):
        run_import(store, "tensorboard", str(tmp_path), ImportOptions(step_column="x"))


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("1700000000", 1700000000.0),
        ("1700000000.5", 1700000000.5),
        ("1700000000500", 1700000000.5),  # milliseconds
        ("1700000000500000", 1700000000.5),  # microseconds
        ("1700000000500000000", 1700000000.5),  # nanoseconds
        ("2023-11-14T22:13:20Z", 1700000000.0),
        ("2023-11-14 22:13:20", 1700000000.0),
        ("2023-11-14T22:13:20+01:00", 1699996400.0),
        ("2023-11-14 22:13:20.5", 1700000000.5),  # 1 fraction digit (not accepted by 3.10)
        ("2023-11-14T22:13:20.12", 1700000000.12),
        ("2023-11-14T22:13:20.123456789+00:00", 1700000000.123456),  # nanoseconds
        ("2023-11-14T22:13:20,5Z", 1700000000.5),  # comma decimal separator
        ("12.5", None),  # elapsed seconds are not a timestamp
        ("0", None),
        ("-5", None),
        ("nan", None),
        ("inf", None),
        ("yesterday", None),
        ("", None),
        ("9" * 70, None),
    ],
)
def test_time_parsing(cell, expected):
    assert table._parse_time(cell) == expected


def test_elapsed_time_column_falls_back_to_the_mtime_with_a_warning(tmp_path, store):
    path = write(tmp_path / "x.csv", "step,time,loss\n0,0.5,1\n1,1.5,2\n")
    os.utime(path, (MTIME, MTIME))
    report, options = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert reader.meta()["created_at"].startswith("2023-11-14T22:13:20")
    assert any("no usable timestamps" in w for w in options.warnings)
    assert "time" not in reader.keys()


def test_rows_with_a_missing_time_reuse_the_previous_one(tmp_path, store):
    path = write(tmp_path / "x.csv", "step,timestamp,loss\n0,,1\n1,1700000010,2\n2,,3\n3,1700000030,4\n")
    report, _ = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    # the run starts at the first row that has a time; earlier rows sit at 0
    assert reader.meta()["created_at"].startswith("2023-11-14T22:13:30")
    assert [secs for _s, secs in seconds(reader)] == [0.0, 0.0, 0.0, 20.0]


def test_constant_step_series_is_renumbered_in_file_order(tmp_path, store):
    path = write(tmp_path / "x.csv", "step,loss,acc\n0,1,0.1\n0,2,\n0,3,0.3\n")
    report, _ = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert points(reader, "loss") == [(0, 1.0), (1, 2.0), (2, 3.0)]
    assert points(reader, "acc") == [(0, pytest.approx(0.1)), (1, pytest.approx(0.3))]
    assert reader.meta()["imported_from"]["renumbered_steps"] == ["acc", "loss"]


def test_a_single_point_series_is_not_renumbered(tmp_path, store):
    path = write(tmp_path / "x.csv", "step,loss,final\n3,1,\n4,2,\n5,3,9\n")
    report, _ = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert points(reader, "final") == [(5, 9.0)]
    assert "renumbered_steps" not in reader.meta()["imported_from"]


def test_header_only_and_empty_files(tmp_path, store):
    header_only = write(tmp_path / "h.csv", "step,loss\n")
    report, _ = do_import(store, header_only)
    (row,) = report.rows
    assert row.status == "imported" and row.points == 0 and "config only" in row.reason
    empty = write(tmp_path / "e.csv", "")
    with pytest.raises(ImportSourceError, match="empty"):
        do_import(store, empty)
    blank = write(tmp_path / "b.csv", "\n\n  \n")
    with pytest.raises(ImportSourceError, match="empty"):
        do_import(store, blank)


def test_csv_quirks_bom_crlf_ragged_rows_blank_lines_duplicates(tmp_path, store):
    text = "﻿step,loss,loss,,acc\r\n0,1,9,x,0.1\r\n\r\n1,2\r\n2,3,9,x,0.3,extra,more\r\n"
    path = write(tmp_path / "q.csv", text.encode("utf-8"))
    report, _ = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert sorted(reader.keys()) == ["acc", "loss"]
    assert points(reader, "loss") == [(0, 1.0), (1, 2.0), (2, 3.0)]  # the first duplicate wins
    assert points(reader, "acc") == [(0, pytest.approx(0.1)), (2, pytest.approx(0.3))]


def test_csv_values_nan_inf_and_text_cells(tmp_path, store):
    path = write(tmp_path / "v.csv", "step,loss\n0,nan\n1,inf\n2,abc\n3,1_0\n4,0x10\n5,True\n6,1e3\n7, 4 \n")
    report, options = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    values = reader.series("loss")["points"]
    assert [step for step, _ in values] == [0, 1, 6, 7]
    assert values[0][1] != values[0][1]
    assert values[1][1] == float("inf")
    assert values[2:] == [[6, 1000.0], [7, 4.0]]
    # loss has numeric cells, so it is not an "ignored column"
    assert not any("ignored" in w for w in options.warnings)


def test_rows_without_a_usable_step_are_skipped_and_counted(tmp_path, store):
    path = write(tmp_path / "s.csv", "step,loss\n0,1\n,2\nx,3\n1.5,4\n1.0,5\n2,6\n-1,7\n99999999999,8\n")
    report, options = do_import(store, path)
    (row,) = report.rows
    reader = reader_for(store, row)
    assert points(reader, "loss") == [(0, 1.0), (1, 5.0), (2, 6.0)]
    assert row.dropped_points == 2  # negative and > 2**32-1 steps, dropped by the core
    assert any("skipped 3 row(s) without a usable step" in w for w in options.warnings)


def test_csv_nul_bytes_and_invalid_utf8_do_not_crash(tmp_path, store):
    path = write(tmp_path / "n.csv", b"step,lo\x00ss\n0,1\x00\n1,\xff\xfe2\n2,3\n")
    report, _ = do_import(store, path)
    (row,) = report.rows
    assert row.status == "imported"
    assert [step for step, _ in points(reader_for(store, row), "loss")] == [0, 2]


def test_csv_field_over_the_csv_limit_fails_that_run_only(tmp_path, store):
    good = write(tmp_path / "a_good.csv", "step,loss\n0,1\n")
    bad = write(tmp_path / "b_bad.csv", "step,loss,note\n0,1," + "x" * 200000 + "\n")
    with pytest.raises(ImportSourceError, match="not valid CSV"):
        do_import(store, bad)
    # inside a directory search one bad file is a failed row, the others still import
    folder = tmp_path / "dir" / "version_0"
    write(folder / "metrics.csv", "step,loss,note\n0,1," + "x" * 200000 + "\n")
    write(tmp_path / "dir" / "version_1" / "metrics.csv", "step,loss\n0,1\n")
    report, _ = do_import(store, tmp_path / "dir")
    assert report.imported == 1 and report.failed == 1
    rows = by_name(report)
    assert rows["version_0"].status == "failed" and "not valid CSV" in rows["version_0"].reason
    assert rows["version_1"].status == "imported"
    assert good.exists()


def test_many_columns_are_capped(tmp_path, store):
    header = ",".join(["step"] + [f"m{i}" for i in range(table._MAX_KEYS + 50)])
    row = ",".join(["0"] + ["1"] * (table._MAX_KEYS + 50))
    path = write(tmp_path / "wide.csv", f"{header}\n{row}\n")
    report, options = do_import(store, path)
    (row_,) = report.rows
    assert row_.points == table._MAX_KEYS
    assert any(f"kept the first {table._MAX_KEYS}" in w for w in options.warnings)


def test_jsonl_hostile_lines(tmp_path, store):
    lines = [
        '{"step": 0, "loss": 1}',
        "not json",
        "[1, 2, 3]",
        '"just a string"',
        "",
        "[" * 50000,
        '{"step": 1, "loss": 2, "bad": {"a": {"b": {"c": {"d": 1}}}}}',
        '{"step": 2, "loss": null, "ok": true, "s": "text", "l": [1, 2]}',
        '{"step": "3", "loss": "4"}',
    ]
    path = write(tmp_path / "h.jsonl", "\n".join(lines) + "\n")
    report, options = do_import(store, path)
    (row,) = report.rows
    assert row.status == "imported"
    reader = reader_for(store, row)
    assert points(reader, "loss") == [(0, 1.0), (1, 2.0), (3, 4.0)]
    assert any("not a JSON object" in w for w in options.warnings)
    assert "ok" not in reader.keys()


def test_jsonl_line_over_the_size_cap_is_skipped(tmp_path, store, monkeypatch):
    monkeypatch.setattr(table, "_MAX_LINE", 200)
    path = write(
        tmp_path / "big.jsonl",
        '{"step": 0, "loss": 1}\n' + '{"step": 1, "loss": 2, "pad": "' + "x" * 1000 + '"}\n' + '{"step": 2, "loss": 3}\n',
    )
    report, options = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert points(reader, "loss") == [(0, 1.0), (2, 3.0)]
    assert any("too long" in w for w in options.warnings)


def test_jsonl_without_newline_at_the_end_and_crlf(tmp_path, store):
    path = write(tmp_path / "c.jsonl", b'{"step": 0, "loss": 1}\r\n{"step": 1, "loss": 2}')
    report, _ = do_import(store, path)
    assert points(reader_for(store, report.rows[0]), "loss") == [(0, 1.0), (1, 2.0)]


def test_jsonl_per_line_step_fallback_is_row_number(tmp_path, store):
    path = write(tmp_path / "r.jsonl", '{"loss": 1}\n{"loss": 2}\n')
    report, _ = do_import(store, path)
    assert points(reader_for(store, report.rows[0]), "loss") == [(0, 1.0), (1, 2.0)]


# --- directory search and path handling -------------------------------------


def test_directory_without_known_files_is_an_error(tmp_path, store):
    (tmp_path / "empty").mkdir()
    write(tmp_path / "empty" / "notes.csv", "step,loss\n0,1\n")
    with pytest.raises(ImportSourceError, match="no metrics.csv or trainer_state.json"):
        do_import(store, tmp_path / "empty")


def test_missing_path_and_wrong_extension(tmp_path, store):
    with pytest.raises(ImportSourceError, match="does not exist"):
        do_import(store, tmp_path / "nope.csv")
    other = write(tmp_path / "x.txt", "step,loss\n0,1\n")
    with pytest.raises(ImportSourceError, match="not a .csv or .jsonl"):
        do_import(store, other)


def test_directory_search_does_not_follow_symlinks(tmp_path, store):
    outside = tmp_path / "outside"
    write(outside / "version_9" / "metrics.csv", "step,loss\n0,1\n")
    root = tmp_path / "root"
    write(root / "version_0" / "metrics.csv", "step,loss\n0,1\n")
    (root / "version_1").mkdir()
    try:
        os.symlink(outside, root / "linked_dir", target_is_directory=True)
        os.symlink(outside / "version_9" / "metrics.csv", root / "version_1" / "metrics.csv")
    except OSError:
        pytest.skip("symlinks not available")
    report, _ = do_import(store, root)
    assert [r.name for r in report.rows] == ["version_0"]


def test_hidden_directories_and_deep_trees_are_not_searched(tmp_path, store):
    root = tmp_path / "root"
    write(root / ".hidden" / "metrics.csv", "step,loss\n0,1\n")
    deep = root
    for index in range(table._MAX_WALK_DEPTH + 1):
        deep = deep / f"d{index}"
    write(deep / "metrics.csv", "step,loss\n0,1\n")
    write(root / "ok" / "metrics.csv", "step,loss\n0,1\n")
    report, _ = do_import(store, root)
    assert [r.name for r in report.rows] == ["ok"]


def test_run_glob_filters_before_reading(src, store):
    options = ImportOptions(run_globs=["*version_1"])
    report, _ = do_import(store, src / "lightning", options=options)
    assert len(report.rows) == 1 and report.rows[0].name.endswith("version_1")


def test_dry_run_writes_nothing_but_counts(src, store):
    report, _ = do_import(store, src / "lightning", dry_run=True)
    assert report.imported == 2 and sum(r.points for r in report.rows) > 0
    assert not (store / "runs").exists() or list_runs(store) == []


def test_dedupe_replace_and_nothing_written_to_the_source(src, store):
    before = {p: (p.stat().st_mtime, p.read_bytes()) for p in src.rglob("*") if p.is_file()}
    first, _ = do_import(store, src / "plain" / "train.csv")
    second, _ = do_import(store, src / "plain" / "train.csv")
    assert first.imported == 1 and second.skipped == 1
    third, _ = do_import(store, src / "plain" / "train.csv", replace=True)
    assert third.imported == 1 and third.rows[0].run_id == first.rows[0].run_id
    after = {p: (p.stat().st_mtime, p.read_bytes()) for p in src.rglob("*") if p.is_file()}
    assert before == after
    assert sorted(path.name for path in src.rglob("*") if path.is_file() and path.parent.name == "plain") == [
        "train.csv",
        "train.jsonl",
    ]


def test_same_source_run_id_is_stable_across_import_calls(src, store, tmp_path):
    first, _ = do_import(store, src / "plain" / "train.csv")
    other_store = tmp_path / "other"
    second, _ = do_import(other_store, src / "plain" / "train.csv")
    assert first.rows[0].run_id == second.rows[0].run_id


# --- CLI ----------------------------------------------------------------------


def test_cli_end_to_end_with_options(src, store, capsys):
    path = src / "plain" / "train.csv"
    code = cli.main(
        ["import", "csv", str(path), "--dir", str(store), "--project", "mine", "--step-column", "step",
         "--time-column", "timestamp"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "imported" in out and "1 imported, 0 skipped, 0 failed" in out
    (run,) = list_runs(store)
    assert RunReader(store, run["run_id"]).meta()["project"] == "mine"
    assert cli.main(["import", "csv", str(path), "--dir", str(store)]) == 0
    assert "1 skipped" in capsys.readouterr().out


def test_cli_bad_path_and_bad_column_exit_2(src, store, capsys):
    assert cli.main(["import", "csv", str(src / "missing.csv"), "--dir", str(store)]) == 2
    assert cli.main(["import", "csv", str(src / "plain" / "train.csv"), "--dir", str(store),
                     "--step-column", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


# --- review fixes -------------------------------------------------------------


def test_hparams_yaml_counts_entries_not_continuation_lines(tmp_path):
    parsed, skipped = table.parse_flat_yaml("layers:\n- 64\n- 32\nmap:\n  a: 1\n  b:\n    c: 2\nlr: 0.1\n")
    assert parsed == {"lr": 0.1} and skipped == 2


def test_hparams_yaml_folded_plain_scalar_is_skipped_not_truncated():
    parsed, skipped = table.parse_flat_yaml("name: x\nlong: aaaa bbb\n  ccc ddd\n  eee\nlr: 1\n")
    assert parsed == {"name": "x", "lr": 1} and skipped == 1
    # a quoted scalar over several lines is not supported either: one skip
    parsed, skipped = table.parse_flat_yaml('q: "aa\n  bb"\nz: 1\n')
    assert parsed == {"z": 1} and skipped == 1


def test_hparams_yaml_nested_list_warning_counts_one(src, store):
    report, options = do_import(store, src / "lightning")
    assert any("skipped 1 nested or unsupported entries" in w for w in options.warnings)


def test_jsonl_lines_without_a_step_are_skipped_when_others_have_one(tmp_path, store):
    path = write(
        tmp_path / "mixed.jsonl",
        '{"step": 1, "loss": 1}\n{"eval_loss": 0.5}\n{"step": 2, "loss": 2}\n{"eval_loss": 0.4}\n',
    )
    report, options = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert points(reader, "loss") == [(1, 1.0), (2, 2.0)]
    assert "eval_loss" not in reader.keys()
    assert any("skipped 2 row(s) without a usable step" in w for w in options.warnings)


def test_jsonl_explicit_step_column_also_skips_lines_without_it(tmp_path, store):
    path = write(tmp_path / "mixed.jsonl", '{"it": 3, "loss": 1}\n{"loss": 9}\n{"it": 4, "loss": 2}\n')
    report, _ = do_import(store, path, options=ImportOptions(step_column="it"))
    assert points(reader_for(store, report.rows[0]), "loss") == [(3, 1.0), (4, 2.0)]


def test_directory_where_every_file_is_corrupt_is_failed_rows(tmp_path, store):
    root = tmp_path / "out"
    write(root / "trainer_state.json", "{bad")
    report, _ = do_import(store, root)
    assert report.imported == 0 and report.failed == 1
    (row,) = report.rows
    assert row.status == "failed" and "not valid JSON" in row.reason
    assert list_runs(store) == []


def test_directory_corrupt_csv_and_json_each_fail_alone(tmp_path, store):
    root = tmp_path / "exp"
    write(root / "version_0" / "metrics.csv", "step,loss,note\n0,1," + "x" * 200000 + "\n")
    write(root / "hf" / "trainer_state.json", "[1, 2]")
    report, _ = do_import(store, root)
    assert report.imported == 0 and report.failed == 2


def test_csv_line_over_the_size_cap_fails(tmp_path, store, monkeypatch):
    monkeypatch.setattr(table, "_MAX_LINE", 200)
    path = write(tmp_path / "big.csv", "step,loss\n0,1\n1," + "9" * 1000 + "\n")
    with pytest.raises(ImportSourceError, match="line longer than"):
        do_import(store, path)
    header = write(tmp_path / "bighead.csv", "step," + "m" * 1000 + "\n0,1\n")
    with pytest.raises(ImportSourceError, match="line longer than"):
        do_import(store, header)


def test_control_characters_in_column_names_are_escaped_in_warnings(tmp_path, store):
    path = write(tmp_path / "esc.csv", "step,loss,\x1b[2Jx\n0,1,abc\n")
    report, options = do_import(store, path)
    assert report.imported == 1
    assert options.warnings and all("\x1b" not in w for w in options.warnings)
    assert any("\\x1b[2Jx" in w for w in options.warnings)


def test_directory_file_without_a_header_is_a_failed_row(tmp_path, store):
    root = tmp_path / "exp"
    write(root / "version_0" / "metrics.csv", "step,loss\n0,1\n")
    write(root / "version_1" / "metrics.csv", "")
    report, options = do_import(store, root)
    assert [r.status for r in report.rows] == ["imported", "failed"]
    assert "no header row" in report.rows[1].reason


def test_directory_search_file_cap_is_reported(tmp_path, store, monkeypatch):
    monkeypatch.setattr(table, "_MAX_FILES", 2)
    root = tmp_path / "root"
    for index in range(4):
        write(root / f"version_{index}" / "metrics.csv", "step,loss\n0,1\n")
    report, options = do_import(store, root)
    assert report.imported == 2
    assert any("only the first 2 files were searched" in w for w in options.warnings)


def test_directory_search_depth_limit_is_reported(tmp_path, store, monkeypatch):
    monkeypatch.setattr(table, "_MAX_WALK_DEPTH", 2)
    root = tmp_path / "root"
    write(root / "ok" / "metrics.csv", "step,loss\n0,1\n")
    write(root / "a" / "b" / "c" / "metrics.csv", "step,loss\n0,1\n")
    report, options = do_import(store, root)
    assert [r.name for r in report.rows] == ["ok"]
    assert any("deeper than 2 levels" in w for w in options.warnings)


def test_hparams_yaml_symlink_and_oversize_are_refused(src, store, tmp_path, monkeypatch):
    version = src / "lightning" / "lightning_logs" / "version_0"
    outside = write(tmp_path / "outside.yaml", "secret: 1\n")
    (version / "hparams.yaml").unlink()
    try:
        os.symlink(outside, version / "hparams.yaml")
    except OSError:
        pytest.skip("symlinks not available")
    report, _ = do_import(store, version / "metrics.csv")
    assert reader_for(store, report.rows[0]).meta()["config"] == {}
    assert table._read_small_text(version / "hparams.yaml", 1024) is None

    (version / "hparams.yaml").unlink()
    write(version / "hparams.yaml", "lr: 0.5\n")
    assert table._read_small_text(version / "hparams.yaml", 1024) == "lr: 0.5\n"
    monkeypatch.setattr(table, "_MAX_YAML_BYTES", 4)
    report, _ = do_import(store, version / "metrics.csv", replace=True)
    assert reader_for(store, report.rows[0]).meta()["config"] == {}


def test_hf_state_over_the_size_cap_is_refused(tmp_path, store, monkeypatch):
    monkeypatch.setattr(table, "_MAX_JSON_BYTES", 50)
    path = write(tmp_path / "trainer_state.json", json.dumps({"log_history": [{"step": 1, "loss": 1.0}] * 10}))
    with pytest.raises(ImportSourceError, match="larger than 50 bytes") as caught:
        do_import(store, path)
    assert "not valid JSON" not in str(caught.value)


def test_hf_state_is_parsed_once(tmp_path, store, monkeypatch):
    path = write(
        tmp_path / "trainer_state.json",
        json.dumps({"max_steps": 10, "logging_steps": 2, "log_history": [{"step": 1, "loss": 1.0}]}),
    )
    calls = []
    real = json.loads
    monkeypatch.setattr(table.json, "loads", lambda *a, **k: calls.append(1) or real(*a, **k))
    report, _ = do_import(store, path)
    assert len(calls) == 1
    assert reader_for(store, report.rows[0]).meta()["config"] == {"max_steps": 10, "logging_steps": 2}


def test_jsonl_key_cap(tmp_path, store, monkeypatch):
    monkeypatch.setattr(table, "_MAX_KEYS", 3)
    path = write(tmp_path / "keys.jsonl", '{"step": 0, "a": 1, "b": 2, "c": 3, "d": 4, "e": 5}\n')
    report, options = do_import(store, path)
    assert sorted(reader_for(store, report.rows[0]).keys()) == ["a", "b", "c"]
    assert any("more than 3 metrics" in w for w in options.warnings)


def test_jsonl_step_column_is_chosen_once_per_file(tmp_path, store):
    lines = [
        '{"step": 100, "loss": 1.0, "epoch": 0.5}',
        '{"step": 200, "loss": 0.75, "epoch": 1.0}',
        '{"eval_loss": 0.8, "epoch": 1.0}',
        '{"step": 300, "loss": 0.5, "epoch": 1.5}',
        '{"eval_loss": 0.6, "epoch": 2.0}',
    ]
    path = write(tmp_path / "mixed.jsonl", "\n".join(lines) + "\n")
    report, options = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert points(reader, "loss") == [(100, 1.0), (200, 0.75), (300, 0.5)]
    assert points(reader, "epoch") == [(100, 0.5), (200, 1.0), (300, 1.5)]
    assert "eval_loss" not in reader.keys()
    assert any("skipped 2 row(s) without a usable step" in w for w in options.warnings)


def test_jsonl_time_column_is_chosen_once_per_file(tmp_path, store):
    lines = [
        '{"step": 0, "loss": 1, "timestamp": 1700000000}',
        '{"step": 1, "loss": 2, "time": 1700000999}',
        '{"step": 2, "loss": 3, "timestamp": 1700000020}',
    ]
    path = write(tmp_path / "t.jsonl", "\n".join(lines) + "\n")
    report, _ = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    # "time" is a metric here, not a second time source for line 1
    assert [secs for _s, secs in seconds(reader)] == [0.0, 0.0, 20.0]
    assert [step for step, _v in points(reader, "time")] == [1]


def test_time_fraction_digits_in_a_csv_keep_the_row_time(tmp_path, store):
    path = write(
        tmp_path / "x.csv",
        "step,timestamp,loss\n0,2023-11-14T22:13:20,1\n1,2023-11-14T22:13:21,2\n2,2023-11-14 22:13:22.5,3\n",
    )
    report, _ = do_import(store, path)
    reader = reader_for(store, report.rows[0])
    assert [secs for _s, secs in seconds(reader)] == [0.0, 1.0, 2.5]
