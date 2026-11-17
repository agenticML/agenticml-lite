"""API keys shared by the running server, CLI, and future key-management UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import hmac
import json
import logging
import math
from pathlib import Path
import re
import secrets
import threading
import time

from agenticml.store import layout

logger = logging.getLogger("agenticml.server.keys")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ENV_KEY = re.compile(r"[A-Za-z0-9._~+/=-]{32,256}")


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
    workspace_id: str | None = None
    account_id: str | None = None

    def to_public(self):
        """Metadata safe for API and CLI output; never includes the hash."""
        return {k: v for k, v in asdict(self).items() if k not in ("sha256", "workspace_id", "account_id")}


_V2_FIELDS = set(KeyRecord.__dataclass_fields__) - {"workspace_id", "account_id"}
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


class KeyStore:
    """Public key API backed by transactional database repositories."""

    def __init__(self, server_dir, env_key=None, *, db=None, workspace_id):
        from .db import for_server_dir
        from .db.repos import KeyRepo, KeyVerifier, SettingsRepo
        from .db.workspaces import WorkspaceRepo, require_workspace_id

        validate_env_key(env_key)
        self.server_dir = Path(server_dir)
        self.db = db if db is not None else for_server_dir(self.server_dir)
        self.workspace_id = require_workspace_id(workspace_id) if workspace_id is not None else None
        self.repo = KeyRepo(self.db, self.workspace_id) if self.workspace_id else None
        self.settings = SettingsRepo(self.db)
        self.verifier = KeyVerifier(self.db)
        self._touch_log_lock = threading.Lock()
        self._touch_error_at = None
        self._env = None
        self._env_missing_warned = False
        if env_key is not None:
            default = WorkspaceRepo(self.db).get('default')
            if default is None:
                logger.warning('AGENTICML_SERVER_KEY refused: default workspace does not exist')
                self._env_missing_warned = True
            elif self.workspace_id is not None and default['id'] == self.workspace_id:
                if self._call(self.verifier.by_digest, self._hash(env_key)) is not None:
                    raise ValueError('AGENTICML_SERVER_KEY conflicts with a stored key')
                self._env = KeyRecord("env", "admin", self._hash(env_key), "env",
                                      layout.format_timestamp(), "env", None, user="env", workspace_id=default["id"])

    def _current_env(self):
        if self._env is None:
            return None
        from .db.workspaces import WorkspaceRepo
        default = self._call(WorkspaceRepo(self.db).get, 'default')
        if default is None or default['id'] != self._env.workspace_id:
            if not self._env_missing_warned:
                logger.warning('AGENTICML_SERVER_KEY refused: default workspace does not exist')
                self._env_missing_warned = True
            return None
        return self._env

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _call(self, method, *args, **kwargs):
        from .db import DatabaseError
        try:
            return method(*args, **kwargs)
        except DatabaseError as exc:
            raise KeyStoreUnavailable("could not read or write database") from exc

    def setup_closed(self):
        try:
            return self._call(self.settings.get, 'setup_closed') is not None
        except KeyStoreUnavailable:
            # Match the old marker's fail-closed result on stat errors.
            return True

    def _write_setup_closed(self):
        self._call(self.settings.set, 'setup_closed', layout.format_timestamp())

    def clear_setup_closed(self):
        if not self._call(self.settings.delete, 'setup_closed'):
            raise FileNotFoundError('setup_closed')

    def prepare_setup(self):
        if self.repo is None:
            return False
        try:
            return self._call(self.repo.prepare_setup, self._env is not None)
        except KeyStoreUnavailable:
            logger.error("could not read or write database; setup disabled until restart")
            return False

    def create(self, name, scope="sync", created_by="cli", *, user=None,
               only_if_no_admin=False, created_users=None):
        if self.repo is None:
            raise ClientError('unknown workspace')
        record, secret, bootstrap = self._call(
            self.repo.create, name, scope, created_by, user=user,
            only_if_no_admin=only_if_no_admin, env=self._env is not None)
        if bootstrap is not None and created_users is not None:
            created_users.append(bootstrap)
        return record, secret

    def list(self):
        return (self._call(self.repo.list) if self.repo else []) + ([self._env] if self._env is not None else [])

    def for_user(self, name):
        return [record for record in self.list() if record.user == name]

    def revoke(self, name, *, keep_admin=False, expected=None):
        return self._call(self.repo.revoke, name, keep_admin=keep_admin,
                          expected=expected, env=self._env is not None)

    def verify(self, key_str):
        digest = self._hash(key_str)
        record = self._call(self.verifier.by_digest, digest)
        if record is not None and hmac.compare_digest(digest, record.sha256):
            return record
        env = self._current_env()
        if env is not None and hmac.compare_digest(digest, env.sha256):
            return env
        return None

    def touch(self, record):
        try:
            if record.name == 'env':
                with self._touch_log_lock:
                    if self._env is not None and self._due(self._env):
                        self._env = replace(self._env, last_used_at=layout.format_timestamp())
                return
            if not self._due(record):
                return
            from datetime import datetime, timezone
            from .db.repos import KeyRepo
            now = time.time()
            repo = self.repo
            if record.workspace_id and record.workspace_id != self.workspace_id:
                repo = KeyRepo(self.db, record.workspace_id)
            repo.touch(record, layout.format_timestamp(datetime.fromtimestamp(now, timezone.utc)),
                       layout.format_timestamp(datetime.fromtimestamp(now - 60, timezone.utc)))
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
        digest = sha256 if isinstance(sha256, str) else ''
        record = self._call(self.verifier.by_digest, digest)
        if record is not None and record.name == name and hmac.compare_digest(record.sha256, digest):
            return record
        env = self._current_env()
        if env is not None and name == 'env' and hmac.compare_digest(env.sha256, digest):
            return env
        return None

    def has_admin(self):
        return any(record.scope == "admin" for record in self.list())
