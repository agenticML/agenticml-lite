"""API keys shared by the running server, CLI, and future key-management UI."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from agenticml.store import layout

logger = logging.getLogger("agenticml.server.keys")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ENV_KEY = re.compile(r"[A-Za-z0-9._~+/=-]{32,256}")
_UNSET = object()


def validate_env_key(value):
    """Reject invalid environment keys without disclosing their value."""
    if value is not None and (not isinstance(value, str) or _ENV_KEY.fullmatch(value) is None):
        raise ValueError("AGENTICML_SERVER_KEY must be 32–256 allowed ASCII characters")


@dataclass(frozen=True)
class KeyRecord:
    name: str
    scope: str
    sha256: str = field(repr=False)
    prefix: str
    created_at: str
    created_by: str
    last_used_at: str | None

    def to_public(self):
        """Metadata safe for API and CLI output; never includes the hash."""
        return {k: v for k, v in asdict(self).items() if k != "sha256"}


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
        if env_key is not None:
            self._env = KeyRecord("env", "admin", self._hash(env_key), "env",
                                  layout.format_timestamp(), "env", None)

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _read(self):
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("keys.json is not a regular file")
        obj = json.loads(layout.read_file_nofollow(self.path).decode("utf-8"))
        if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != 1:
            raise ValueError("invalid keys.json version")
        if not isinstance(obj.get("keys"), list):
            raise ValueError("invalid keys.json records")
        records = []
        names = set()
        fields = set(KeyRecord.__dataclass_fields__)
        for row in obj["keys"]:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("invalid key record fields")
            if any(not isinstance(row[k], str) for k in fields - {"last_used_at"}):
                raise ValueError("invalid key record types")
            if (not _NAME.fullmatch(row["name"]) or row["name"] == "env"
                    or row["name"] in names or row["scope"] not in ("sync", "admin")
                    or not layout.valid_sha(row["sha256"])
                    or len(row["prefix"]) != 8):
                raise ValueError("invalid key record")
            layout.parse_timestamp(row["created_at"])
            if row["last_used_at"] is not None:
                layout.parse_timestamp(row["last_used_at"])
            names.add(row["name"])
            records.append(KeyRecord(**row))
        return records

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
        except (OSError, ValueError, TypeError, RecursionError):
            self._records = []
            logger.error("could not load keys.json; file keys disabled until it changes")
        self._signature = signature

    @contextmanager
    def _file_lock(self):
        try:
            import fcntl
        except ImportError as exc:
            raise ValueError("the server needs POSIX fcntl") from exc
        with layout.open_nofollow(self.server_dir / "keys.lock", "ab") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def _write(self, records):
        tmp = layout.unique_tmp(self.server_dir, ".keys")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                data = {"version": 1, "keys": [asdict(record) for record in records]}
                handle.write(json.dumps(data, indent=2).encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            layout.fsync_dir(self.server_dir)
        finally:
            tmp.unlink(missing_ok=True)
            self._signature = _UNSET

    def create(self, name, scope="sync", created_by="cli"):
        """Return (KeyRecord, secret); the secret is never persisted."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name == "env":
            raise ValueError("invalid or reserved key name")
        if scope not in ("sync", "admin"):
            raise ValueError("invalid key scope")
        if not isinstance(created_by, str):
            raise ValueError("created_by must be a string")
        with self._lock, self._file_lock():
            records = self._read()
            if any(record.name == name for record in records):
                raise ValueError("key name already exists")
            key = "aml_" + secrets.token_urlsafe(32)
            record = KeyRecord(name, scope, self._hash(key), key[:8],
                               layout.format_timestamp(), created_by, None)
            self._write(records + [record])
            return record, key

    def list(self):
        """Return file records followed by the environment record, if configured.

        Use KeyRecord.to_public() for presentation; no secret is retained.
        """
        with self._lock:
            self._maybe_reload()
            return self._records[:] + ([self._env] if self._env is not None else [])

    def revoke(self, name):
        """Delete a named file key, returning whether it existed.

        Names are run-owner identities: recreating a revoked name grants access
        to that name's runs. The env key cannot be revoked here.
        """
        if name == "env":
            raise ValueError("the env key cannot be revoked")
        with self._lock, self._file_lock():
            records = self._read()
            remaining = [record for record in records if record.name != name]
            if len(remaining) == len(records):
                return False
            self._write(remaining)
            return True

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
