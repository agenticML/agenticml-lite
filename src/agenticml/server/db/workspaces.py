"""Global accounts and workspace lifecycle; workspace data uses scoped repos."""
import re
import secrets

from agenticml.store import layout
from ..keys import ClientError
from .errors import DatabaseError

_RESERVED = {'admin', 'api', 'static', 'w', 'account', 'login', 'logout', 'setup', 'invite', 'healthz', 'v2'}


def validate_slug(slug):
    if not isinstance(slug, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,39}', slug) or slug in _RESERVED:
        raise ClientError('invalid or reserved workspace slug')
    return slug


def validate_login(login):
    if isinstance(login, str) and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', login) and len(login) <= 254:
        return login.lower()
    from ..keys import validate_name
    validate_name(login)
    return login


def require_workspace_id(workspace_id):
    if not isinstance(workspace_id, str) or not re.fullmatch('[0-9a-f]{32}', workspace_id):
        raise ValueError('workspace id is required')
    return workspace_id


class AccountRepo:
    def __init__(self, db):
        self.db = db

    def get(self, login, tx=None):
        login = login.lower() if isinstance(login, str) and "@" in login else login
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(login, tx)
        row = tx.fetchone('SELECT * FROM accounts WHERE login=?', (login,))
        return dict(row) if row else None

    def workspace_slugs(self, login, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.workspace_slugs(login, tx)
        account = self.get(login, tx)
        if account is None:
            return []
        return [r[0] for r in tx.fetchall(
            'SELECT w.slug FROM memberships m JOIN workspaces w ON w.id=m.workspace_id '
            'WHERE m.account_id=? ORDER BY w.slug', (account['id'],))]

    def list(self):
        with self.db.transaction() as tx:
            return [dict(r) for r in tx.fetchall(
                'SELECT id, login, display_name, identity, server_admin, generation, created_at, created_by '
                'FROM accounts ORDER BY created_at, login')]

    def set_server_admin(self, login, enabled):
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            account = self.get(login, tx)
            if account is None:
                raise ClientError('unknown account')
            tx.execute('UPDATE accounts SET server_admin=? WHERE id=?', (int(enabled), account['id']))

    def discard_unreferenced(self, account_id, login, tx):
        if not tx.fetchone('SELECT 1 FROM memberships WHERE account_id=?', (account_id,)) and not tx.fetchone(
                'SELECT 1 FROM runs WHERE owner_account_id=?', (account_id,)):
            tx.execute('UPDATE workspaces SET created_by=NULL WHERE created_by=?', (account_id,))
            tx.execute('DELETE FROM sessions WHERE account_id=?', (account_id,))
            tx.execute('DELETE FROM accounts WHERE id=? AND login=?', (account_id, login))

    def ensure_attribution(self, login, tx):
        """Retain removed 2.x owners without giving them membership or credentials."""
        if login is None:
            return None
        tx.advisory_lock('users')
        account = self.get(login, tx)
        if account is None:
            aid = secrets.token_hex(16)
            tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by) '
                       'VALUES (?, ?, ?, ?, ?)', (aid, login, login, layout.format_timestamp(), 'legacy run owner'))
            return aid
        return account['id']


