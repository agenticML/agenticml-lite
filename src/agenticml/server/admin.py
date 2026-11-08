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
from .db import DatabaseError, DatabaseUrlError, SchemaTooNew, for_server_dir
from .db.repos import RunRepo
from .db.workspaces import WorkspaceRepo
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
    except (SchemaTooNew, DatabaseUrlError):
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


def status(data, *, database=None) -> dict:
    data, root = _data_root(data, database)
    counts = dict.fromkeys(_STATUSES, 0)
    keys = []
    warnings = []
    keys_loaded = True
    db = None
    try:
        db = for_server_dir(data / 'server', database, create=False)
        from .workspaces import default_workspace_id
        wid = default_workspace_id(db) if db is not None else None
        records = KeyStore(data / 'server', db=db, workspace_id=wid).list() if db is not None else []
        user_records = UserStore(data / 'server', db=db, workspace_id=wid).list() if db is not None else []
        if db is None:
            for name in ('users.json', 'keys.json'):
                try:
                    (data / 'server' / name).lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                warnings.append(
                    'found 2.x users.json/keys.json with no database; '
                    'start the server to import them')
                break
    except (SchemaTooNew, DatabaseUrlError):
        raise
    except (DatabaseError, KeyStoreUnavailable):
        records, user_records = [], []
        keys_loaded = False
        warnings.append('could not load server database; key and user status is unknown')
    for record in records:
        row = record.to_public()
        row.pop('created_by')
        row.update(runs=0, last_sync_at=None)
        keys.append(row)
    by_key = {(row['name'], row['user']): row for row in keys}
    users = [{"name": user.name, "role": user.role, "keys": 0, "runs": 0, "last_sync_at": None}
             for user in user_records]
    by_user = {row["name"]: row for row in users}
    for record in records:
        if record.user in by_user:
            by_user[record.user]["keys"] += 1

    def key_row(name, user):
        pair = (name, user)
        if pair not in by_key:
            row = dict(name=name, user=user, scope=None, prefix=None, created_at=None,
                       last_used_at=None, runs=0, last_sync_at=None)
            if name == "env":
                row["env"] = True
            elif keys_loaded:
                row["revoked"] = True
            keys.append(row)
            by_key[pair] = row
        return by_key[pair]

    def last_sync(row, stamp):
        if stamp and (row["last_sync_at"] is None or
                      layout.parse_timestamp(stamp) > layout.parse_timestamp(row["last_sync_at"])):
            row["last_sync_at"] = stamp

    _safe_path(root, root / "runs")
    run_rows = []
    if db is not None:
        try:
            run_rows = RunRepo(db, WorkspaceRepo(db).require('default')['id']).all_rows()
        except DatabaseError:
            run_rows = []
            warnings.append('could not load runs from the server database')
    else:
        run_rows = []
    now = time.time()
    for row in run_rows:
        if row["status"]:
            effective = _remote_status(
                {"status": row["status"], "last_sync_at": row["last_sync_at"]}, now)
        else:
            effective = "unsynced"
        counts[effective] = counts.get(effective, 0) + 1
        owner_user = row["owner_user"] if isinstance(row["owner_user"], str) else None
        owner_key = row["owner_key"] if isinstance(row["owner_key"], str) else None
        stamp = row["last_sync_at"]
        writer = row["last_sync_key"] if isinstance(row["last_sync_key"], str) else None
        if owner_user in by_user:
            by_user[owner_user]["runs"] += 1
            last_sync(by_user[owner_user], stamp)
        if owner_key is not None and owner_user is not None:
            key_row(owner_key, owner_user)["runs"] += 1
            last_sync(key_row(writer or owner_key, owner_user), stamp)
    runs = run_rows
    sizes = {"objects_count": 0, "skipped": 0}

    def skipped(path, exc):
        sizes["skipped"] += 1

    for name in ("runs", "objects", "lineage"):
        sizes[name] = 0
        for path, info in _walk(root / name, skipped):
            if not stat.S_ISREG(info.st_mode):
                continue
            sizes[name] += info.st_size
            if (name == "objects" and len(path.relative_to(root / name).parts) == 2
                    and layout.valid_sha(path.name) and path.parent.name == path.name[:2]):
                sizes["objects_count"] += 1
    sizes["total"] = sum(sizes[name] for name in ("runs", "objects", "lineage"))
    return {"data": str(data), "runs": {"total": len(runs), "by_status": counts},
            "sizes": sizes, "keys": keys, "users": users, "warnings": warnings}


