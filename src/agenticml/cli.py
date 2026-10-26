"""The `agenticml` command line.

`agenticml run` executes a training script inside a tracked run, without any
agenticml code in the script:

    agenticml run --project demo -- python train.py --lr 1e-3
    agenticml python train.py --lr 1e-3
    python -m agenticml run train.py
    torchrun --nproc_per_node 4 -m agenticml run train.py

`agenticml sync` replays offline journals against a live server.

Offline runs land in AGENTICML_DIR (default ~/.agenticml) under runs/<run_id>/.
Each run dir holds a `journal.jsonl` plus optional `code/`, `artifacts/`,
`media/` subtrees. Sync re-issues each entry against the real server and,
on success, renames the run dir to `<run_id>.synced`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .client import Client, resolve_dir
from .launch import LaunchError, RunSettings, Target, parse_log_every, run_target


logger = logging.getLogger("agenticml.cli")


def _replay_run(client: Client, run_dir: Path) -> bool:
    journal = run_dir / "journal.jsonl"
    if not journal.exists():
        logger.warning("no journal in %s; skipping", run_dir)
        return False

    # The server assigns a real run_id at /init; we map the offline id to it
    # so subsequent entries reference the correct row.
    real_run_id: str | None = None

    with journal.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            entry = json.loads(raw)
            kind = entry["kind"]
            payload = entry["payload"]

            if kind == "init":
                real_run_id = client.init_run(payload)
                continue

            if real_run_id is None:
                logger.error("entry %s before init in %s", kind, run_dir)
                return False

            if kind == "log":
                payload = {**payload, "run_id": real_run_id}
                client.log(payload)
            elif kind == "log_batch":
                items = [
                    {**item, "run_id": real_run_id}
                    for item in payload.get("items", [])
                ]
                client.log_batch(items)
            elif kind == "finalize":
                client.finalize(
                    real_run_id,
                    payload.get("summary") or {},
                    status=payload.get("status", "finished"),
                    snapshot_hash=payload.get("snapshot_hash"),
                )
            elif kind == "upload_code":
                files = []
                for spec in payload.get("files", []):
                    target = run_dir / spec["stored_at"]
                    content = target.read_bytes()
                    files.append({
                        "path": spec["path"],
                        "content_type": spec["content_type"],
                        # Journals written before sha256 was recorded still
                        # replay: recompute it from the stored bytes.
                        "sha256": spec.get("sha256") or hashlib.sha256(content).hexdigest(),
                        "source": spec.get("source", "code"),
                        "content": content,
                    })
                client.upload_code(
                    real_run_id,
                    files,
                )
            elif kind == "log_artifact":
                target = run_dir / payload["stored_at"]
                client.log_artifact(
                    real_run_id,
                    payload["name"],
                    payload.get("type", "file"),
                    payload.get("metadata") or {},
                    str(target),
                    payload.get("content_type") or "application/octet-stream",
                )
            elif kind == "log_media":
                target = run_dir / payload["stored_at"]
                client.log_media(
                    real_run_id,
                    payload["key"],
                    int(payload["step"]),
                    payload.get("kind", "image"),
                    payload.get("caption") or "",
                    str(target),
                    payload.get("content_type") or "application/octet-stream",
                )
            else:
                logger.warning("unknown journal entry kind=%r; skipping", kind)

    return True


def cmd_sync(args):
    root = resolve_dir(args.dir) / "runs"
    if not root.exists():
        print(f"no runs directory at {root}", file=sys.stderr)
        return 1

    client = Client(base_url=args.host, api_key=args.api_key)

    failed = 0
    synced = 0
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if run_dir.name.endswith(".synced"):
            continue
        try:
            if _replay_run(client, run_dir):
                target = run_dir.with_name(run_dir.name + ".synced")
                if target.exists():
                    shutil.rmtree(target)
                run_dir.rename(target)
                synced += 1
            else:
                failed += 1
        except Exception:
            logger.exception("failed to sync %s", run_dir)
            failed += 1

    print(f"synced={synced} failed={failed}")
    return 0 if failed == 0 else 2


RUN_USAGE = """\
usage: agenticml run [options] [--] <script.py> [args ...]
       agenticml run [options] [--] -m <module> [args ...]
       agenticml run [options] [--] <python> [python flags] <script.py | -m module> [args ...]
       agenticml <python> [python flags] <script.py | -m module> [args ...]

