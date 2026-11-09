"""The `agenticml` command line.

`agenticml run` executes a training script inside a tracked run, without any
agenticml code in the script:

    agenticml run --project demo -- python train.py --lr 1e-3
    agenticml python train.py --lr 1e-3
    python -m agenticml run train.py
    torchrun --nproc_per_node 4 -m agenticml run train.py

`agenticml show` inspects a local run; `agenticml export` dumps its metric series.
`agenticml sync` incrementally uploads local runs to a server.
`agenticml edit`, `artifact`, `finish`, and `note` update a finished run.
"""

from __future__ import annotations

import argparse
import errno
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
    LaunchError, RunSettings, Target, parse_config_pair, parse_json_value, parse_log_every,
    parse_positive_float, parse_positive_int, parse_resume, run_target,
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


def cmd_edit(args):
    from .amend import cmd_edit as edit
    try:
        return edit(args)
    except KeyboardInterrupt:
        return 130


def cmd_artifact(args):
    from .amend import cmd_artifact as artifact
    try:
        return artifact(args)
    except KeyboardInterrupt:
        return 130


def cmd_finish(args):
    from .amend import cmd_finish as finish
    try:
        return finish(args)
    except KeyboardInterrupt:
        return 130


def cmd_note(args):
    from .amend import cmd_note as note
    try:
        return note(args)
    except KeyboardInterrupt:
        return 130


def _table_cell(value, *, missing=""):
    """Text for one table cell. ``None`` must not render as the word None."""
    if value is None or value == "":
        return missing
    return str(value)