class WorkspaceRepo:
    def __init__(self, db):
        self.db = db

    def get(self, slug, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(slug, tx)
        row = tx.fetchone('SELECT * FROM workspaces WHERE slug=?', (slug,))
        return dict(row) if row else None

    def by_id(self, wid, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.by_id(wid, tx)
        row = tx.fetchone('SELECT * FROM workspaces WHERE id=?', (wid,))
        return dict(row) if row else None

    def require(self, slug):
        row = self.get(slug)
        if row is None:
            raise ClientError('unknown workspace')
        return row

    def list(self):
        with self.db.transaction() as tx:
            return [dict(r) for r in tx.fetchall(
                'SELECT w.*, (SELECT COUNT(*) FROM memberships m WHERE m.workspace_id=w.id) AS members '
                'FROM workspaces w ORDER BY w.created_at, w.slug')]

    def create(self, slug, name=None, owner=None):
        validate_slug(slug)
        if not isinstance(name or slug, str) or not (name or slug).strip() or len(name or slug) > 256:
            raise ClientError('invalid workspace name')
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            if self.get(slug, tx):
                raise ClientError('workspace slug already exists')
            account = AccountRepo(self.db).get(validate_login(owner), tx) if owner else None
            if account is None:
                raise ClientError('workspace needs an existing owner account')
            wid = secrets.token_hex(16)
            tx.execute('INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?)',
                       (wid, slug, name or slug, 'active', account['id'], layout.format_timestamp()))
            tx.execute('INSERT INTO memberships VALUES (?, ?, ?)', (wid, account['id'], 'owner'))
            return self.by_id(wid, tx)

    def update(self, slug, *, name=None, new_slug=None, status=None):
        if new_slug is not None:
            validate_slug(new_slug)
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 256):
            raise ClientError('invalid workspace name')
        if status is not None and status not in ('active', 'suspended'):
            raise ClientError('invalid workspace status')
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            row = self.get(slug, tx)
            if row is None:
                raise ClientError('unknown workspace')
            if row['slug'] == 'default' and new_slug not in (None, 'default'):
                raise ClientError('cannot change the default workspace slug; the env key and the self-hosted default depend on it')
            if new_slug != slug and new_slug is not None and self.get(new_slug, tx):
                raise ClientError('workspace slug already exists')
            tx.execute('UPDATE workspaces SET slug=?, name=?, status=? WHERE id=?',
                       (new_slug or slug, name if name is not None else row['name'],
                        status or row['status'], row['id']))
            return self.by_id(row['id'], tx)

    def delete(self, slug):
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            row = self.get(slug, tx)
            if row is None:
                raise ClientError('unknown workspace')
            if row['slug'] == 'default':
                raise ClientError('cannot delete the default workspace; the env key and the self-hosted default depend on it')
            tx.execute('DELETE FROM workspaces WHERE id=?', (row['id'],))
            return row

    def set_owner(self, slug, login):
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            row = self.get(slug, tx)
            account = AccountRepo(self.db).get(validate_login(login), tx)
            if row is None or account is None:
                raise ClientError('unknown workspace or account')
            tx.execute("UPDATE memberships SET role='admin' WHERE workspace_id=? AND role='owner'", (row['id'],))
            tx.execute(self.db.dialect.upsert('memberships', ('workspace_id', 'account_id', 'role'),
                                             ('workspace_id', 'account_id'), ('role',)),
                       (row['id'], account['id'], 'owner'))


class MembershipRepo:
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)

    def get(self, account_id, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.get(account_id, tx)
        row = tx.fetchone('SELECT m.*, a.login FROM memberships m JOIN accounts a ON a.id=m.account_id '
                          'WHERE m.workspace_id=? AND m.account_id=?', (self.workspace_id, account_id))
        return dict(row) if row else None

    def list(self, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.list(tx)
        return [dict(row) for row in tx.fetchall(
            'SELECT m.*, a.login FROM memberships m JOIN accounts a ON a.id=m.account_id '
            'WHERE m.workspace_id=? ORDER BY a.created_at, a.login', (self.workspace_id,))]

    def add(self, account_id, role, tx=None):
        if role not in ('admin', 'member', 'viewer'):
            raise ClientError('invalid role')
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.add(account_id, role, tx)
        tx.advisory_lock('users')
        if self.get(account_id, tx):
            raise ClientError('membership already exists')
        tx.execute('INSERT INTO memberships VALUES (?, ?, ?)', (self.workspace_id, account_id, role))

    def remove(self, account_id):
        from .repos import UserRepo
        row = self.get(account_id)
        if row is None:
            raise ClientError('unknown member')
        return UserRepo(self.db, self.workspace_id).remove(row['login'])

    def set_role(self, account_id, role):
        from .repos import UserRepo
        row = self.get(account_id)
        if row is None:
            raise ClientError('unknown member')
        return UserRepo(self.db, self.workspace_id).set_role(row['login'], role)


class InviteRepo:
    """Workspace invites. Existing accounts receive membership without a token."""
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)

    def list(self):
        with self.db.transaction() as tx:
            return [dict(r) for r in tx.fetchall(
                'SELECT login, role, expires_at, invited_by FROM invites WHERE workspace_id=? ORDER BY login',
                (self.workspace_id,))]

    def create(self, login, role, invited_by):
        from datetime import datetime, timedelta, timezone
        import hashlib
        login = validate_login(login)
        if role not in ('admin', 'member', 'viewer'):
            raise ClientError('invalid role')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            account = AccountRepo(self.db).get(login, tx)
            if account:
                MembershipRepo(self.db, self.workspace_id).add(account['id'], role, tx)
                return None
            tx.execute(self.db.dialect.upsert('invites',
                       ('workspace_id', 'login', 'role', 'sha256', 'expires_at', 'invited_by'),
                       ('workspace_id', 'login'), ('role', 'sha256', 'expires_at', 'invited_by')),
                       (self.workspace_id, login, role, digest,
                        layout.format_timestamp(datetime.now(timezone.utc) + timedelta(hours=72)), invited_by))
        return token

    def redeem(self, token, password):
        from datetime import datetime, timezone
        import hashlib
        import hmac
        import json
        from ..users import _password_record, InviteInvalid
        record = _password_record(password)
        try:
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        except (AttributeError, UnicodeEncodeError) as exc:
            raise InviteInvalid('invalid invite') from exc
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            workspace = WorkspaceRepo(self.db).by_id(self.workspace_id, tx)
            invite = tx.fetchone('SELECT * FROM invites WHERE workspace_id=? AND sha256=?',
                                (self.workspace_id, digest))
            if (not workspace or workspace['status'] != 'active' or not invite or
                    not hmac.compare_digest(invite['sha256'], digest) or
                    layout.parse_timestamp(invite['expires_at']) <= datetime.now(timezone.utc)):
                raise InviteInvalid('invalid invite')
            account = AccountRepo(self.db).get(invite['login'], tx)
            existing = account is not None
            if account is None:
                aid = secrets.token_hex(16)
                tx.execute('INSERT INTO accounts (id, login, display_name, password, created_at, created_by) '
                           'VALUES (?, ?, ?, ?, ?, ?)', (aid, invite['login'], invite['login'], json.dumps(record),
                                                        layout.format_timestamp(), invite['invited_by']))
            else:
                aid = account['id']
            MembershipRepo(self.db, self.workspace_id).add(aid, invite['role'], tx)
            tx.execute('DELETE FROM invites WHERE workspace_id=? AND login=?', (self.workspace_id, invite['login']))
        if existing:
            from ..users import InviteExistingAccount
            raise InviteExistingAccount(aid)
        return aid
