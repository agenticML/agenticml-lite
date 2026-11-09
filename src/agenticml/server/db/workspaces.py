"""Global accounts and workspace lifecycle; workspace data uses scoped repos."""
import re
import secrets

from agenticml.store import layout
from ..keys import ClientError
from .errors import DatabaseError

_RESERVED = {'admin', 'api', 'static', 'w', 'account', 'login', 'logout', 'setup', 'invite', 'healthz', 'v2'}
_EMAIL_LOGIN = re.compile(r'[^\s@]+@[^\s@]+\.[^\s@]+')
_SLUG_OTHER = re.compile('[^a-z0-9]+')


def validate_slug(slug):
    if not isinstance(slug, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,39}', slug) or slug in _RESERVED:
        raise ClientError('invalid or reserved workspace slug')
    return slug


def slug_from_email_local(email):
    """Turn an email's local part into a workspace slug: ``[a-z0-9-]``, max 32."""
    local = email.split('@', 1)[0].lower() if isinstance(email, str) and '@' in email else ''
    cleaned = _SLUG_OTHER.sub('-', local).strip('-')[:32].rstrip('-')
    return cleaned or 'workspace'


def is_email_login(login):
    return isinstance(login, str) and _EMAIL_LOGIN.fullmatch(login) is not None and len(login) <= 254


def validate_login(login):
    if is_email_login(login):
        return login.lower()
    from ..keys import validate_name
    validate_name(login)
    return login