def _json_safe(value):
    """Replace non-finite floats with None so JSON never emits NaN/Infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _print_table(rows, *, indent=""):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        line = "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        print(f"{indent}{line}" if indent else line)


def _open_run_reader(root, run_id, command):
    from .store import RunReader

    try:
        reader = RunReader(root, run_id)
        return reader, reader.meta()
    except FileNotFoundError:
        print(f"agenticml {command}: no run {run_id!r} in {root}", file=sys.stderr)
        return None, None
    except (OSError, ValueError) as exc:
        print(f"agenticml {command}: {exc}", file=sys.stderr)
        return None, None


def _series_snapshot(reader, key):
    snap = reader.tail(key)
    if snap is not None:
        return snap
    # Fallback: largest step and last finite value (pruned/overview-only series).
    result = reader.series(key, max_points=1)
    points = result.get("points") or []
    count = result.get("total")
    if not points:
        return {"last_step": None, "last": None, "count": count}
    point = points[0]
    mode = result.get("mode")
    if mode == "raw" and len(point) >= 2:
        return {"last_step": point[0], "last": point[1], "count": count}
    if mode == "overview" and len(point) >= 6:
        return {"last_step": point[1], "last": point[5], "count": count}
    return {"last_step": None, "last": None, "count": count}


def _format_metric_last(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return format(value, ".6g")
    return str(value)


def _truncate_cli(text, limit=200):
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _artifact_path(event):
    path = event.get("path")
    if path:
        return str(path)
    sha = event.get("sha256")
    if event.get("copy") and isinstance(sha, str) and sha:
        return sha[:12]
    return "-"


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


def cmd_show(args):
    import time
    from datetime import datetime
    from . import lineage
    from .store.gc import _format_bytes
    from .store.layout import resolve_root
    from .store.reader import _effective_status

    root = resolve_root(args.dir)
    reader, meta = _open_run_reader(root, args.run_id, "show")
    if reader is None:
        return 1

    try:
        status = _effective_status(
            meta, str(reader._meta_path), str(reader.run_dir), time.time(),
        )
        task, task_source = lineage.run_task(meta)
        lin = meta.get("lineage")
        parent = meta.get("parent_run_id") or (lin.get("parent_run_id") if isinstance(lin, dict) else None)
        metrics = {}
        for key in reader.keys():
            try:
                metrics[key] = _series_snapshot(reader, key)
            except (OSError, ValueError, KeyError):
                metrics[key] = None
        artifacts = reader.artifacts()
        media = reader.media()
        media_counts = {name: len(rows) for name, rows in media.items()}
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        print(f"agenticml show: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "run_id": reader.run_id,
            "meta": meta,
            "effective_status": status,
            "task": task,
            "task_source": task_source,
            "metrics": metrics,
            "artifacts": artifacts,
            "media": media_counts,
        }
        try:
            print(json.dumps(
                _json_safe(payload), indent=2, ensure_ascii=False, default=str, allow_nan=False,
            ))
        except RecursionError as exc:
            print(f"agenticml show: {exc}", file=sys.stderr)
            return 1
        return 0

    created = meta.get("created_at") or ""
    try:
        created = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        created = created.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        created = "-"
    if task:
        task_text = f"{task} ({task_source})" if task_source else task
    else:
        task_text = "-"

    fields = [
        ("run", reader.run_id),
        ("project", _table_cell(meta.get("project"))),
        ("name", _table_cell(meta.get("name"), missing="-")),
        ("status", _table_cell(status, missing="-")),
        ("task", task_text),
        ("created", created),
        ("parent", _table_cell(parent, missing="-")),
    ]
    tags = meta.get("tags")
    if isinstance(tags, list) and tags:
        fields.append(("tags", ", ".join(_table_cell(tag) for tag in tags)))
    group = meta.get("group")
    if group not in (None, ""):
        fields.append(("group", str(group)))
    job_type = meta.get("job_type")
    if job_type not in (None, ""):
        fields.append(("job type", str(job_type)))
    notes = meta.get("notes")
    if notes not in (None, ""):
        lines = str(notes).splitlines()
        shown = lines[0] if lines else ""
        if len(lines) > 1:
            shown += " …"
        fields.append(("notes", shown))
    label_width = max(len(label) for label, _ in fields)
    for label, value in fields:
        print(f"{label.ljust(label_width)}  {value}")

    config = meta.get("config")
    config_items = []
    if isinstance(config, dict):
        config_items = [
            (key, config[key]) for key in sorted(config)
            if not (isinstance(key, str) and key.startswith("_"))
        ]
    summary = meta.get("summary")
    summary_items = []
    if isinstance(summary, dict):
        summary_items = [(key, summary[key]) for key in sorted(summary, key=str)]

    has_config = bool(config_items)
    has_summary = bool(summary_items)
    has_metrics = bool(metrics)
    has_artifacts = bool(artifacts)
    has_media = bool(media_counts)
    if has_config or has_summary or has_metrics or has_artifacts or has_media:
        print()

    if has_config:
        print("config")
        for key, value in config_items:
            rendered = _truncate_cli(json.dumps(value, ensure_ascii=False, default=str))
            print(f"  {key} = {rendered}")
    if has_summary:
        print("summary")
        for key, value in summary_items:
            rendered = _truncate_cli(json.dumps(value, ensure_ascii=False, default=str))
            print(f"  {key} = {rendered}")
    if (has_config or has_summary) and (has_metrics or has_artifacts or has_media):
        print()

    if has_metrics:
        print(f"metrics ({len(metrics)})")
        rows = [["KEY", "LAST", "STEP", "POINTS"]]
        for key, snap in metrics.items():
            if snap is None:
                rows.append([key, "?", "?", "?"])
                continue
            last_step = snap.get("last_step")
            rows.append([
                key,
                _format_metric_last(snap.get("last")),
                "-" if last_step is None else str(last_step),
                "-" if snap.get("count") is None else str(snap.get("count")),
            ])
        _print_table(rows, indent="  ")
    if has_artifacts:
        print(f"artifacts ({len(artifacts)})")
        rows = [["NAME", "TYPE", "VERSION", "SIZE", "PATH"]]
        for event in artifacts:
            size = event.get("size")
            rows.append([
                _table_cell(event.get("name")),
                _table_cell(event.get("type")),
                _table_cell(event.get("version"), missing="-"),
                _format_bytes(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else "-",
                _artifact_path(event),
            ])
        _print_table(rows, indent="  ")
    if has_media:
        print(f"media ({len(media_counts)})")
        rows = [[key, f"{count} item(s)"] for key, count in media_counts.items()]
        _print_table(rows, indent="  ")
    return 0


def cmd_export(args):
    import csv
    from .store.layout import resolve_root

    root = resolve_root(args.dir)
    reader, _meta = _open_run_reader(root, args.run_id, "export")
    if reader is None:
        return 1

    try:
        available = reader.keys()
    except (OSError, ValueError, KeyError) as exc:
        print(f"agenticml export: {exc}", file=sys.stderr)
        return 1

    selected = list(args.key) if args.key else list(available)
    have = set(available)
    for key in selected:
        if key not in have:
            print(f"agenticml export: run {args.run_id} has no metric {key!r}", file=sys.stderr)
            return 1

    series = {}
    for key in selected:
        try:
            records, _offset = reader.read_records(key)
        except (OSError, ValueError, KeyError) as exc:
            print(f"agenticml export: {exc}", file=sys.stderr)
            return 1
        if not records:
            try:
                total = reader.series(key, max_points=1).get("total") or 0
            except (OSError, ValueError, KeyError):
                total = 0
            if total > 0:
                print(
                    f"agenticml export: {key}: raw points were pruned by sync --prune; skipped",
                    file=sys.stderr,
                )
                continue
        series[key] = records

    if selected and not series:
        return 1

    handle = None
    try:
        if args.output is not None:
            dest = os.fspath(args.output)
            kwargs = {"encoding": "utf-8"}
            if args.format == "csv":
                kwargs["newline"] = ""
            handle = open(dest, "w", **kwargs)
            out = handle
        else:
            out = sys.stdout
        if args.format == "json":
            payload = {
                "run_id": reader.run_id,
                "series": {
                    key: [[step, value] for step, value in points]
                    for key, points in series.items()
                },
            }
            print(json.dumps(
                _json_safe(payload), indent=2, ensure_ascii=False, default=str, allow_nan=False,
            ), file=out)
        else:
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(["key", "step", "value"])
            for key, points in series.items():
                for step, value in points:
                    writer.writerow([
                        key, step, repr(value) if isinstance(value, float) else value,
                    ])
    except (OSError, ValueError, RecursionError) as exc:
        print(f"agenticml export: {exc}", file=sys.stderr)
        return 1
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
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


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _firebase_from_args(args):
    from .server.firebase import parse_firebase_config

    project = getattr(args, "firebase_project", None)
    if project is None:
        project = os.environ.get("AGENTICML_SERVER_FIREBASE_PROJECT") or None
    api_key = getattr(args, "firebase_api_key", None)
    if api_key is None:
        api_key = os.environ.get("AGENTICML_SERVER_FIREBASE_API_KEY") or None
    auth_domain = getattr(args, "firebase_auth_domain", None)
    if auth_domain is None:
        auth_domain = os.environ.get("AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN") or None
    return parse_firebase_config(project, api_key, auth_domain)


def _limits_from_args(args):
    from .server.limits import Plans

    path = getattr(args, "limits", None) or os.environ.get("AGENTICML_SERVER_LIMITS")
    if not path:
        return None, None
    path = str(Path(path).expanduser().resolve())
    return path, Plans.load(path)


def _open_signup_from_args(args, firebase, plans=None):
    from .server.firebase import require_open_signup

    open_signup = bool(getattr(args, "open_signup", False) or _env_flag("AGENTICML_SERVER_OPEN_SIGNUP"))
    require_open_signup(open_signup, firebase, plans)
    return open_signup


def cmd_server_serve(args):
    from .server import serve
    from .server.db.errors import DatabaseError
    from .server.webauth import parse_identity_header, parse_trusted_proxies

    public_url = args.public_url
    if public_url is None:
        public_url = os.environ.get("AGENTICML_SERVER_PUBLIC_URL") or None
    if args.trusted_proxy is None:
        raw = os.environ.get("AGENTICML_SERVER_TRUSTED_PROXIES", "")
        proxies = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        proxies = args.trusted_proxy
    identity = args.identity_header
    if identity is None:
        identity = os.environ.get("AGENTICML_SERVER_IDENTITY_HEADER") or None
    extra = {}
    if args.objects is not None:
        extra["objects"] = args.objects
    if args.database is not None:
        extra["database"] = args.database
    try:
        _, plans = _limits_from_args(args)
        if plans is not None:
            extra["plans"] = plans
        firebase = _firebase_from_args(args)
        if firebase is not None:
            extra["firebase"] = firebase
        if _open_signup_from_args(args, firebase, plans):
            extra["open_signup"] = True
        serve(args.data, args.host, args.port, public_url,
              trusted_proxies=parse_trusted_proxies(proxies),
              identity_header=parse_identity_header(identity),
              **extra)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, OverflowError, DatabaseError) as exc:
        print(f"agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}", file=sys.stderr)
        return 1
    print("stopped")
    return 0


def _parse_up_port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be 1..65535")
    if port != 0 and not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1..65535")
    return port


def cmd_server_up(args):
    from .server.db.errors import DatabaseError
    from .server.up import UpError, up

    extra = {}
    if args.database is not None:
        extra["database"] = args.database
    try:
        limits_file, plans = _limits_from_args(args)
        if plans is not None:
            extra["plans"] = plans
        if limits_file is not None:
            extra["limits"] = limits_file
        firebase = _firebase_from_args(args)
        if firebase is not None:
            extra["firebase"] = firebase
        if _open_signup_from_args(args, firebase, plans):
            extra["open_signup"] = True
        up(args.data, args.port, install=args.install, force=args.force, **extra)
    except KeyboardInterrupt:
        pass
    except UpError as exc:
        print((str(exc).splitlines() or [type(exc).__name__])[0], file=sys.stderr)
        return 1
    except (OSError, ValueError, OverflowError, DatabaseError) as exc:
        print(f"agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}", file=sys.stderr)
        return 1
    if not args.install:
        print("stopped")
    return 0


def _server_data_dir(args):
    from .server.app import resolve_data_dir

    data = resolve_data_dir(args.data)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = data / "server"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("server directory is a symlink or not a directory")
    return data, directory


_MIGRATE_FAILED = "could not migrate keys.json; fix the file and retry"


def cmd_server_keys(args):
    from .server.audit import AuditLog, cli_actor
    from .server.keys import KeyStore, KeyStoreUnavailable
    from .server.users import UserStore
    from .server.db import DatabaseError, for_server_dir

    try:
        data, directory = _server_data_dir(args)
        actor = cli_actor()
        db = for_server_dir(directory, args.database, actor=actor)
        from .server.db.workspaces import WorkspaceRepo
        wid = WorkspaceRepo(db).require(args.workspace)['id']
        log = AuditLog(directory, db=db, workspace_id=wid)
        keys = KeyStore(directory, env_key=None, db=db, workspace_id=wid)
        if args.keys_action == "list":
            try:
                keys = KeyStore(directory, os.environ.get("AGENTICML_SERVER_KEY"), db=db, workspace_id=wid)
            except ValueError as exc:
                print(f"agenticml server: warning: {exc}; listing file keys only", file=sys.stderr)
        if args.keys_action == "create":
            created_users = []
            record, key = keys.create(
                args.name, args.scope, user=args.user, created_users=created_users)
            if created_users:
                log.record(actor, "cli", "user.add", record.user,
                           detail={"role": created_users[0].role, "identity": None})
            log.record(actor, "cli", "key.create", record.name,
                       detail={"user": record.user, "scope": record.scope})
            print(key)
            print("shown once; store it now", file=sys.stderr)
        elif args.keys_action == "revoke":
            record = keys.revoke(args.name)
            if not record:
                raise ValueError("unknown key name")
            log.record(actor, "cli", "key.revoke", record.name,
                       detail={"user": record.user, "scope": record.scope})
        else:
            user = args.user
            if user is not None:
                from .server.db.workspaces import validate_login
                user = validate_login(user)
                if UserStore(directory, db=db, workspace_id=wid).get(user) is None:
                    raise ValueError("unknown user")
            records = [record.to_public() for record in keys.list()
                       if user is None or record.user == user]
            if args.json:
                print(json.dumps(records))
            else:
                fields = ("name", "scope", "user", "prefix", "created_at", "created_by", "last_used_at")
                _print_table([list(fields)] + [[str(record[field] or "-") for field in fields]
                                              for record in records])
    except KeyboardInterrupt:
        print("stopped")
        return 0
    except (OSError, ValueError, OverflowError, KeyStoreUnavailable, DatabaseError) as exc:
        print(f"agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}", file=sys.stderr)
        return 1
    return 0


def _invite_base(args):
    url = getattr(args, "public_url", None) or os.environ.get("AGENTICML_SERVER_PUBLIC_URL")
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    return None


def _print_invite(token, base):
    if base:
        print(f"{base}/invite?token={token}")
    else:
        print(f"/invite?token={token}")
        print("open it on your server's address")


def _read_cli_password(args):
    if getattr(args, "stdin", False):
        raw = sys.stdin.readline()
        if raw.endswith("\n"):
            raw = raw[:-1]
        if raw.endswith("\r"):
            raw = raw[:-1]
        return raw
    import getpass
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm: ")
    if first != second:
        raise ValueError("passwords do not match")
    return first


def _print_account_workspaces(db, login):
    from .server.db.workspaces import AccountRepo
    slugs = AccountRepo(db).workspace_slugs(login)
    if len(slugs) > 1:
        print(f"{login} belongs to workspaces: {', '.join(slugs)}")


def cmd_server_users(args):
    from .server.audit import AuditLog, cli_actor
    from .server.keys import KeyStore, KeyStoreUnavailable
    from .server.users import UserStore
    from .server.db import DatabaseError, for_server_dir

    try:
        data, directory = _server_data_dir(args)
        actor = cli_actor()
        db = for_server_dir(directory, args.database, actor=actor)
        from .server.db.workspaces import WorkspaceRepo
        wid = WorkspaceRepo(db).require(args.workspace)['id']
        log = AuditLog(directory, db=db, workspace_id=wid)
        users = UserStore(directory, db=db, workspace_id=wid)
        keys = KeyStore(directory, db=db, workspace_id=wid)
        if args.users_action == "add":
            from .server.db.workspaces import AccountRepo
            existing_account = AccountRepo(db).get(args.name)
            user = users.add(args.name, role=args.role, identity=args.identity,
                             reclaim_runs=args.reclaim_runs)
            log.record(actor, "cli", "user.add", user.name,
                       detail={"role": user.role, "identity": user.identity})
            if not args.no_invite and existing_account is None:
                token = users.create_invite(args.name, operator=True)
                log.record(actor, "cli", "user.invite", args.name,
                           detail={"expires_at": users.get(args.name).invite["expires_at"]})
                _print_invite(token, _invite_base(args))
        elif args.users_action == "invite":
            token = users.create_invite(args.name, operator=True)
            log.record(actor, "cli", "user.invite", args.name,
                       detail={"expires_at": users.get(args.name).invite["expires_at"]})
            _print_invite(token, _invite_base(args))
            _print_account_workspaces(db, args.name)
        elif args.users_action == "set-password":
            users.set_password(args.name, _read_cli_password(args), operator=True)
            log.record(actor, "cli", "user.password", args.name, detail={"method": "cli"})
            _print_account_workspaces(db, args.name)
        elif args.users_action == "remove":
            removed_keys = keys.for_user(args.name)
            revoked = users.remove(args.name)
            for record in removed_keys:
                log.record(actor, "cli", "key.revoke", record.name,
                           detail={"user": record.user, "scope": record.scope})
            log.record(actor, "cli", "user.remove", args.name, detail={"keys_revoked": revoked})
            print(f"removed {args.name} ({revoked} keys)")
        elif args.users_action == "set-role":
            previous = users.get(args.name)
            users.set_role(args.name, args.role)
            log.record(actor, "cli", "user.role", args.name,
                       detail={"from": previous.role, "to": args.role})
        elif args.users_action == "set-identity":
            user = users.set_identity(args.name, args.identity, operator=True)
            log.record(actor, "cli", "user.identity", args.name,
                       detail={"identity": user.identity})
            _print_account_workspaces(db, args.name)
        else:
            rows = []
            for user in users.list():
                public = user.to_public()
                public["keys"] = len(keys.for_user(user.name))
                rows.append(public)
            if args.json:
                print(json.dumps(rows))
            else:
                fields = ("name", "role", "identity", "keys", "created_at", "created_by")
                _print_table([list(fields)] + [
                    [str(row[field] if row[field] not in (None, "") else "-") for field in fields]
                    for row in rows
                ])
    except KeyboardInterrupt:
        print("stopped")
        return 0
    except (OSError, ValueError, OverflowError, KeyStoreUnavailable, DatabaseError) as exc:
        print(f"agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}", file=sys.stderr)
        return 1
    return 0


def cmd_server_workspaces(args):
    from .server.audit import AuditLog, cli_actor
    from .server.db import DatabaseError, for_server_dir
    from .server.db.workspaces import WorkspaceRepo, AccountRepo
    from .server.workspaces import prepare_store, delete_workspace

    try:
        data, directory = _server_data_dir(args)
        db = for_server_dir(directory, args.database)
        repo = WorkspaceRepo(db)
        action = args.workspaces_action
        actor = cli_actor()
        log = AuditLog(directory, db=db, workspace_id=None)
        if action == 'list':
            rows = repo.list()
            if args.json:
                print(json.dumps(rows))
            else:
                fields = ('slug', 'name', 'status', 'members', 'created_at')
                _print_table([list(fields)] + [[str(r[k]) for k in fields] for r in rows])
        elif action == 'usage':
            from .server.limits import Plans
            from .server.db.usage import UsageRepo, utc_day
            limits_file = args.limits or os.environ.get('AGENTICML_SERVER_LIMITS')
            plans = Plans.load(limits_file) if limits_file else None
            rows = []
            for workspace in repo.list():
                usage = UsageRepo(db, workspace['id'])
                counters = usage.get()
                limits = (plans.limits_for(workspace) if plans else
                          json.loads(workspace['custom_limits']) if workspace['plan'] == 'custom' else None)
                rows.append({'slug': workspace['slug'], 'plan': workspace['plan'],
                             'stored_bytes': counters['stored_bytes'], 'limits': limits,
                             'upload_bytes': counters['upload_bytes'] if counters['upload_day'] and counters['upload_day'] >= utc_day() else 0,
                             'people': usage.people()})
            rows.sort(key=lambda row: (-row['stored_bytes'], row['slug']))
            if args.json:
                print(json.dumps(rows))
            else:
                def maximum(value):
                    return 'unlimited' if value is None else str(value)
                fields = ('slug', 'plan', 'storage used/limit (bytes)', 'uploads today (bytes)', 'people')
                _print_table([list(fields)] + [[row['slug'], row['plan'],
                    str(row['stored_bytes']) + '/' + (maximum(row['limits']['storage_bytes']) if row['limits'] is not None else 'no limits file'),
                    str(row['upload_bytes']), str(row['people'])] for row in rows])
        elif action == 'set-plan':
            from .server.limits import Plans, UNLIMITED, parse_size, parse_count
            raw = (args.storage, args.uploads_per_day, args.requests_per_minute, args.people)
            if args.plan == 'free' and any(value is not None for value in raw):
                raise ValueError('free plan cannot have custom limits')
            current = repo.require(args.slug)
            custom = None
            if args.plan == 'custom':
                limits_file = args.limits or os.environ.get('AGENTICML_SERVER_LIMITS')
                if current['plan'] == 'custom':
                    custom = json.loads(current['custom_limits'])
                elif limits_file:
                    custom = dict(Plans.load(limits_file).free)
                else:
                    if all(value is None for value in raw):
                        raise ValueError('custom plan requires at least one limit flag without a limits file')
                    custom = dict(UNLIMITED)
                keys = ('storage_bytes', 'upload_bytes_per_day', 'requests_per_minute', 'people')
                for key, value, parse in zip(keys, raw, (parse_size, parse_size, parse_count, parse_count)):
                    if value is not None:
                        custom[key] = parse(value)
            old, new = repo.set_plan(args.slug, args.plan, custom if args.plan == 'custom' else None)
            def plan_values(row):
                return {'plan': row['plan'], 'limits': json.loads(row['custom_limits']) if row['custom_limits'] else None}
            log.record(actor, 'cli', 'workspace.plan', new['id'],
                       detail={'slug': new['slug'], 'old': plan_values(old), 'new': plan_values(new)})
            print(json.dumps(plan_values(new)))
        elif action == 'create':
            owner = args.owner
            if owner is None:
                admins = [r for r in AccountRepo(db).list() if r['server_admin']]
                if len(admins) != 1:
                    raise ValueError('--owner is required when there is not exactly one server admin')
                owner = admins[0]['login']
            row = repo.create(args.slug, args.name, owner)
            prepare_store(data, row['id'])
            log.record(actor, 'cli', 'workspace.create', row['id'], detail={'slug': row['slug'], 'owner': owner})
        elif action == 'delete':
            repo.require(args.slug)
            confirmation = args.yes_delete if args.yes_delete is not None else input('Type the workspace slug to delete it: ')
            if confirmation != args.slug:
                raise ValueError('workspace deletion not confirmed')
            from .server.objectstore import open_backend
            backend = open_backend(args.objects if args.objects is not None else os.environ.get('AGENTICML_SERVER_OBJECTS'), data)
            row = delete_workspace(db, data, args.slug, objects=backend)
            if row.get('object_cleanup_failed'):
                print(f"Warning: object cleanup failed for workspace {row['id']}; bytes are orphaned", file=sys.stderr)
            log.record(actor, 'cli', 'workspace.delete', row['id'], detail={'slug': args.slug})
        elif action == 'recount':
            from .server.db.usage import UsageRepo
            if bool(args.slug) == bool(args.all):
                raise ValueError('specify a workspace slug or --all')
            rows = repo.list() if args.all else [repo.require(args.slug)]
            for row in rows:
                usage = UsageRepo(db, row['id'])
                with db.transaction(write=True) as tx:
                    usage.lock(tx)
                    old = usage.get(tx)['stored_bytes']
                    new = usage.recount(tx)
                log.record(actor, 'cli', 'workspace.recount', row['id'],
                           detail={'slug': row['slug'], 'old_stored_bytes': old, 'new_stored_bytes': new})
                print(f"{row['slug']}: {old} -> {new} stored bytes")
        elif action == 'set-owner':
            row = repo.require(args.slug)
            repo.set_owner(args.slug, args.owner)
            log.record(actor, 'cli', 'workspace.owner', row['id'], detail={'owner': args.owner})
        else:
            row = repo.update(args.slug, name=args.name, new_slug=args.new_slug) if action == 'rename' else repo.update(
                args.slug, status='suspended' if action == 'suspend' else 'active')
            log.record(actor, 'cli', 'workspace.' + action, row['id'], detail={'slug': row['slug']})
        return 0
    except (OSError, ValueError, EOFError, DatabaseError) as exc:
        print(f'agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}', file=sys.stderr)
        return 1


def cmd_server_accounts(args):
    from .server.audit import AuditLog, cli_actor
    from .server.db import DatabaseError, for_server_dir
    from .server.db.workspaces import AccountRepo
    try:
        data, directory = _server_data_dir(args)
        db = for_server_dir(directory, args.database)
        repo = AccountRepo(db)
        if args.accounts_action == 'list':
            rows = repo.list()
            if args.json:
                print(json.dumps(rows))
            else:
                fields = ('login', 'display_name', 'server_admin', 'firebase', 'email_verified', 'created_at')
                table = [list(fields)]
                for row in rows:
                    table.append([
                        row['login'], row['display_name'] or '',
                        str(row['server_admin']),
                        'yes' if row.get('firebase_uid') else 'no',
                        str(row.get('email_verified') or 0),
                        row['created_at'] or '',
                    ])
                _print_table(table)
        elif args.accounts_action == 'add':
            account = repo.create(args.login, display_name=args.display_name, created_by='cli')
            if args.server_admin:
                repo.set_server_admin(account['login'], True)
                account = repo.get(account['login'])
            AuditLog(directory, db=db, workspace_id=None).record(
                cli_actor(), 'cli', 'account.create', account['login'],
                detail={'server_admin': bool(args.server_admin),
                        'display_name': account['display_name']})
        else:
            repo.set_server_admin(args.login, args.value == 'on')
            AuditLog(directory, db=db, workspace_id=None).record(
                cli_actor(), 'cli', 'account.server_admin', args.login, detail={'enabled': args.value == 'on'})
        return 0
    except (OSError, ValueError, DatabaseError) as exc:
        print(f'agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}', file=sys.stderr)
        return 1


def cmd_server_audit(args):
    from .server.audit import cli_main

    return cli_main(args.data, args.limit, args.json, database=args.database,
                    workspace=args.workspace, server=args.server)


def _cmd_server_report(args, verify=False):
    from .server import admin
    from .server.db.errors import DatabaseError

    try:
        report = (admin.verify(args.data, progress=not args.json, database=args.database, objects=args.objects) if verify
                  else admin.status(args.data, database=args.database, objects=args.objects))
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(admin.format_verify(report) if verify else admin.format_status(report))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except DatabaseError as exc:
        print(f"agenticml server: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, OverflowError) as exc:
        print(f"agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}", file=sys.stderr)
        return 2
    return int(not report["ok"]) if verify else 0


def cmd_server_status(args):
    return _cmd_server_report(args)


def cmd_server_verify(args):
    return _cmd_server_report(args, verify=True)


def cmd_server_reindex(args):
    from .server.admin import _data_root
    from .server.app import acquire_server_lock
    from .server.db import for_server_dir, resolve_url, _is_postgres
    from .server.db.errors import DatabaseError, DatabaseUrlError, SchemaTooNew
    from .server.db.repos import RunRepo
    from .server.runs import reindex

    handle = None
    try:
        data, directory = _server_data_dir(args)
        try:
            if not _is_postgres(resolve_url(data, args.database)):
                handle = acquire_server_lock(data / "server")
        except ValueError as exc:
            cause = exc.__cause__
            if isinstance(cause, OSError) and cause.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise ValueError("stop the server first") from exc
            raise
        db = for_server_dir(data / "server", args.database)
        if db.dialect.name == "postgres":
            from contextlib import ExitStack
            handle = ExitStack()
            handle.enter_context(db.server_lock())
        from .server.db.workspaces import WorkspaceRepo
        from .server.workspaces import prepare_store
        workspaces = WorkspaceRepo(db)
        selected = [workspaces.require(args.workspace)] if args.workspace else workspaces.list()
        counts = dict(adopted=0, removed=0, rebuilt=0)
        for workspace in selected:
            result = reindex(prepare_store(data, workspace['id']), RunRepo(db, workspace['id']))
            for key in counts:
                counts[key] += result[key]
        print(f"adopted {counts['adopted']}, removed {counts['removed']}, rebuilt {counts['rebuilt']}")
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (DatabaseError, DatabaseUrlError, SchemaTooNew) as exc:
        print(f"agenticml server: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, OverflowError) as exc:
        print(f"agenticml server: {(str(exc).splitlines() or [type(exc).__name__])[0]}", file=sys.stderr)
        return 2
    finally:
        if handle is not None:
            handle.close()


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
                try:
                    key, item = parse_config_pair(value, "--config")
                except ValueError as exc:
                    raise _UsageError(str(exc)) from None
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
            config = parse_json_value(
                Path(config_file).read_text(encoding="utf-8"),
                "",
            )
        except (OSError, ValueError) as exc:
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
    from . import __version__
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Run a Python script inside a tracked run")

    p_ls = sub.add_parser("ls", help="List local runs newest first")
    p_ls.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_ls.add_argument("--project", default=None, help="Filter by project")
    p_ls.add_argument("--json", action="store_true", help="Print run metadata as JSON")
    p_ls.set_defaults(func=cmd_ls)

    p_show = sub.add_parser("show", help="Show one local run")
    p_show.add_argument("run_id")
    p_show.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_show.add_argument("--json", action="store_true", help="Print run details as JSON")
    p_show.set_defaults(func=cmd_show)

    p_export = sub.add_parser("export", help="Export a run's metric series")
    p_export.add_argument("run_id")
    p_export.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_export.add_argument("--key", action="append", default=None, metavar="K",
                          help="Metric key; repeatable (default: all)")
    p_export.add_argument("--format", choices=["csv", "json"], default="csv",
                          help="Output format (default: csv)")
    p_export.add_argument("-o", "--output", default=None, metavar="PATH",
                          help="Write to PATH instead of stdout")
    p_export.set_defaults(func=cmd_export)

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

    p_server = sub.add_parser("server", help="Serve and administer an on-prem store")
    server_sub = p_server.add_subparsers(dest="server_action", required=True)
    p_audit = server_sub.add_parser("audit", help="Show recent audit events")
    p_audit.add_argument("--data", default=None)
    p_audit.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    scope = p_audit.add_mutually_exclusive_group()
    scope.add_argument('--workspace', default='default')
    scope.add_argument('--server', action='store_true')
    p_audit.add_argument("--limit", type=int, default=50)
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=cmd_server_audit)
    p_serve = server_sub.add_parser("serve", help="Serve protocol v2 ingest")
    p_serve.add_argument("--data", default=None, help="Data directory (default: AGENTICML_SERVER_DATA or ~/.agenticml-server)")
    p_serve.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_serve.add_argument("--objects", default=None, metavar="URL", help="Object backend (default: AGENTICML_SERVER_OBJECTS or file://)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--public-url", default=None, help="Public server URL (default: AGENTICML_SERVER_PUBLIC_URL)")
    p_serve.add_argument("--trusted-proxy", action="append", default=None, metavar="CIDR",
                        help="Trust this proxy CIDR for X-Forwarded-For and identity headers (repeatable)")
    p_serve.add_argument("--identity-header", choices=("alb", "tailscale", "oauth2-proxy"),
                        default=None, help="Honour this proxy identity header (requires --trusted-proxy)")
    p_serve.add_argument("--firebase-project", default=None, metavar="ID",
                        help="Firebase project id (default: AGENTICML_SERVER_FIREBASE_PROJECT)")
    p_serve.add_argument("--firebase-api-key", default=None, metavar="KEY",
                        help="Firebase web API key (default: AGENTICML_SERVER_FIREBASE_API_KEY)")
    p_serve.add_argument("--firebase-auth-domain", default=None, metavar="HOST",
                        help="Firebase auth domain (default: AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN or <project>.firebaseapp.com)")
    p_serve.add_argument("--limits", default=None, metavar="FILE", help="Workspace limits JSON (default: AGENTICML_SERVER_LIMITS)")
    p_serve.add_argument("--open-signup", action="store_true",
                        help="Allow verified Firebase sign-in to create an account (default: AGENTICML_SERVER_OPEN_SIGNUP)")
    p_serve.set_defaults(func=cmd_server_serve)
    p_up = server_sub.add_parser("up", help="Serve on loopback through Tailscale Funnel")
    p_up.add_argument("--data", default=None, help="Data directory (default: AGENTICML_SERVER_DATA or ~/.agenticml-server)")
    p_up.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_up.add_argument("--port", type=_parse_up_port, default=8080)
    p_up.add_argument("--install", action="store_true", help="Write a user service that runs server up, then exit")
    p_up.add_argument("--force", action="store_true", help="Overwrite an existing user service file")
    p_up.add_argument("--firebase-project", default=None, metavar="ID",
                     help="Firebase project id (default: AGENTICML_SERVER_FIREBASE_PROJECT)")
    p_up.add_argument("--firebase-api-key", default=None, metavar="KEY",
                     help="Firebase web API key (default: AGENTICML_SERVER_FIREBASE_API_KEY)")
    p_up.add_argument("--firebase-auth-domain", default=None, metavar="HOST",
                     help="Firebase auth domain (default: AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN or <project>.firebaseapp.com)")
    p_up.add_argument("--limits", default=None, metavar="FILE",
                     help="Workspace limits JSON (default: AGENTICML_SERVER_LIMITS)")
    p_up.add_argument("--open-signup", action="store_true",
                     help="Allow verified Firebase sign-in to create an account (default: AGENTICML_SERVER_OPEN_SIGNUP)")
    p_up.set_defaults(func=cmd_server_up)
    for action, func, help_text in (
            ("status", cmd_server_status, "Show runs, sizes, and key activity"),
            ("verify", cmd_server_verify, "Check objects, run sizes, and lineage")):
        child = server_sub.add_parser(action, help=help_text)
        child.add_argument("--data", default=None)
        child.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
        child.add_argument("--objects", default=None, metavar="URL")
        child.add_argument("--json", action="store_true")
        child.set_defaults(func=func)
    p_reindex = server_sub.add_parser("reindex", help="Rebuild the run index from on-disk files")
    p_reindex.add_argument("--data", default=None)
    p_reindex.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_reindex.add_argument("--workspace", default=None, help="Workspace slug (default: every workspace)")
    p_reindex.set_defaults(func=cmd_server_reindex)
    p_workspaces = server_sub.add_parser('workspaces', help='Manage server workspaces')
    ws_sub = p_workspaces.add_subparsers(dest='workspaces_action', required=True)
    for action in ('list', 'create', 'rename', 'suspend', 'resume', 'delete', 'set-owner', 'recount', 'usage', 'set-plan'):
        child = ws_sub.add_parser(action)
        child.add_argument('--data', default=None)
        child.add_argument('--database', default=None, metavar='URL')
        child.set_defaults(func=cmd_server_workspaces)
        if action in ('list', 'usage'):
            if action == 'usage':
                child.add_argument('--limits', default=None, metavar='FILE',
                                   help='Workspace limits JSON (default: AGENTICML_SERVER_LIMITS)')
            child.add_argument('--json', action='store_true')
            continue
        if action == 'recount':
            child.add_argument('slug', nargs='?')
            child.add_argument('--all', action='store_true')
            continue
        child.add_argument('slug')
        if action == 'set-plan':
            child.add_argument('--limits', default=None, metavar='FILE',
                               help='Base free plan limits JSON (default: AGENTICML_SERVER_LIMITS)')
            child.add_argument('plan', choices=('free', 'custom'))
            child.add_argument('--storage', default=None)
            child.add_argument('--uploads-per-day', default=None)
            child.add_argument('--requests-per-minute', default=None)
            child.add_argument('--people', default=None)
        elif action == 'create':
            child.add_argument('--name', default=None)
            child.add_argument('--owner', default=None)
        elif action == 'rename':
            child.add_argument('--name', required=True)
            child.add_argument('--slug', dest='new_slug', default=None)
        elif action == 'delete':
            child.add_argument('--objects', default=None, metavar='URL')
            child.add_argument('--yes-delete', default=None, metavar='SLUG')
        elif action == 'set-owner':
            child.add_argument('--owner', required=True)
    p_accounts = server_sub.add_parser('accounts', help='Manage global accounts')
    acc_sub = p_accounts.add_subparsers(dest='accounts_action', required=True)
    for action in ('list', 'add', 'set-server-admin'):
        child = acc_sub.add_parser(action)
        child.add_argument('--data', default=None)
        child.add_argument('--database', default=None, metavar='URL')
        child.set_defaults(func=cmd_server_accounts)
        if action == 'list':
            child.add_argument('--json', action='store_true')
        elif action == 'add':
            child.add_argument('login')
            child.add_argument('--display-name', default=None)
            child.add_argument('--server-admin', action='store_true')
        else:
            child.add_argument('login')
            child.add_argument('value', choices=('on', 'off'))
    p_keys = server_sub.add_parser("keys", help="Create, list, or revoke API keys")
    keys_sub = p_keys.add_subparsers(dest="keys_action", required=True)
    for action in ("create", "list", "revoke"):
        child = keys_sub.add_parser(action)
        child.add_argument("--data", default=None)
        child.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
        child.add_argument("--workspace", default="default")
        child.set_defaults(func=cmd_server_keys)
        if action == "create":
            child.add_argument("--name", required=True)
            child.add_argument("--scope", choices=("sync", "admin"), default="sync")
            child.add_argument("--user", default=None)
        elif action == "list":
            child.add_argument("--json", action="store_true")
            child.add_argument("--user", default=None)
        else:
            child.add_argument("name")
    p_users = server_sub.add_parser("users", help="Add, list, remove, or change users")
    users_sub = p_users.add_subparsers(dest="users_action", required=True)
    p_users_add = users_sub.add_parser("add")
    p_users_add.add_argument("name")
    p_users_add.add_argument("--role", choices=("viewer", "member", "admin"), default="member")
    p_users_add.add_argument("--identity", default=None)
    p_users_add.add_argument("--reclaim-runs", action="store_true")
    p_users_add.add_argument("--no-invite", action="store_true")
    p_users_add.add_argument("--public-url", default=None)
    p_users_add.add_argument("--data", default=None)
    p_users_add.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_add.add_argument("--workspace", default="default")
    p_users_add.set_defaults(func=cmd_server_users)
    p_users_invite = users_sub.add_parser("invite")
    p_users_invite.add_argument("name")
    p_users_invite.add_argument("--public-url", default=None)
    p_users_invite.add_argument("--data", default=None)
    p_users_invite.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_invite.add_argument("--workspace", default="default")
    p_users_invite.set_defaults(func=cmd_server_users)
    p_users_password = users_sub.add_parser("set-password")
    p_users_password.add_argument("name")
    p_users_password.add_argument("--stdin", action="store_true")
    p_users_password.add_argument("--data", default=None)
    p_users_password.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_password.add_argument("--workspace", default="default")
    p_users_password.set_defaults(func=cmd_server_users)
    p_users_list = users_sub.add_parser("list")
    p_users_list.add_argument("--json", action="store_true")
    p_users_list.add_argument("--data", default=None)
    p_users_list.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_list.add_argument("--workspace", default="default")
    p_users_list.set_defaults(func=cmd_server_users)
    p_users_remove = users_sub.add_parser("remove")
    p_users_remove.add_argument("name")
    p_users_remove.add_argument("--data", default=None)
    p_users_remove.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_remove.add_argument("--workspace", default="default")
    p_users_remove.set_defaults(func=cmd_server_users)
    p_users_role = users_sub.add_parser("set-role")
    p_users_role.add_argument("name")
    p_users_role.add_argument("role", choices=("viewer", "member", "admin"))
    p_users_role.add_argument("--data", default=None)
    p_users_role.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_role.add_argument("--workspace", default="default")
    p_users_role.set_defaults(func=cmd_server_users)
    p_users_identity = users_sub.add_parser("set-identity")
    p_users_identity.add_argument("name")
    p_users_identity.add_argument("identity", nargs="?", default=None)
    p_users_identity.add_argument("--data", default=None)
    p_users_identity.add_argument("--database", default=None, metavar="URL", help="Server database (default: AGENTICML_SERVER_DATABASE_URL or local SQLite)")
    p_users_identity.add_argument("--workspace", default="default")
    p_users_identity.set_defaults(func=cmd_server_users)

    p_sync = sub.add_parser("sync", help="Incrementally upload local runs to a server")
    p_sync.add_argument("run_ids", nargs="*", metavar="RUN_ID")
    p_sync.add_argument("--all", action="store_true", help="Sync every incomplete or changed run")
    p_sync.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_sync.add_argument("--host", default=None, help="Server base URL (default: saved default host)")
    p_sync.add_argument("--api-key", default=None, help="API key")
    p_sync.add_argument("--watch", type=float, metavar="SECONDS", help="Repeat until all selected runs complete")
    p_sync.add_argument("--follow", action="store_true", help="Keep syncing until stopped (implies --all)")
    p_sync.add_argument("--interval", type=float, metavar="SECONDS", help="Pause between --follow passes (default: 30)")
    p_sync.add_argument("--dry-run", action="store_true", help="Show counts without sending or saving")
    p_sync.add_argument("--artifacts", action="store_true", help="Upload unchanged copy=False artifact files")
    p_sync.add_argument("--prune", action="store_true", help="Delete raw series (.bin) with valid overviews after sync")
    p_sync.add_argument("--reset", action="store_true", help="Forget this host's saved cursor for the given runs; the next sync resumes from the server")
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

    p_edit = sub.add_parser("edit", help="Change tags, notes, group, job type, config, or summary on a finished run")
    p_edit.add_argument("run_id")
    p_edit.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_edit.add_argument("--tag", action="append", default=None, metavar="T", help="Add a tag if absent; repeatable")
    p_edit.add_argument("--untag", action="append", default=None, metavar="T", help="Remove a tag if present; repeatable")
    p_edit.add_argument("--notes", default=None, help="Set the launch note (\"\" clears it)")
    p_edit.add_argument("--group", default=None, metavar="NAME", help="Set the run group (cannot be cleared)")
    p_edit.add_argument("--job-type", default=None, dest="job_type", metavar="NAME",
                        help="Set the job type (cannot be cleared)")
    p_edit.add_argument("--config", action="append", default=None, metavar="KEY=VALUE",
                        help="Merge a config value; repeatable (JSON value or raw string)")
    p_edit.add_argument("--summary", action="append", default=None, metavar="KEY=VALUE",
                        help="Merge a summary value; repeatable (JSON value or raw string)")
    p_edit.add_argument("--unset-summary", action="append", default=None, dest="unset_summary", metavar="KEY",
                        help="Remove a summary key; repeatable")
    p_edit.set_defaults(func=cmd_edit)

    p_artifact = sub.add_parser("artifact", help="Record an artifact on a finished run")
    artifact_sub = p_artifact.add_subparsers(dest="artifact_cmd", required=True)
    p_artifact_add = artifact_sub.add_parser("add", help="Record a file as an artifact")
    p_artifact_add.add_argument("run_id")
    p_artifact_add.add_argument("path")
    p_artifact_add.add_argument("--name", default=None, help="Artifact name (default: file stem)")
    p_artifact_add.add_argument("--type", default="file", help="Artifact type (default: file)")
    p_artifact_add.add_argument("--metadata", default=None, help="JSON object of extra metadata")
    p_artifact_add.add_argument("--copy", action="store_true", help="Copy bytes into the object store")
    p_artifact_add.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_artifact_add.set_defaults(func=cmd_artifact)

    p_finish = sub.add_parser("finish", help="Set the final status of a dead run")
    p_finish.add_argument("run_id")
    p_finish.add_argument("--status", required=True, choices=["finished", "crashed"],
                          help="Final status")
    p_finish.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    p_finish.set_defaults(func=cmd_finish)

    p_note = sub.add_parser(
        "note",
        help="Read or write the run's local note (not synced)",
        description="Read or write the run's local Markdown note (runs/<id>/notes.md); not synced.",
    )
    p_note.add_argument("run_id")
    p_note.add_argument("--dir", default=None, help="Local root (default: AGENTICML_DIR or ~/.agenticml)")
    note_src = p_note.add_mutually_exclusive_group()
    note_src.add_argument("--set", dest="note_text", default=None, metavar="TEXT",
                          help="Replace the notepad text")
    note_src.add_argument("--file", dest="note_file", default=None, metavar="PATH",
                          help="Read notepad text from PATH ('-' = stdin)")
    note_src.add_argument("--clear", action="store_true", help="Clear the notepad")
    p_note.set_defaults(func=cmd_note)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run([])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
