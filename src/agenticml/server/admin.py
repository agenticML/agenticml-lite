"""Read-only reports for an on-disk server store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

from agenticml import lineage
from agenticml.store import layout
from agenticml.store.gc import _format_bytes
from agenticml.store.reader import _remote_status, run_references
from .app import resolve_data_dir
from .keys import KeyStore, KeyStoreUnavailable
from .db import ConvertedTwice, DatabaseError, DatabaseUrlError, SchemaTooNew, for_server_dir
from .db.inventory import InventoryRepo
from .db.repos import RunRepo
from .db.usage import UsageRepo
from .db.workspaces import WorkspaceRepo
from .runs import is_run_file
from .workspaces import store_root
from .lineage import _git
from .users import UserStore

_STATUSES = ("running", "finished", "crashed", "stale", "unsynced")
_OBJECT_TMP = re.compile(r"\.(?:tmp|[0-9a-f]{64})\.\d+\.\d+\.[0-9a-f]{8}\.tmp\Z")


def _data_root(data, database=None):
    data = resolve_data_dir(data)
    for path in (data, data / "server"):
        try:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError(f"not a directory or is a symlink: {path}")
            with os.scandir(path):
                pass
        except FileNotFoundError:
            raise ValueError(f"no server data at {data}") from None
    try:
        db = for_server_dir(data / 'server', database, create=False)
        default = WorkspaceRepo(db).get('default') if db else None
    except (SchemaTooNew, DatabaseUrlError, ConvertedTwice):
        raise
    except DatabaseError:
        default = None
    root = store_root(data, default['id']) if default else data / 'store'
    if root.is_symlink():
        raise ValueError("store is a symlink")
    return data, root


def _safe_path(root, path):
    try:
        resolved = layout.contained(root, path)
    except RuntimeError as exc:
        raise ValueError(f"refusing symlinked path: {path}") from exc
    if resolved != path:
        raise ValueError(f"refusing symlinked path: {path}")
    return path


def _walk(path, on_error, *, missing_ok=True, descend=None):
    try:
        info = path.lstat()
    except OSError as exc:
        if not (missing_ok and isinstance(exc, FileNotFoundError)):
            on_error(path, exc)
        return
    yield path, info
    if not stat.S_ISDIR(info.st_mode) or (descend is not None and not descend(path)):
        return
    try:
        with os.scandir(path) as entries:
            children = sorted((Path(entry.path) for entry in entries))
    except OSError as exc:
        on_error(path, exc)
        return
    for child in children:
        yield from _walk(child, on_error, missing_ok=False, descend=descend)


def _object_url(objects):
    return objects if objects is not None else os.environ.get('AGENTICML_SERVER_OBJECTS', '')


def _selected_workspaces(db, workspace):
    """(all workspace rows, rows to report, whether the report needs a workspace column)."""
    rows = WorkspaceRepo(db).list()
    chosen = rows
    if workspace is not None:
        chosen = [row for row in rows if row['slug'] == workspace]
        if not chosen:
            raise ValueError(f'unknown workspace {workspace}')
    return rows, chosen, not (len(rows) == 1 and rows[0]['slug'] == 'default')


def status(data, *, database=None, objects=None, workspace=None) -> dict:
    data, root = _data_root(data, database)
    url = _object_url(objects)
    s3 = url.startswith('s3://')
    warnings = []
    db = None
    rows, chosen, multi = [], [], False
    try:
        db = for_server_dir(data / 'server', database, create=False)
        if db is not None:
            rows, chosen, multi = _selected_workspaces(db, workspace)
        else:
            for name in ('users.json', 'keys.json'):
                try:
                    (data / 'server' / name).lstat()
                except OSError:
                    continue
                warnings.append(
                    'found 2.x users.json/keys.json with no database; '
                    'start the server to import them')
                break
    except (SchemaTooNew, DatabaseUrlError, ConvertedTwice):
        raise
    except DatabaseError:
        db = None
        warnings.append('could not load server database; key and user status is unknown')
    if workspace is not None and db is None:
        raise ValueError(f'unknown workspace {workspace}')
    # Without a readable database the legacy single store is reported as-is.
    entries = [(row, store_root(data, row['id'])) for row in chosen] if db is not None else [(None, root)]
    now = time.time()
    parts = []
    for row, store in entries:
        parts.append(_status_workspace(data, store, db, row, s3, now, warnings, db is not None))
    counts = dict.fromkeys(_STATUSES, 0)
    sizes = {"objects_count": 0, "skipped": 0, "runs": 0, "objects": 0, "lineage": 0}
    keys, users = [], []
    for part in parts:
        for name, count in part["runs"]["by_status"].items():
            counts[name] = counts.get(name, 0) + count
        for name in ("objects_count", "skipped", "runs", "objects", "lineage"):
            sizes[name] += part["sizes"][name]
        for kind, target in (("keys", keys), ("users", users)):
            for item in part.pop("_" + kind):
                if multi:
                    item = dict(item, workspace=part["slug"])
                target.append(item)
    sizes["total"] = sum(sizes[name] for name in ("runs", "objects", "lineage"))
    report = {"data": str(data), "runs": {"total": sum(p["runs"]["total"] for p in parts), "by_status": counts},
              "sizes": sizes, "keys": keys, "users": users, "warnings": warnings}
    if db is not None:
        report["workspaces"] = parts
    return report


def _status_workspace(data, root, db, row, s3, now, warnings, keys_loaded):
    wid = row['id'] if row else None
    server_dir = data / 'server'
    records, user_records = [], []
    if db is not None:
        try:
            records = KeyStore(server_dir, db=db, workspace_id=wid).list()
            user_records = UserStore(server_dir, db=db, workspace_id=wid).list()
        except KeyStoreUnavailable:
            keys_loaded = False
            message = 'could not load server database; key and user status is unknown'
            if message not in warnings:
                warnings.append(message)
    keys = []
    for record in records:
        public = record.to_public()
        public.pop('created_by')
        public.update(runs=0, last_sync_at=None)
        keys.append(public)
    by_key = {(public['name'], public['user']): public for public in keys}
    users = [{"name": user.name, "role": user.role, "keys": 0, "runs": 0, "last_sync_at": None}
             for user in user_records]
    by_user = {item["name"]: item for item in users}
    for record in records:
        if record.user in by_user:
            by_user[record.user]["keys"] += 1

    def key_row(name, user):
        pair = (name, user)
        if pair not in by_key:
            item = dict(name=name, user=user, scope=None, prefix=None, created_at=None,
                        last_used_at=None, runs=0, last_sync_at=None)
            if name == "env":
                item["env"] = True
            elif keys_loaded:
                item["revoked"] = True
            keys.append(item)
            by_key[pair] = item
        return by_key[pair]

    def last_sync(item, stamp):
        if stamp and (item["last_sync_at"] is None or
                      layout.parse_timestamp(stamp) > layout.parse_timestamp(item["last_sync_at"])):
            item["last_sync_at"] = stamp

    _safe_path(root, root / "runs")
    run_rows = []
    if db is not None:
        try:
            run_rows = RunRepo(db, wid).all_rows()
        except DatabaseError:
            warnings.append('could not load runs from the server database')
    counts = dict.fromkeys(_STATUSES, 0)
    for run in run_rows:
        if run["status"]:
            effective = _remote_status(
                {"status": run["status"], "last_sync_at": run["last_sync_at"]}, now)
        else:
            effective = "unsynced"
        counts[effective] = counts.get(effective, 0) + 1
        owner_user = run["owner_user"] if isinstance(run["owner_user"], str) else None
        owner_key = run["owner_key"] if isinstance(run["owner_key"], str) else None
        stamp = run["last_sync_at"]
        writer = run["last_sync_key"] if isinstance(run["last_sync_key"], str) else None
        if owner_user in by_user:
            by_user[owner_user]["runs"] += 1
            last_sync(by_user[owner_user], stamp)
        if owner_key is not None and owner_user is not None:
            key_row(owner_key, owner_user)["runs"] += 1
            last_sync(key_row(writer or owner_key, owner_user), stamp)
    sizes = {"objects_count": 0, "skipped": 0}

    def skipped(path, exc):
        sizes["skipped"] += 1

    recorded = {}
    if db is not None:
        try:
            recorded = InventoryRepo(db, wid).objects()
        except DatabaseError:
            warnings.append('could not load objects from the server database')
    for name in ("runs", "objects", "lineage"):
        sizes[name] = 0
        if s3 and name == "objects":
            sizes[name] = sum(recorded.values())
            sizes["objects_count"] = len(recorded)
            continue
        for path, info in _walk(root / name, skipped):
            if not stat.S_ISREG(info.st_mode):
                continue
            sizes[name] += info.st_size
            if (name == "objects" and len(path.relative_to(root / name).parts) == 2
                    and layout.valid_sha(path.name) and path.parent.name == path.name[:2]):
                sizes["objects_count"] += 1
    sizes["total"] = sum(sizes[name] for name in ("runs", "objects", "lineage"))
    part = {"slug": row['slug'] if row else 'default', "status": row['status'] if row else None,
            "plan": row.get('plan') if row else None, "members": row.get('members') if row else None,
            "runs": {"total": len(run_rows), "by_status": counts}, "sizes": sizes,
            "objects": {"count": len(recorded), "size": sum(recorded.values())},
            "stored_bytes": None, "upload_bytes_today": None,
            "keys_count": len(records), "_keys": keys, "_users": users}
    if db is not None:
        try:
            usage = UsageRepo(db, wid)
            current = usage.get()
            part["stored_bytes"] = current["stored_bytes"]
            part["upload_bytes_today"] = current["upload_bytes"] if current["upload_day"] == usage.day() else 0
        except DatabaseError:
            warnings.append('could not load usage from the server database')
    return part


def verify(data, *, progress=False, database=None, objects=None, workspace=None, deep=False) -> dict:
    data, root = _data_root(data, database)
    db = for_server_dir(data / 'server', database, create=False)
    report = {"ok": True, "checked": {"objects": 0, "runs": 0}, "problems": [], "warnings": []}
    rows, chosen, multi = [], [], False
    if db is not None:
        rows, chosen, multi = _selected_workspaces(db, workspace)
    elif workspace is not None:
        raise ValueError(f'unknown workspace {workspace}')
    backend = None
    url = _object_url(objects)
    if db is not None and url.startswith('s3://'):
        from .objectstore import open_backend
        backend = open_backend(url, data)
    # Without a database the legacy single store is checked as-is.
    entries = [(row, store_root(data, row['id'])) for row in chosen] if db is not None else [(None, root)]
    per_workspace = {}
    for row, store in entries:
        slug = row['slug'] if row else 'default'
        before = dict(report["checked"])

        def add(kind, path, detail, *, warning=False, _slug=slug):
            item = {"kind": kind, "path": str(path.relative_to(data)), "detail": detail}
            if multi:
                item["workspace"] = _slug
            report["warnings" if warning else "problems"].append(item)

        _verify_workspace(data, store, db, row['id'] if row else None, backend, deep, progress, report, add)
        per_workspace[slug] = {name: report["checked"][name] - before[name] for name in before}
    if multi:
        report["workspaces"] = per_workspace
    report["ok"] = not report["problems"]
    return report


def _hash_stream(handle):
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _verify_workspace(data, root, db, wid, backend, deep, progress, report, add):
    def object_error(path, exc):
        add("object_type", path, str(exc))

    s3 = backend is not None
    recorded = InventoryRepo(db, wid).objects() if db is not None else None
    objects = root / "objects"
    prefixes = set()
    on_disk = {}

    def object_directory(path):
        return path == objects or (path.parent == objects
                                   and re.fullmatch(r"[0-9a-f]{2}", path.name) is not None)

    for path, info in (() if s3 else _walk(objects, object_error, descend=object_directory)):
        parts = path.relative_to(objects).parts
        if stat.S_ISREG(info.st_mode) and _OBJECT_TMP.fullmatch(path.name):
            continue
        if stat.S_ISDIR(info.st_mode):
            if not object_directory(path):
                add("object_name", path, "expected objects/<hh>/<sha256>")
            elif parts:
                prefixes.add(path.name)
            continue
        if not stat.S_ISREG(info.st_mode):
            add("object_type", path, "not a regular file")
            continue
        report["checked"]["objects"] += 1
        if len(parts) != 2 or not layout.valid_sha(path.name) or parts[0] != path.name[:2]:
            add("object_name", path, "expected objects/<hh>/<sha256>")
        else:
            on_disk[path.name] = info.st_size
            try:
                with layout.open_nofollow(path) as handle:
                    found = _hash_stream(handle)
                if found != path.name:
                    add("object_hash", path, f"SHA-256 is {found}")
            except (OSError, ValueError) as exc:
                add("object_hash", path, str(exc))
        if progress and sys.stderr.isatty() and report["checked"]["objects"] % 1000 == 0:
            print(f"verify: checked {report['checked']['objects']} objects", file=sys.stderr)

    if s3:
        from agenticml.store.errors import ObjectStoreUnavailable
        try:
            for sha, size in backend.inventory(wid):
                on_disk[sha] = size
                report["checked"]["objects"] += 1
                if deep and recorded is not None and recorded.get(sha) == size:
                    with backend.open(wid, sha) as handle:
                        found = _hash_stream(handle)
                    if found != sha:
                        add("object_hash", root / "objects" / sha[:2] / sha, f"SHA-256 is {found}")
        except (ObjectStoreUnavailable, FileNotFoundError) as exc:
            add("object_store", root / "objects", str(exc) or "object store unavailable")
            on_disk = None

    runs = _safe_path(root, root / "runs")
    try:
        with os.scandir(runs) as entries:
            directories = sorted(Path(entry.path) for entry in entries if layout.valid_run_id(entry.name))
    except FileNotFoundError:
        directories = []
    git_available = shutil.which("git") is not None
    for directory in directories:
        try:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ValueError("not a run directory")
        except (OSError, ValueError) as exc:
            add("run_unreadable", directory, str(exc))
            continue
        report["checked"]["runs"] += 1
        _verify_run_files(root, directory, add, db, wid)
        try:
            refs = run_references(root, directory.name)
            for sha in sorted(set().union(*refs.values())):
                path = root / "objects" / sha[:2] / sha
                if recorded is not None and sha not in recorded:
                    add("object_missing", path, f"referenced by run {directory.name}")
                elif s3:
                    if on_disk is not None and sha not in on_disk:
                        add("object_missing", path, f"referenced by run {directory.name}")
                elif not _referenced_object(objects, sha, prefixes):
                    add("object_missing", path, f"referenced by run {directory.name}")
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            add("run_unreadable", directory, str(exc))
        if git_available:
            _verify_lineage(root, directory, add)

    if db is not None:
        listed = {directory.name for directory in directories}
        for run_id in sorted(InventoryRepo(db, wid).run_ids() - listed):
            add("run_missing", runs / run_id, "indexed run has no directory")
    if recorded is not None and on_disk is not None:
        for sha, size in sorted(recorded.items()):
            path = objects / sha[:2] / sha
            if sha not in on_disk:
                # A file object may land between the walk and this check.
                if s3 or not _referenced_object(objects, sha, prefixes):
                    add("object_missing", path, "recorded in the database but not stored")
            elif on_disk[sha] != size:
                add("object_size", path, f"expected {size}, found {on_disk[sha]}")
        for sha in sorted(set(on_disk) - set(recorded)):
            add("object_orphan", objects / sha[:2] / sha,
                "stored but not recorded in the database", warning=True)
    # package 22: for every run_metrics row, recompute the summary from the
    # committed run bytes and report mismatches as problems ("metric_summary",
    # detail naming `agenticml server metrics rebuild` as the fix).

    def lock_error(path, exc):
        add("lineage_lock", path, str(exc))

    now = time.time()
    for path, info in _lineage_locks(root / "lineage", lock_error):
        quoted = shlex.quote(str(path))
        if stat.S_ISREG(info.st_mode):
            detail = f"stop the server, then run: rm -- {quoted}"
        else:
            rm = "rm -r --" if stat.S_ISDIR(info.st_mode) else "rm --"
            detail = f"not a regular file; stop the server, then run: {rm} {quoted}"
        add("lineage_lock", path, detail, warning=now - info.st_mtime < 600)


def _lineage_locks(directory, on_error):
    def real_directory(path):
        try:
            return stat.S_ISDIR(path.lstat().st_mode)
        except FileNotFoundError:
            return False
        except OSError as exc:
            on_error(path, exc)
            return False

    if not real_directory(directory):
        return
    try:
        with os.scandir(directory) as entries:
            repos = sorted(Path(entry.path) for entry in entries
                           if entry.name.endswith(".git")
                           and lineage._KEY_RE.fullmatch(entry.name[:-4]))
    except OSError as exc:
        on_error(directory, exc)
        return
    for repo in repos:
        refs = repo / "refs"
        runs = refs / "runs"
        if not all(real_directory(path) for path in (repo, refs, runs)):
            continue
        try:
            with os.scandir(runs) as entries:
                for entry in entries:
                    if not entry.name.endswith(".lock"):
                        continue
                    path = Path(entry.path)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        on_error(path, exc)
                        continue
                    yield path, info
        except OSError as exc:
            on_error(runs, exc)


def _referenced_object(objects, sha, prefixes):
    prefix = sha[:2]
    path = objects / prefix / sha
    try:
        if prefix not in prefixes:
            if not stat.S_ISDIR(objects.lstat().st_mode):
                return False
            info = (objects / prefix).lstat()
            if not stat.S_ISDIR(info.st_mode):
                return False
            prefixes.add(prefix)
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        with layout.open_nofollow(path):
            pass
    except (OSError, ValueError):
        return False
    return True


def _run_file_names(directory):
    """Run file names present on disk, in the set the server indexes."""
    names = ["meta.json", "artifacts.json", "events.jsonl", "scalars/index.json",
             "media/index.json", "scalars/_time.bin"]
    try:
        scalars = directory / "scalars"
        if stat.S_ISDIR(scalars.lstat().st_mode):
            names.extend("scalars/" + entry.name for entry in scalars.iterdir())
    except OSError:
        pass
    found = []
    for name in dict.fromkeys(names):
        if not is_run_file(name):
            continue
        try:
            if stat.S_ISREG((directory / name).lstat().st_mode):
                found.append(name)
        except OSError:
            continue
    return found


def _verify_run_files(root, directory, add, db, wid):
    if db is None:
        return
    index = RunRepo(db, wid)
    if index.get(directory.name) is None:
        add("unindexed_run", directory, "unindexed run; run agenticml server reindex",
            warning=True)
        return
    files = index.files(directory.name)
    for name in _run_file_names(directory):
        if name not in files:
            add("file_unrecorded", directory / name, "run file has no committed size", warning=True)
    for name, info in files.items():
        expected = info["size"]
        target = directory / name
        try:
            st = _safe_path(root, target).lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("not a regular file")
            found = st.st_size
        except FileNotFoundError:
            add("size_mismatch", target, "missing")
            continue
        except (OSError, ValueError) as exc:
            add("size_mismatch", target, str(exc))
            continue
        if found < expected:
            add("size_mismatch", target, f"expected {expected}, found {found}")
        elif found > expected:
            add("uncommitted_tail", target, f"expected {expected}, found {found}", warning=True)
        sha = info.get("sha256")
        if sha and found == expected:
            try:
                digest = hashlib.sha256()
                with layout.open_nofollow(target) as handle:
                    remaining = expected
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                if digest.hexdigest() != sha:
                    add("file_hash", target, f"SHA-256 is {digest.hexdigest()}")
            except (OSError, ValueError) as exc:
                add("file_hash", target, str(exc))


def _verify_lineage(root, directory, add):
    try:
        meta = json.loads(layout.read_file_nofollow(directory / "meta.json"))
        info = lineage.replication_info(meta) if isinstance(meta, dict) else None
    except (OSError, ValueError, TypeError, RecursionError):
        return
    if info is None:
        return
    try:
        repo = lineage._repo_path(root, info["project_key"])
        _safe_path(root, repo)
        if not repo.is_dir():
            return
        result = _git(repo, ["rev-parse", "--verify", "-q", lineage._ref(directory.name)], check=False)
        if result.returncode == 1 and not os.path.lexists(repo / lineage._ref(directory.name)):
            return
        if result.returncode:
            raise ValueError("could not read lineage ref")
        found = result.stdout.decode().strip()
        if found != info["commit"]:
            add("lineage_ref", directory / "meta.json", f"expected {info['commit']}, found {found}")
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        add("lineage_ref", directory / "meta.json", str(exc))


def _text(value):
    """ASCII-safe terminal text, including surrogate-escaped filenames."""
    return "".join(char if " " <= char <= "~" else ascii(char)[1:-1]
                   for char in str(value))


def format_status(report):
    sizes = report["sizes"]
    multi = any("workspace" in row for row in report["keys"] + report["users"])
    prefix = "WORKSPACE  " if multi else ""
    lines = [f"Data: {report['data']}",
             f"Runs: {report['runs']['total']} (" + ", ".join(
                 f"{name} {count}" for name, count in report["runs"]["by_status"].items()) + ")",
             f"Sizes: runs {_format_bytes(sizes['runs'])}, objects {_format_bytes(sizes['objects'])} "
             f"({sizes['objects_count']}), lineage {_format_bytes(sizes['lineage'])}, "
             f"total {_format_bytes(sizes['total'])}; skipped {sizes['skipped']}"]
    if multi:
        lines.append("WORKSPACE  STATUS  PLAN  MEMBERS  KEYS  RUNS  STORED  UPLOADED TODAY  OBJECTS  LINEAGE")
        for part in report["workspaces"]:
            lines.append("  ".join(str(value if value is not None else "-") for value in (
                part["slug"], part["status"], part["plan"], part["members"], part["keys_count"],
                part["runs"]["total"],
                _format_bytes(part["stored_bytes"]) if part["stored_bytes"] is not None else None,
                _format_bytes(part["upload_bytes_today"]) if part["upload_bytes_today"] is not None else None,
                f"{part['objects']['count']} ({_format_bytes(part['objects']['size'])})",
                _format_bytes(part["sizes"]["lineage"]))))
    lines.append(prefix + "USER  ROLE  KEYS  RUNS  LAST SYNC")
    for row in report.get("users", []):
        lines.append("  ".join(str(value or "-") for value in (
            ((row["workspace"],) if multi else ()) +
            (row["name"], row["role"], str(row["keys"]), str(row["runs"]), row["last_sync_at"]))))
    lines.append(prefix + "KEY  SCOPE  USER  PREFIX  RUNS  LAST SYNC  LAST USED  CREATED")
    for row in report["keys"]:
        lines.append("  ".join(str(value or "-") for value in (
            ((row["workspace"],) if multi else ()) + (
            row["name"] + (" (revoked)" if row.get("revoked") else ""),
            "env" if row.get("env") else row["scope"], row["user"], row["prefix"],
            str(row["runs"]), row["last_sync_at"], row["last_used_at"], row["created_at"]))))
    lines.extend(f"warning: {warning}" for warning in report.get("warnings", []))
    return "\n".join(_text(line) for line in lines)


def format_verify(report):
    lines = [f"{label}: {row['kind']} {row['path']}: {row['detail']}"
             + (f" [workspace {row['workspace']}]" if "workspace" in row else "")
             for label, rows in (("problem", report["problems"]), ("warning", report["warnings"]))
             for row in rows]
    lines.append(f"verify: {len(report['problems'])} problems, {len(report['warnings'])} warnings "
                 f"(objects {report['checked']['objects']}, runs {report['checked']['runs']})")
    return "\n".join(_text(line) for line in lines)
