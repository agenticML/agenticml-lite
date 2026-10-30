"""Local list and dashboard command dispatch."""

import json
import os
import sys
import time

from agenticml import cli
from agenticml.store import RunWriter, list_runs


def test_ls_lists_newest_first_filters_and_handles_empty_runs(tmp_path, capsys):
    first = RunWriter.create(tmp_path, run_id="first", meta={"project": "p", "name": "one"})
    first.write_scalars([(10, {"loss": 1})])
    first.write_scalars([(2, {"loss": 2})])
    first.finish("finished")
    second = RunWriter.create(tmp_path, run_id="second", meta={"project": "q", "name": "two"})
    stale = time.time() - 121
    os.utime(second.run_dir / "meta.json", (stale, stale))
    assert cli.main(["ls", "--dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("second") and "running" in lines[1] and lines[1].endswith("-")
    assert lines[2].startswith("first") and "finished" in lines[2] and lines[2].endswith("2")
    assert lines[-1] == "2 runs"
    cli.main(["ls", "--dir", str(tmp_path), "--project", "p"])
    filtered = capsys.readouterr().out
    assert "first" in filtered and "second" not in filtered and "1 run" in filtered
    second.finish("finished")


def test_ls_json_is_list_runs_metadata(tmp_path, monkeypatch, capsys):
    writer = RunWriter.create(tmp_path, meta={"project": "p"})
    writer.finish("finished")
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path))
    assert cli.main(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == list_runs(tmp_path)
    cli.main(["ls", "--project", "absent", "--json"])
    assert json.loads(capsys.readouterr().out) == []


def test_ls_empty_root(tmp_path, capsys):
    assert cli.main(["ls", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.endswith("0 runs\n")


def test_ui_dispatch_defaults_and_options(tmp_path, monkeypatch):
    from agenticml.ui import server

    calls = []
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path))
    monkeypatch.setattr(server, "serve", lambda *args: calls.append(args))
    cli.main(["ui"])
    cli.main(["ui", "--dir", str(tmp_path / "other"), "--host", "0.0.0.0", "--port", "8123", "--open"])
    assert calls == [(tmp_path, "127.0.0.1", 6006, False),
                     (tmp_path / "other", "0.0.0.0", 8123, True)]


def test_ui_bind_errors_are_one_line(tmp_path, capsys):
    from agenticml.ui import server

    held = server.make_server(tmp_path, "127.0.0.1", 0)
    port = held.server_address[1]
    try:
        code = cli.main(["ui", "--dir", str(tmp_path), "--port", str(port)])
    finally:
        held.server_close()
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("agenticml ui:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1

    code = cli.main(["ui", "--dir", str(tmp_path), "--port", "70000"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("agenticml ui:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1

    code = cli.main(["ui", "--dir", str(tmp_path), "--host", "not a host"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("agenticml ui:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1


def test_ui_ctrl_c_prints_stopped(tmp_path, monkeypatch, capsys):
    from agenticml.ui import server

    def serve_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(server.DashboardServer, "serve_forever", serve_forever)
    assert cli.main(["ui", "--dir", str(tmp_path), "--port", "0"]) == 0
    captured = capsys.readouterr()
    assert "stopped" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_du_table_and_json(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="du-run", meta={"project": "proj", "name": "train"})
    writer.write_scalars([(0, {"loss": 0.5})])
    writer.add_media("preview", 0, b"fake-png", "image/png", None)
    writer.finish("finished")

    # Table output
    assert cli.main(["du", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr().out
    assert "RUN ID" in captured
    assert "PROJECT/NAME" in captured
    assert "STATUS" in captured
    assert "SYNCED?" in captured
    assert "RAW" in captured
    assert "OVERVIEW" in captured
    assert "MEDIA" in captured
    assert "TOTAL" in captured
    assert "du-run" in captured
    assert "proj/train" in captured
    assert "Runs dir total:" in captured
    assert "Objects dir total:" in captured
    assert "Total store usage:" in captured

    # JSON output
    assert cli.main(["du", "--dir", str(tmp_path), "--json"]) == 0
    raw_json = capsys.readouterr().out
    data = json.loads(raw_json)
    assert "runs" in data
    assert "runs_total" in data
    assert "objects_total" in data
    assert "unreferenced_total" in data
    assert len(data["runs"]) == 1
    assert data["runs"][0]["run_id"] == "du-run"


def _ls_cells(header, line):
    titles = ("RUN ID", "PROJECT", "NAME", "STATUS", "CREATED", "LAST STEP")
    starts = [header.index(title) for title in titles]
    padded = line.ljust(len(header))
    cells = {}
    for index, title in enumerate(titles):
        end = starts[index + 1] if index + 1 < len(starts) else len(padded)
        cells[title] = padded[starts[index]:end].strip()
    return cells


def test_ls_missing_name_prints_dash_not_none(tmp_path, capsys):
    none_name = RunWriter.create(tmp_path, run_id="none-name", meta={"project": "p", "name": None})
    none_name.finish("finished")
    blank = RunWriter.create(tmp_path, run_id="blank-name", meta={"project": "p", "name": ""})
    blank.finish("finished")
    omitted = RunWriter.create(tmp_path, run_id="omitted-name", meta={"project": "q"})
    omitted.finish("finished")
    named = RunWriter.create(tmp_path, run_id="named", meta={"project": "p", "name": "kept"})
    named.finish("finished")
    literal = RunWriter.create(tmp_path, run_id="literal-none", meta={"project": None, "name": "None"})
    literal.finish("finished")

    assert cli.main(["ls", "--dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    header, *body = lines
    assert body[-1] == "5 runs"
    rows = {_ls_cells(header, line)["RUN ID"]: _ls_cells(header, line) for line in body[:-1]}
    for run_id in ("none-name", "blank-name", "omitted-name"):
        assert rows[run_id]["NAME"] == "-"
        assert rows[run_id]["NAME"] != "None"
    assert rows["named"]["NAME"] == "kept"
    assert rows["none-name"]["PROJECT"] == "p"
    assert rows["literal-none"]["NAME"] == "None"
    assert rows["literal-none"]["PROJECT"] == ""

    assert cli.main(["ls", "--dir", str(tmp_path), "--json"]) == 0
    payload = {row["run_id"]: row for row in json.loads(capsys.readouterr().out)}
    assert payload["none-name"]["name"] is None
    assert payload["named"]["name"] == "kept"
    assert payload["literal-none"]["name"] == "None"

    assert cli.main(["du", "--dir", str(tmp_path)]) == 0
    usage = capsys.readouterr().out
    assert "p/kept" in usage
    assert "None/None" not in usage and "p/None" not in usage


def test_verbose_init_and_sync_label_omit_literal_none(tmp_path, monkeypatch, capsys):
    import agenticml
    from agenticml.store import RunReader
    from agenticml.sync import SyncResult, _run_label, sync_run

    assert _run_label({"project": "demo", "name": "train"}) == "demo/train"
    assert _run_label({"project": 67890, "name": 12345}) == "67890/12345"
    assert _run_label({"project": "p", "name": None}) == "p/-"
    assert _run_label({"project": "p", "name": ""}) == "p/-"
    assert _run_label({"project": "p"}) == "p/-"
    assert _run_label({"project": None, "name": "n"}) == "/n"
    assert "None" not in _run_label({"project": None, "name": None})

    writer = RunWriter.create(tmp_path, run_id="sync-none", meta={"project": "p", "name": None})
    writer.finish("finished")
    result = SyncResult(writer.run_id)
    sync_run(RunReader(tmp_path, writer.run_id), "http://127.0.0.1", None, result, dry_run=True)
    assert result.label == "p/-"
    assert "None" not in result.line()

    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    monkeypatch.delenv("AGENTICML_PROJECT", raising=False)
    monkeypatch.delenv("AGENTICML_NAME", raising=False)
    monkeypatch.delattr(sys.modules["__main__"], "__file__", raising=False)
    run = agenticml.init(
        project="p",
        name=None,
        mode="local",
        dir=tmp_path,
        verbose=True,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
        auto_track=False,
    )
    try:
        out = capsys.readouterr().out
        assert "project=p" in out
        assert "name=-" in out
        assert "name=None" not in out
        assert run.name is None
    finally:
        agenticml.finish()


def test_gc_cli(tmp_path, monkeypatch, capsys):
    writer = RunWriter.create(tmp_path, run_id="gc-run", meta={"project": "p", "name": "n"})
    writer.write_scalars([(0, {"x": 1.0})])
    writer.finish("finished")

    # In tests sys.stdin.isatty() is False -> non-TTY without --yes must refuse with exit 2
    assert cli.main(["gc", "--dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "confirmation required on non-interactive terminal (use --yes)" in err

    # --dry-run succeeds without --yes
    assert cli.main(["gc", "--dir", str(tmp_path), "--dry-run", "--include-unsynced"]) == 0
    out = capsys.readouterr().out
    assert "Would delete" in out
    assert (tmp_path / "runs" / "gc-run").is_dir()

    # With --yes and --include-unsynced -> deletes
    assert cli.main(["gc", "--dir", str(tmp_path), "--yes", "--include-unsynced"]) == 0
    out = capsys.readouterr().out
    assert "Deleted 1 run" in out
    assert not (tmp_path / "runs" / "gc-run").exists()

