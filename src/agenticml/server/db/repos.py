"""Repositories own SQL and transactional rules; facades retain public records."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import secrets
import time

from agenticml.store import layout
from agenticml.store import notes as note_store
from .errors import DatabaseError
from ..keys import (ClientError, KeyNameTaken, LastAdminError, SetupClosed,
                    UserRequired, KeyRecord, validate_name)
from ..users import (UserRecord, UserNameTaken, UserNotFound, IdentityTaken, InviteInvalid,
                     CROSS_WORKSPACE_CREDENTIALS, ROLES, INVITE_HOURS, _new_user_id, _identity_taken,
                     validate_identity, _password_record, _dummy_verify, _check_password)

from .workspaces import AccountRepo, WorkspaceRepo, require_workspace_id, validate_login

logger = logging.getLogger('agenticml.server.db.repos')


class SettingsRepo:
    def __init__(self, db):
        self.db = db

    def get(self, name, tx=None):
        name = name.lower() if isinstance(name, str) and "@" in name else name
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
    """Compatibility facade for accounts joined to one workspace's membership."""
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)
        self.accounts = AccountRepo(db)

    @staticmethod
    def decode(row):
        invite = ({'sha256': row['invite_sha256'], 'expires_at': row['invite_expires_at']}
                  if row['invite_sha256'] is not None else None)
        try:
            password = json.loads(row['password']) if row['password'] is not None else None
        except (ValueError, TypeError, RecursionError) as exc:
            raise DatabaseError('could not read or write database') from exc
        return UserRecord(row['login'], row['id'], row['role'], row['identity'],
                          password, invite, row['generation'], row['created_at'], row['created_by'])

    def _select(self):
        return ('SELECT a.*, m.role, i.sha256 AS invite_sha256, i.expires_at AS invite_expires_at '
                'FROM memberships m JOIN accounts a ON a.id=m.account_id '
                'LEFT JOIN invites i ON i.workspace_id=m.workspace_id AND i.login=a.login '
                'WHERE m.workspace_id=? ')

    def save(self, tx, record):
        existing = self.accounts.get(record.name, tx)
        aid = existing['id'] if existing else record.id
        columns = ('id', 'login', 'display_name', 'identity', 'identity_folded', 'password',
                   'generation', 'created_at', 'created_by')
        updates = ('identity', 'identity_folded', 'password', 'generation')
        if existing is not None and existing['created_by'] == 'legacy run owner':
            updates = updates + ('created_by',)
        tx.execute(self.db.dialect.upsert('accounts', columns, ('login',), updates),
                   (aid, record.name, record.name, record.identity,
                    record.identity.casefold() if record.identity is not None else None,
                    json.dumps(record.password) if record.password is not None else None,
                    record.generation, record.created_at, record.created_by))
        role = record.role
        owner = tx.fetchone("SELECT 1 FROM memberships WHERE workspace_id=? AND role='owner'", (self.workspace_id,))
        if role == 'admin' and owner is None:
            role = 'owner'
            workspace = WorkspaceRepo(self.db).by_id(self.workspace_id, tx)
            if workspace['slug'] == 'default':
                tx.execute('UPDATE accounts SET server_admin=1 WHERE id=?', (aid,))
            tx.execute('UPDATE workspaces SET created_by=? WHERE id=?', (aid, self.workspace_id))
        tx.execute(self.db.dialect.upsert('memberships', ('workspace_id', 'account_id', 'role'),
                                         ('workspace_id', 'account_id'), ('role',)),
                   (self.workspace_id, aid, role))
        if record.invite:
            tx.execute(self.db.dialect.upsert('invites',
                       ('workspace_id', 'login', 'role', 'sha256', 'expires_at', 'invited_by'),
                       ('workspace_id', 'login'), ('role', 'sha256', 'expires_at', 'invited_by')),
                       (self.workspace_id, record.name, 'admin' if role == 'owner' else role, record.invite['sha256'],
                        record.invite['expires_at'], record.created_by))
        else:
            tx.execute('DELETE FROM invites WHERE workspace_id=? AND login=?', (self.workspace_id, record.name))

    @staticmethod
    def _new(name, role, identity, created_by):
        name = validate_login(name)
        if role not in ROLES[:-1]:
            raise ClientError('invalid role')
        if not isinstance(created_by, str):
            raise ValueError('created_by must be a string')
        return UserRecord(name, _new_user_id(), role, validate_identity(identity), None, None, 0,
                          layout.format_timestamp(), created_by)

    def list(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.list(tx)
        return [self.decode(row) for row in tx.fetchall(self._select() + 'ORDER BY a.created_at, a.login', (self.workspace_id,))]

    def get(self, name, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(name, tx)
        row = tx.fetchone(self._select() + 'AND a.login=?', (self.workspace_id, name))
        return self.decode(row) if row else None

    def by_identity(self, value):
        with self.db.transaction() as tx:
            row = tx.fetchone(self._select() + 'AND a.identity=?', (self.workspace_id, value))
            return self.decode(row) if row else None

    def add(self, name, role='member', identity=None, created_by='cli', *, reclaim_runs=False):
        record = self._new(name, role, identity, created_by)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            if self.get(record.name, tx):
                raise UserNameTaken('user name already exists')
            existing = self.accounts.get(record.name, tx)
            if not reclaim_runs:
                if existing is not None and existing['created_by'] == 'legacy run owner':
                    raise ClientError(f'runs are still owned by {record.name}; pass --reclaim-runs to give them to the new user')
                if existing is None and RunRepo(self.db, self.workspace_id).name_owns(record.name, tx):
                    raise ClientError(f'runs are still owned by {record.name}; pass --reclaim-runs to give them to the new user')
            if identity is not None:
                other = tx.fetchone('SELECT login FROM accounts WHERE identity_folded=?', (identity.casefold(),))
                if other and other['login'] != record.name:
                    raise IdentityTaken('identity already in use')
            if existing:
                # Adding an existing account grants membership; never resets its credentials.
                if identity is not None and identity != existing['identity']:
                    raise IdentityTaken('identity already in use')
                self.save(tx, replace(record, id=existing['id'], identity=existing['identity'],
                                      password=json.loads(existing['password']) if existing['password'] else None,
                                      generation=existing['generation']))
            else:
                self.save(tx, record)
            return self.get(record.name, tx)

    def remove(self, name):
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            found = self.get(name, tx)
            if found is None:
                raise UserNotFound('unknown user')
            if found.role == 'owner':
                raise LastAdminError('workspace owner cannot be removed or demoted')
            name = found.name
            revoked = tx.fetchone('SELECT COUNT(*) FROM keys WHERE workspace_id=? AND account_id=?',
                                  (self.workspace_id, found.id))[0]
            tx.execute('DELETE FROM invites WHERE workspace_id=? AND login=?', (self.workspace_id, name))
            tx.execute('DELETE FROM memberships WHERE workspace_id=? AND account_id=?', (self.workspace_id, found.id))
            return revoked

    def discard(self, name, user_id):
        try:
            with self.db.transaction(write=True) as tx:
                tx.advisory_lock('users')
                found = self.get(name, tx)
                if found is None or found.id != user_id:
                    return
                tx.execute('DELETE FROM invites WHERE workspace_id=? AND login=?', (self.workspace_id, name))
                tx.execute('DELETE FROM memberships WHERE workspace_id=? AND account_id=?', (self.workspace_id, user_id))
                self.accounts.discard_unreferenced(user_id, name, tx)
        except DatabaseError:
            logger.warning('could not roll back user after failed setup')

    def set_role(self, name, role):
        if role not in ROLES[:-1]:
            raise ClientError('invalid role')
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            found = self.get(name, tx)
            if found is None:
                raise UserNotFound('unknown user')
            if found.role == 'owner':
                raise LastAdminError('workspace owner cannot be removed or demoted')
            updated = replace(found, role=role, generation=found.generation + 1)
            self.save(tx, updated)
            return self.get(name, tx)

    def _reject_cross_workspace_credentials(self, tx, account_id, *, operator=False,
                                           acting_account_id=None):
        if operator:
            return
        if acting_account_id == account_id:
            return
        other = tx.fetchone('SELECT 1 FROM memberships WHERE account_id=? AND workspace_id!=?',
                            (account_id, self.workspace_id))
        if other:
            raise ClientError(CROSS_WORKSPACE_CREDENTIALS)

    def set_identity(self, name, value, *, operator=False):
        identity = validate_identity(value)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            found = self.get(name, tx)
            if found is None:
                raise UserNotFound('unknown user')
            self._reject_cross_workspace_credentials(tx, found.id, operator=operator)
            other = tx.fetchone('SELECT login FROM accounts WHERE identity_folded=?',
                                (identity.casefold() if identity is not None else None,))
            if other and other['login'] != found.name:
                raise IdentityTaken('identity already in use')
            updated = replace(found, identity=identity)
            self.save(tx, updated)
            return updated

    def set_password(self, name, password, *, operator=False, acting_account_id=None):
        record = _password_record(password)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            found = self.get(name, tx)
            if found is None:
                raise UserNotFound('unknown user')
            self._reject_cross_workspace_credentials(
                tx, found.id, operator=operator, acting_account_id=acting_account_id)
            updated = replace(found, password=record, invite=None, generation=found.generation + 1)
            self.save(tx, updated)
            return updated

    def verify_password(self, name, password):
        user = self.get(name) if isinstance(name, str) else None
        if user is None or user.password is None or not isinstance(password, str):
            _dummy_verify(password if isinstance(password, str) else '')
            return None
        return user if _check_password(password, user.password) else None

    def create_invite(self, name, *, operator=False):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        expires_at = layout.format_timestamp(datetime.now(timezone.utc) + timedelta(hours=INVITE_HOURS))
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            found = self.get(name, tx)
            if found is None:
                raise UserNotFound('unknown user')
            self._reject_cross_workspace_credentials(tx, found.id, operator=operator)
            # The legacy default API also supports password-reset invite links.
            self.save(tx, replace(found, password=None, invite={'sha256': digest, 'expires_at': expires_at},
                                  generation=found.generation + 1))
        return token

    def redeem_invite(self, token, password):
        record = _password_record(password)
        if not isinstance(token, str) or not token:
            raise InviteInvalid('invalid invite')
        try:
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        except UnicodeEncodeError as exc:
            raise InviteInvalid('invalid invite') from exc
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            row = tx.fetchone(self._select() + 'AND i.sha256=?', (self.workspace_id, digest))
            matched = self.decode(row) if row else None
            if matched is None or not hmac.compare_digest(matched.invite['sha256'], digest):
                raise InviteInvalid('invalid invite')
            try:
                expires = layout.parse_timestamp(matched.invite['expires_at'])
            except (TypeError, ValueError) as exc:
                raise InviteInvalid('invalid invite') from exc
            if expires <= datetime.now(timezone.utc):
                raise InviteInvalid('invalid invite')
            self._reject_cross_workspace_credentials(tx, matched.id)
            updated = replace(matched, password=record, invite=None, generation=matched.generation + 1)
            self.save(tx, updated)
            return updated


class KeyVerifier:
    """Only key verification is global: a unique digest determines its workspace."""
    def __init__(self, db):
        self.db = db

    def by_digest(self, digest):
        with self.db.transaction() as tx:
            row = tx.fetchone('SELECT k.*, a.login AS user_name FROM keys k JOIN accounts a ON a.id=k.account_id '
                              'WHERE k.sha256=?', (digest,))
            return KeyRepo.decode(row) if row else None


class KeyRepo:
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)
        self.users = UserRepo(db, workspace_id)
        self.settings = SettingsRepo(db)

    @staticmethod
    def decode(row):
        return KeyRecord(row['name'], row['scope'], row['sha256'], row['prefix'],
                         row['created_at'], row['created_by'], row['last_used_at'], row['user_name'], row['workspace_id'], row['account_id'])

    def list(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.list(tx)
        return [self.decode(row) for row in tx.fetchall('SELECT k.*, a.login AS user_name FROM keys k JOIN accounts a ON a.id=k.account_id '
            'WHERE k.workspace_id=? ORDER BY k.created_at, k.name', (self.workspace_id,))]

    def insert(self, tx, record):
        chosen = self.users.get(record.user, tx)
        if chosen is None:
            raise DatabaseError('could not read or write database')
        tx.execute('INSERT INTO keys (workspace_id, name, account_id, scope, sha256, prefix, '
                   'created_at, created_by, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (self.workspace_id, record.name, chosen.id, record.scope, record.sha256,
                    record.prefix, record.created_at, record.created_by, record.last_used_at))

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
                    existing = self.users.accounts.get('admin', tx)
                    if existing is None:
                        bootstrap = self.users._new('admin', 'admin', None, created_by)
                        self.users.save(tx, bootstrap)
                        chosen = self.users.get(bootstrap.name, tx)
                        bootstrap = chosen
                    else:
                        password = json.loads(existing['password']) if existing['password'] else None
                        self.users.save(tx, UserRecord(
                            existing['login'], existing['id'], 'admin', existing['identity'],
                            password, None, existing['generation'], existing['created_at'],
                            existing['created_by']))
                        chosen = self.users.get(existing['login'], tx)
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
            if scope == 'admin' and chosen.role not in ('owner', 'admin'):
                raise ClientError('admin keys need an admin user')
            secret = 'aml_' + secrets.token_urlsafe(32)
            record = KeyRecord(name, scope, hashlib.sha256(secret.encode()).hexdigest(), secret[:8],
                               layout.format_timestamp(), created_by, None, chosen.name, self.workspace_id, chosen.id)
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
                admins = {u.name for u in self.users.list(tx) if u.role in ('owner', 'admin')}
                usable = sum(r.scope == 'admin' and r.user in admins for r in records) + bool(env)
                if removed.user in admins and usable <= 1:
                    raise LastAdminError('last admin key; create another admin key first, or use the CLI')
            tx.execute('DELETE FROM keys WHERE workspace_id=? AND name=?', (self.workspace_id, name))
            return removed

    def touch(self, record, now, threshold):
        with self.db.transaction(write=True) as tx:
            tx.execute('UPDATE keys SET last_used_at=? WHERE workspace_id=? AND name=? AND sha256=? '
                       'AND (last_used_at IS NULL OR last_used_at < ?)',
                       (now, record.workspace_id, record.name, record.sha256, threshold))

    def prepare_setup(self, env):
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            self.settings.delete('setup_closed', tx)
            return not env and not any(r.scope == 'admin' for r in self.list(tx))


class SessionRepo:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def decode(row):
        return dict(user=row['login'] or ('env' if row['key_name'] == 'env' else ''), id=row['account_id'],
                    workspace_id=row['workspace_id'], generation=row['generation'],
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
            tx.execute('INSERT INTO sessions (token_sha256, account_id, workspace_id, generation, via, '
                       'key_name, key_sha256, created, last_seen, created_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (digest, entry['id'], entry.get('workspace_id'), entry['generation'], entry['via'],
                        entry['key'], entry['key_sha256'], entry['created'], entry['last_seen'], order))

    def get(self, digest):
        with self.db.transaction() as tx:
            row = tx.fetchone('SELECT s.*, a.login FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id WHERE token_sha256=?', (digest,))
            return self.decode(row) if row else None

    def list(self):
        with self.db.transaction() as tx:
            return {r['token_sha256']: self.decode(r) for r in tx.fetchall('SELECT s.*, a.login FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id')}

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
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id) if workspace_id is not None else None

    def insert(self, event, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.insert(event, tx)
        tx.execute('INSERT INTO audit_events (workspace_id, ts, actor, via, action, target, client, detail) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (self.workspace_id,) + tuple(event.get(k) for k in
                   ('ts', 'actor', 'via', 'action', 'target', 'client')) +
                   (json.dumps(event.get('detail')) if event.get('detail') is not None else None,))

    def tail(self, limit, before=None):
        with self.db.transaction() as tx:
            sql = ('SELECT ts, actor, via, action, target, client, detail FROM audit_events WHERE ' +
                   ('workspace_id=? ' if self.workspace_id is not None else 'workspace_id IS NULL '))
            params = (self.workspace_id,) if self.workspace_id is not None else ()
            rows = tx.fetchall(sql + ('AND ts < ? ' if before is not None else '') +
                               'ORDER BY ts DESC, id DESC LIMIT ?',
                               params + ((before, limit) if before is not None else (limit,)))
            return [{**dict(row), 'detail': json.loads(row['detail']) if row['detail'] is not None else None}
                    for row in rows]