def verify(data, *, progress=False, database=None) -> dict:
    data, root = _data_root(data, database)
    db = for_server_dir(data / 'server', database, create=False)
    report = {"ok": True, "checked": {"objects": 0, "runs": 0}, "problems": [], "warnings": []}

    def add(kind, path, detail, *, warning=False):
        report["warnings" if warning else "problems"].append(
            {"kind": kind, "path": str(path.relative_to(data)), "detail": detail})

    def object_error(path, exc):
        add("object_type", path, str(exc))

    objects = root / "objects"
    prefixes = set()

    def object_directory(path):
        return path == objects or (path.parent == objects
                                   and re.fullmatch(r"[0-9a-f]{2}", path.name) is not None)

    for path, info in _walk(objects, object_error, descend=object_directory):
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
            try:
                digest = hashlib.sha256()
                with layout.open_nofollow(path) as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != path.name:
                    add("object_hash", path, f"SHA-256 is {digest.hexdigest()}")
            except (OSError, ValueError) as exc:
                add("object_hash", path, str(exc))
        if progress and sys.stderr.isatty() and report["checked"]["objects"] % 1000 == 0:
            print(f"verify: checked {report['checked']['objects']} objects", file=sys.stderr)

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
        _verify_run_files(root, directory, add, db)
        try:
            refs = run_references(root, directory.name)
            for sha in sorted(set().union(*refs.values())):
                path = root / "objects" / sha[:2] / sha
                if not _referenced_object(objects, sha, prefixes):
                    add("object_missing", path, f"referenced by run {directory.name}")
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            add("run_unreadable", directory, str(exc))
        if git_available:
            _verify_lineage(root, directory, add)

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
    report["ok"] = not report["problems"]
    return report


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


def _verify_run_files(root, directory, add, db):
    if db is None:
        return
    index = RunRepo(db, WorkspaceRepo(db).require('default')['id'])
    if index.get(directory.name) is None:
        add("unindexed_run", directory, "unindexed run; run agenticml server reindex",
            warning=True)
        return
    files = index.files(directory.name)
    if not files:
        return
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
    lines = [f"Data: {report['data']}",
             f"Runs: {report['runs']['total']} (" + ", ".join(
                 f"{name} {count}" for name, count in report["runs"]["by_status"].items()) + ")",
             f"Sizes: runs {_format_bytes(sizes['runs'])}, objects {_format_bytes(sizes['objects'])} "
             f"({sizes['objects_count']}), lineage {_format_bytes(sizes['lineage'])}, "
             f"total {_format_bytes(sizes['total'])}; skipped {sizes['skipped']}",
             "USER  ROLE  KEYS  RUNS  LAST SYNC"]
    for row in report.get("users", []):
        lines.append("  ".join(str(value or "-") for value in (
            row["name"], row["role"], str(row["keys"]), str(row["runs"]), row["last_sync_at"])))
    lines.append("KEY  SCOPE  USER  PREFIX  RUNS  LAST SYNC  LAST USED  CREATED")
    for row in report["keys"]:
        lines.append("  ".join(str(value or "-") for value in (
            row["name"] + (" (revoked)" if row.get("revoked") else ""),
            "env" if row.get("env") else row["scope"], row["user"], row["prefix"],
            str(row["runs"]), row["last_sync_at"], row["last_used_at"], row["created_at"])))
    lines.extend(f"warning: {warning}" for warning in report.get("warnings", []))
    return "\n".join(_text(line) for line in lines)


def format_verify(report):
    lines = [f"{label}: {row['kind']} {row['path']}: {row['detail']}"
             for label, rows in (("problem", report["problems"]), ("warning", report["warnings"]))
             for row in rows]
    lines.append(f"verify: {len(report['problems'])} problems, {len(report['warnings'])} warnings "
                 f"(objects {report['checked']['objects']}, runs {report['checked']['runs']})")
    return "\n".join(_text(line) for line in lines)
