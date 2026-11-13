"""Firebase ID token verification and AccountRepo linking (package 18b.1)."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import base64
import json
import time

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agenticml import cli
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.webauth import firebase_login_csp
from agenticml.ui.server import _CSP
from agenticml.server.db import connect, for_server_dir
from agenticml.server.db.errors import DatabaseError
from agenticml.server.db.repos import AuditRepo
from agenticml.server.db.workspaces import (
    AccountRepo, InviteRepo, MembershipRepo, emails_equal)
from agenticml.server.firebase import (
    FIREBASE_EXTRA, FIREBASE_IDENTITY, LEEWAY, MAX_CERTS_BODY, CertificateSource, Claims,
    FirebaseConfig, FirebaseTokenInvalid, FirebaseUnavailable, _capped_body, _http_fetch,
    firebase_available, parse_firebase_config, verify_id_token)
from agenticml.server.keys import ClientError
from agenticml.server.runs import reconcile
from agenticml.store import RunWriter
from test_server_signin import PASSWORD, running
from test_server_web import authed, call, csrf, login

PROJECT = 'demo-project'
NOW = 1_700_000_000
UID = 'firebase-uid-123'


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(',', ':')).encode()).rstrip(b'=').decode()


def _cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return SimpleNamespace(key=key, pem=pem, kid='test-kid')


CERT = _cert()


def _payload(**overrides):
    body = {
        'iss': f'https://securetoken.google.com/{PROJECT}',
        'aud': PROJECT,
        'exp': NOW + 3600,
        'iat': NOW,
        'auth_time': NOW,
        'sub': UID,
        'email': 'User@Example.COM',
        'email_verified': True,
        'name': 'Ada',
        'firebase': {'sign_in_provider': 'google.com'},
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not _OMIT}


_OMIT = object()


def _token(key=None, kid=None, now=NOW, **overrides):
    overrides.setdefault('exp', now + 3600)
    overrides.setdefault('iat', now)
    overrides.setdefault('auth_time', now)
    return jwt.encode(_payload(**overrides), key or CERT.key, algorithm='RS256',
                      headers={'kid': CERT.kid if kid is None else kid})


def _unsigned(alg='none', kid='test-kid', **overrides):
    return f"{_b64({'alg': alg, 'kid': kid})}.{_b64(_payload(**overrides))}."


class FakeCerts:
    def __init__(self, certs=None, max_age=3600, status=200, payload=None):
        self.status_code = status
        self.headers = {'Cache-Control': f'public, max-age={max_age}'}
        self._payload = {CERT.kid: CERT.pem} if certs is None and payload is None else (
            payload if payload is not None else certs)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _source(fetch=None, **kwargs):
    return CertificateSource(fetch=fetch or (lambda: FakeCerts(**kwargs)))


def _verify(token, *, now=NOW, keys=None, project=PROJECT):
    return verify_id_token(token, project, now=now, keys=keys or _source())


def _db(tmp_path):
    prepare_data_dir(tmp_path)
    return connect('sqlite:///' + str(tmp_path / 'server' / 'agenticml.db'))


def test_firebase_available_and_extra_message():
    assert firebase_available() is True
    assert FIREBASE_EXTRA == "Firebase sign-in needs the [firebase] extra: pip install 'agenticml-lite[firebase]'"


def test_valid_token_returns_claims():
    claims = _verify(_token())
    assert claims == Claims(uid=UID, email='user@example.com', email_verified=True,
                            name='Ada', provider='google.com')


def test_email_lowercased_and_email_verified_defaults_false():
    claims = _verify(_token(email='Ada@Example.COM', email_verified=_OMIT, name=_OMIT,
                            firebase=_OMIT))
    assert claims.email == 'ada@example.com'
    assert claims.email_verified is False
    assert claims.name is None and claims.provider is None


def test_wrong_signature():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(FirebaseTokenInvalid, match='signature'):
        _verify(_token(key=other))


@pytest.mark.parametrize('field,value,reason', [
    ('iss', 'https://securetoken.google.com/other', 'iss'),
    ('aud', 'other-project', 'aud'),
    ('exp', NOW - 120, 'expired'),
    ('iat', NOW + 61, 'iat'),
    ('auth_time', NOW - 601, 'auth_time'),
    ('auth_time', NOW + 61, 'auth_time'),
])
def test_claim_checks(field, value, reason):
    with pytest.raises(FirebaseTokenInvalid, match=reason):
        _verify(_token(**{field: value}))


def test_exp_at_leeway_boundary():
    with pytest.raises(FirebaseTokenInvalid, match='expired'):
        _verify(_token(exp=NOW - LEEWAY))
    _verify(_token(exp=NOW - LEEWAY + 1))


def test_email_verified_must_be_exactly_true():
    assert _verify(_token(email_verified=True)).email_verified is True
    assert _verify(_token(email_verified=1)).email_verified is False
    assert _verify(_token(email_verified='true')).email_verified is False


def test_huge_int_time_claim_is_invalid(monkeypatch):
    real = jwt.decode

    def decode(*args, **kwargs):
        payload = real(*args, **kwargs)
        payload['exp'] = 10 ** 400
        return payload

    monkeypatch.setattr(jwt, 'decode', decode)
    with pytest.raises(FirebaseTokenInvalid, match='exp'):
        _verify(_token())


def test_pyjwt_errors_map_to_reasons(monkeypatch):
    def boom_sig(*args, **kwargs):
        raise jwt.exceptions.InvalidSignatureError('bad')

    monkeypatch.setattr(jwt, 'decode', boom_sig)
    with pytest.raises(FirebaseTokenInvalid, match='signature'):
        _verify(_token())

    def boom_decode(*args, **kwargs):
        raise jwt.exceptions.DecodeError('bad')

    monkeypatch.setattr(jwt, 'decode', boom_decode)
    with pytest.raises(FirebaseTokenInvalid, match='decode'):
        _verify(_token())

    def boom_claim(*args, **kwargs):
        raise jwt.exceptions.MissingRequiredClaimError('sub')

    monkeypatch.setattr(jwt, 'decode', boom_claim)
    with pytest.raises(FirebaseTokenInvalid, match='claim'):
        _verify(_token())


def test_exp_iat_auth_time_reject_string_nan_bool(monkeypatch):
    with pytest.raises(FirebaseTokenInvalid, match='exp'):
        _verify(_token(exp='9999999999'))
    with pytest.raises(FirebaseTokenInvalid, match='iat'):
        _verify(_token(iat=True))
    real = jwt.decode

    def decode(*args, **kwargs):
        payload = real(*args, **kwargs)
        payload['auth_time'] = float('nan')
        return payload

    monkeypatch.setattr(jwt, 'decode', decode)
    with pytest.raises(FirebaseTokenInvalid, match='auth_time'):
        _verify(_token())


def test_alg_none_and_hs256_refused_before_key_lookup():
    calls = []

    def fetch():
        calls.append(1)
        return FakeCerts()

    source = CertificateSource(fetch=fetch)
    hs_key = 'secret-key-32-bytes-minimum!!!!!!'
    for token in (_unsigned(), jwt.encode(_payload(), hs_key, algorithm='HS256',
                                          headers={'kid': CERT.kid})):
        with pytest.raises(FirebaseTokenInvalid, match='alg'):
            verify_id_token(token, PROJECT, now=NOW, keys=source)
    assert calls == []


@pytest.mark.parametrize('sub', [_OMIT, '', 'x' * 129])
def test_sub_missing_empty_or_too_long(sub):
    with pytest.raises(FirebaseTokenInvalid, match='sub'):
        _verify(_token(sub=sub))


def test_unknown_kid_refetches_once_then_refused():
    calls = []

    def fetch():
        calls.append(1)
        return FakeCerts()

    source = CertificateSource(fetch=fetch)
    _verify(_token(), keys=source)
    assert len(calls) == 1
    with pytest.raises(FirebaseTokenInvalid, match='unknown kid'):
        verify_id_token(_token(kid='rotated'), PROJECT, now=NOW + 60, keys=source)
    assert len(calls) == 2


def test_unknown_kid_refetch_rate_limited():
    calls = []

    def fetch():
        calls.append(1)
        return FakeCerts()

    source = CertificateSource(fetch=fetch)
    _verify(_token(), keys=source)
    with pytest.raises(FirebaseTokenInvalid, match='unknown kid'):
        verify_id_token(_token(kid='nope'), PROJECT, now=NOW + 60, keys=source)
    assert len(calls) == 2
    with pytest.raises(FirebaseTokenInvalid, match='unknown kid'):
        verify_id_token(_token(kid='also-nope'), PROJECT, now=NOW + 90, keys=source)
    assert len(calls) == 2


def test_cert_fetch_failure_is_unavailable():
    def boom():
        raise OSError('offline')

    with pytest.raises(FirebaseUnavailable):
        verify_id_token(_token(), PROJECT, now=NOW, keys=CertificateSource(fetch=boom))
    with pytest.raises(FirebaseUnavailable):
        verify_id_token(_token(), PROJECT, now=NOW, keys=_source(status=500))
    with pytest.raises(FirebaseUnavailable):
        verify_id_token(_token(), PROJECT, now=NOW, keys=_source(payload=['not', 'a', 'map']))


def test_cert_fetch_negative_cache_and_single_flight():
    from concurrent.futures import ThreadPoolExecutor
    import threading

    calls = []
    started = threading.Event()
    release = threading.Event()

    def fetch():
        calls.append(1)
        started.set()
        assert release.wait(timeout=2)
        raise OSError('offline')

    source = CertificateSource(fetch=fetch)

    def once():
        with pytest.raises(FirebaseUnavailable):
            verify_id_token(_token(), PROJECT, now=NOW, keys=source)

    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(once) for _ in range(8)]
        assert started.wait(timeout=2)
        release.set()
        for fut in futs:
            fut.result(timeout=2)
    assert len(calls) == 1
    for offset in (1, 10, 29):
        with pytest.raises(FirebaseUnavailable):
            verify_id_token(_token(), PROJECT, now=NOW + offset, keys=source)
    assert len(calls) == 1
    with pytest.raises(FirebaseUnavailable):
        verify_id_token(_token(), PROJECT, now=NOW + 30, keys=source)
    assert len(calls) == 2

    calls.clear()
    mode = ['ok']

    def mixed():
        calls.append(1)
        if mode[0] == 'fail':
            raise OSError('offline')
        return FakeCerts()

    cached = CertificateSource(fetch=mixed)
    _verify(_token(), keys=cached, now=NOW)
    mode[0] = 'fail'
    _verify(_token(), keys=cached, now=NOW + 1)
    assert len(calls) == 1
    with pytest.raises(FirebaseTokenInvalid, match='unknown kid'):
        verify_id_token(_token(kid='nope'), PROJECT, now=NOW + 60, keys=cached)
    assert len(calls) == 2
    with pytest.raises(FirebaseTokenInvalid, match='unknown kid'):
        verify_id_token(_token(kid='nope'), PROJECT, now=NOW + 90, keys=cached)
    assert len(calls) == 2


def test_capped_cert_body():
    class OversizeHeader:
        headers = {'Content-Length': str(MAX_CERTS_BODY + 1)}

        def iter_content(self, size):
            raise AssertionError('must not read an oversized Content-Length')

    with pytest.raises(FirebaseUnavailable):
        _capped_body(OversizeHeader())

    class OversizeStream:
        headers = {}

        def iter_content(self, size):
            yield b'x' * (MAX_CERTS_BODY + 1)

    with pytest.raises(FirebaseUnavailable):
        _capped_body(OversizeStream())

    class Ok:
        headers = {'Content-Length': '4'}

        def iter_content(self, size):
            yield b'{}'

    assert _capped_body(Ok()) == b'{}'


def test_capped_body_total_deadline():
    times = iter([0.0, 11.0])

    class Slow:
        headers = {}

        def iter_content(self, size):
            yield b'{'
            yield b'}'

    with pytest.raises(FirebaseUnavailable):
        _capped_body(Slow(), deadline=10.0, clock=lambda: next(times))

    class Tiny:
        headers = {}

        def iter_content(self, size):
            yield b'ok'

    assert _capped_body(Tiny(), deadline=time.monotonic() + 10) == b'ok'


def test_single_flight_waiter_times_out(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    import agenticml.server.firebase as firebase_mod

    monkeypatch.setattr(firebase_mod, 'WAITER_TIMEOUT', 0.05)
    started = threading.Event()
    hold = threading.Event()

    def fetch():
        started.set()
        assert hold.wait(timeout=2)
        return FakeCerts()

    source = CertificateSource(fetch=fetch)

    def leader():
        return _verify(_token(), keys=source)

    with ThreadPoolExecutor(2) as pool:
        lead = pool.submit(leader)
        assert started.wait(timeout=2)
        with pytest.raises(FirebaseUnavailable):
            _verify(_token(), keys=source)
        hold.set()
        assert lead.result(timeout=2).uid == UID


def test_http_fetch_streams_and_caps(monkeypatch):
    import requests

    class Oversize:
        status_code = 200
        headers = {'Content-Length': str(MAX_CERTS_BODY + 1)}
        closed = False

        def iter_content(self, size):
            yield b'x' * (MAX_CERTS_BODY + 1)

        def close(self):
            self.closed = True

    oversize = Oversize()
    seen = {}

    def fake_get(*args, **kwargs):
        seen['kwargs'] = kwargs
        return oversize

    monkeypatch.setattr(requests, 'get', fake_get)
    with pytest.raises(FirebaseUnavailable):
        _http_fetch()
    assert seen['kwargs'].get('stream') is True
    assert oversize.closed is True


def test_max_age_clamped_to_five_minutes_and_one_day():
    calls = []

    def fetch():
        calls.append(1)
        return FakeCerts(max_age=10)

    source = CertificateSource(fetch=fetch)
    _verify(_token(), keys=source, now=NOW)
    _verify(_token(), keys=source, now=NOW + 299)
    assert len(calls) == 1
    _verify(_token(), keys=source, now=NOW + 300)
    assert len(calls) == 2

    calls.clear()

    def long_lived():
        calls.append(1)
        return FakeCerts(max_age=10 ** 7)

    source = CertificateSource(fetch=long_lived)
    _verify(_token(), keys=source, now=NOW)
    still = NOW + 24 * 60 * 60 - 1
    _verify(_token(now=still), keys=source, now=still)
    assert len(calls) == 1
    expired = NOW + 24 * 60 * 60
    _verify(_token(now=expired), keys=source, now=expired)
    assert len(calls) == 2


def test_account_claimable_rules(tmp_path):
    db = _db(tmp_path)
    repo = AccountRepo(db)
    with db.transaction() as tx:
        cols = {row[1] for row in tx.fetchall('PRAGMA table_info(accounts)')}
    assert {'firebase_uid', 'email', 'email_verified'} <= cols
    bare = repo.create('bare@example.com')
    hashed = repo.create('hashed@example.com')
    ident = repo.create('ident@example.com')
    linked = repo.create('linked@example.com')
    ghost = repo.create('ghost', created_by='legacy run owner')
    with db.transaction(write=True) as tx:
        tx.execute('UPDATE accounts SET password=? WHERE id=?', ('{"n":1}', hashed['id']))
        tx.execute('UPDATE accounts SET identity=?, identity_folded=? WHERE id=?',
                   ('proxy:1', 'proxy:1', ident['id']))
        repo.link_firebase(linked['id'], 'uid-1', 'Linked@Example.com', True, tx)
        assert repo.claimable('bare@example.com', tx) is True
        assert repo.claimable('hashed@example.com', tx) is False
        assert repo.claimable('ident@example.com', tx) is False
        assert repo.claimable('linked@example.com', tx) is False
        assert repo.claimable('ghost', tx) is False
        assert repo.claimable('missing@example.com', tx) is False
        assert repo.by_firebase_uid('uid-1', tx)['id'] == linked['id']
        assert repo.by_firebase_uid('uid-1', tx)['email'] == 'linked@example.com'
        assert repo.by_firebase_uid('uid-1', tx)['email_verified'] == 1
        repo.refresh_firebase_email(linked['id'], 'New@Example.com', False, tx)
        assert repo.get('linked@example.com', tx)['email'] == 'new@example.com'
        assert repo.get('linked@example.com', tx)['email_verified'] == 0
        with pytest.raises(ClientError, match='already exists'):
            repo.create('bare@example.com', tx=tx)
    assert bare['login'] == 'bare@example.com'
    db.close()


def test_link_firebase_twice_refused_and_uid_unique(tmp_path):
    db = _db(tmp_path)
    repo = AccountRepo(db)
    first = repo.create('one@example.com')
    second = repo.create('two@example.com')
    with db.transaction(write=True) as tx:
        repo.link_firebase(first['id'], 'uid-shared', 'one@example.com', True, tx)
        with pytest.raises(ClientError, match='already linked'):
            repo.link_firebase(first['id'], 'uid-other', 'one@example.com', True, tx)
        with pytest.raises(ClientError, match='already linked'):
            repo.link_firebase(second['id'], 'uid-shared', 'two@example.com', True, tx)
        assert repo.by_firebase_uid('uid-other', tx) is None
        assert repo.claimable('one@example.com', tx) is False
    with pytest.raises(DatabaseError):
        with db.transaction(write=True) as tx:
            tx.execute('UPDATE accounts SET firebase_uid=? WHERE id=?', ('uid-shared', second['id']))
    db.close()


FIREBASE_CFG = FirebaseConfig(
    project=PROJECT, api_key='test-api-key-1', auth_domain=f'{PROJECT}.firebaseapp.com')


def _live_token(**overrides):
    now = int(time.time())
    return _token(now=now, **overrides)


def _post_firebase(server, token, invite=None, extra=None):
    body = {'id_token': token}
    if invite is not None:
        body['invite'] = invite
    headers = csrf(server)
    if extra:
        headers.update(extra)
    return call(server, '/auth/firebase', 'POST', json.dumps(body).encode(), headers)


@contextmanager
def firebase_running(data, **kwargs):
    source = kwargs.pop('cert_source', None) or _source()
    with running(data, firebase=FIREBASE_CFG, cert_source=source, **kwargs) as httpd:
        yield httpd


def _member_account(httpd, login, uid=None):
    repo = AccountRepo(httpd.db)
    account = repo.create(login)
    MembershipRepo(httpd.db, httpd.default_workspace_id).add(account['id'], 'member')
    if uid is not None:
        with httpd.db.transaction(write=True) as tx:
            repo.link_firebase(account['id'], uid, login, True, tx)
        account = repo.get(login)
    return account


def _write_run(httpd):
    writer = RunWriter.create(httpd.root, run_id='run-1', meta={'project': 'p', 'name': 'n'})
    writer.finish('finished')
    writer.close()
    reconcile(httpd.root, httpd.run_index)


def test_parse_firebase_config_and_defaults():
    cfg = parse_firebase_config('demo-project', 'AIzaSyTestKey', None)
    assert cfg == FirebaseConfig(
        project='demo-project', api_key='AIzaSyTestKey',
        auth_domain='demo-project.firebaseapp.com')
    assert parse_firebase_config(None, None, None) is None
    with pytest.raises(ValueError, match='required'):
        parse_firebase_config('demo-project', None, None)
    with pytest.raises(ValueError, match=r'\[a-z0-9-\]'):
        parse_firebase_config('NOPE', 'AIzaSyTestKey', None)
    with pytest.raises(ValueError, match='hostname'):
        parse_firebase_config('demo-project', 'AIzaSyTestKey', 'https://nope')
    with pytest.raises(ValueError, match='require --firebase-project'):
        parse_firebase_config(None, 'AIzaSyTestKey', None)


def test_linked_uid_session_and_csrf_write(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    caplog.set_level('DEBUG')
    token = _live_token(sub='uid-linked', email='ada@example.com')
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'ada@example.com', uid='uid-linked')
        _write_run(httpd)
        status, hdrs, body = _post_firebase(httpd, token)
        assert status == 200 and json.loads(body) == {'ok': True, 'user': 'ada@example.com'}
        cookie = hdrs['Set-Cookie']
        note = json.dumps({'text': 'hello', 'revision': None}).encode()
        status, _, body, _ = authed(
            httpd, '/api/w/default/runs/run-1/notes', 'PUT', note, cookie=cookie)
        assert status == 200
        events = AuditRepo(httpd.db, None).tail(20)
        assert any(row['action'] == 'login.ok' and row['via'] == 'firebase'
                   and row['detail'] == {'method': 'firebase', 'provider': 'google.com'}
                   for row in events)
    assert token not in caplog.text
    assert FIREBASE_CFG.api_key not in caplog.text


def test_claimable_account_linked_by_verified_email(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        account = _member_account(httpd, 'claim@example.com')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-claim', email='Claim@Example.com'))
        assert status == 200 and json.loads(body)['user'] == 'claim@example.com'
        linked = AccountRepo(httpd.db).by_firebase_uid('uid-claim')
        assert linked['id'] == account['id']
        assert hdrs.get('Set-Cookie')


def test_password_account_not_claimed(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        httpd.users.add('hashed@example.com', role='member')
        httpd.users.set_password('hashed@example.com', PASSWORD)
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-hashed', email='hashed@example.com'))
        assert status == 403 and json.loads(body) == {
            'error': 'no access', 'reason': 'no_account'}
        assert AccountRepo(httpd.db).by_firebase_uid('uid-hashed') is None
        assert AccountRepo(httpd.db).get('hashed@example.com')['firebase_uid'] is None


def test_unverified_email_not_claimed(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'bare@example.com')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-unverified', email='bare@example.com',
                               email_verified=False))
        assert status == 403 and json.loads(body)['reason'] == 'no_account'
        assert AccountRepo(httpd.db).by_firebase_uid('uid-unverified') is None
        assert AccountRepo(httpd.db).get('bare@example.com')['firebase_uid'] is None


def test_unknown_uid_no_account_row(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        before = {row['id'] for row in AccountRepo(httpd.db).list()}
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-unknown', email='nobody@example.com'))
        assert status == 403
        assert json.loads(body) == {'error': 'no access', 'reason': 'no_account'}
        assert 'Set-Cookie' not in hdrs
        assert {row['id'] for row in AccountRepo(httpd.db).list()} == before
        events = AuditRepo(httpd.db, None).tail(20)
        assert any(row['action'] == 'login.failed' and row['via'] == 'firebase'
                   for row in events)


def test_invite_accepted_by_matching_verified_email(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'new@example.com', 'member', 'admin')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-invite', email='New@Example.com'), invite=invite)
        assert status == 200 and json.loads(body)['user'] == 'new@example.com'
        assert hdrs.get('Set-Cookie')
        account = AccountRepo(httpd.db).by_firebase_uid('uid-invite')
        assert account['login'] == 'new@example.com'
        member = MembershipRepo(httpd.db, httpd.default_workspace_id).get(account['id'])
        assert member['role'] == 'member'
        assert InviteRepo.find_pending(httpd.db, invite) is None


def test_invite_mismatch_or_unverified_stays_pending(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'other@example.com', 'member', 'admin')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-mis', email='wrong@example.com'), invite=invite)
        assert status == 403
        assert json.loads(body) == {
            'error': 'this invite is for a different or unverified email'}
        assert InviteRepo.find_pending(httpd.db, invite) is not None
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-unv', email='other@example.com',
                               email_verified=False), invite=invite)
        assert status == 403
        assert json.loads(body)['error'] == (
            'this invite is for a different or unverified email')
        assert InviteRepo.find_pending(httpd.db, invite) is not None
        named = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'alice', 'viewer', 'admin')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-name', email='alice@example.com'), invite=named)
        assert status == 403
        assert InviteRepo.find_pending(httpd.db, named) is not None


def test_emails_equal_uses_lower_not_casefold():
    assert emails_equal('Ada@Example.COM', 'ada@example.com') is True
    assert emails_equal('user@straße.de', 'user@strasse.de') is False
    assert emails_equal('user@straße.de', 'user@STRASSE.de') is False
    assert emails_equal('user@straße.de', 'user@Straße.de') is True
    assert emails_equal('', 'a@b.co') is False
    assert emails_equal(None, 'a@b.co') is False


def test_sharp_s_refused_for_invite_and_claim_ascii_case_still_matches(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        sharp_invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'user@straße.de', 'member', 'admin')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-ss-inv', email='user@strasse.de'),
            invite=sharp_invite)
        assert status == 403
        assert json.loads(body)['error'] == (
            'this invite is for a different or unverified email')
        assert InviteRepo.find_pending(httpd.db, sharp_invite) is not None
        ascii_invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'Ada@Example.com', 'member', 'admin')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-ascii-inv', email='ADA@EXAMPLE.COM'),
            invite=ascii_invite)
        assert status == 200 and json.loads(body)['user'] == 'ada@example.com'
        assert hdrs.get('Set-Cookie')

        _member_account(httpd, 'user@straße.de')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-ss-claim', email='user@strasse.de'))
        assert status == 403 and json.loads(body)['reason'] == 'no_account'
        assert AccountRepo(httpd.db).by_firebase_uid('uid-ss-claim') is None
        _member_account(httpd, 'ClaimMe@Example.com')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-ascii-claim', email='CLAIMME@EXAMPLE.COM'))
        assert status == 200 and json.loads(body)['user'] == 'claimme@example.com'


def test_password_and_key_login_and_setup_404_when_firebase_on(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        httpd.users.add('alice', role='admin')
        httpd.users.set_password('alice', PASSWORD)
        _, key = httpd.keys.create('admin', 'admin')
        status, _, body = call(
            httpd, '/login', 'POST',
            json.dumps({'user': 'alice', 'password': PASSWORD}).encode(), csrf(httpd))
        assert status == 404 and json.loads(body) == {'error': 'not found'}
        status, _, body = call(
            httpd, '/login', 'POST', json.dumps({'key': key}).encode(), csrf(httpd))
        assert status == 404 and json.loads(body) == {'error': 'not found'}
        status, _, body = call(httpd, '/setup')
        assert status == 404 and json.loads(body) == {'error': 'not found'}
        status, _, body = call(
            httpd, '/setup', 'POST',
            json.dumps({'token': 'x', 'name': 'admin', 'password': PASSWORD}).encode(),
            csrf(httpd))
        assert status == 404 and json.loads(body) == {'error': 'not found'}
        status, _, page = call(httpd, '/login')
        assert status == 200 and b'Sign in to your workspace</h1>' in page
        assert b'/static/firebase-login.js' in page
        assert b'/static/vendor/firebase/firebase-app-compat.js' in page
        assert b'/static/vendor/firebase/firebase-auth-compat.js' in page
        assert b'onclick=' not in page and b'<script>' not in page
        status, _, invite_page = call(httpd, '/invite?token=abc')
        assert status == 200 and invite_page == page
        status, _, body = call(
            httpd, '/invite', 'POST',
            json.dumps({'token': 'x', 'password': PASSWORD}).encode(), csrf(httpd))
        assert status == 404 and json.loads(body) == {'error': 'not found'}


def test_firebase_failures_count_in_throttle(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        token = _live_token(sub='uid-throttle', email='nope@example.com')
        seen = []
        for _ in range(5):
            status, _, body = _post_firebase(httpd, token)
            seen.append(status)
            assert status == 403
        assert seen == [403] * 5
        status, hdrs, body = _post_firebase(httpd, token)
        assert status == 429
        assert json.loads(body)['error'] == 'too many failed logins; try again later'
        assert 'Retry-After' in hdrs
        events = AuditRepo(httpd.db, None).tail(20)
        assert sum(1 for row in events if row['action'] == 'login.failed') >= 5


def test_invalid_token_is_401_and_audited(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        status, hdrs, body = _post_firebase(httpd, _live_token(key=other))
        assert status == 401 and json.loads(body) == {'error': 'sign-in failed'}
        assert 'Set-Cookie' not in hdrs
        events = AuditRepo(httpd.db, None).tail(5)
        assert events[0]['action'] == 'login.failed' and events[0]['via'] == 'firebase'


def test_cert_fetch_failure_is_503(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)

    def boom():
        raise OSError('offline')

    with firebase_running(tmp_path / 'data', cert_source=CertificateSource(fetch=boom)) as httpd:
        status, _, body = _post_firebase(httpd, _live_token())
        assert status == 503 and json.loads(body) == {'error': 'sign-in unavailable'}


def test_identity_header_with_firebase_refused(tmp_path):
    with pytest.raises(ValueError, match=FIREBASE_IDENTITY):
        make_server(
            tmp_path / 'data', port=0, trusted_proxies=('127.0.0.1',),
            identity_header='alb', firebase=FIREBASE_CFG, cert_source=_source())


def test_missing_firebase_extra_refused(tmp_path, monkeypatch):
    monkeypatch.setattr('agenticml.server.firebase.firebase_available', lambda: False)
    with pytest.raises(ValueError, match=r'\[firebase\] extra'):
        parse_firebase_config('demo-project', 'AIzaSyTestKey', None)
    with pytest.raises(ValueError, match=r'\[firebase\] extra'):
        make_server(
            tmp_path / 'data', port=0, firebase=FIREBASE_CFG, cert_source=_source())


def test_firebase_config_route(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'fb') as httpd:
        status, hdrs, body = call(httpd, '/auth/firebase/config')
        assert status == 200
        assert json.loads(body) == {
            'projectId': PROJECT, 'apiKey': 'test-api-key-1',
            'authDomain': f'{PROJECT}.firebaseapp.com'}
        assert hdrs.get('Cache-Control') == 'no-store'
    with running(tmp_path / 'off') as httpd:
        unknown_get = call(httpd, '/no-such-agenticml-route')
        got_config = call(httpd, '/auth/firebase/config')
        got_auth = call(httpd, '/auth/firebase')
        assert (got_config[0], got_config[2]) == (unknown_get[0], unknown_get[2])
        assert (got_auth[0], got_auth[2]) == (unknown_get[0], unknown_get[2])
        unknown_post = call(httpd, '/no-such-agenticml-route', 'POST', b'{}', csrf(httpd))
        got_post = _post_firebase(httpd, _live_token())
        assert got_post[0] == unknown_post[0]
        assert json.loads(got_post[2]) == json.loads(unknown_post[2])


_FIREBASE_JS = (
    '/static/firebase-login.js',
    '/static/vendor/firebase/firebase-app-compat.js',
    '/static/vendor/firebase/firebase-auth-compat.js',
)
_FIREBASE_LOGIN_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' https://apis.google.com; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; connect-src 'self' https://identitytoolkit.googleapis.com "
    "https://securetoken.googleapis.com https://www.googleapis.com https://apis.google.com; "
    f"frame-src https://{PROJECT}.firebaseapp.com https://apis.google.com"
)


def test_firebase_login_csp_and_assets(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    expected = firebase_login_csp(FIREBASE_CFG.auth_domain)
    assert expected == _FIREBASE_LOGIN_CSP
    assert f'https://{FIREBASE_CFG.auth_domain}' in expected
    with firebase_running(tmp_path / 'on') as httpd:
        for path in ('/login', '/invite'):
            status, hdrs, _ = call(httpd, path)
            assert status == 200
            assert hdrs.get('Content-Security-Policy') == _FIREBASE_LOGIN_CSP
        for path in _FIREBASE_JS:
            status, hdrs, body = call(httpd, path)
            assert status == 200
            assert hdrs.get('Content-Type', '').startswith('text/javascript')
            assert body
        status, hdrs, _ = call(httpd, '/static/app.css')
        assert status == 200
        assert hdrs.get('Content-Security-Policy') == _CSP
        _member_account(httpd, 'ada@example.com', uid='uid-csp')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-csp', email='ada@example.com'))
        assert status == 200
        status, hdrs, body, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        assert hdrs.get('Content-Security-Policy') == _CSP
        status, hdrs, body = call(httpd, '/login', 'POST', b'{}', csrf(httpd))
        assert status == 404
        assert json.loads(body) == {'error': 'not found'}
        assert hdrs.get('Content-Security-Policy') == _CSP
    with running(tmp_path / 'off') as httpd:
        status, hdrs, _ = call(httpd, '/login')
        assert status == 200
        assert hdrs.get('Content-Security-Policy') == _CSP
        unknown = call(httpd, '/no-such-agenticml-route')
        for path in _FIREBASE_JS:
            got = call(httpd, path)
            assert (got[0], got[2]) == (unknown[0], unknown[2])
            assert got[1].get('Location') == unknown[1].get('Location')


def test_session_payload_auth_field(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'off') as httpd:
        _, httpd.admin_key = httpd.keys.create('admin', 'admin')
        _, _, hdrs = login(httpd)
        status, _, body, _ = authed(httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        payload = json.loads(body)
        assert payload['auth'] == 'password'
        assert payload['via'] == 'key'
    with firebase_running(tmp_path / 'on') as httpd:
        _member_account(httpd, 'ada@example.com', uid='uid-session')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-session', email='ada@example.com'))
        assert status == 200
        status, _, body, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        payload = json.loads(body)
        assert payload['auth'] == 'firebase'
        assert payload['via'] == 'firebase'


def test_cli_accounts_add_and_list(tmp_path, capsys):
    assert cli.main([
        'server', 'accounts', 'add', 'op@example.com',
        '--display-name', 'Op', '--server-admin', '--data', str(tmp_path),
    ]) == 0
    assert cli.main(['server', 'accounts', 'list', '--data', str(tmp_path)]) == 0
    table = capsys.readouterr().out
    assert 'op@example.com' in table and 'Op' in table
    assert 'firebase' in table and 'email_verified' in table
    assert 'no' in table
    assert cli.main(['server', 'accounts', 'list', '--json', '--data', str(tmp_path)]) == 0
    rows = json.loads(capsys.readouterr().out)
    found = next(row for row in rows if row['login'] == 'op@example.com')
    assert found['firebase_uid'] is None and found['email_verified'] == 0
    assert found['server_admin'] == 1 and found['display_name'] == 'Op'
    db = for_server_dir(tmp_path / 'server')
    actions = {row['action'] for row in AuditRepo(db, None).tail(20)}
    assert 'account.create' in actions
    db.close()


def test_concurrent_first_sign_ins_one_link(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    token = _live_token(sub='uid-race', email='race@example.com')
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'race@example.com')

        def once(_):
            return _post_firebase(httpd, token)

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(once, range(2)))
        assert all(status == 200 for status, _, _ in results)
        rows = [row for row in AccountRepo(httpd.db).list() if row['login'] == 'race@example.com']
        assert len(rows) == 1 and rows[0]['firebase_uid'] == 'uid-race'


def test_cli_serve_firebase_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('AGENTICML_SERVER_FIREBASE_PROJECT', 'demo-project')
    monkeypatch.setenv('AGENTICML_SERVER_FIREBASE_API_KEY', 'AIzaSyEnvKey')
    seen = {}

    def fake_serve(*args, **kwargs):
        seen['kwargs'] = kwargs

    monkeypatch.setattr('agenticml.server.serve', fake_serve)
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0']) == 0
    cfg = seen['kwargs']['firebase']
    assert cfg.project == 'demo-project'
    assert cfg.api_key == 'AIzaSyEnvKey'
    assert cfg.auth_domain == 'demo-project.firebaseapp.com'
    monkeypatch.delenv('AGENTICML_SERVER_FIREBASE_PROJECT')
    monkeypatch.delenv('AGENTICML_SERVER_FIREBASE_API_KEY')
    from agenticml.server.app import serve as real_serve
    monkeypatch.setattr('agenticml.server.serve', real_serve)
    assert cli.main([
        'server', 'serve', '--data', str(tmp_path), '--port', '0',
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyCli',
        '--identity-header', 'alb',
        '--trusted-proxy', '127.0.0.1',
    ]) == 1
    assert FIREBASE_IDENTITY in capsys.readouterr().err
    seen.clear()
    monkeypatch.setattr('agenticml.server.up.serve', fake_serve)
    monkeypatch.setattr('agenticml.server.up.check_tailscale', lambda: 'https://ex.ts.net')
    monkeypatch.setattr('agenticml.server.up.bootstrap_admin', lambda *a, **k: tmp_path)
    monkeypatch.setattr('agenticml.server.up.ensure_funnel_443_free', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_on', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_off', lambda: None)
    assert cli.main([
        'server', 'up', '--data', str(tmp_path), '--port', '0',
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyUp',
    ]) == 0
    assert seen['kwargs']['firebase'].api_key == 'AIzaSyUp'


def test_github_provider_does_not_claim_or_accept_invite(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    github = {'sign_in_provider': 'github.com'}
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'gh@example.com')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-gh', email='gh@example.com', firebase=github))
        assert status == 403 and json.loads(body) == {
            'error': 'no access', 'reason': 'no_account'}
        assert 'Set-Cookie' not in hdrs
        assert AccountRepo(httpd.db).by_firebase_uid('uid-gh') is None
        events = AuditRepo(httpd.db, None).tail(5)
        assert any(row['action'] == 'login.failed' and row['detail'].get('provider') == 'github.com'
                   for row in events)
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'newgh@example.com', 'member', 'admin')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-gh-inv', email='newgh@example.com', firebase=github),
            invite=invite)
        assert status == 403
        assert json.loads(body)['error'] == (
            'this invite is for a different or unverified email')
        assert InviteRepo.find_pending(httpd.db, invite) is not None
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-pw', email='gh@example.com',
                               firebase={'sign_in_provider': 'password'}))
        assert status == 200 and json.loads(body)['user'] == 'gh@example.com'
        assert hdrs.get('Set-Cookie')
        assert AccountRepo(httpd.db).get('gh@example.com')['email_verified'] == 1


def test_github_linked_uid_stores_provider_filtered_email_verified(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    github = {'sign_in_provider': 'github.com'}
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'gh@example.com', uid='uid-gh-linked')
        assert AccountRepo(httpd.db).get('gh@example.com')['email_verified'] == 1
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-gh-linked', email='gh@example.com',
                               email_verified=True, firebase=github))
        assert status == 200 and json.loads(body)['user'] == 'gh@example.com'
        assert hdrs.get('Set-Cookie')
        row = AccountRepo(httpd.db).get('gh@example.com')
        assert row['firebase_uid'] == 'uid-gh-linked'
        assert row['email_verified'] == 0


def test_bad_invite_token_is_invite_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'ada@example.com', uid='uid-linked')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-linked', email='ada@example.com'),
            invite='not-a-real-token')
        assert status == 403
        assert json.loads(body) == {
            'error': 'this invite is no longer valid', 'reason': 'invite_invalid'}
        assert 'Set-Cookie' not in hdrs
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'used@example.com', 'member', 'admin')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-used', email='used@example.com'), invite=invite)
        assert status == 200
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-used', email='used@example.com'), invite=invite)
        assert status == 403
        assert json.loads(body)['reason'] == 'invite_invalid'
        assert 'Set-Cookie' not in hdrs


def test_invite_existing_credentialed_account_is_account_exists(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        repo = AccountRepo(httpd.db)
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'taken@example.com', 'member', 'admin')
        taken = repo.create('taken@example.com')
        with httpd.db.transaction(write=True) as tx:
            tx.execute('UPDATE accounts SET password=? WHERE id=?', ('{"n":1}', taken['id']))
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-taken', email='taken@example.com'), invite=invite)
        assert status == 403
        assert json.loads(body) == {
            'error': 'an account with this email already exists; '
                     'sign in with it or ask a server admin',
            'reason': 'account_exists'}
        assert 'Set-Cookie' not in hdrs
        assert InviteRepo.find_pending(httpd.db, invite) is not None
        assert AccountRepo(httpd.db).by_firebase_uid('uid-taken') is None
        ident_invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'ident@example.com', 'member', 'admin')
        ident = repo.create('ident@example.com')
        with httpd.db.transaction(write=True) as tx:
            tx.execute('UPDATE accounts SET identity=?, identity_folded=? WHERE id=?',
                       ('proxy:1', 'proxy:1', ident['id']))
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-ident', email='ident@example.com'),
            invite=ident_invite)
        assert status == 403 and json.loads(body)['reason'] == 'account_exists'
        assert InviteRepo.find_pending(httpd.db, ident_invite) is not None


def test_cross_site_origin_refused(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'ada@example.com', uid='uid-linked')
        headers = csrf(httpd, Origin='http://evil.example')
        status, hdrs, body = call(
            httpd, '/auth/firebase', 'POST',
            json.dumps({'id_token': _live_token(sub='uid-linked', email='ada@example.com')}).encode(),
            headers)
        assert status == 403
        assert json.loads(body) == {'error': 'cross-site request refused'}
        assert 'Set-Cookie' not in hdrs


def test_stale_generation_between_verify_and_session(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'ada@example.com', uid='uid-linked')

        def bump(account):
            httpd.users.create_invite(account['login'])

        monkeypatch.setattr(httpd.web, '_between_verify_and_session', bump)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-linked', email='ada@example.com'))
        assert status == 401
        assert json.loads(body) == {'error': 'invalid user name or password'}
        assert 'Set-Cookie' not in hdrs


def test_cert_503_leaves_throttle_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)

    def boom():
        raise OSError('offline')

    with firebase_running(tmp_path / 'data', cert_source=CertificateSource(fetch=boom)) as httpd:
        for _ in range(6):
            status, _, body = _post_firebase(httpd, _live_token())
            assert status == 503
            assert json.loads(body) == {'error': 'sign-in unavailable'}
        status, _, body = _post_firebase(httpd, _live_token())
        assert status == 503
        for _ in range(50):
            if not httpd.web.throttle._entries:
                break
            time.sleep(0.01)
        assert httpd.web.throttle._entries == {}


def test_invalid_token_401_counts_in_address_bucket(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with firebase_running(tmp_path / 'data') as httpd:
        token = _live_token(key=other)
        for _ in range(5):
            status, _, body = _post_firebase(httpd, token)
            assert status == 401
            assert json.loads(body) == {'error': 'sign-in failed'}
        status, hdrs, body = _post_firebase(httpd, token)
        assert status == 429
        assert json.loads(body)['error'] == 'too many failed logins; try again later'
        assert 'Retry-After' in hdrs


def test_unverified_email_already_linked_can_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'ada@example.com', uid='uid-linked')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-linked', email='ada@example.com',
                               email_verified=False))
        assert status == 200 and json.loads(body)['user'] == 'ada@example.com'
        assert hdrs.get('Set-Cookie')


def test_firebase_off_routes_match_unknown_path(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'off') as httpd:
        cases = [
            ('GET', '/auth/firebase/config', None),
            ('GET', '/auth/firebase', None),
            ('POST', '/auth/firebase', json.dumps({'id_token': 'x'}).encode()),
            ('POST', '/auth/firebase/config', b'{}'),
            ('PUT', '/auth/firebase', b'{}'),
            ('DELETE', '/auth/firebase/config', None),
            ('GET', '/static/firebase-login.js', None),
            ('GET', '/static/vendor/firebase/firebase-app-compat.js', None),
            ('GET', '/static/vendor/firebase/firebase-auth-compat.js', None),
        ]
        for method, path, body in cases:
            headers = csrf(httpd) if method not in ('GET', 'HEAD') else None
            unknown = call(httpd, '/no-such-agenticml-route', method, body, headers)
            got = call(httpd, path, method, body, headers)
            assert got[0] == unknown[0], (method, path, got[0], unknown[0])
            assert got[2] == unknown[2], (method, path)
            assert got[1].get('Location') == unknown[1].get('Location')


def test_server_up_firebase_skips_admin_and_writes_install_flags(
        tmp_path, monkeypatch, capsys):
    import plistlib

    from agenticml.server.up import write_launchd_plist
    from agenticml.server.users import UserStore
    from server_db_helpers import default_workspace_id
    from test_server_up import install_fake, run_up

    install_fake(tmp_path, monkeypatch)
    monkeypatch.setattr('agenticml.server.up._stdout_is_tty', lambda: False)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    data = tmp_path / 'data'
    assert run_up(
        data, '--install',
        '--firebase-project', 'demo-project',
        '--firebase-api-key', 'AIzaSyTestKey',
    ) == 0
    out = capsys.readouterr().out
    assert '/invite?token=' not in out
    assert 'agenticml server accounts add <your-email> --server-admin' in out
    assert 'sign in with Firebase' in out
    users = UserStore(data / 'server', workspace_id=default_workspace_id(data / 'server'))
    assert users.get('admin') is None
    text = (home / '.config/systemd/user/agenticml-server-up.service').read_text()
    assert '--firebase-project demo-project' in text
    assert '--firebase-api-key AIzaSyTestKey' in text
    assert '--firebase-auth-domain demo-project.firebaseapp.com' in text
    path = write_launchd_plist(
        data, 8080, home=tmp_path / 'mac-home', firebase=FIREBASE_CFG)
    payload = plistlib.loads(path.read_bytes())
    args = payload['ProgramArguments']
    assert args[args.index('--firebase-project') + 1] == FIREBASE_CFG.project
    assert args[args.index('--firebase-api-key') + 1] == FIREBASE_CFG.api_key
    assert args[args.index('--firebase-auth-domain') + 1] == FIREBASE_CFG.auth_domain


def _email_admin(httpd, login='boss@example.com', uid='uid-boss'):
    """An email admin signed in with Firebase; returns the session cookie."""
    account = AccountRepo(httpd.db).create(login)
    MembershipRepo(httpd.db, httpd.default_workspace_id).add(account['id'], 'admin')
    with httpd.db.transaction(write=True) as tx:
        AccountRepo(httpd.db).link_firebase(account['id'], uid, login, True, tx)
    status, hdrs, _ = _post_firebase(httpd, _live_token(sub=uid, email=login))
    assert status == 200
    return hdrs['Set-Cookie']


def _api(httpd, cookie, path, method='GET', payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    extra = csrf(httpd) if method != 'GET' else None
    status, _, raw, _ = authed(httpd, path, method, body, extra=extra, cookie=cookie)
    return status, (json.loads(raw) if raw else None)


def test_email_admin_creates_key_for_self(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        cookie = _email_admin(httpd)
        status, data = _api(httpd, cookie, '/api/w/default/keys', 'POST',
                            {'name': 'laptop', 'scope': 'sync'})
        assert status == 201 and data['record']['user'] == 'boss@example.com'
        status, data = _api(httpd, cookie, '/api/w/default/keys', 'POST',
                            {'name': 'desktop', 'scope': 'sync', 'user': 'Boss@Example.com'})
        assert status == 201 and data['record']['user'] == 'boss@example.com'
        status, data = _api(httpd, cookie, '/api/w/default/keys?user=boss%40example.com')
        assert status == 200
        assert {row['name'] for row in data['keys']} == {'laptop', 'desktop'}


def test_email_admin_key_user_and_name_validation(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        cookie = _email_admin(httpd)
        status, data = _api(httpd, cookie, '/api/w/default/keys', 'POST',
                            {'name': 'a@b.com', 'scope': 'sync'})
        assert status == 400 and data == {'error': 'invalid or reserved key name'}
        for user in ('a@b', 'env', '..', '', 'x' * 255 + '@example.com', 7):
            status, data = _api(httpd, cookie, '/api/w/default/keys', 'POST',
                                {'name': 'ok', 'scope': 'sync', 'user': user})
            assert status == 400, user
            assert data == {'error': 'invalid or reserved user name'}, user
        status, data = _api(httpd, cookie, '/api/w/default/keys', 'POST',
                            {'name': 'ok', 'scope': 'sync', 'user': 'ghost@example.com'})
        assert status == 404 and data == {'error': 'unknown user'}


def test_email_admin_changes_role_and_removes_member(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        cookie = _email_admin(httpd)
        _member_account(httpd, 'bob@example.com')
        path = '/api/w/default/members/bob%40example.com'
        status, data = _api(httpd, cookie, path, 'PATCH', {'role': 'viewer'})
        assert status == 200 and data['user']['role'] == 'viewer'
        status, data = _api(httpd, cookie, '/api/w/default/members/Bob%40Example.com',
                            'PATCH', {'role': 'member'})
        assert status == 200 and data['user']['name'] == 'bob@example.com'
        status, data = _api(httpd, cookie, '/api/w/default/members/ghost%40example.com',
                            'PATCH', {'role': 'member'})
        assert status == 404 and data == {'error': 'not found'}
        status, data = _api(httpd, cookie, path, 'DELETE')
        assert status == 200 and data['removed'] == 'bob@example.com'
        status, data = _api(httpd, cookie, path, 'DELETE')
        assert status == 404


def test_email_admin_invite_link_and_self_checks(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        cookie = _email_admin(httpd)
        _member_account(httpd, 'bob@example.com')
        status, data = _api(httpd, cookie, '/api/w/default/members/Bob%40Example.com/invite',
                            'POST', {})
        assert status == 200 and '/invite?token=' in data['invite_url']
        status, data = _api(httpd, cookie, '/api/w/default/members/Boss%40Example.com/invite',
                            'POST', {})
        assert status == 409 and 'yourself' in data['error']
        status, data = _api(httpd, cookie, '/api/w/default/members/Boss%40Example.com',
                            'DELETE')
        assert status == 409 and data == {'error': 'you cannot remove yourself'}


def test_email_admin_member_path_validation(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        cookie = _email_admin(httpd)
        for login in ('a%40b', 'env', '..'):
            for method, suffix, payload in (
                    ('PATCH', '', {'role': 'member'}), ('DELETE', '', None),
                    ('POST', '/invite', {})):
                status, data = _api(httpd, cookie, f'/api/w/default/members/{login}{suffix}',
                                    method, payload)
                assert status == 400, (login, method)
                assert data == {'error': 'invalid or reserved user name'}, (login, method)
