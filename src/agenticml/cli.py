"""The `agenticml` command line.

`agenticml run` executes a training script inside a tracked run, without any
agenticml code in the script:

    agenticml run --project demo -- python train.py --lr 1e-3
    agenticml python train.py --lr 1e-3
    python -m agenticml run train.py
    torchrun --nproc_per_node 4 -m agenticml run train.py

`agenticml sync` incrementally uploads local runs to a server.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .code_tracking import _normalize_task
from .launch import (
    LaunchError, RunSettings, Target, parse_log_every, parse_positive_float,
    parse_positive_int, parse_resume, run_target,
)
from .store.layout import valid_run_id


logger = logging.getLogger("agenticml.cli")


def cmd_sync(args):
    from .sync import cmd_sync as sync
    return sync(args)


def cmd_login(args):
    from .auth import cmd_login as login
    try:
        return login(args)
    except KeyboardInterrupt:
        return 130


def cmd_logout(args):
    from .auth import cmd_logout as logout
    try:
        return logout(args)
    except KeyboardInterrupt:
        return 130


def cmd_whoami(args):
    from .auth import cmd_whoami as whoami
    try:
        return whoami(args)
    except KeyboardInterrupt:
        return 130


def _table_cell(value, *, missing=""):
    """Text for one table cell. ``None`` must not render as the word None."""
    if value is None or value == "":
        return missing
    return str(value)


def cmd_ls(args):
    import json
    from datetime import datetime
    from .store import RunReader, list_runs
    from .store.layout import resolve_root

    root = resolve_root(args.dir)
    runs = list_runs(root)
    if args.project is not None:
        runs = [run for run in runs if run.get("project") == args.project]
    if args.json:
        print(json.dumps(runs))
        return 0

    rows = [["RUN ID", "PROJECT", "NAME", "STATUS", "CREATED", "LAST STEP"]]
    for run in runs:
        created = run.get("created_at") or ""
        try:
            created = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            created = created.astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            created = "-"
        try:
            last_step = RunReader(root, run["run_id"]).last_step()
        except (OSError, ValueError):
            last_step = None
        rows.append([
            _table_cell(run.get("run_id")),
            _table_cell(run.get("project")),
            _table_cell(run.get("name"), missing="-"),
            _table_cell(run.get("effective_status")),
            created, "-" if last_step is None else str(last_step),
        ])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
    print(f"{len(runs)} run{'s' if len(runs) != 1 else ''}")
    return 0


def cmd_ui(args):
    from .store.layout import resolve_root
    from .ui.server import serve

    try:
        serve(resolve_root(args.dir), args.host, args.port, args.open, args.read_only)
    except KeyboardInterrupt:
        print("stopped")
        return 0
    except (OSError, OverflowError) as exc:
        text = str(exc).strip()
        line = text.splitlines()[0] if text else type(exc).__name__
        print(f"agenticml ui: {line}", file=sys.stderr)
        return 1
    return 0


def cmd_du(args):
    import json
    from .store.gc import _format_bytes, usage
    from .store.layout import resolve_root

    root = resolve_root(args.dir)
    report = usage(root)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    rows = [["RUN ID", "PROJECT/NAME", "STATUS", "SYNCED?", "RAW", "OVERVIEW", "MEDIA", "TOTAL"]]
    for run in report.get("runs", []):
        proj = run.get("project") or ""
        name = run.get("name") or ""
        label = f"{proj}/{name}" if (proj or name) else "-"
        synced_str = "yes" if run.get("synced") else "no"
        rows.append([
            str(run.get("run_id", "")),
            label,
            str(run.get("status", "")),
            synced_str,
            _format_bytes(run.get("raw", 0)),
            _format_bytes(run.get("overview", 0)),
            _format_bytes(run.get("media", 0)),
            _format_bytes(run.get("total", 0)),
        ])

    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())

    print()
    print(f"Runs dir total:        {_format_bytes(report.get('runs_total', 0))}")
    print(
        f"Objects dir total:     {_format_bytes(report.get('objects_total', 0))} "
        f"({report.get('unreferenced_count', 0)} unreferenced: {_format_bytes(report.get('unreferenced_total', 0))})"
    )
    print(f"Lineage repos total:   {_format_bytes(report.get('lineage_total', 0))}")
    print(f"Worktrees total:       {_format_bytes(report.get('worktrees_total', 0))}")
    print(f"Total store usage:     {_format_bytes(report.get('total', 0))}")
    return 0


def cmd_lineage(args):
    from . import lineage
    from .store.layout import resolve_root
    from .store.reader import list_runs

    root = resolve_root(args.dir)
    try:
        if args.lineage_cmd == "checkout":
            print(lineage.checkout(root, args.run_id, args.path))
        elif args.lineage_cmd == "diff":
            print("ADDED  DELETED  STATUS  PATH")
            for entry in lineage.diff(root, args.run_id, args.other_run_id):
                added = "-" if entry["added"] is None else entry["added"]
                deleted = "-" if entry["deleted"] is None else entry["deleted"]
                print(f"{added}  {deleted}  {entry['status']}  {entry['path']}")
        else:
            runs = list_runs(root)
            inferred = lineage.infer_parents(runs)
            projects = {}
            for meta in runs:
                try:
                    project, rid = meta.get("project", ""), meta["run_id"]
                    if not isinstance(project, str) or not isinstance(rid, str):
                        continue
                    task, _ = lineage.run_task(meta)
                    if args.task is not None and task != args.task:
                        continue
                    projects.setdefault(project, {}).setdefault(task, {})[rid] = meta
                except (KeyError, TypeError, AttributeError):
                    continue
            for project, tasks in projects.items():
                if args.project is not None and project != args.project:
                    continue
                print(project)
                for task, members in tasks.items():
                    print(f"  [{task or 'no task'}]")
                    children = {}
                    for rid, meta in members.items():
                        try:
                            info = meta.get("lineage") or {}
                            parent = info.get("parent_run_id") or meta.get("parent_run_id")
                            if not parent or parent == rid:
                                parent, _ = inferred.get(rid, (None, None))
                            if not isinstance(parent, str):
                                parent = None
                        except (RuntimeError, TypeError, OSError, ValueError, AttributeError):
                            parent = None
                        children.setdefault(parent if parent in members else None, []).append(rid)
                    seen = set()
                    # One iterative traversal, including roots of explicit cycles.
                    for first in [*children.get(None, []), *members]:
                        stack = [(first, 2)]
                        while stack:
                            rid, depth = stack.pop()
                            if rid in seen:
                                continue
                            seen.add(rid)
                            meta = members[rid]
                            print(f"{'  ' * depth}{rid}  {meta.get('name') or ''}".rstrip())
                            stack.extend((child, depth + 1) for child in reversed(children.get(rid, [])))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"agenticml lineage: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_gc(args):
    from .store.gc import _format_bytes, collect
    from .store.layout import resolve_root

    root = resolve_root(args.dir)
    dry_run = getattr(args, "dry_run", False)
    yes = getattr(args, "yes", False)
    older_than = getattr(args, "older_than", None)
    keep_last = getattr(args, "keep_last", None)
    include_unsynced = getattr(args, "include_unsynced", False)

    if not dry_run and not yes:
        if not sys.stdin.isatty():
            print(
                "agenticml gc: confirmation required on non-interactive terminal (use --yes)",
                file=sys.stderr,
            )
            return 2
        preview = collect(
            root,
            older_than_days=older_than,
            keep_last=keep_last,
            include_unsynced=include_unsynced,
            dry_run=True,
        )
        runs_count = preview["runs_deleted"]
        runs_bytes = preview["runs_bytes_freed"]
        objs_count = preview["objects_deleted"]
        objs_bytes = preview["objects_bytes_freed"]
        total_bytes = preview["total_bytes_freed"]
        if runs_count == 0 and objs_count == 0:
            print("No runs or objects eligible for garbage collection.")
            return 0
        print(
            f"Would delete {runs_count} run(s) ({_format_bytes(runs_bytes)}) and "
            f"{objs_count} unreferenced object(s) ({_format_bytes(objs_bytes)})."
        )
        print(f"Total space to free: {_format_bytes(total_bytes)}")
        try:
            answer = input("Proceed with garbage collection? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    report = collect(
        root,
        older_than_days=older_than,
        keep_last=keep_last,
        include_unsynced=include_unsynced,
        dry_run=dry_run,
    )
    prefix = "Would delete" if dry_run else "Deleted"
    print(
        f"{prefix} {report['runs_deleted']} run(s) ({_format_bytes(report['runs_bytes_freed'])}) and "
        f"{report['objects_deleted']} object(s) ({_format_bytes(report['objects_bytes_freed'])}). "
        f"Total space freed: {_format_bytes(report['total_bytes_freed'])}"
    )
    for path in report.get("worktrees_kept", []):
        print(f"Kept user-owned checkout: {path}")
    return 0


RUN_USAGE = """\
usage: agenticml run [options] [--] <script.py> [args ...]
       agenticml run [options] [--] -m <module> [args ...]
       agenticml run [options] [--] <python> [python flags] <script.py | -m module> [args ...]
       agenticml <python> [python flags] <script.py | -m module> [args ...]