def emails_equal(left, right):
    """True when both values are emails that match after ``validate_login`` lowercasing.

    Uses ``str.lower``, not ``casefold``, so ``ß`` and ``ss`` stay distinct.
    """
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    return left.lower() == right.lower()


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

    def workspaces(self, login, tx=None):
        """Active workspaces the account belongs to, with membership role."""
        if tx is None:
            with self.db.transaction() as tx:
                return self.workspaces(login, tx)
        account = self.get(login, tx)
        if account is None:
            return []
        return [{'slug': row['slug'], 'name': row['name'], 'role': row['role']}
                for row in tx.fetchall(
                    'SELECT w.slug, w.name, m.role FROM memberships m '
                    'JOIN workspaces w ON w.id=m.workspace_id '
                    'WHERE m.account_id=? AND w.status=? ORDER BY w.slug',
                    (account['id'], 'active'))]

    def by_identity(self, value, *, exact=False):
        """Return the unique account for ``value``, or None.

        ``exact`` compares ``identity`` as stored; otherwise ``identity_folded``.
        More than one match is treated as no match, same as 2.2.
        """
        if not isinstance(value, str) or not value:
            return None
        with self.db.transaction() as tx:
            if exact:
                rows = tx.fetchall('SELECT * FROM accounts WHERE identity=?', (value,))
            else:
                rows = tx.fetchall(
                    'SELECT * FROM accounts WHERE identity_folded=?', (value.casefold(),))
            if len(rows) != 1:
                return None
            return dict(rows[0])

    def by_firebase_uid(self, uid, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.by_firebase_uid(uid, tx)
        row = tx.fetchone('SELECT * FROM accounts WHERE firebase_uid=?', (uid,))
        return dict(row) if row else None

    def list(self):
        with self.db.transaction() as tx:
            return [dict(r) for r in tx.fetchall(
                'SELECT id, login, display_name, identity, server_admin, generation, created_at, created_by, '
                'firebase_uid, email, email_verified FROM accounts ORDER BY created_at, login')]

    def create(self, login, display_name=None, created_by='cli', tx=None):
        login = validate_login(login)
        if display_name is None:
            display_name = login
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.create(login, display_name, created_by, tx)
        tx.advisory_lock('users')
        if self.get(login, tx):
            raise ClientError('account already exists')
        aid = secrets.token_hex(16)
        tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by) '
                   'VALUES (?, ?, ?, ?, ?)', (aid, login, display_name, layout.format_timestamp(), created_by))
        return dict(tx.fetchone('SELECT * FROM accounts WHERE id=?', (aid,)))

    def claimable(self, login, tx):
        account = self.get(login, tx)
        return bool(account and account['password'] is None and account['identity'] is None
                    and account['firebase_uid'] is None and account['created_by'] != 'legacy run owner')

    def link_firebase(self, account_id, uid, email, verified, tx):
        tx.advisory_lock('users')
        row = tx.fetchone('SELECT * FROM accounts WHERE id=?', (account_id,))
        if row is None:
            raise ClientError('unknown account')
        if row['firebase_uid'] is not None:
            raise ClientError('account already linked')
        if tx.fetchone('SELECT id FROM accounts WHERE firebase_uid=?', (uid,)):
            raise ClientError('firebase uid already linked')
        tx.execute('UPDATE accounts SET firebase_uid=?, email=?, email_verified=? WHERE id=?',
                   (uid, email.lower() if isinstance(email, str) else email, 1 if verified else 0, account_id))

    def refresh_firebase_email(self, account_id, email, verified, tx):
        tx.advisory_lock('users')
        if tx.fetchone('SELECT id FROM accounts WHERE id=?', (account_id,)) is None:
            raise ClientError('unknown account')
        tx.execute('UPDATE accounts SET email=?, email_verified=? WHERE id=?',
                   (email.lower() if isinstance(email, str) else email, 1 if verified else 0, account_id))

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

    def create(self, slug, name=None, owner=None, tx=None, *, plan='free', personal=0):
        validate_slug(slug)
        if not isinstance(name or slug, str) or not (name or slug).strip() or len(name or slug) > 256:
            raise ClientError('invalid workspace name')
        if plan not in ('free', 'custom'):
            raise ClientError('plan must be free or custom')
        if tx is None:
            with self.db.transaction(write=True) as tx:
                return self.create(slug, name, owner, tx, plan=plan, personal=personal)
        tx.advisory_lock('users')
        if self.get(slug, tx):
            raise ClientError('workspace slug already exists')
        account = AccountRepo(self.db).get(validate_login(owner), tx) if owner else None
        if account is None:
            raise ClientError('workspace needs an existing owner account')
        wid = secrets.token_hex(16)
        tx.execute(
            'INSERT INTO workspaces (id, slug, name, status, created_by, created_at, plan, personal) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (wid, slug, name or slug, 'active', account['id'], layout.format_timestamp(),
             plan, 1 if personal else 0))
        tx.execute('INSERT INTO workspace_usage (workspace_id) VALUES (?)', (wid,))
        tx.execute('INSERT INTO memberships (workspace_id, account_id, role) VALUES (?, ?, ?)',
                   (wid, account['id'], 'owner'))
        return self.by_id(wid, tx)

    def allocate_slug(self, base, tx):
        candidate, n = base or 'workspace', 2
        while True:
            try:
                validate_slug(candidate)
            except ClientError:
                candidate = f'{base}-{n}'
                n += 1
                continue
            if self.get(candidate, tx) is None:
                return candidate
            candidate = f'{base}-{n}'
            n += 1

    def owned_free_count(self, account_id, tx):
        return tx.fetchone(
            "SELECT COUNT(*) FROM workspaces w JOIN memberships m ON m.workspace_id=w.id "
            "WHERE m.account_id=? AND m.role='owner' AND w.plan='free'",
            (account_id,))[0]

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
            if row['slug'] == 'default' and status == 'suspended':
                raise ClientError('cannot suspend the default workspace; the env key and the self-hosted default depend on it')
            if new_slug != slug and new_slug is not None and self.get(new_slug, tx):
                raise ClientError('workspace slug already exists')
            tx.execute('UPDATE workspaces SET slug=?, name=?, status=? WHERE id=?',
                       (new_slug or slug, name if name is not None else row['name'],
                        status or row['status'], row['id']))
            return self.by_id(row['id'], tx)

    def set_plan(self, slug, plan, custom_limits=None):
        import json
        from ..limits import validate_limits
        if plan not in ('free', 'custom'):
            raise ClientError('plan must be free or custom')
        if plan == 'free' and custom_limits is not None:
            raise ClientError('free plan cannot have custom limits')
        encoded = json.dumps(validate_limits(custom_limits)) if plan == 'custom' else None
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            old = self.get(slug, tx)
            if old is None:
                raise ClientError('unknown workspace')
            tx.execute('UPDATE workspaces SET plan=?, custom_limits=? WHERE id=?',
                       (plan, encoded, old['id']))
            return old, self.by_id(old['id'], tx)

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
        tx.execute('INSERT INTO memberships (workspace_id, account_id, role) VALUES (?, ?, ?)', (self.workspace_id, account_id, role))

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

    def create(self, login, role, invited_by, *, plans=None):
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
            if account and MembershipRepo(self.db, self.workspace_id).get(account['id'], tx):
                raise ClientError('membership already exists')
            if plans is not None:
                pending = tx.fetchone(
                    'SELECT 1 FROM invites WHERE workspace_id=? AND login=? AND expires_at>?',
                    (self.workspace_id, login, layout.format_timestamp(plans.clock())))
                if not (account is None and pending is not None):
                    plans.check_people(self.db, self.workspace_id, tx)
            if account:
                MembershipRepo(self.db, self.workspace_id).add(account['id'], role, tx)
                return None
            tx.execute(self.db.dialect.upsert('invites',
                       ('workspace_id', 'login', 'role', 'sha256', 'expires_at', 'invited_by'),
                       ('workspace_id', 'login'), ('role', 'sha256', 'expires_at', 'invited_by')),
                       (self.workspace_id, login, role, digest,
                        layout.format_timestamp(datetime.now(timezone.utc) + timedelta(hours=72)), invited_by))
        return token

    @staticmethod
    def find_pending(db, token):
        """Return a live workspace invite for ``token``, or None."""
        from datetime import datetime, timezone
        import hashlib
        import hmac
        if not isinstance(token, str) or not token:
            return None
        try:
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        except UnicodeEncodeError:
            return None
        with db.transaction() as tx:
            row = tx.fetchone('SELECT * FROM invites WHERE sha256=?', (digest,))
            if row is None or not hmac.compare_digest(row['sha256'], digest):
                return None
            try:
                if layout.parse_timestamp(row['expires_at']) <= datetime.now(timezone.utc):
                    return None
            except (TypeError, ValueError):
                return None
            workspace = WorkspaceRepo(db).by_id(row['workspace_id'], tx)
            if workspace is None or workspace['status'] != 'active':
                return None
            return dict(row)

    def redeem(self, token, password=None):
        from datetime import datetime, timezone
        import hashlib
        import hmac
        import json
        from ..users import _password_record, InviteInvalid
        record = None if password is None else _password_record(password)
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
                if record is None:
                    raise InviteInvalid('invalid invite')
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

    def revoke(self, login):
        login = validate_login(login)
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            row = tx.fetchone('SELECT * FROM invites WHERE workspace_id=? AND login=?',
                              (self.workspace_id, login))
            if row is None:
                return None
            tx.execute('DELETE FROM invites WHERE workspace_id=? AND login=?',
                       (self.workspace_id, login))
            return dict(row)

    @staticmethod
    def redeem_firebase(db, token, uid, email, verified, display_name=None):
        """Accept a workspace invite for a Firebase identity in one transaction.

        The uid's account (already linked, claimable by email, or newly created
        credential-less with ``login = email``) receives the membership and the
        invite is deleted. Email matching is re-checked under the lock so a
        mismatch never redeems.
        """
        from datetime import datetime, timezone
        import hashlib
        import hmac

        from ..users import InviteAccountExists, InviteEmailMismatch, InviteInvalid

        if not isinstance(token, str) or not token:
            raise InviteInvalid('invalid invite')
        try:
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        except UnicodeEncodeError as exc:
            raise InviteInvalid('invalid invite') from exc
        accounts = AccountRepo(db)
        with db.transaction(write=True) as tx:
            tx.advisory_lock('users')
            invite = tx.fetchone('SELECT * FROM invites WHERE sha256=?', (digest,))
            if invite is None or not hmac.compare_digest(invite['sha256'], digest):
                raise InviteInvalid('invalid invite')
            try:
                if layout.parse_timestamp(invite['expires_at']) <= datetime.now(timezone.utc):
                    raise InviteInvalid('invalid invite')
            except (TypeError, ValueError) as exc:
                raise InviteInvalid('invalid invite') from exc
            workspace = WorkspaceRepo(db).by_id(invite['workspace_id'], tx)
            if workspace is None or workspace['status'] != 'active':
                raise InviteInvalid('invalid invite')
            login = invite['login']
            if (not verified or not is_email_login(login)
                    or not emails_equal(email, login)):
                raise InviteEmailMismatch()
            account = accounts.by_firebase_uid(uid, tx)
            if account is None and accounts.claimable(email, tx):
                account = accounts.get(email, tx)
            if account is None:
                existing = accounts.get(email, tx)
                if existing is not None and (
                        existing['password'] is not None or existing['identity'] is not None):
                    raise InviteAccountExists()
                try:
                    account = accounts.create(
                        email, display_name or email, invite['invited_by'], tx)
                except ClientError:
                    account = accounts.by_firebase_uid(uid, tx)
                    if account is None and accounts.claimable(email, tx):
                        account = accounts.get(email, tx)
                    if account is None:
                        existing = accounts.get(email, tx)
                        if existing is not None and (
                                existing['password'] is not None or existing['identity'] is not None):
                            raise InviteAccountExists()
                        raise InviteInvalid('invalid invite')
            if account.get('firebase_uid') is None:
                try:
                    accounts.link_firebase(account['id'], uid, email, verified, tx)
                except (ClientError, DatabaseError):
                    linked = accounts.by_firebase_uid(uid, tx)
                    if linked is None:
                        raise
                    account = linked
            members = MembershipRepo(db, invite['workspace_id'])
            if members.get(account['id'], tx) is None:
                members.add(account['id'], invite['role'], tx)
            tx.execute('DELETE FROM invites WHERE workspace_id=? AND login=?',
                       (invite['workspace_id'], invite['login']))
            return dict(tx.fetchone('SELECT * FROM accounts WHERE id=?', (account['id'],)))