_RUN_COLUMNS = ('run_id', 'project', 'created_at', 'owner_user', 'owner_key', 'status',
                'fingerprint', 'last_sync_at', 'last_sync_key', 'list_row', 'last_step',
                'launch_note', 'commit_sizes', 'updated_at')


_RUN_DB_COLUMNS = tuple('owner_account_id' if c == 'owner_user' else c for c in _RUN_COLUMNS)


def _run_dict(row):
    return {name: row[name] for name in _RUN_COLUMNS}


class RunRepo:
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)

    def get(self, run_id, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(run_id, tx)
        row = tx.fetchone('SELECT *, (SELECT login FROM accounts WHERE '
                          'id=runs.owner_account_id) AS owner_user FROM runs WHERE '
                          'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
        return _run_dict(row) if row else None

    def ids(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.ids(tx)
        return {row['run_id'] for row in tx.fetchall('SELECT run_id FROM runs WHERE workspace_id=?', (self.workspace_id,))}

    def list_rows(self, tx=None):
        """Dashboard list: newest created_at first, matching reader._run_sort_key."""
        if tx is None:
            with self.db.transaction() as tx:
                return self.list_rows(tx)
        return [_run_dict(row) for row in tx.fetchall(
            'SELECT *, (SELECT login FROM accounts WHERE id=runs.owner_account_id) AS '
            "owner_user FROM runs WHERE workspace_id=? ORDER BY COALESCE(created_at, '') DESC, "
            'run_id DESC', (self.workspace_id,))]

    def all_rows(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.all_rows(tx)
        return [_run_dict(row) for row in tx.fetchall('SELECT *, (SELECT login FROM accounts WHERE '
                                                      'id=runs.owner_account_id) AS owner_user FROM runs '
                                                      'WHERE workspace_id=?', (self.workspace_id,))]

    def files(self, run_id, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.files(run_id, tx)
        return {row['path']: {'size': row['size'], 'sha256': row['sha256']}
                for row in tx.fetchall('SELECT path, size, sha256 FROM run_files WHERE workspace_id=? AND run_id=?',
                                       (self.workspace_id, run_id))}

    def committed_size(self, run_id, path, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.committed_size(run_id, path, tx)
        row = tx.fetchone('SELECT size FROM run_files WHERE workspace_id=? AND run_id=? AND path=?', (self.workspace_id, run_id, path))
        return 0 if row is None else row['size']

    def name_owns(self, name, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.name_owns(name, tx)
        return tx.fetchone('SELECT 1 FROM runs WHERE workspace_id=? AND '
                           'owner_account_id=(SELECT id FROM accounts WHERE login=?)', (self.workspace_id, name)) is not None

    def owner_key_counts(self, tx=None):
        """(owner_key, owner_user) -> run count for keys that created a run."""
        if tx is None:
            with self.db.transaction() as tx:
                return self.owner_key_counts(tx)
        counts = {}
        for row in tx.fetchall(
                'SELECT owner_key, (SELECT login FROM accounts WHERE id=owner_account_id) AS '
                'owner_user, COUNT(*) AS n FROM runs WHERE workspace_id=? AND owner_key IS NOT '
                'NULL AND owner_account_id IS NOT NULL GROUP BY owner_key, owner_account_id', (self.workspace_id,)):
            counts[(row['owner_key'], row['owner_user'])] = row['n']
        return counts

    def owner_user_counts(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.owner_user_counts(tx)
        return {row['owner_user']: row['n'] for row in tx.fetchall(
            'SELECT (SELECT login FROM accounts WHERE id=owner_account_id) AS owner_user, '
            'COUNT(*) AS n FROM runs WHERE workspace_id=? AND owner_account_id IS NOT NULL '
            'GROUP BY owner_account_id', (self.workspace_id,))}

    def as_remote(self, run_id, tx=None):
        """2.2 remote.json object shape for /state and tests."""
        if tx is None:
            with self.db.transaction() as tx:
                return self.as_remote(run_id, tx)
        row = self.get(run_id, tx)
        if row is None:
            return {}
        payload = {}
        if isinstance(row['owner_user'], str):
            payload['owner'] = row['owner_user']
        elif row['status'] is not None:
            payload['owner'] = None
        if isinstance(row['owner_key'], str):
            payload['owner_key'] = row['owner_key']
        if row['status'] is not None:
            payload['status'] = row['status']
            payload['fingerprint'] = row['fingerprint']
            payload['last_sync_at'] = row['last_sync_at']
            payload['last_sync_key'] = row['last_sync_key']
            raw = row['commit_sizes']
            if raw is not None:
                try:
                    sizes = json.loads(raw)
                except (ValueError, TypeError, RecursionError):
                    sizes = None
                if isinstance(sizes, dict):
                    payload['sizes'] = sizes
        return payload

    def normalized_remote(self, run_id, tx=None):
        """Shape of store.reader.read_remote, or None when the run has no row."""
        if tx is None:
            with self.db.transaction() as tx:
                return self.normalized_remote(run_id, tx)
        row = self.get(run_id, tx)
        if row is None:
            return None
        owner = row['owner_user'] if isinstance(row['owner_user'], str) else None
        attribution = {name: row[name] if isinstance(row[name], str) else None
                       for name in ('owner_key', 'last_sync_key')}
        if row['status'] is None:
            return {'status': None, 'last_sync_at': None, 'owner': owner, **attribution}
        last_sync_at = row['last_sync_at']
        if isinstance(last_sync_at, str):
            try:
                layout.parse_timestamp(last_sync_at)
            except (TypeError, ValueError, OverflowError):
                last_sync_at = None
        else:
            last_sync_at = None
        return {'status': row['status'], 'last_sync_at': last_sync_at, 'owner': owner, **attribution}

    def attribution(self, run_id, tx=None):
        row = self.get(run_id, tx)
        if row is None:
            return None, None
        owner = row['owner_user'] if isinstance(row['owner_user'], str) else None
        owner_key = row['owner_key'] if isinstance(row['owner_key'], str) else None
        return owner, owner_key

    @contextmanager
    def file_guard(self, run_id, path):
        """Serialize a run-file mutation. SQLite: no-op; caller holds run_lock."""
        yield

    def insert(self, row, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.insert(row, tx)
        values = self._values(row, tx)
        tx.execute(
            'INSERT INTO runs (workspace_id, run_id, project, created_at, owner_account_id, '
            'owner_key, status, fingerprint, last_sync_at, last_sync_key, list_row, last_step, '
            'launch_note, commit_sizes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
            '?, ?, ?, ?)', (self.workspace_id, *values))

    def _values(self, row, tx):
        aid = AccountRepo(self.db).ensure_attribution(row.get('owner_user'), tx)
        return tuple(aid if name == 'owner_user' else row.get(name) for name in _RUN_COLUMNS)

    def _ensure(self, tx, run_id):
        if tx.fetchone('SELECT 1 FROM runs WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id)) is None:
            tx.execute(
                'INSERT INTO runs (workspace_id, run_id, list_row, updated_at) VALUES (?, ?, ?, ?)',
                (self.workspace_id, run_id, json.dumps({'run_id': run_id}), layout.format_timestamp()))

    def claim_meta(self, run_id, *, owner_user, owner_key, list_row, project, created_at,
                   launch_note, claim, recreate=False):
        """Insert or claim the run row before meta.json is written."""
        now = layout.format_timestamp()
        encoded = json.dumps(list_row)
        with self.db.transaction(write=True) as tx:
            tx.lock_row('runs', 'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            owner_user = AccountRepo(self.db).ensure_attribution(owner_user, tx)
            row = tx.fetchone('SELECT *, (SELECT login FROM accounts WHERE '
                              'id=runs.owner_account_id) AS owner_user FROM runs WHERE '
                              'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            if row is None:
                tx.execute(
                    'INSERT INTO runs (workspace_id, run_id, project, created_at, '
                    'owner_account_id, owner_key, list_row, launch_note, updated_at) VALUES '
                    '(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (self.workspace_id, run_id, project, created_at, owner_user, owner_key, encoded, launch_note, now))
            else:
                if recreate:
                    tx.execute(
                        'UPDATE runs SET project=?, created_at=?, list_row=?, launch_note=?, '
                        'owner_key=?, status=NULL, fingerprint=NULL, last_sync_at=NULL, '
                        'last_sync_key=NULL, last_step=NULL, commit_sizes=NULL, updated_at=? '
                        'WHERE workspace_id=? AND run_id=?',
                        (project, created_at, encoded, launch_note, owner_key, now, self.workspace_id, run_id))
                    tx.execute('DELETE FROM run_files WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id))
                if claim and row['owner_user'] is None:
                    tx.execute(
                        'UPDATE runs SET owner_account_id=?, owner_key=?, project=?, '
                        'created_at=?, list_row=?, launch_note=?, updated_at=? WHERE '
                        'workspace_id=? AND run_id=?',
                        (owner_user, owner_key, project, created_at, encoded, launch_note, now, self.workspace_id, run_id))
                elif not recreate:
                    tx.execute(
                        'UPDATE runs SET project=?, created_at=?, list_row=?, launch_note=?, '
                        'updated_at=? WHERE workspace_id=? AND run_id=?',
                        (project, created_at, encoded, launch_note, now, self.workspace_id, run_id))

    def record_file(self, run_id, path, size, sha256=None, last_step=None):
        now = layout.format_timestamp()
        with self.db.transaction(write=True) as tx:
            tx.lock_row('run_files', 'workspace_id=? AND run_id=? AND path=?', (self.workspace_id, run_id, path))
            self._ensure(tx, run_id)
            tx.execute(self.db.dialect.upsert(
                'run_files', ('workspace_id', 'run_id', 'path', 'size', 'sha256'), ('workspace_id', 'run_id', 'path'),
                ('size', 'sha256')), (self.workspace_id, run_id, path, size, sha256))
            if path == 'scalars/_time.bin':
                tx.execute('UPDATE runs SET last_step=?, updated_at=? WHERE workspace_id=? AND run_id=?',
                           (last_step, now, self.workspace_id, run_id))
            else:
                tx.execute('UPDATE runs SET updated_at=? WHERE workspace_id=? AND run_id=?', (now, self.workspace_id, run_id))

    def commit_sync(self, run_id, status, fingerprint, last_sync_at, last_sync_key,
                    commit_sizes=None):
        now = layout.format_timestamp()
        encoded = json.dumps(commit_sizes) if commit_sizes is not None else None
        with self.db.transaction(write=True) as tx:
            tx.lock_row('runs', 'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            self._ensure(tx, run_id)
            if commit_sizes is not None:
                tx.execute(
                    'UPDATE runs SET status=?, fingerprint=?, last_sync_at=?, last_sync_key=?, '
                    'commit_sizes=?, updated_at=? WHERE workspace_id=? AND run_id=?',
                    (status, fingerprint, last_sync_at, last_sync_key, encoded, now, self.workspace_id, run_id))
            else:
                tx.execute(
                    'UPDATE runs SET status=?, fingerprint=?, last_sync_at=?, last_sync_key=?, '
                    'updated_at=? WHERE workspace_id=? AND run_id=?',
                    (status, fingerprint, last_sync_at, last_sync_key, now, self.workspace_id, run_id))

    def set_owner(self, run_id, owner_user, owner_key):
        now = layout.format_timestamp()
        with self.db.transaction(write=True) as tx:
            tx.lock_row('runs', 'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            self._ensure(tx, run_id)
            owner_user = AccountRepo(self.db).ensure_attribution(owner_user, tx)
            tx.execute(
                'UPDATE runs SET owner_account_id=?, owner_key=?, updated_at=? WHERE workspace_id=? AND run_id=?',
                (owner_user, owner_key, now, self.workspace_id, run_id))

    def delete(self, run_id, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.delete(run_id, tx)
        tx.execute('DELETE FROM runs WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id))

    def replace_files(self, run_id, files, tx=None):
        """files: iterable of (path, size, sha256). Replaces all run_files rows."""
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.replace_files(run_id, files, tx)
        tx.execute('DELETE FROM run_files WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id))
        for path, size, sha256 in files:
            tx.execute('INSERT INTO run_files (workspace_id, run_id, path, size, sha256) VALUES (?, ?, ?, ?, ?)',
                       (self.workspace_id, run_id, path, size, sha256))

    def upsert_adopted(self, row, files, tx=None):
        """Insert or replace an adopted/imported run and its files (import only)."""
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.upsert_adopted(row, files, tx)
        existing = tx.fetchone('SELECT 1 FROM runs WHERE workspace_id=? AND run_id=?', (self.workspace_id, row['run_id']))
        if existing:
            tx.execute(
                'UPDATE runs SET project=?, created_at=?, owner_account_id=?, owner_key=?, '
                'status=?, fingerprint=?, last_sync_at=?, last_sync_key=?, list_row=?, '
                'last_step=?, launch_note=?, commit_sizes=?, updated_at=? WHERE workspace_id=? '
                'AND run_id=?',
                (row.get('project'), row.get('created_at'), AccountRepo(self.db).ensure_attribution(row.get('owner_user'), tx), row.get('owner_key'), row.get('status'), row.get('fingerprint'), row.get('last_sync_at'), row.get('last_sync_key'), row['list_row'], row.get('last_step'), row.get('launch_note'), row.get('commit_sizes'), row['updated_at'], self.workspace_id, row['run_id']))
        else:
            self.insert(row, tx)
        self.replace_files(row['run_id'], files, tx)

    def insert_adopted(self, row, files, tx=None):
        """Insert a reconcile-adopted run only when the row is still absent."""
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.insert_adopted(row, files, tx)
        if tx.fetchone('SELECT 1 FROM runs WHERE workspace_id=? AND run_id=?', (self.workspace_id, row['run_id'])):
            return False
        result = tx.execute(
            self.db.dialect.upsert('runs', ('workspace_id',) + _RUN_DB_COLUMNS, ('workspace_id', 'run_id'), ()),
            (self.workspace_id, *self._values(row, tx)))
        if result.rowcount == 0:
            return False
        for path, size, sha256 in files:
            tx.execute(
                self.db.dialect.upsert(
                    'run_files', ('workspace_id', 'run_id', 'path', 'size', 'sha256'), ('workspace_id', 'run_id', 'path'), ()),
                (self.workspace_id, row['run_id'], path, size, sha256))
        return True

    def rebuild_index(self, run_id, list_row, project, created_at, launch_note, last_step, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.rebuild_index(
                    run_id, list_row, project, created_at, launch_note, last_step, tx)
        now = layout.format_timestamp()
        tx.execute(
            'UPDATE runs SET list_row=?, project=?, created_at=?, launch_note=?, last_step=?, '
            'updated_at=? WHERE workspace_id=? AND run_id=?',
            (json.dumps(list_row), project, created_at, launch_note, last_step, now, self.workspace_id, run_id))

    def dashboard_list(self):
        """One ``/api/runs`` row per indexed run. No file access."""
        from agenticml.store.reader import _remote_status

        now = time.time()
        rows = []
        for run in self.list_rows():
            try:
                list_row = json.loads(run['list_row'])
            except (ValueError, TypeError, RecursionError):
                list_row = {}
            if not isinstance(list_row, dict):
                list_row = {}
            row = dict(list_row)
            row['run_id'] = run['run_id']
            row['status'] = _effective_status_from_run(run, list_row, now, _remote_status)
            row['last_step'] = run['last_step']
            row['last_sync_at'] = run['last_sync_at']
            row['owner'] = run['owner_user']
            rows.append(row)
        return rows

    def effective_status(self, run_id):
        """Dashboard status for one run: same rule as ``dashboard_list``."""
        from agenticml.store.reader import _remote_status

        run = self.get(run_id)
        if run is None:
            return None
        try:
            list_row = json.loads(run['list_row'])
        except (ValueError, TypeError, RecursionError):
            list_row = {}
        if not isinstance(list_row, dict):
            list_row = {}
        return _effective_status_from_run(run, list_row, time.time(), _remote_status)


class NoteRepo:
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)
        self.runs = RunRepo(db, workspace_id)

    def _current(self, run, note_row):
        if note_row is not None:
            body = note_row['body']
            data = body.encode('utf-8')
            return {
                'text': body,
                'updated_at': note_row['updated_at'],
                'revision': hashlib.sha256(data).hexdigest(),
                'source': 'file',
            }
        launch = run['launch_note']
        if isinstance(launch, str) and launch:
            return {
                'text': launch,
                'updated_at': None,
                'revision': None,
                'source': 'launch',
            }
        return note_store._empty()

    def read(self, run_id):
        with self.db.transaction() as tx:
            run = tx.fetchone('SELECT *, (SELECT login FROM accounts WHERE '
                              'id=runs.owner_account_id) AS owner_user FROM runs WHERE '
                              'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            if run is None:
                raise FileNotFoundError(run_id)
            note = tx.fetchone('SELECT * FROM run_notes WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            return self._current(run, note)

    def write(self, run_id, text, base_revision):
        if not isinstance(text, str):
            raise ValueError('note text must be a string')
        text = note_store._normalize_newlines(text)
        if '\x00' in text:
            raise ValueError('note text contains NUL')
        data = text.encode('utf-8')
        if len(data) > note_store.MAX_NOTE_BYTES:
            raise note_store.NoteTooLarge(f'note exceeds {note_store.MAX_NOTE_BYTES} bytes')
        now = layout.format_timestamp()
        with self.db.transaction(write=True) as tx:
            tx.lock_row('run_notes', 'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            run = tx.fetchone('SELECT *, (SELECT login FROM accounts WHERE '
                              'id=runs.owner_account_id) AS owner_user FROM runs WHERE '
                              'workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            if run is None:
                raise FileNotFoundError(run_id)
            note = tx.fetchone('SELECT * FROM run_notes WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id))
            current = self._current(run, note)
            if base_revision != current['revision']:
                raise note_store.NoteConflict(current)
            if not text.strip():
                if current.get('source') == 'launch' or run['launch_note']:
                    tx.execute(self.db.dialect.upsert(
                        'run_notes', ('workspace_id', 'run_id', 'body', 'updated_at'), ('workspace_id', 'run_id'),
                        ('body', 'updated_at')), (self.workspace_id, run_id, '', now))
                    return self._current(run, {'body': '', 'updated_at': now})
                if note is not None:
                    tx.execute('DELETE FROM run_notes WHERE workspace_id=? AND run_id=?', (self.workspace_id, run_id))
                return note_store._empty()
            tx.execute(self.db.dialect.upsert(
                'run_notes', ('workspace_id', 'run_id', 'body', 'updated_at'), ('workspace_id', 'run_id'),
                ('body', 'updated_at')), (self.workspace_id, run_id, text, now))
            return self._current(run, {'body': text, 'updated_at': now})

    def upsert(self, run_id, body, updated_at, tx=None):
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.upsert(run_id, body, updated_at, tx)
        tx.execute(self.db.dialect.upsert(
            'run_notes', ('workspace_id', 'run_id', 'body', 'updated_at'), ('workspace_id', 'run_id'),
            ('body', 'updated_at')), (self.workspace_id, run_id, body, updated_at))

    def list_notes(self, project=None, limit_bytes=4 * 1024 * 1024):
        from agenticml.store.reader import _remote_status
        from agenticml.ui.server import _text

        now = datetime.now(timezone.utc).timestamp()
        with self.db.transaction() as tx:
            runs = list(tx.fetchall('SELECT *, (SELECT login FROM accounts WHERE '
                                    'id=runs.owner_account_id) AS owner_user FROM runs WHERE '
                                    'workspace_id=?', (self.workspace_id,)))
            notes = {row['run_id']: row for row in tx.fetchall('SELECT * FROM run_notes WHERE workspace_id=?', (self.workspace_id,))}
        candidates = []
        for run in runs:
            try:
                list_row = json.loads(run['list_row'])
            except (ValueError, TypeError, RecursionError):
                list_row = {}
            if not isinstance(list_row, dict):
                list_row = {}
            row_project = _text(list_row.get('project'))
            if project is not None and row_project != project:
                continue
            note = notes.get(run['run_id'])
            if note is not None:
                body = note['body']
                if not body.strip():
                    continue
                size = len(body.encode('utf-8'))
                try:
                    when = layout.parse_timestamp(note['updated_at']).timestamp()
                except (TypeError, ValueError):
                    when = float('-inf')
                candidates.append((when, size, 'file', run, list_row, row_project, body, note))
            else:
                launch = run['launch_note']
                if not isinstance(launch, str) or not launch.strip():
                    continue
                size = len(launch.encode('utf-8'))
                try:
                    when = layout.parse_timestamp(run['created_at']).timestamp()
                except (TypeError, ValueError):
                    when = float('-inf')
                candidates.append((when, size, 'launch', run, list_row, row_project, launch, None))
        candidates.sort(key=lambda item: item[0], reverse=True)
        kept = []
        used = 0
        truncated = False
        for _when, size, source, run, list_row, row_project, text, note in candidates:
            if used + size > limit_bytes:
                truncated = True
                break
            if source == 'file' and size > note_store.MAX_NOTE_BYTES:
                truncated = True
                continue
            text_size = len(text.encode('utf-8'))
            if used + text_size > limit_bytes:
                truncated = True
                break
            status = _effective_status_from_run(run, list_row, now, _remote_status)
            kept.append({
                'run_id': run['run_id'],
                'name': _text(list_row.get('name')),
                'project': row_project,
                'status': status,
                'created_at': list_row.get('created_at', run['created_at']),
                'text': text,
                'updated_at': note['updated_at'] if note is not None else None,
                'revision': hashlib.sha256(text.encode('utf-8')).hexdigest() if source == 'file' else None,
                'source': source,
            })
            used += text_size
        return {'notes': kept, 'truncated': truncated}


def _effective_status_from_run(run, list_row, now, remote_status):
    if run['status'] is not None:
        return remote_status(
            {'status': run['status'], 'last_sync_at': run['last_sync_at']}, now)
    status = list_row.get('status')
    if not isinstance(status, str):
        status = ''
    return 'stale' if status == 'running' else status