Run a Python script or module as __main__ inside an agenticml run.
Options must come before the target; everything after it goes to the script.

options:
  --project NAME       project (env AGENTICML_PROJECT; default: repo or directory name)
  --name NAME          run name (env AGENTICML_NAME; default: entry file path relative to the repo root (absolute outside a repo), or the module name)
  --tags A,B           comma-separated tags (env AGENTICML_TAGS)
  --notes TEXT         run notes (env AGENTICML_NOTES)
  --config KEY=VALUE   set a config value; repeatable (JSON value or raw string)
  --config-file PATH   read config from a JSON object (env AGENTICML_CONFIG_FILE)
  --group NAME         run group (env AGENTICML_GROUP)
  --job-type NAME      job type (env AGENTICML_JOB_TYPE)
  --task NAME          run task (env AGENTICML_TASK; default: parent's task, agenticml.toml task, else the entry script's folder)
  --parent RUN_ID      parent run for lineage (env AGENTICML_PARENT; default: inferred)
  --allow-cross-task   do not warn when --task differs from the --parent run's task (env AGENTICML_ALLOW_CROSS_TASK=1)
  --id RUN_ID          run id to resume (env AGENTICML_RUN_ID)
  --resume MODE        never, allow, or must (env AGENTICML_RESUME; --id requires allow or must)
  --mode MODE          local (default), offline (alias), or disabled (env AGENTICML_MODE)
  --dir DIR            local store root (default: $AGENTICML_DIR or ~/.agenticml)
  --no-auto-track      do not detect model/optimizer/loss/dataloader (env AGENTICML_AUTO_TRACK=0)
  --no-auto-metrics    do not log auto/loss, auto/lr, auto/epoch (env AGENTICML_AUTO_METRICS=0)
  --log-every N        log auto metrics every N optimizer steps (env AGENTICML_LOG_EVERY; default 1)
  --no-tensorboard     do not mirror TensorBoard SummaryWriter calls (env AGENTICML_SYNC_TENSORBOARD=0)
  --no-capture-args    do not record argparse/Hydra values in config (env AGENTICML_CAPTURE_ARGS=0)
  --no-system-metrics  do not sample system metrics (env AGENTICML_SYSTEM_METRICS=0)
  --system-metrics-interval S  system sampling interval in seconds (env AGENTICML_SYSTEM_METRICS_INTERVAL; default 10.0)
  --no-lineage         do not snapshot lineage (env AGENTICML_LINEAGE=0)
  --no-code            do not track source files (env AGENTICML_TRACK_CODE=0)
  --no-env             do not capture environment metadata (env AGENTICML_CAPTURE_ENV=0)
  --track-file PATH    extra source file; repeatable (env AGENTICML_TRACK_FILES; path-separated)
  --source-root DIR    source directory; repeatable (env AGENTICML_SOURCE_ROOTS; path-separated)
  --max-file-bytes N   source size cap per file (env AGENTICML_MAX_FILE_BYTES)
  --max-total-bytes N  total source size cap (env AGENTICML_MAX_TOTAL_BYTES)
  --flush-interval S   flush interval in seconds (env AGENTICML_FLUSH_INTERVAL; default 2)
  --all-ranks          track every distributed rank (env AGENTICML_ALL_RANKS=1)
  --verbose            print the run id and asset paths
  -h, --help           show this help
