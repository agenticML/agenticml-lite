"""API keys shared by the running server, CLI, and future key-management UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from agenticml.store import layout
from . import storeio

logger = logging.getLogger("agenticml.server.keys")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ENV_KEY = re.compile(r"[A-Za-z0-9._~+/=-]{32,256}")
_SETUP_CLOSED = "setup-closed"
_UNSET = object()


class ClientError(ValueError):
    """Validation failure with a fixed message safe to return to clients."""


class UserRequired(ClientError):
    """Key creation needs an explicit user on a multi-user server."""


class KeyNameTaken(ValueError):
    pass


class LastAdminError(ValueError):
    pass


class SetupClosed(ValueError):
    pass


class KeyStoreUnavailable(RuntimeError):
    pass


def validate_name(name):
    if (not isinstance(name, str) or not _NAME.fullmatch(name)
            or name in ("env", ".", "..")):
        raise ClientError("invalid or reserved key name")


def validate_env_key(value):
    """Reject invalid environment keys without disclosing their value."""
    if value is not None and (not isinstance(value, str) or _ENV_KEY.fullmatch(value) is None):
        raise ValueError("AGENTICML_SERVER_KEY must be 32–256 allowed ASCII characters")


def env_key_looks_weak(value):
    """True when estimated entropy is below 128 bits. Never logs the value."""
    if not value:
        return True
    alphabet = len(set(value))
    if alphabet < 2:
        return True
    return len(value) * math.log2(alphabet) < 128


@dataclass(frozen=True)
class KeyRecord:
    name: str
    scope: str
    sha256: str = field(repr=False)
    prefix: str
    created_at: str
    created_by: str
    last_used_at: str | None
    user: str = ""

    def to_public(self):
        """Metadata safe for API and CLI output; never includes the hash."""
        return {k: v for k, v in asdict(self).items() if k != "sha256"}


_V2_FIELDS = set(KeyRecord.__dataclass_fields__)
_V1_FIELDS = _V2_FIELDS - {"user"}
_V1_MESSAGE = (
    "keys.json is version 1; restore users.json or move users.json away to migrate again"
)


def _parse_keys_object(obj, versions):
    if not isinstance(obj, dict) or type(obj.get("version")) is not int:
        raise ValueError("invalid keys.json version")
    version = obj["version"]
    if version == 1 and 1 not in versions:
        raise ValueError(_V1_MESSAGE)
    if version not in versions:
        raise ValueError("invalid keys.json version")
    if not isinstance(obj.get("keys"), list):
        raise ValueError("invalid keys.json records")
    records = []
    names = set()
    expected = _V1_FIELDS if version == 1 else _V2_FIELDS
    for row in obj["keys"]:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("invalid key record fields")
        if any(not isinstance(row[k], str) for k in expected - {"last_used_at"}):
            raise ValueError("invalid key record types")
        if (not _NAME.fullmatch(row["name"]) or row["name"] == "env"
                or row["name"] in names or row["scope"] not in ("sync", "admin")
                or not layout.valid_sha(row["sha256"])
                or len(row["prefix"]) != 8):
            raise ValueError("invalid key record")
        if version == 2 and (not _NAME.fullmatch(row["user"]) or row["user"] in ("env", ".", "..")):
            raise ValueError("invalid key record")
        try:
            layout.parse_timestamp(row["created_at"])
            if row["last_used_at"] is not None:
                layout.parse_timestamp(row["last_used_at"])
        except ArithmeticError as exc:
            raise ValueError("invalid key record") from exc
        names.add(row["name"])
        if version == 1:
            row = {**row, "user": ""}
        records.append(KeyRecord(**row))
    return records


def _unavailable(exc, default="could not load keys.json"):
    if isinstance(exc, ValueError) and str(exc) == _V1_MESSAGE:
        raise KeyStoreUnavailable(_V1_MESSAGE) from exc
    raise KeyStoreUnavailable(default) from exc


class KeyStore:
    """Reloadable, fail-closed key registry rooted at ``<data>/server``.

    Mutations serialize with other processes through keys.lock. The cache lock
    protects request threads; keys.json is always re-read under the file lock
    before a mutation, so CLI changes cannot be overwritten by a stale cache.
    """

    def __init__(self, server_dir, env_key=None):
        validate_env_key(env_key)
        self.server_dir = Path(server_dir)
        self.path = self.server_dir / "keys.json"
        self._lock = threading.Lock()
        self._signature = _UNSET
        self._records = []
        self._env = None
        self._touch_log_lock = threading.Lock()
        self._touch_error_at = None
        if env_key is not None:
            self._env = KeyRecord("env", "admin", self._hash(env_key), "env",
                                  layout.format_timestamp(), "env", None, user="env")

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _read(self):
        return self._load(versions=(2,))

    def _read_v1_or_v2(self):
        """Accept version 1 or 2. Used only by migrate() and admin.status."""
        return self._load(versions=(1, 2))

    def _load(self, versions):
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("keys.json is not a regular file")
        obj = json.loads(layout.read_file_nofollow(self.path).decode("utf-8"))
        return _parse_keys_object(obj, versions)

    def _maybe_reload(self):
        """Caller holds the cache lock. One error is logged per file change."""
        try:
            info = os.stat(self.path, follow_symlinks=False)
            signature = (info.st_ino, info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            signature = None
        except OSError as exc:
            signature = ("stat error", exc.errno)
        if signature == self._signature:
            return
        try:
            self._records = self._read()
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self._records = []
            if isinstance(exc, ValueError) and str(exc) == _V1_MESSAGE:
                logger.error("could not load keys.json; %s", exc)
            else:
                logger.error("could not load keys.json; file keys disabled until it changes")
        self._signature = signature

    def _file_lock(self):
        return storeio.exclusive_lock(self.server_dir)

    def _write(self, records):
        try:
            storeio.write_json(
                self.server_dir, self.path,
                {"version": 2, "keys": [asdict(record) for record in records]},
                ".keys",
            )
        finally:
            self._signature = _UNSET

    def _write_setup_closed(self):
        """Empty <data>/server/setup-closed; atomic, no-follow, mode 0600."""
        path = self.server_dir / _SETUP_CLOSED
        tmp = layout.unique_tmp(self.server_dir, ".setup-closed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(tmp, flags, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            layout.fsync_dir(self.server_dir)
        finally:
            tmp.unlink(missing_ok=True)

    def setup_closed(self):
        """Whether an admin key has ever been created in this data dir.

        The path is not followed, so a planted symlink still counts as closed.
        """
        try:
            os.lstat(self.server_dir / _SETUP_CLOSED)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def clear_setup_closed(self):
        """Unlink the leftover marker.

        Raises FileNotFoundError when the marker is already absent; the
        caller treats that as success. Any other OSError is raised.
        """
        os.unlink(self.server_dir / _SETUP_CLOSED)

    def prepare_setup(self):
        """Clear a stale marker and say whether first-admin setup may arm.

        Holds the cache lock and keys.lock so a CLI create cannot interleave
        with the clear, the admin check (file records plus the env key), and
        the strict keys.json read. A missing marker is fine. Any other
        OSError while clearing, a failed strict read, or a lock that
        cannot be taken, logs a fixed message and returns False.
        """
        with self._lock:
            try:
                file_lock = self._file_lock()
                file_lock.__enter__()
            except (OSError, ValueError):
                logger.error("could not lock keys store; setup disabled until the "
                             "file is fixed and the server restarts")
                return False
            try:
                try:
                    self.clear_setup_closed()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.error("could not clear setup-closed marker; setup disabled until the "
                                 "file is fixed and the server restarts")
                    return False
                try:
                    records = self._read()
                except (OSError, ValueError, TypeError, RecursionError):
                    logger.error("could not load keys.json; setup disabled until the file is fixed "
                                 "and the server restarts")
                    return False
                if self._env is not None or any(record.scope == "admin" for record in records):
                    return False
                return True
            finally:
                file_lock.__exit__(None, None, None)

    def create(self, name, scope="sync", created_by="cli", *, user=None,
               only_if_no_admin=False, created_users=None):
        """Return (KeyRecord, secret); the secret is never persisted.

        If supplied, append the bootstrapped admin UserRecord to created_users
        under the mutation lock, after successfully saving the key.
        """
        from .users import UserNotFound, UserStore

        validate_name(name)
        if scope not in ("sync", "admin"):
            raise ClientError("invalid key scope")
        if not isinstance(created_by, str):
            raise ValueError("created_by must be a string")
        with self._lock, self._file_lock():
            try:
                records = self._read()
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                _unavailable(exc)
            if only_if_no_admin and (self.setup_closed() or self._env is not None
                                     or any(record.scope == "admin" for record in records)):
                raise SetupClosed("setup closed")
            if any(record.name == name for record in records):
                raise KeyNameTaken("key name already exists")
            users_store = UserStore(self.server_dir)
            try:
                user_records = users_store._read()
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                _unavailable(exc, "could not load users.json")
            bootstrap_user = None
            if user is None:
                if not user_records:
                    admin = UserStore._new("admin", "admin", None, created_by)
                    users_store._write([admin])
                    bootstrap_user = admin
                    chosen, chosen_role = admin.name, admin.role
                elif len(user_records) == 1:
                    chosen, chosen_role = user_records[0].name, user_records[0].role
                else:
                    raise UserRequired(
                        "--user is required when the server has more than one user")
            else:
                if not isinstance(user, str):
                    raise ClientError("invalid user")
                found = next((item for item in user_records if item.name == user), None)
                if found is None:
                    raise UserNotFound("unknown user")
                chosen, chosen_role = found.name, found.role
            if scope == "admin" and chosen_role != "admin":
                raise ClientError("admin keys need an admin user")
            key = "aml_" + secrets.token_urlsafe(32)
            record = KeyRecord(name, scope, self._hash(key), key[:8],
                               layout.format_timestamp(), created_by, None, user=chosen)
            if scope == "admin":
                self._write_setup_closed()
            self._write(records + [record])
            if created_users is not None and bootstrap_user is not None:
                created_users.append(bootstrap_user)
            return record, key

    def list(self):
        """Return file records followed by the environment record, if configured.

        Use KeyRecord.to_public() for presentation; no secret is retained.
        """
        with self._lock:
            self._maybe_reload()
            return self._records[:] + ([self._env] if self._env is not None else [])

    def for_user(self, name):
        """Return file and env records belonging to ``name``."""
        return [record for record in self.list() if record.user == name]

    def revoke(self, name, *, keep_admin=False, expected=None):
        """Delete a named file key, returning the removed record or None.

        When expected is supplied, only remove that key's secret and user,
        checked under the mutation lock so a reused name is never revoked.
        The env key cannot be revoked here.
        """
        if name == "env":
            raise ValueError("the env key cannot be revoked")
        with self._lock, self._file_lock():
            try:
                records = self._read()
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                _unavailable(exc)
            removed = next((record for record in records if record.name == name), None)
            if removed is None or (expected is not None and (
                    removed.sha256 != expected.sha256 or removed.user != expected.user)):
                return None
            if keep_admin and removed.scope == "admin":
                from .users import UserStore
                try:
                    user_records = UserStore(self.server_dir)._read()
                except (OSError, ValueError, TypeError, RecursionError) as exc:
                    _unavailable(exc, "could not load users.json")
                # Missing or empty users.json cannot prove any file key is
                # still a usable admin; fail closed instead of revoking.
                if not user_records:
                    raise KeyStoreUnavailable("could not load users.json")
                admin_users = {user.name for user in user_records if user.role == "admin"}
                usable = removed.user in admin_users
                admins = (
                    sum(record.scope == "admin" and record.user in admin_users
                        for record in records)
                    + (self._env is not None)
                )
                if usable and admins <= 1:
                    raise LastAdminError(
                        "last admin key; create another admin key first, or use the CLI")
            remaining = [record for record in records if record.name != name]
            self._write(remaining)
            return removed

    def verify(self, key_str):
        """Compare every digest, without an early exit or hash-index lookup."""
        records = self.list()
        digest = self._hash(key_str)
        result = None
        for record in records:
            if hmac.compare_digest(digest, record.sha256):
                result = record
        return result

    def touch(self, record):
        """Persist last use at most once a minute; never resurrect revoked keys.

        Last use is advisory, so I/O errors must not fail an authenticated request.
        """
        try:
            with self._lock:
                self._maybe_reload()
                if record.name == "env":
                    if self._env is not None and self._due(self._env):
                        self._env = replace(self._env, last_used_at=layout.format_timestamp())
                    return
                current = next((item for item in self._records
                                if item.name == record.name and item.sha256 == record.sha256), None)
                if current is None or not self._due(current):
                    return
                with self._file_lock():
                    records = self._read()
                    for index, current in enumerate(records):
                        if (current.name == record.name and current.sha256 == record.sha256
                                and self._due(current)):
                            records[index] = replace(current, last_used_at=layout.format_timestamp())
                            self._write(records)
                            break
        except Exception:
            with self._touch_log_lock:
                now = time.monotonic()
                if self._touch_error_at is None or now - self._touch_error_at >= 60:
                    self._touch_error_at = now
                    logger.error("could not update key last_used_at")

    @staticmethod
    def _due(record):
        return (record.last_used_at is None
                or time.time() - layout.parse_timestamp(record.last_used_at).timestamp() >= 60)

    def current(self, name, sha256):
        """Return the live record with this exact name and hash, else None."""
        records = self.list()
        digest = sha256 if isinstance(sha256, str) else ""
        result = None
        for record in records:
            hashed = record.sha256
            if record.name == name and len(hashed) == len(digest) and hmac.compare_digest(hashed, digest):
                result = record
        return result

    def has_admin(self):
        """Whether a file or environment key can administer the server."""
        return any(record.scope == "admin" for record in self.list())
