"""Users, roles, principals, and 2.0/2.1 data-directory migration."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading

from agenticml.store import layout
from .keys import (
    ClientError, KeyStoreUnavailable, LastAdminError, validate_name,
)
from . import storeio

logger = logging.getLogger("agenticml.server.users")
_UNSET = object()
ROLES = ("viewer", "member", "admin")
RUN_SCAN_MAX = 10000
INVITE_HOURS = 72
PASSWORD_MIN = 12
PASSWORD_MAX = 1024
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_UNAVAILABLE = "password sign-in needs Python with OpenSSL scrypt"
_DUMMY_SALT = bytes(range(16))
_DUMMY_HASH = None
ACTIONS = {
    "read": "viewer", "password": "viewer",
    "notes": "member", "sync": "member", "own_keys": "member",
    "admin_keys": "admin", "all_keys": "admin", "users": "admin", "audit": "admin",
}
_USER_FIELDS = {
    "name", "id", "role", "identity", "password", "invite", "generation",
    "created_at", "created_by",
}
_USER_ID_HEX = "0123456789abcdef"


class UserNotFound(ValueError):
    pass


class UserNameTaken(ValueError):
    pass


class IdentityTaken(ValueError):
    pass


class InviteInvalid(ValueError):
    pass


class ScryptUnavailable(ValueError):
    def __init__(self):
        super().__init__(SCRYPT_UNAVAILABLE)


@dataclass(frozen=True)
class Principal:
    user: str
    role: str
    via: str
    key: str | None = None
    id: str | None = None
    generation: int | None = None


def allowed(principal, action) -> bool:
    needed = ACTIONS[action]
    return ROLES.index(principal.role) >= ROLES.index(needed)


def scrypt_available():
    try:
        hashlib.scrypt(b"x", salt=_DUMMY_SALT, n=2, r=1, p=1, dklen=16)
        return True
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def _new_user_id():
    return secrets.token_hex(16)


def _valid_user_id(value):
    return (isinstance(value, str) and len(value) == 32
            and all(ch in _USER_ID_HEX for ch in value))


def _identity_taken(records, identity, *, except_name=None):
    if identity is None:
        return False
    folded = identity.casefold()
    return any(
        item.identity is not None
        and item.identity.casefold() == folded
        and item.name != except_name
        for item in records)


def _scrypt(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN):
    if not isinstance(password, str):
        raise ClientError("invalid password")
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ClientError("invalid password") from exc
    try:
        return hashlib.scrypt(encoded, salt=salt, n=n, r=r, p=p, dklen=dklen)
    except (AttributeError, TypeError, ValueError, OSError) as exc:
        raise ScryptUnavailable() from exc


def validate_password(password):
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ClientError("password must be 12 to 1024 characters")
    return password


def _password_record(password):
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt)
    return {
        "alg": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def _dummy_verify(password):
    """Run scrypt against a fixed dummy so unknown names take the same time."""
    global _DUMMY_HASH
    if not isinstance(password, str):
        password = ""
    try:
        digest = _scrypt(password, _DUMMY_SALT)
    except (ScryptUnavailable, ClientError):
        raise
    except (TypeError, ValueError):
        digest = _scrypt("x" * PASSWORD_MIN, _DUMMY_SALT)
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _scrypt("timing-dummy-not-a-password", _DUMMY_SALT)
    hmac.compare_digest(digest, _DUMMY_HASH)
    return False


def _check_password(password, record):
    if not isinstance(record, dict) or record.get("alg") != "scrypt":
        return _dummy_verify(password)
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        n, r, p = record["n"], record["r"], record["p"]
        if not expected or type(n) is not int or type(r) is not int or type(p) is not int:
            return _dummy_verify(password)
        digest = _scrypt(password, salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except ClientError:
        raise
    except (KeyError, TypeError, ValueError, ScryptUnavailable):
        if not scrypt_available():
            raise ScryptUnavailable()
        return _dummy_verify(password)


def validate_identity(value):
    """Return ``value`` or raise ClientError. ``None`` clears identity."""
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or value.strip() != value
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ClientError("invalid identity")
    return value


@dataclass(frozen=True)
class UserRecord:
    name: str
    id: str
    role: str
    identity: str | None
    password: dict | None = field(repr=False)
    invite: dict | None = field(repr=False)
    generation: int = 0
    created_at: str = ""
    created_by: str = ""

    def to_public(self):
        return {
            "name": self.name,
            "role": self.role,
            "identity": self.identity,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "password_set": self.password is not None,
            "invite_pending": self.invite is not None,
        }


def resolve(keys, users, key_record) -> Principal | None:
    """Map a verified key to a live principal, or None if the user is gone."""
    if key_record is None:
        return None
    if key_record.name == "env":
        return Principal("env", "admin", "key", "env")
    user = users.get(key_record.user)
    if user is None:
        return None
    return Principal(
        user.name, user.role, "key", key_record.name, user.id, user.generation)


def attribution(run_dir):
    """Return ``(owner_user, owner_key)`` from ``remote.json``; either may be None."""
    path = Path(run_dir) / "remote.json"
    try:
        obj = json.loads(layout.read_file_nofollow(path).decode("utf-8"))
        if not isinstance(obj, dict):
            return None, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, RecursionError, TypeError):
        return None, None
    owner = obj.get("owner") if isinstance(obj.get("owner"), str) else None
    owner_key = obj.get("owner_key") if isinstance(obj.get("owner_key"), str) else None
    return owner, owner_key


class UserStore:
    """Reloadable, fail-closed user registry rooted at ``<data>/server``.

    Mutations share ``keys.lock`` with KeyStore. The cache lock protects request
    threads; users.json is always re-read under the file lock before a mutation.
    """

    def __init__(self, server_dir):
        self.server_dir = Path(server_dir)
        self.path = self.server_dir / "users.json"
        self._lock = threading.Lock()
        self._signature = _UNSET
        self._records = []

    def _read(self):
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("users.json is not a regular file")
        obj = json.loads(layout.read_file_nofollow(self.path).decode("utf-8"))
        if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != 1:
            raise ValueError("invalid users.json version")
        if not isinstance(obj.get("users"), list):
            raise ValueError("invalid users.json records")
        records = []
        names = set()
        identities = set()
        ids = set()
        for row in obj["users"]:
            records.append(self._parse_row(row, names, identities, ids))
        return records

    @staticmethod
    def _parse_row(row, names, identities, ids):
        if not isinstance(row, dict) or set(row) != _USER_FIELDS:
            raise ValueError("invalid user record fields")
        if (not isinstance(row["name"], str) or not isinstance(row["role"], str)
                or not isinstance(row["created_at"], str)
                or not isinstance(row["created_by"], str)):
            raise ValueError("invalid user record types")
        if row["identity"] is not None and not isinstance(row["identity"], str):
            raise ValueError("invalid user record types")
        if row["password"] is not None and not isinstance(row["password"], dict):
            raise ValueError("invalid user record types")
        if row["invite"] is not None and not isinstance(row["invite"], dict):
            raise ValueError("invalid user record types")
        if type(row["generation"]) is not int or row["generation"] < 0:
            raise ValueError("invalid user record types")
        if not _valid_user_id(row.get("id")):
            raise ValueError("invalid user record")
        try:
            validate_name(row["name"])
        except ClientError as exc:
            raise ValueError("invalid user record") from exc
        if row["name"] in names or row["id"] in ids or row["role"] not in ROLES:
            raise ValueError("invalid user record")
        if row["identity"] is not None:
            try:
                validate_identity(row["identity"])
            except ClientError as exc:
                raise ValueError("invalid user record") from exc
            folded = row["identity"].casefold()
            if folded in identities:
                raise ValueError("invalid user record")
            identities.add(folded)
        try:
            layout.parse_timestamp(row["created_at"])
        except ArithmeticError as exc:
            raise ValueError("invalid user record") from exc
        names.add(row["name"])
        ids.add(row["id"])
        return UserRecord(
            row["name"], row["id"], row["role"], row["identity"], row["password"],
            row["invite"], row["generation"], row["created_at"], row["created_by"],
        )

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
            logger.error("could not load users.json; file users disabled until it changes")
        self._signature = signature

    def _file_lock(self):
        return storeio.exclusive_lock(self.server_dir)

    def _write(self, records):
        try:
            storeio.write_json(
                self.server_dir, self.path,
                {"version": 1, "users": [asdict(record) for record in records]},
                ".users",
            )
        finally:
            self._signature = _UNSET

    def _unavailable(self, exc, kind="users.json"):
        raise KeyStoreUnavailable(f"could not load {kind}") from exc

    def _load_strict(self):
        try:
            return self._read()
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self._unavailable(exc)

    @staticmethod
    def _new(name, role, identity, created_by):
        validate_name(name)
        if role not in ROLES:
            raise ClientError("invalid role")
        if not isinstance(created_by, str):
            raise ValueError("created_by must be a string")
        identity = validate_identity(identity)
        return UserRecord(
            name, _new_user_id(), role, identity, None, None, 0,
            layout.format_timestamp(), created_by,
        )

    def list(self):
        with self._lock:
            self._maybe_reload()
            return self._records[:]

    def get(self, name):
        with self._lock:
            self._maybe_reload()
            return next((record for record in self._records if record.name == name), None)

    def by_identity(self, value):
        with self._lock:
            self._maybe_reload()
            return next((record for record in self._records if record.identity == value), None)

    def add(self, name, role="member", identity=None, created_by="cli", *,
            reclaim_runs=False):
        record = self._new(name, role, identity, created_by)
        with self._lock, self._file_lock():
            records = self._load_strict()
            if any(item.name == record.name for item in records):
                raise UserNameTaken("user name already exists")
            if _identity_taken(records, record.identity):
                raise IdentityTaken("identity already in use")
            while any(item.id == record.id for item in records):
                record = replace(record, id=_new_user_id())
            if not reclaim_runs and _name_owns_runs(self.server_dir.parent, record.name):
                raise ClientError(
                    f"runs are still owned by {record.name}; "
                    "pass --reclaim-runs to give them to the new user")
            self._write(records + [record])
            return record

    def remove(self, name):
        """Delete the user and all of their keys. Returns the number of keys removed."""
        from .keys import KeyStore

        with self._lock, self._file_lock():
            records = self._load_strict()
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            remaining_users = [item for item in records if item.name != name]
            if found.role == "admin" and not any(item.role == "admin" for item in remaining_users):
                raise LastAdminError("last admin user; promote another user first")
            keys = KeyStore(self.server_dir)
            try:
                key_records = keys._read()
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                self._unavailable(exc, "keys.json")
            remaining_keys = [item for item in key_records if item.user != name]
            revoked = len(key_records) - len(remaining_keys)
            keys._write(remaining_keys)
            self._write(remaining_users)
            return revoked

    def discard(self, name, user_id):
        """Best-effort user delete for setup rollback. Ignores last-admin; leaves keys alone."""
        try:
            with self._lock, self._file_lock():
                records = self._load_strict()
                remaining = [
                    item for item in records
                    if item.name != name or item.id != user_id]
                if len(remaining) == len(records):
                    return
                self._write(remaining)
        except (KeyStoreUnavailable, OSError, ValueError):
            logger.warning("could not roll back user %s after failed setup", name)

    def set_role(self, name, role):
        if role not in ROLES:
            raise ClientError("invalid role")
        with self._lock, self._file_lock():
            records = self._load_strict()
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            if found.role == "admin" and role != "admin":
                if not any(item.role == "admin" and item.name != name for item in records):
                    raise LastAdminError("last admin user; promote another user first")
            updated = replace(found, role=role, generation=found.generation + 1)
            self._write([updated if item.name == name else item for item in records])
            return updated

    def set_identity(self, name, value):
        identity = validate_identity(value)
        with self._lock, self._file_lock():
            records = self._load_strict()
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            if _identity_taken(records, identity, except_name=name):
                raise IdentityTaken("identity already in use")
            updated = replace(found, identity=identity)
            self._write([updated if item.name == name else item for item in records])
            return updated

    def set_password(self, name, password):
        record = _password_record(password)
        with self._lock, self._file_lock():
            records = self._load_strict()
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            updated = replace(
                found, password=record, invite=None, generation=found.generation + 1)
            self._write([updated if item.name == name else item for item in records])
            return updated

    def verify_password(self, name, password):
        """Return the matching user, or None. Unknown names still run scrypt."""
        user = self.get(name) if isinstance(name, str) else None
        if user is None or user.password is None:
            _dummy_verify(password if isinstance(password, str) else "")
            return None
        if not isinstance(password, str):
            _dummy_verify("")
            return None
        if not _check_password(password, user.password):
            return None
        return user

    def create_invite(self, name):
        """Store a one-time invite, clear the password, and return the secret token."""
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires_at = layout.format_timestamp(
            datetime.now(timezone.utc) + timedelta(hours=INVITE_HOURS))
        invite = {"sha256": digest, "expires_at": expires_at}
        with self._lock, self._file_lock():
            records = self._load_strict()
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            updated = replace(
                found, password=None, invite=invite, generation=found.generation + 1)
            self._write([updated if item.name == name else item for item in records])
            return token

    def redeem_invite(self, token, password):
        """Find a pending invite by digest (no early exit), then set_password."""
        record = _password_record(password)
        if not isinstance(token, str) or not token:
            raise InviteInvalid("invalid invite")
        try:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        except UnicodeEncodeError as exc:
            raise InviteInvalid("invalid invite") from exc
        now = datetime.now(timezone.utc)
        with self._lock, self._file_lock():
            records = self._load_strict()
            matched = None
            for item in records:
                invite = item.invite
                if not isinstance(invite, dict) or not isinstance(invite.get("sha256"), str):
                    continue
                stored = invite["sha256"]
                if len(stored) != len(digest):
                    continue
                if hmac.compare_digest(stored, digest):
                    matched = item
            if matched is None:
                raise InviteInvalid("invalid invite")
            expires_raw = matched.invite.get("expires_at") if isinstance(matched.invite, dict) else None
            try:
                expires = layout.parse_timestamp(expires_raw)
            except (TypeError, ValueError) as exc:
                raise InviteInvalid("invalid invite") from exc
            if expires <= now:
                raise InviteInvalid("invalid invite")
            updated = replace(
                matched, password=record, invite=None, generation=matched.generation + 1)
            self._write([updated if item.name == matched.name else item for item in records])
            return updated


def migrate(data_dir):
    """Upgrade a 2.0/2.1 data directory in place. Idempotent; users.json is the commit.

    Returns True when migration is complete or not needed, False when it did not
    complete. Server start keeps going on False; CLI commands abort.
    """
    from .keys import KeyStore

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
        keys = KeyStore(server_dir)
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
                UserStore(server_dir)._write([admin])
            else:
                UserStore(server_dir)._write([])
        except OSError:
            logger.error("migration failed writing keys.json or users.json")
            return False
        if need_admin:
            logger.info("migrated 2.0 keys to user admin (%d keys, %d runs)",
                        len(records), rewritten)
        return True
    finally:
        lock.__exit__(None, None, None)


def _store_paths_safe(data):
    """lstat store/ and store/runs/ without following; abort on a symlink or non-dir."""
    for rel in ("store", os.path.join("store", "runs")):
        path = Path(data) / rel
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            logger.error("migration aborted: could not stat %s", rel)
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            logger.error("migration aborted: %s is a symlink or not a directory", rel)
            return False
    return True


def _remote_is_inside(data, path):
    """True when ``path`` stays under ``data`` with no symlink on the way."""
    data = Path(data)
    path = Path(path)
    try:
        layout.contained(data, path)
    except ValueError:
        return False
    current = path
    while True:
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return False
        except OSError:
            return False
        if current == data:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _name_owns_runs(data_dir, name):
    """True if any remote.json owner is ``name``, or the scan could not finish."""
    data = Path(data_dir)
    store = data / "store"
    runs = store / "runs"
    for path in (store, runs):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return True
    try:
        with os.scandir(runs) as entries:
            for index, entry in enumerate(entries):
                if index >= RUN_SCAN_MAX:
                    return True
                if not entry.is_dir(follow_symlinks=False):
                    continue
                owner, _ = attribution(entry.path)
                if owner == name:
                    return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _scan_runs(runs_dir):
    """Collect remotes to rewrite. Skip unreadable files with one warning each."""
    to_rewrite = []
    skipped = 0
    try:
        with os.scandir(runs_dir) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                directory = Path(entry.path)
                path = directory / "remote.json"
                try:
                    obj = json.loads(layout.read_file_nofollow(path).decode("utf-8"))
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, RecursionError, TypeError, UnicodeDecodeError):
                    logger.warning("migration skipped unreadable remote.json in %s", entry.name)
                    skipped += 1
                    continue
                if not isinstance(obj, dict):
                    logger.warning("migration skipped unreadable remote.json in %s", entry.name)
                    skipped += 1
                    continue
                owner = obj.get("owner")
                if isinstance(owner, str) and owner != "env":
                    to_rewrite.append((path, directory, obj, owner))
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("migration skipped unreadable runs directory")
    return to_rewrite, skipped
