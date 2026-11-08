"""Isolated 2.0/2.1 conversion, used only by the one-time importer."""
from dataclasses import asdict, replace
import json
import logging
import os
from pathlib import Path
import stat

from agenticml.store import layout
from .. import storeio
from ..keys import _parse_keys_object
from ..users import UserRecord, _new_user_id, _store_paths_safe, _scan_runs, _remote_is_inside

logger = logging.getLogger('agenticml.server.db.import2x')


class LegacyKeys:
    def __init__(self, directory):
        self.server_dir = directory
        self.path = directory / 'keys.json'

    def _read_v1_or_v2(self):
        try:
            obj = read_json(self.path)
        except FileNotFoundError:
            return []
        return _parse_keys_object(obj, (1, 2))

    def _write(self, records):
        storeio.write_json(self.server_dir, self.path,
                           {'version': 2, 'keys': [{k: v for k, v in asdict(r).items() if k not in ('workspace_id', 'account_id')}
                                                    for r in records]}, '.keys')


class LegacyUsers:
    def __init__(self, directory):
        self.directory = directory

    def _write(self, records):
        storeio.write_json(self.directory, self.directory / 'users.json',
                           {'version': 1, 'users': [asdict(r) for r in records]}, '.users')


def read_json(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'{path.name} is not a regular file')
    return json.loads(layout.read_file_nofollow(path).decode('utf-8'))


def migrate(data_dir, *, actor="server", db=None):
    """Upgrade a 2.0/2.1 data directory in place. Idempotent; users.json is the commit.

    Returns True when migration is complete or not needed, False when it did not
    complete. Server start keeps going on False; CLI commands abort.
    """

    data = Path(data_dir)
    server_dir = data / "server"
    users_path = server_dir / "users.json"
    try:
        lock = storeio.exclusive_lock(server_dir)
        lock.__enter__()
    except (OSError, ValueError):
        # Same lock as KeyStore; a planted symlink/FIFO must not prevent serving.
        logger.error("could not lock keys store; migration skipped")
        return False
    try:
        try:
            os.lstat(users_path)
            return True
        except FileNotFoundError:
            pass
        except OSError:
            logger.error("could not stat users.json; migration skipped")
            return False
        keys = LegacyKeys(server_dir)
        try:
            records = keys._read_v1_or_v2()
        except (OSError, ValueError, TypeError, RecursionError):
            logger.error("could not read keys.json; migration skipped")
            return False
        if any(record.user not in ("", "admin") for record in records):
            logger.error("keys.json is already version 2 with non-admin users; "
                         "refusing to reassign them to admin")
            return False
        if not _store_paths_safe(data):
            return False
        to_rewrite, skipped = _scan_runs(data / "store" / "runs")
        if skipped:
            logger.warning(
                "migration skipped %d corrupt or unreadable remote.json file(s); "
                "those runs keep their old owner",
                skipped,
            )
        need_admin = bool(records) or bool(to_rewrite)
        rewritten = 0
        if need_admin:
            for path, directory, obj, owner in to_rewrite:
                if not _remote_is_inside(data, path):
                    logger.error("migration aborted: remote.json path escapes the data directory")
                    return False
                payload = dict(obj)
                payload["owner"] = "admin"
                if "owner_key" not in payload:
                    payload["owner_key"] = owner
                try:
                    layout.atomic_write(
                        path, json.dumps(payload).encode("utf-8"), fsync=True)
                    layout.fsync_dir(directory)
                    rewritten += 1
                except OSError:
                    logger.error("migration failed writing remote.json in %s",
                                 directory.name)
                    return False
        try:
            os.lstat(keys.path)
            keys_existed = True
        except FileNotFoundError:
            keys_existed = False
        except OSError:
            logger.error("could not stat keys.json; migration skipped")
            return False
        try:
            if keys_existed:
                keys._write([replace(record, user="admin") for record in records])
            if need_admin:
                stamp = layout.format_timestamp()
                admin = UserRecord(
                    "admin", _new_user_id(), "admin", None, None, None, 0, stamp,
                    "migration")
                LegacyUsers(server_dir)._write([admin])
            else:
                LegacyUsers(server_dir)._write([])
        except OSError:
            logger.error("migration failed writing keys.json or users.json")
            return False
        if need_admin:
            logger.info("migrated 2.0 keys to user admin (%d keys, %d runs)",
                        len(records), rewritten)
    finally:
        lock.__exit__(None, None, None)
    if need_admin:
        _audit_migration(db, actor, records, rewritten)
    return True


def _audit_migration(db, actor, records, rewritten):
    try:
        from ..audit import AuditLog
        from ..workspaces import default_workspace_id
        AuditLog(Path('.'), db=db, workspace_id=default_workspace_id(db)).record(
            actor, "server" if actor == "server" else "cli", "migration", "admin",
            detail={"keys": len(records), "runs": rewritten})
    except Exception:
        logger.error("could not record audit event")