"""

_RUN_VALUE_OPTIONS = {
    "--project": "project",
    "--name": "name",
    "--tags": "tags",
    "--notes": "notes",
    "--config": "config",
    "--config-file": "config_file",
    "--group": "group",
    "--job-type": "job_type",
    "--task": "task",
    "--parent": "parent_run_id",
    "--id": "id",
    "--resume": "resume",
    "--mode": "mode",
    "--dir": "dir",
    "--log-every": "log_every",
    "--system-metrics-interval": "system_metrics_interval",
    "--track-file": "extra_files",
    "--source-root": "source_roots",
    "--max-file-bytes": "max_file_bytes",
    "--max-total-bytes": "max_total_bytes",
    "--flush-interval": "flush_interval",
}
_RUN_FLAG_OPTIONS = {
    "--allow-cross-task": ("allow_cross_task", True),
    "--no-auto-track": ("auto_track", False),
    "--no-auto-metrics": ("auto_metrics", False),
    "--no-tensorboard": ("sync_tensorboard", False),
    "--no-capture-args": ("capture_args", False),
    "--no-system-metrics": ("system_metrics", False),
    "--no-lineage": ("lineage", False),
    "--no-code": ("track_source", False),
    "--no-env": ("capture_env", False),
    "--all-ranks": ("only_main_process", False),
    "--verbose": ("verbose", True),
}
# Interpreter flags that consume the next token when not attached (-W ignore).
_PYTHON_FLAGS_WITH_VALUE = {"-W", "-X", "--check-hash-based-pycs"}
_PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)*)?[tdmu]*(\.exe)?$", re.IGNORECASE)


class _UsageError(Exception):
    pass


class _NonFiniteConfigValue(ValueError):
    pass


def _reject_config_constant(value):
    raise _NonFiniteConfigValue("must not be NaN or Infinity")


def _parse_config_float(value):
    result = float(value)
    if not math.isfinite(result):
        _reject_config_constant(value)
    return result


def _is_python_executable(token: str) -> bool:
    return bool(_PYTHON_NAME.match(os.path.basename(token)))


def _parse_run_options(args):
    """Split leading agenticml options from the target. Returns (settings, raw, rest)."""
    try:
        settings = RunSettings.from_env()
    except ValueError as exc:
        raise _UsageError(str(exc)) from None
    raw: list[str] = []
    config_file = (os.environ.get("AGENTICML_CONFIG_FILE") or "").strip() or None
    config_pairs = {}
    seen_lists = set()
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token in ("-h", "--help"):
            raise _UsageError(None)
        if token in _RUN_FLAG_OPTIONS:
            attr, value = _RUN_FLAG_OPTIONS[token]
            setattr(settings, attr, value)
            raw.append(token)
            index += 1
            continue
        option, sep, attached = token.partition("=")
        if option in _RUN_VALUE_OPTIONS:
            if sep:
                value = attached
                raw.append(token)
                index += 1
            else:
                if index + 1 >= len(args):
                    raise _UsageError(f"{option} expects a value")
                value = args[index + 1]
                raw.extend([token, value])
                index += 2
            attr = _RUN_VALUE_OPTIONS[option]
            if attr == "tags":
                value = [t.strip() for t in value.split(",") if t.strip()]
            elif attr == "task":
                normalized = _normalize_task(value)
                if normalized is None:
                    raise _UsageError("--task must be a non-empty name of at most 128 characters "
                                      "without control characters")
                value = normalized
            elif attr == "parent_run_id":
                if not valid_run_id(value):
                    raise _UsageError("--parent must be a valid run id")
            elif attr == "id":
                if not valid_run_id(value):
                    raise _UsageError("--id must be a valid run id")
            elif attr == "resume":
                try:
                    value = parse_resume(value)
                except ValueError as exc:
                    raise _UsageError(str(exc)) from None
            elif attr == "config":
                key, equals, item = value.partition("=")
                if not equals or not key:
                    raise _UsageError("--config expects KEY=VALUE with a non-empty key")
                try:
                    config_pairs[key] = json.loads(
                        item, parse_constant=_reject_config_constant, parse_float=_parse_config_float,
                    )
                except _NonFiniteConfigValue:
                    raise _UsageError(f"--config value for {key!r} must not be NaN or Infinity") from None
                except (ValueError, RecursionError):
                    config_pairs[key] = item
                continue
            elif attr == "config_file":
                config_file = value
                continue
            elif attr in {"extra_files", "source_roots"}:
                if attr not in seen_lists:
                    setattr(settings, attr, [])
                    seen_lists.add(attr)
                getattr(settings, attr).append(value)
                continue
            elif attr in {"log_every", "max_file_bytes", "max_total_bytes",
                          "system_metrics_interval", "flush_interval"}:
                try:
                    if attr == "log_every":
                        value = parse_log_every(value)
                    elif attr in {"max_file_bytes", "max_total_bytes"}:
                        value = parse_positive_int(value, option[2:])
                    else:
                        value = parse_positive_float(value, option[2:])
                except ValueError as exc:
                    raise _UsageError(str(exc)) from None
            setattr(settings, attr, value)
            continue
        if token.startswith("-") and token != "-m":
            raise _UsageError(f"unknown option {token!r}")
        break
    if settings.id is not None and settings.resume not in {"allow", "must"}:
        raise _UsageError("--id needs --resume allow or must")
    if settings.resume in {"allow", "must"} and settings.id is None:
        raise _UsageError("--resume needs --id")
    if config_file is not None:
        try:
            config = json.loads(
                Path(config_file).read_text(encoding="utf-8"),
                parse_constant=_reject_config_constant, parse_float=_parse_config_float,
            )
        except (OSError, ValueError, RecursionError) as exc:
            raise _UsageError(f"cannot read config file {config_file!r}: {exc}") from None
        if not isinstance(config, dict):
            raise _UsageError(f"config file {config_file!r} must contain a JSON object")
        settings.config.update(config)
    settings.config.update(config_pairs)
    return settings, raw, args[index:]


def _parse_python_command(rest):
    """Parse `<python> [flags] <script | -m module> [args]`. Returns (flags, target)."""
    flags: list[str] = []
    index = 1
    while index < len(rest):
        token = rest[index]
        if token == "-m":
            if index + 1 >= len(rest):
                raise _UsageError("-m expects a module name")
            return flags, Target("module", rest[index + 1], rest[index + 2:])
        if token == "-c" or token.startswith("-c") and not token.startswith("--"):
            raise _UsageError("python -c is not supported; put the code in a script")
        if token == "-" or not token.startswith("-"):
            break
        flags.append(token)
        if token in _PYTHON_FLAGS_WITH_VALUE and index + 1 < len(rest):
            flags.append(rest[index + 1])
            index += 1
        index += 1
    if index >= len(rest):
        raise _UsageError("missing script to run")
    if rest[index] == "-":
        raise _UsageError("reading the script from stdin is not supported")
    return flags, Target("script", rest[index], rest[index + 1:])


def _parse_target(rest):
    if not rest:
        raise _UsageError("missing script to run")
    if rest[0] == "-m":
        if len(rest) < 2:
            raise _UsageError("-m expects a module name")
        return Target("module", rest[1], rest[2:])
    return Target("script", rest[0], rest[1:])


def _exec_interpreter(python, flags, raw_options, target):
    """Re-run `agenticml run` under another interpreter, replacing this process."""
    resolved = shutil.which(python)
    if resolved is None:
        print(f"agenticml: {python}: command not found", file=sys.stderr)
        return 127
    probe = subprocess.run(
        [resolved, "-c", "import importlib.util, sys; sys.exit(importlib.util.find_spec('agenticml.__main__') is None)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        print(
            f"agenticml: {resolved} has no agenticml with `run` support; "
            f"install it there with `{resolved} -m pip install -U agenticml-lite`",
            file=sys.stderr,
        )
        return 1
    head = ["-m", target.value] if target.kind == "module" else [target.value]
    cmd = [resolved, *flags, "-m", "agenticml", "run", *raw_options, "--", *head, *target.argv]
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        return subprocess.call(cmd)
    os.execv(resolved, cmd)
    return 0  # unreachable


def _is_current_interpreter(python: str) -> bool:
    resolved = shutil.which(python)
    if resolved is None or not sys.executable:
        return False
    return os.path.abspath(resolved) == os.path.abspath(sys.executable)


def cmd_run(args):
    try:
        settings, raw_options, rest = _parse_run_options(args)
        if rest and _is_python_executable(rest[0]):
            flags, target = _parse_python_command(rest)
            if flags or not _is_current_interpreter(rest[0]):
                return _exec_interpreter(rest[0], flags, raw_options, target)
        else:
            target = _parse_target(rest)
    except _UsageError as exc:
        if exc.args and exc.args[0]:
            usage = RUN_USAGE.split("\n\n", 1)[0]
            print(f"{usage}\nagenticml run: error: {exc.args[0]}", file=sys.stderr)
            return 2
        print(RUN_USAGE, end="")
        return 0

    try:
        return run_target(target, settings)
    except LaunchError as exc:
        print(f"agenticml run: {exc}", file=sys.stderr)
        return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    if argv and _is_python_executable(argv[0]):
        return cmd_run(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="agenticml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Run a Python script inside a tracked run")

    p_ls = sub.add_parser("ls", help="List local runs newest first")
    p_ls.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_ls.add_argument("--project", default=None, help="Filter by project")
    p_ls.add_argument("--json", action="store_true", help="Print run metadata as JSON")
    p_ls.set_defaults(func=cmd_ls)

    p_lineage = sub.add_parser("lineage", help="Inspect run code snapshots")
    p_lineage.add_argument("--dir", default=None, help="Local store root")
    lineage_sub = p_lineage.add_subparsers(dest="lineage_cmd", required=True)
    for command in ("checkout", "diff", "ls"):
        child = lineage_sub.add_parser(command)
        child.add_argument("--dir", default=argparse.SUPPRESS, help="Local store root")
        child.set_defaults(func=cmd_lineage)
        if command == "ls":
            child.add_argument("--project", default=None)
            child.add_argument("--task", default=None)
        else:
            child.add_argument("run_id")
            if command == "checkout":
                child.add_argument("--path", default=None)
            else:
                child.add_argument("other_run_id", nargs="?", default=None)

    p_du = sub.add_parser("du", help="Show local run and object storage usage")
    p_du.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_du.add_argument("--json", action="store_true", help="Print usage totals as JSON")
    p_du.set_defaults(func=cmd_du)

    p_gc = sub.add_parser("gc", help="Garbage collect runs and unreferenced objects")
    p_gc.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_gc.add_argument("--older-than", type=float, metavar="DAYS", help="Only delete runs older than DAYS")
    p_gc.add_argument("--keep-last", type=int, metavar="N", help="Keep the N newest runs per project")
    p_gc.add_argument("--include-unsynced", action="store_true", help="Include unsynced runs (default: synced only)")
    p_gc.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    p_gc.add_argument("-y", "--yes", action="store_true", help="Confirm deletion without prompting")
    p_gc.set_defaults(func=cmd_gc)

    p_ui = sub.add_parser("ui", help="Serve the local run dashboard")
    p_ui.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=6006)
    p_ui.add_argument("--open", action="store_true", help="Open the dashboard in a browser")
    p_ui.add_argument("--read-only", action="store_true", help="Disable note editing in the dashboard")
    p_ui.set_defaults(func=cmd_ui)

    p_sync = sub.add_parser("sync", help="Incrementally upload local runs to a server")
    p_sync.add_argument("run_ids", nargs="*", metavar="RUN_ID")
    p_sync.add_argument("--all", action="store_true", help="Sync every incomplete or changed run")
    p_sync.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_sync.add_argument("--host", default=None, help="Server base URL")
    p_sync.add_argument("--api-key", default=None, help="API key")
    p_sync.add_argument("--watch", type=float, metavar="SECONDS", help="Repeat until all selected runs complete")
    p_sync.add_argument("--dry-run", action="store_true", help="Show counts without sending or saving")
    p_sync.add_argument("--prune", action="store_true", help="Delete raw series (.bin) with valid overviews after sync")
    p_sync.add_argument("--reset", action="store_true", help="Clear saved remote run state for the given runs on this host")
    p_sync.add_argument("--insecure", action="store_true", help="Allow http:// to a non-local host")
    p_sync.set_defaults(func=cmd_sync)

    p_login = sub.add_parser("login", help="Validate and save an API key")
    p_login.add_argument("--host", default=None, help="Server base URL")
    p_login.add_argument("--key", default=None, help="API key (default: prompt or stdin)")
    p_login.add_argument("--no-verify", action="store_true", help="Save without checking the server")
    p_login.add_argument("--insecure", action="store_true", help="Allow http:// to a non-local host")
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="Remove saved credentials for a host")
    p_logout.add_argument("--host", default=None, help="Server base URL (default: saved default host)")
    p_logout.set_defaults(func=cmd_logout)

    p_whoami = sub.add_parser("whoami", help="Show the current host, key prefix, and validity")
    p_whoami.add_argument("--host", default=None, help="Server base URL")
    p_whoami.add_argument("--offline", action="store_true", help="Skip server verification")
    p_whoami.add_argument("--insecure", action="store_true", help="Allow http:// to a non-local host")
    p_whoami.set_defaults(func=cmd_whoami)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run([])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
