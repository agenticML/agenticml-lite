"""Users, roles, principals, and 2.0/2.1 data-directory migration."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import stat

from agenticml.store import layout
from .keys import (
    ClientError, KeyStoreUnavailable, LastAdminError, validate_name,
)

logger = logging.getLogger("agenticml.server.users")
ROLES = ("viewer", "member", "admin", "owner")
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


class InviteEmailMismatch(ValueError):
    """Firebase invite email does not match the verified token email."""

    def __init__(self):
        super().__init__('this invite is for a different or unverified email')


class InviteAccountExists(ValueError):
    """Invite email matches a credentialed account; the invite stays pending."""

    def __init__(self):
        super().__init__(
            'an account with this email already exists; sign in with it or ask a server admin')


class InviteExistingAccount(Exception):
    """Membership was added; the caller should sign in with existing credentials."""

    def __init__(self, account_id):
        super().__init__('sign in with your existing credentials')
        self.account_id = account_id


CROSS_WORKSPACE_CREDENTIALS = (
    'this account belongs to other workspaces; only the person or a server admin can change it')


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
    workspace_id: str | None = None
    server_admin: bool = False


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


def _scrypt_params_allowed(n, r, p, dklen):
    """True when params are no more expensive than the 2.2 login record."""
    return (type(n) is int and type(r) is int and type(p) is int and type(dklen) is int
            and 2 <= n <= SCRYPT_N and (n & (n - 1)) == 0
            and 1 <= r <= SCRYPT_R and 1 <= p <= SCRYPT_P
            and 16 <= dklen <= SCRYPT_DKLEN)


def _safe_password_record(record):
    """True when ``record`` is a 2.2 scrypt dict that login may evaluate."""
    if not isinstance(record, dict) or record.get("alg") != "scrypt":
        return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(salt) and 8 <= len(salt) <= 64 and _scrypt_params_allowed(
        record.get("n"), record.get("r"), record.get("p"), len(expected))


def _check_password(password, record):
    if not isinstance(record, dict) or record.get("alg") != "scrypt":
        return _dummy_verify(password)
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        n, r, p = record["n"], record["r"], record["p"]
        if not expected or not _scrypt_params_allowed(n, r, p, len(expected)):
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
    if key_record.workspace_id != users.workspace_id:
        return None
    if key_record.name == "env":
        return Principal("env", "admin", "key", "env", workspace_id=users.workspace_id)
    user = users.get(key_record.user)
    if user is None:
        return None
    return Principal(
        user.name, user.role, "key", key_record.name, user.id, user.generation, users.workspace_id,
        bool(users.repo.accounts.get(user.name)["server_admin"]))


def attribution(run_dir, db, workspace_id):
    """Return ``(owner_user, owner_key)`` from the runs table; either may be None."""
    from .db.repos import RunRepo

    return RunRepo(db, workspace_id).attribution(Path(run_dir).name)


class UserStore:
    """Public user API backed by the shared database pool."""

    def __init__(self, server_dir, *, db=None, workspace_id):
        from .db import for_server_dir
        from .db.repos import UserRepo
        from .db.workspaces import require_workspace_id

        self.server_dir = Path(server_dir)
        self.db = db if db is not None else for_server_dir(self.server_dir)
        self.workspace_id = require_workspace_id(workspace_id)
        self.repo = UserRepo(self.db, self.workspace_id)

    def _call(self, name, *args, **kwargs):
        from .db import DatabaseError
        try:
            return getattr(self.repo, name)(*args, **kwargs)
        except DatabaseError as exc:
            raise KeyStoreUnavailable("could not read or write database") from exc

    def list(self):
        return self._call('list')

    def get(self, name):
        return self._call('get', name)

    def by_identity(self, value):
        return self._call('by_identity', value)

    def add(self, name, role="member", identity=None, created_by="cli", *, reclaim_runs=False, plans=None):
        return self._call('add', name, role, identity, created_by, reclaim_runs=reclaim_runs, plans=plans)

    def remove(self, name):
        return self._call('remove', name)

    def discard(self, name, user_id):
        return self._call('discard', name, user_id)

    def set_role(self, name, role):
        return self._call('set_role', name, role)

    def set_identity(self, name, value, *, operator=False):
        return self._call('set_identity', name, value, operator=operator)

    def set_password(self, name, password, *, operator=False, acting_account_id=None):
        return self._call('set_password', name, password, operator=operator,
                          acting_account_id=acting_account_id)

    def verify_password(self, name, password):
        return self._call('verify_password', name, password)

    def create_invite(self, name, *, operator=False):
        return self._call('create_invite', name, operator=operator)

    def redeem_invite(self, token, password):
        return self._call('redeem_invite', token, password)

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
        if row["password"] is not None and not _safe_password_record(row["password"]):
            raise ValueError("invalid user record")
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
        if row["name"] in names or row["id"] in ids or row["role"] not in ROLES[:-1]:
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


def migrate(data_dir, *, actor="server"):
    """Compatibility entry point: open and import the old directory once."""
    from .db import DatabaseError, for_server_dir
    try:
        for_server_dir(Path(data_dir) / 'server', actor=actor)
        return True
    except DatabaseError:
        logger.error("could not import legacy server state")
        return False


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
