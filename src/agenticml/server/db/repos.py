"""Repositories own SQL and transactional rules; facades retain public records."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets

from agenticml.store import layout
from .errors import DatabaseError
from ..keys import (ClientError, KeyNameTaken, LastAdminError, SetupClosed,
                    UserRequired, KeyRecord, validate_name)
from ..users import (UserRecord, UserNameTaken, UserNotFound, IdentityTaken, InviteInvalid,
                     ROLES, INVITE_HOURS, _new_user_id, _identity_taken, _name_owns_runs,
                     validate_identity, _password_record, _dummy_verify, _check_password)

logger = logging.getLogger('agenticml.server.db.repos')


class SettingsRepo:
    def __init__(self, db):
        self.db = db

    def get(self, name, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(name, tx)
        row = tx.fetchone('SELECT value FROM server_settings WHERE name=?', (name,))
        return row[0] if row else None

    def set(self, name, value, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.set(name, value, tx)
        tx.execute(self.db.dialect.upsert('server_settings', ('name', 'value'), ('name',), ('value',)),
                   (name, value))

    def delete(self, name, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.delete(name, tx)
        return tx.execute('DELETE FROM server_settings WHERE name=?', (name,)).rowcount


class UserRepo:
    def __init__(self, db, server_dir):
        self.db = db
        self.server_dir = Path(server_dir)

    @staticmethod
    def decode(row):
        invite = ({'sha256': row['invite_sha256'], 'expires_at': row['invite_expires_at']}
                  if row['invite_sha256'] is not None else None)
        try:
            password = json.loads(row['password']) if row['password'] is not None else None
        except (ValueError, TypeError, RecursionError) as exc:
            raise DatabaseError('could not read or write database') from exc
        return UserRecord(row['name'], row['id'], row['role'], row['identity'],
                          password, invite, row['generation'], row['created_at'], row['created_by'])

    def save(self, tx, record):
        columns = ('id', 'name', 'role', 'identity', 'identity_folded', 'password',
                   'invite_sha256', 'invite_expires_at', 'generation', 'created_at', 'created_by')
        invite = record.invite or {}
        values = (record.id, record.name, record.role, record.identity,
                  record.identity.casefold() if record.identity is not None else None,
                  json.dumps(record.password) if record.password is not None else None,
                  invite.get('sha256'), invite.get('expires_at'), record.generation,
                  record.created_at, record.created_by)
        tx.execute(self.db.dialect.upsert('users', columns, ('name',),
                                         tuple(c for c in columns if c != 'name')), values)

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

    def list(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.list(tx)
        return [self.decode(row) for row in tx.fetchall('SELECT * FROM users ORDER BY created_at, name')]

    def get(self, name, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(name, tx)
        row = tx.fetchone('SELECT * FROM users WHERE name=?', (name,))
        return self.decode(row) if row else None

    def by_identity(self, value):
        with self.db.transaction() as tx:
            row = tx.fetchone('SELECT * FROM users WHERE identity=?', (value,))
            return self.decode(row) if row else None

    def add(self, name, role="member", identity=None, created_by="cli", *,
            reclaim_runs=False):
        record = self._new(name, role, identity, created_by)
        owns_runs = False if reclaim_runs else _name_owns_runs(self.server_dir.parent, record.name)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            if any(item.name == record.name for item in records):
                raise UserNameTaken("user name already exists")
            if _identity_taken(records, record.identity):
                raise IdentityTaken("identity already in use")
            while any(item.id == record.id for item in records):
                record = replace(record, id=_new_user_id())
            if owns_runs:
                raise ClientError(
                    f"runs are still owned by {record.name}; "
                    "pass --reclaim-runs to give them to the new user")
            self.save(tx, record)
            return record

    def remove(self, name):
        """Delete the user and all of their keys. Returns the number of keys removed."""
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            if found.role == "admin" and not any(
                    item.role == "admin" and item.name != name for item in records):
                raise LastAdminError("last admin user; promote another user first")
            revoked = tx.fetchone('SELECT COUNT(*) FROM keys WHERE user_name=?', (name,))[0]
            tx.execute('DELETE FROM users WHERE name=?', (name,))
            return revoked

    def discard(self, name, user_id):
        """Best-effort user delete for setup rollback. Ignores last-admin; keys cascade."""
        try:
            with self.db.transaction(write=True) as tx:
                tx.advisory_lock('users')
                tx.execute('DELETE FROM users WHERE name=? AND id=?', (name, user_id))
        except DatabaseError:
            logger.warning("could not roll back user %s after failed setup", name)

    def set_role(self, name, role):
        if role not in ROLES:
            raise ClientError("invalid role")
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            if found.role == "admin" and role != "admin":
                if not any(item.role == "admin" and item.name != name for item in records):
                    raise LastAdminError("last admin user; promote another user first")
            updated = replace(found, role=role, generation=found.generation + 1)
            self.save(tx, updated)
            return updated

    def set_identity(self, name, value):
        identity = validate_identity(value)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            if _identity_taken(records, identity, except_name=name):
                raise IdentityTaken("identity already in use")
            updated = replace(found, identity=identity)
            self.save(tx, updated)
            return updated

    def set_password(self, name, password):
        record = _password_record(password)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            updated = replace(
                found, password=record, invite=None, generation=found.generation + 1)
            self.save(tx, updated)
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
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            found = next((item for item in records if item.name == name), None)
            if found is None:
                raise UserNotFound("unknown user")
            updated = replace(
                found, password=None, invite=invite, generation=found.generation + 1)
            self.save(tx, updated)
            return token

    def redeem_invite(self, token, password):
        """Redeem one indexed invite digest atomically."""
        record = _password_record(password)
        if not isinstance(token, str) or not token:
            raise InviteInvalid("invalid invite")
        try:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        except UnicodeEncodeError as exc:
            raise InviteInvalid("invalid invite") from exc
        now = datetime.now(timezone.utc)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            row = tx.fetchone('SELECT * FROM users WHERE invite_sha256=?', (digest,))
            matched = self.decode(row) if row else None
            if matched is None or not hmac.compare_digest(matched.invite['sha256'], digest):
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
            self.save(tx, updated)
            return updated


class KeyRepo:
    def __init__(self, db, server_dir):
        self.db = db
        self.users = UserRepo(db, server_dir)
        self.settings = SettingsRepo(db)

    @staticmethod
    def decode(row):
        return KeyRecord(row['name'], row['scope'], row['sha256'], row['prefix'],
                         row['created_at'], row['created_by'], row['last_used_at'], row['user_name'])

    def list(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.list(tx)
        return [self.decode(row) for row in tx.fetchall('SELECT * FROM keys ORDER BY created_at, name')]

    def by_digest(self, digest):
        with self.db.transaction() as tx:
            row = tx.fetchone('SELECT * FROM keys WHERE sha256=?', (digest,))
            return self.decode(row) if row else None

    def insert(self, tx, record):
        tx.execute('INSERT INTO keys (name, scope, sha256, prefix, created_at, created_by, '
                   'last_used_at, user_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (record.name, record.scope, record.sha256, record.prefix, record.created_at,
                    record.created_by, record.last_used_at, record.user))

    def create(self, name, scope, created_by, *, user, only_if_no_admin, env):
        validate_name(name)
        if scope not in ('sync', 'admin'):
            raise ClientError('invalid key scope')
        if not isinstance(created_by, str):
            raise ValueError('created_by must be a string')
        bootstrap = None
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            if only_if_no_admin and (self.settings.get('setup_closed', tx) is not None
                                     or env or any(r.scope == 'admin' for r in records)):
                raise SetupClosed('setup closed')
            if any(r.name == name for r in records):
                raise KeyNameTaken('key name already exists')
            users = self.users.list(tx)
            if user is None:
                if not users:
                    bootstrap = self.users._new('admin', 'admin', None, created_by)
                    self.users.save(tx, bootstrap)
                    chosen = bootstrap
                elif len(users) == 1:
                    chosen = users[0]
                else:
                    raise UserRequired('--user is required when the server has more than one user')
            else:
                if not isinstance(user, str):
                    raise ClientError('invalid user')
                chosen = self.users.get(user, tx)
                if chosen is None:
                    raise UserNotFound('unknown user')
            if scope == 'admin' and chosen.role != 'admin':
                raise ClientError('admin keys need an admin user')
            secret = 'aml_' + secrets.token_urlsafe(32)
            record = KeyRecord(name, scope, hashlib.sha256(secret.encode()).hexdigest(), secret[:8],
                               layout.format_timestamp(), created_by, None, chosen.name)
            self.insert(tx, record)
            if scope == 'admin':
                self.settings.set('setup_closed', layout.format_timestamp(), tx)
        return record, secret, bootstrap

    def revoke(self, name, *, keep_admin=False, expected=None, env=False):
        if name == 'env':
            raise ValueError('the env key cannot be revoked')
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            records = self.list(tx)
            removed = next((r for r in records if r.name == name), None)
            if removed is None or (expected is not None and (
                    removed.sha256 != expected.sha256 or removed.user != expected.user)):
                return None
            if keep_admin and removed.scope == 'admin':
                admins = {u.name for u in self.users.list(tx) if u.role == 'admin'}
                usable = sum(r.scope == 'admin' and r.user in admins for r in records) + bool(env)
                if removed.user in admins and usable <= 1:
                    raise LastAdminError('last admin key; create another admin key first, or use the CLI')
            tx.execute('DELETE FROM keys WHERE name=?', (name,))
            return removed

    def touch(self, record, now, threshold):
        with self.db.transaction(write=True) as tx:
            tx.execute('UPDATE keys SET last_used_at=? WHERE name=? AND sha256=? '
                       'AND (last_used_at IS NULL OR last_used_at < ?)',
                       (now, record.name, record.sha256, threshold))

    def prepare_setup(self, env):
        with self.db.transaction(write=True) as tx:
            self.settings.delete('setup_closed', tx)
            return not env and not any(r.scope == 'admin' for r in self.list(tx))


class SessionRepo:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def decode(row):
        return dict(user=row['user_name'], id=row['user_id'], generation=row['generation'],
                    via=row['via'], key=row['key_name'], key_sha256=row['key_sha256'],
                    name=row['key_name'] or '', sha256=row['key_sha256'] or '',
                    created=row['created'], last_seen=row['last_seen'])

    def create(self, digest, entry, cap):
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('sessions')
            count = tx.fetchone('SELECT COUNT(*) FROM sessions')[0]
            if count >= cap:
                tx.execute('DELETE FROM sessions WHERE token_sha256 IN '
                           '(SELECT token_sha256 FROM sessions ORDER BY last_seen, created_order LIMIT ?)',
                           (count - cap + 1,))
            order = tx.fetchone('SELECT COALESCE(MAX(created_order), 0) + 1 FROM sessions')[0]
            tx.execute('INSERT INTO sessions (token_sha256, user_name, user_id, generation, via, '
                       'key_name, key_sha256, created, last_seen, created_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (digest, entry['user'], entry['id'], entry['generation'], entry['via'],
                        entry['key'], entry['key_sha256'], entry['created'], entry['last_seen'], order))

    def get(self, digest):
        with self.db.transaction() as tx:
            row = tx.fetchone('SELECT * FROM sessions WHERE token_sha256=?', (digest,))
            return self.decode(row) if row else None

    def list(self):
        with self.db.transaction() as tx:
            return {r['token_sha256']: self.decode(r) for r in tx.fetchall('SELECT * FROM sessions')}

    def touch(self, digest, now):
        with self.db.transaction(write=True) as tx:
            tx.execute('UPDATE sessions SET last_seen=? WHERE token_sha256=? AND last_seen<=?',
                       (now, digest, now - 60))

    def delete(self, digest):
        with self.db.transaction(write=True) as tx:
            tx.execute('DELETE FROM sessions WHERE token_sha256=?', (digest,))

    def delete_for_key(self, name, sha256):
        with self.db.transaction(write=True) as tx:
            tx.execute('DELETE FROM sessions WHERE key_name=? AND key_sha256=?', (name, sha256))

    def prune(self, now, idle, max_age):
        with self.db.transaction(write=True) as tx:
            tx.execute('DELETE FROM sessions WHERE last_seen<=? OR created<=?', (now-idle, now-max_age))


class ThrottleRepo:
    def __init__(self, db):
        self.db = db

    def load(self):
        with self.db.transaction() as tx:
            tx.advisory_lock('throttle')
            return [(r['bucket'], r['failures'], r['last_failure']) for r in
                    tx.fetchall('SELECT * FROM login_attempts ORDER BY last_failure, bucket')]

    def save(self, entries, previous):
        current = {bucket: (failures, last) for bucket, (failures, last, _) in entries.items()
                   if failures}
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('throttle')
            for bucket in previous.keys() - current.keys():
                tx.execute('DELETE FROM login_attempts WHERE bucket=?', (bucket,))
            for bucket, value in current.items():
                if previous.get(bucket) != value:
                    tx.execute(self.db.dialect.upsert(
                        'login_attempts', ('bucket', 'failures', 'last_failure'), ('bucket',),
                        ('failures', 'last_failure')), (bucket, *value))


class AuditRepo:
    def __init__(self, db):
        self.db = db

    def insert(self, event, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.insert(event, tx)
        tx.execute('INSERT INTO audit_events (ts, actor, via, action, target, client, detail) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?)',
                   tuple(event.get(k) for k in ('ts', 'actor', 'via', 'action', 'target', 'client')) +
                   (json.dumps(event.get('detail')) if event.get('detail') is not None else None,))

    def tail(self, limit, before=None):
        with self.db.transaction() as tx:
            sql = 'SELECT ts, actor, via, action, target, client, detail FROM audit_events '
            rows = tx.fetchall(sql + ('WHERE ts < ? ' if before is not None else '') +
                               'ORDER BY ts DESC, id DESC LIMIT ?',
                               (before, limit) if before is not None else (limit,))
            return [{**dict(row), 'detail': json.loads(row['detail']) if row['detail'] is not None else None}
                    for row in rows]