Run a Python script or module as __main__ inside an agenticml run.
Options must come before the target; everything after it goes to the script.

options:
  --project NAME       project (env AGENTICML_PROJECT; default: repo directory name)
  --name NAME          run name (env AGENTICML_NAME; default: script or module name)
  --tags A,B           comma-separated tags (env AGENTICML_TAGS)
  --notes TEXT         run notes (env AGENTICML_NOTES)
  --group NAME         run group (env AGENTICML_GROUP)
  --job-type NAME      job type (env AGENTICML_JOB_TYPE)
  --mode MODE          local (default), offline (alias), or disabled (env AGENTICML_MODE)
  --dir DIR            local journal root (env AGENTICML_DIR)
  --no-auto-track      do not detect model/optimizer/loss/dataloader (env AGENTICML_AUTO_TRACK=0)
  --no-auto-metrics    do not log auto/loss, auto/lr, auto/epoch (env AGENTICML_AUTO_METRICS=0)
  --log-every N        log auto metrics every N optimizer steps (env AGENTICML_LOG_EVERY; default 1)
  --no-tensorboard     do not mirror TensorBoard SummaryWriter calls (env AGENTICML_SYNC_TENSORBOARD=0)
  --no-capture-args    do not record argparse/Hydra values in config (env AGENTICML_CAPTURE_ARGS=0)
  --verbose            print the run id and asset paths
  -h, --help           show this help
"""

_RUN_VALUE_OPTIONS = {
    "--project": "project",
    "--name": "name",
    "--tags": "tags",
    "--notes": "notes",
    "--group": "group",
    "--job-type": "job_type",
    "--mode": "mode",
    "--dir": "dir",
    "--log-every": "log_every",
}
_RUN_FLAG_OPTIONS = {
    "--no-auto-track": ("auto_track", False),
    "--no-auto-metrics": ("auto_metrics", False),
    "--no-tensorboard": ("sync_tensorboard", False),
    "--no-capture-args": ("capture_args", False),
    "--verbose": ("verbose", True),
}
# Interpreter flags that consume the next token when not attached (-W ignore).
_PYTHON_FLAGS_WITH_VALUE = {"-W", "-X", "--check-hash-based-pycs"}
_PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)*)?[tdmu]*(\.exe)?$", re.IGNORECASE)


class _UsageError(Exception):
    pass


def _is_python_executable(token: str) -> bool:
    return bool(_PYTHON_NAME.match(os.path.basename(token)))


def _parse_run_options(args):
    """Split leading agenticml options from the target. Returns (settings, raw, rest)."""
    try:
        settings = RunSettings.from_env()
    except ValueError as exc:
        raise _UsageError(str(exc)) from None
    raw: list[str] = []
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
            elif attr == "log_every":
                try:
                    value = parse_log_every(value)
                except ValueError as exc:
                    raise _UsageError(str(exc)) from None
            setattr(settings, attr, value)
            continue
        if token.startswith("-") and token != "-m":
            raise _UsageError(f"unknown option {token!r}")
        break
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
            f"install it there with `{resolved} -m pip install -U agenticml-py`",
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
    sub.add_parser("run", help="Run a script inside a tracked run (see `agenticml run -h`)")

    p_sync = sub.add_parser("sync", help="Replay offline journals against a live server")
    p_sync.add_argument("--dir", default=str(resolve_dir()), help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_sync.add_argument("--host", default=None, help="Server base URL (default: AGENTICML_HOST)")
    p_sync.add_argument("--api-key", default=None, help="API key (default: AGENTICML_API_KEY)")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run([])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
