"""OIDC sign-in (package 35.1): flow, token checks, sign-up modes, sign-out."""
from urllib.parse import parse_qs, urlsplit
import json
import re
import time

import jwt
import pytest

from agenticml import cli
from agenticml.server.app import make_server
from agenticml.server.db.repos import AuditRepo
from agenticml.server.db.workspaces import AccountRepo, InviteRepo, MembershipRepo
from agenticml.server.keycache import Claims
from agenticml.server.oidc import (
    CALLBACK_PATH, OIDC_EXTRA, START_PATH, OidcConfig, OidcStates, OidcTokenInvalid,
    parse_oidc_config, pkce_challenge, safe_next, binding_slot, verify_id_token)
from agenticml.server.webauth import parse_signup_email_domains
from oidc_fake import (
    CLIENT_ID, KEY, KID, OMIT, OTHER_KEY, FakeProvider, cookie_pair, finish_flow, header,
    oidc_server, plans, post_start, raw_call, sign_in, start_flow, _jwk)
from test_server_web import authed, call, cookie_parts, csrf

NOW = 1_700_000_000
ISSUER = 'https://idp.example.com/pool'


@pytest.fixture
def provider():
    fake = FakeProvider()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)


# --- configuration -----------------------------------------------------------

def test_parse_oidc_config_defaults_and_none():
    assert parse_oidc_config(None, None) is None
    assert parse_oidc_config('', '', '') is None
    config = parse_oidc_config(ISSUER, CLIENT_ID)
    assert config == OidcConfig(issuer=ISSUER, client_id=CLIENT_ID, logout_url=None, label='Sign in')
    config = parse_oidc_config(
        ISSUER, CLIENT_ID, 'https://x.auth.us-east-1.amazoncognito.com/logout', ' Company SSO ')
    assert config.label == 'Company SSO'
    assert config.logout_url.endswith('/logout')


@pytest.mark.parametrize('issuer', [
    'http://idp.example.com/pool', 'ftp://idp.example.com', 'idp.example.com',
    'https://user:pw@idp.example.com', 'https://idp.example.com/p#frag',
    'https://idp.example.com/p?x=1', 'https://', 'https://idp example.com'])
def test_issuer_must_be_https(issuer):
    with pytest.raises(ValueError, match='OIDC issuer'):
        parse_oidc_config(issuer, CLIENT_ID)


def test_loopback_http_issuer_allowed_for_tests():
    assert parse_oidc_config('http://127.0.0.1:9/pool', CLIENT_ID) is not None
    assert parse_oidc_config('http://localhost:9/pool', CLIENT_ID) is not None


def test_client_id_and_logout_validation():
    with pytest.raises(ValueError, match='client id is required'):
        parse_oidc_config(ISSUER, None)
    with pytest.raises(ValueError, match='require --oidc-issuer'):
        parse_oidc_config(None, CLIENT_ID)
    with pytest.raises(ValueError, match='require --oidc-issuer'):
        parse_oidc_config('', None, 'https://x.example/logout')
    with pytest.raises(ValueError, match='logout URL'):
        parse_oidc_config(ISSUER, CLIENT_ID, 'http://evil.example/logout')
    with pytest.raises(ValueError, match='label'):
        parse_oidc_config(ISSUER, CLIENT_ID, None, 'x' * 41)


def test_missing_extra_refuses(monkeypatch):
    monkeypatch.setattr('agenticml.server.oidc.oidc_available', lambda: False)
    with pytest.raises(ValueError) as exc:
        parse_oidc_config(ISSUER, CLIENT_ID)
    assert str(exc.value) == OIDC_EXTRA
    assert "pip install 'agenticml-lite[oidc]'" in OIDC_EXTRA


def test_base_install_imports_without_pyjwt_and_server_refuses_oidc_without_it(tmp_path, monkeypatch):
    import subprocess
    import sys
    code = ("import sys; sys.modules['jwt'] = None; sys.modules['cryptography'] = None\n"
            "import agenticml.cli, agenticml.server.app, agenticml.server.oidc, "
            "agenticml.server.webauth\n")
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    monkeypatch.setattr('agenticml.server.oidc.oidc_available', lambda: False)
    with pytest.raises(ValueError) as exc:
        make_server(tmp_path / 'data', port=0, public_url='https://aml.example.com',
                    oidc=OidcConfig(ISSUER, CLIENT_ID))
    assert str(exc.value) == OIDC_EXTRA


@pytest.mark.parametrize('value,expected', [
    ('/', '/'), ('/w/alpha/', '/w/alpha/'), ('/runs?x=1', '/runs?x=1'),
    ('//evil.example', '/'), ('https://evil.example/', '/'), ('/\\evil.example', '/'),
    ('javascript:alert(1)', '/'), ('', '/'), (None, '/'), ('relative', '/'),
    ('/ok\r\nSet-Cookie: x=1', '/'), ('/a b', '/'), ('/' + 'a' * 600, '/'),
    ('/\t/evil.example', '/')])
def test_next_is_same_origin_path_only(value, expected):
    assert safe_next(value) == expected


def test_signup_email_domains_parse():
    assert parse_signup_email_domains('') == frozenset()
    assert parse_signup_email_domains(' Example.COM, @corp.example.org ,') == frozenset(
        {'example.com', 'corp.example.org'})
    for bad in ('example', '*.example.com', 'a@b.com', 'exa mple.com', 'example.com/x'):
        with pytest.raises(ValueError, match='signup-email-domains'):
            parse_signup_email_domains(bad)


def test_pkce_challenge_matches_rfc_vector():
    assert pkce_challenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk') == (
        'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')


# --- ID token checks ----------------------------------------------------------

class Keys:
    def __init__(self, key=KEY, kid=KID):
        self.jwk = _jwk(key, kid)
        self.kid = kid

    def get(self, kid, now=None):
        if kid != self.kid:
            raise OidcTokenInvalid('unknown kid')
        return self.jwk


CONFIG = OidcConfig(issuer=ISSUER, client_id=CLIENT_ID)


def _token(key=KEY, kid=KID, alg='RS256', secret=None, **overrides):
    claims = {'iss': ISSUER, 'aud': CLIENT_ID, 'sub': 'sub-1', 'iat': NOW, 'exp': NOW + 3600,
              'nonce': 'n-1', 'token_use': 'id', 'email': 'Ada@Example.COM',
              'email_verified': True, 'name': 'Ada'}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not OMIT}
    headers = {} if kid is None else {'kid': kid}
    return jwt.encode(claims, secret or key, algorithm=alg, headers=headers)


def _verify(token, nonce='n-1', now=NOW, keys=None):
    return verify_id_token(token, CONFIG, nonce=nonce, now=now, keys=keys or Keys())


def test_valid_token_returns_claims():
    assert _verify(_token()) == Claims(
        uid='oidc:sub-1', email='ada@example.com', email_verified=True, name='Ada',
        provider='oidc')


@pytest.mark.parametrize('value,expected', [
    (True, True), ('true', True), (False, False), ('false', False), ('yes', False),
    (1, False), (None, False), (OMIT, False)])
def test_email_verified_bool_or_string(value, expected):
    assert _verify(_token(email_verified=value)).email_verified is expected


def test_missing_email_and_name():
    claims = _verify(_token(email=OMIT, name=OMIT, token_use=OMIT))
    assert claims.email is None and claims.name is None


@pytest.mark.parametrize('overrides,reason', [
    ({'iss': 'https://other.example.com/pool'}, 'iss'),
    ({'iss': ISSUER + '/'}, 'iss'),
    ({'aud': 'other-client'}, 'aud'),
    ({'aud': ['other-client', 'another']}, 'aud'),
    ({'aud': [CLIENT_ID, 'another']}, 'aud'),      # a list needs azp too
    ({'aud': [CLIENT_ID, 'another'], 'azp': 'another'}, 'aud'),
    ({'aud': OMIT}, 'aud'),
    ({'exp': NOW - 3600}, 'expired'),
    ({'exp': NOW - 61}, 'expired'),
    ({'exp': OMIT}, 'exp'),
    ({'exp': 'soon'}, 'exp'),
    ({'iat': NOW + 3600}, 'iat'),
    ({'iat': OMIT}, 'iat'),
    ({'nonce': 'other'}, 'nonce'),
    ({'nonce': OMIT}, 'nonce'),
    ({'token_use': 'access'}, 'token_use'),
    ({'sub': OMIT}, 'sub'),
    ({'sub': ''}, 'sub'),
    ({'sub': 'x' * 256}, 'sub'),
    ({'sub': 7}, 'claim'),      # PyJWT refuses a non-string sub itself
])
def test_claim_checks_refuse(overrides, reason):
    with pytest.raises(OidcTokenInvalid) as exc:
        _verify(_token(**overrides))
    assert exc.value.reason == reason


def test_audience_list_with_matching_azp_is_accepted():
    claims = _verify(_token(aud=[CLIENT_ID, 'another'], azp=CLIENT_ID))
    assert claims.uid == 'oidc:sub-1'


def test_leeway_accepts_just_expired_token():
    assert _verify(_token(exp=NOW - 30)).uid == 'oidc:sub-1'


def test_signature_alg_and_kid_checks():
    with pytest.raises(OidcTokenInvalid) as exc:
        _verify(_token(key=OTHER_KEY))
    assert exc.value.reason == 'signature'
    with pytest.raises(OidcTokenInvalid, match='refused'):
        _verify(_token(alg='HS256', secret='s' * 40))
    unsigned = (jwt.encode({'sub': 'x'}, key=None, algorithm='none', headers={'kid': KID}))
    with pytest.raises(OidcTokenInvalid, match="'none' refused"):
        _verify(unsigned)
    with pytest.raises(OidcTokenInvalid, match='missing kid'):
        _verify(_token(kid=None))
    with pytest.raises(OidcTokenInvalid, match='unknown kid'):
        _verify(_token(kid='rotated'))
    with pytest.raises(OidcTokenInvalid, match='malformed'):
        _verify('not-a-jwt')


# --- the flow against the fake provider -------------------------------------------

def _post_json(server, path, payload):
    return call(server, path, 'POST', json.dumps(payload).encode(), csrf(server))


def _session(server, cookie):
    status, _, body, _ = authed(server, '/api/account/session', cookie=cookie)
    return status, (json.loads(body) if status == 200 else None)


def _account(server, login):
    return AccountRepo(server.db).get(login)


def test_start_redirect_pkce_state_and_cookie(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        status, headers, body = raw_call(httpd, START_PATH)
        assert status == 302 and body == b''
        location = header(headers, 'Location')[0]
        parts = urlsplit(location)
        assert parts.path == '/oauth2/authorize'
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query['response_type'] == 'code'
        assert query['client_id'] == CLIENT_ID
        assert query['scope'] == 'openid email profile'
        assert query['redirect_uri'] == provider.public_url + CALLBACK_PATH
        assert query['code_challenge_method'] == 'S256'
        assert len(query['code_challenge']) == 43
        assert len(query['state']) >= 32 and len(query['nonce']) >= 32
        assert 'client_secret' not in query
        cookie = header(headers, 'Set-Cookie')[0]
        name, value = cookie_pair(cookie)
        # One of a few slots, named by the state; plain http: no __Host- prefix.
        assert name == 'aml_oidc_' + str(binding_slot(query['state']))
        assert re.fullmatch(r'aml_oidc_[0-7]', name)
        assert value and value != query['state']
        attrs = {a.strip().lower() for a in cookie.split(';')[1:]}
        assert {'httponly', 'samesite=lax', 'path=/', 'max-age=600'} <= attrs
        assert header(headers, 'Cache-Control') == ['no-store']


def test_secure_cookie_name_and_flags_on_https_public_url(tmp_path, provider, monkeypatch):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        monkeypatch.setattr(httpd.web, 'public_url', 'https://aml.example.com')
        status, headers, _ = raw_call(httpd, START_PATH)
        cookie = header(headers, 'Set-Cookie')[0]
        assert re.match(r'__Host-aml_oidc_[0-7]=', cookie)
        attrs = {a.strip().lower() for a in cookie.split(';')[1:]}
        assert {'secure', 'httponly', 'samesite=lax', 'path=/'} <= attrs
        assert not any(a.startswith('domain') for a in attrs)


def test_happy_path_open_signup_creates_personal_workspace(tmp_path, provider, caplog):
    caplog.set_level('DEBUG')
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        status, headers, body, session = sign_in(httpd, provider)
        assert status == 200 and session
        html = body.decode()
        assert 'http-equiv="refresh" content="0;url=/"' in html
        # The session cookie is the LAST Set-Cookie (dict-based clients see it).
        cookies = header(headers, 'Set-Cookie')
        assert len(cookies) == 2
        assert re.match(r'aml_oidc_[0-7]=;', cookies[0]) and 'Max-Age=0' in cookies[0]
        name, token, flags = cookie_parts(cookies[1])
        assert name == 'aml_session' and token and flags['samesite'] == 'Strict'
        assert flags['httponly'] is True
        account = _account(httpd, 'ada@example.com')
        assert account['firebase_uid'] == 'oidc:sub-1'
        assert AccountRepo.external_uid(account) == 'oidc:sub-1'
        assert account['email'] == 'ada@example.com' and account['email_verified'] == 1
        assert account['created_by'] == 'signup'
        workspaces = AccountRepo(httpd.db).workspaces('ada@example.com')
        assert [w['role'] for w in workspaces] == ['owner']
        status, payload = _session(httpd, session)
        assert status == 200
        assert payload['user'] == 'ada@example.com' and payload['auth'] == 'oidc'
        assert payload['open_signup'] is True and len(payload['workspaces']) == 1
        # PKCE: the provider saw S256 and the right verifier; no client secret.
        request = provider.token_requests[0]
        assert request['grant_type'] == 'authorization_code'
        assert request['client_id'] == CLIENT_ID and 'client_secret' not in request
        assert request['redirect_uri'] == provider.public_url + CALLBACK_PATH
        events = AuditRepo(httpd.db, None).tail(20)
        assert any(row['action'] == 'login.ok' and row['detail'].get('method') == 'oidc'
                   for row in events)
        assert any(row['action'] == 'account.signup' for row in events)
        # Secrets never reach the logs.
        for secret in (request['code'], request['code_verifier'], token):
            assert secret not in caplog.text
        assert provider.id_token('x').split('.')[1] not in caplog.text


def test_second_sign_in_reuses_the_account(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        sign_in(httpd, provider)
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        assert len(AccountRepo(httpd.db).list()) == 1
        assert len(AccountRepo(httpd.db).workspaces('ada@example.com')) == 1


def test_state_is_single_use(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        callback, cookie = start_flow(httpd, provider)
        status, _, _ = finish_flow(httpd, callback, cookie)
        assert status == 200
        status, headers, body = finish_flow(httpd, callback, cookie)
        assert status == 400
        assert b'did not start in this browser' in body
        assert not [c for c in header(headers, 'Set-Cookie') if c.startswith('aml_session=') and 'Max-Age=0' not in c]
        assert len(provider.token_requests) == 1


def test_callback_without_or_with_wrong_cookie_is_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        callback, cookie = start_flow(httpd, provider)
        status, headers, _ = finish_flow(httpd, callback, None)
        assert status == 400 and not provider.token_requests
        assert _account(httpd, 'ada@example.com') is None
        callback, cookie = start_flow(httpd, provider)
        own = 'aml_oidc_' + str(binding_slot(parse_qs(urlsplit(callback).query)['state'][0]))
        status, _, _ = finish_flow(httpd, callback, own + '=' + 'x' * 43)
        assert status == 400 and not provider.token_requests
        # A cookie from another sign-in does not bind this state either.
        _, other = start_flow(httpd, provider)
        callback, cookie = start_flow(httpd, provider)
        status, _, _ = finish_flow(httpd, callback, other)
        assert status == 400 and not provider.token_requests
        # The state was consumed by the refused callback.
        status, _, _ = finish_flow(httpd, callback, cookie)
        assert status == 400


@pytest.mark.parametrize('query', [
    '?code=abc&state=nope', '?code=abc', '?state=x', '', '?code=a&code=b&state=x',
    '?code=abc&state=a&state=b'])
def test_unknown_missing_and_repeated_state_or_code(tmp_path, provider, query):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, cookie = start_flow(httpd, provider)
        status, _, _ = finish_flow(httpd, CALLBACK_PATH + query, cookie)
        assert status == 400
        assert not provider.token_requests


def test_failed_callbacks_are_throttled_by_address(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, cookie = start_flow(httpd, provider)
        statuses = [finish_flow(httpd, CALLBACK_PATH + '?code=a&state=nope', cookie)[0]
                    for _ in range(12)]
        assert statuses[0] == 400 and statuses[-1] == 429
        status, headers, body = finish_flow(httpd, CALLBACK_PATH + '?code=a&state=nope', cookie)
        assert status == 429 and header(headers, 'Retry-After') and b'Too many' in body


def test_expired_state_is_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        callback, cookie = start_flow(httpd, provider)
        real = httpd.web.clock
        httpd.web.clock = lambda: real() + 601
        status, _, _ = finish_flow(httpd, callback, cookie)
        assert status == 400 and not provider.token_requests
        assert _account(httpd, 'ada@example.com') is None


def test_state_rows_expire_and_are_capped(tmp_path, provider, monkeypatch):
    monkeypatch.setattr('agenticml.server.oidc.STATE_CAP', 5)
    with oidc_server(tmp_path / 'data', provider) as httpd:
        states = OidcStates(httpd.db)
        for index in range(8):
            states.create(f's{index}', 'b', 'n', 'v', None, '/', now=1000 + index)
        with httpd.db.transaction() as tx:
            rows = tx.fetchall(
                "SELECT name FROM server_settings WHERE name LIKE 'oidc_state:%'")
        assert len(rows) == 5
        assert states.consume('s0', 1008) is None       # evicted: oldest first
        assert states.consume('s7', 1008)['next'] == '/'
        assert states.consume('s7', 1008) is None         # single use
        assert states.consume('s6', 1006 + 601) is None    # expired
        # State values are stored hashed, never as they came.
        with httpd.db.transaction() as tx:
            names = [r[0] for r in tx.fetchall(
                "SELECT name FROM server_settings WHERE name LIKE 'oidc_state:%'")]
        assert names and all(re.fullmatch(r'oidc_state:[0-9a-f]{64}', n) for n in names)


def test_nonce_mismatch_is_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.claims = {'nonce': 'attacker-nonce'}
        status, _, body, session = sign_in(httpd, provider)
        assert status == 401 and session is None
        assert b'Sign-in failed' in body
        assert _account(httpd, 'ada@example.com') is None


@pytest.mark.parametrize('alg', ['none', 'HS256'])
def test_unsigned_and_hmac_tokens_are_refused(tmp_path, provider, alg):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.alg = alg
        status, _, _, session = sign_in(httpd, provider)
        assert status == 401 and session is None
        assert _account(httpd, 'ada@example.com') is None
        events = AuditRepo(httpd.db, None).tail(5)
        assert any(row['action'] == 'login.failed' and row['detail']['method'] == 'oidc'
                   for row in events)


def test_signature_from_another_key_issuer_and_audience_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        for change in ({'sign_with': OTHER_KEY}, {'claims': {'iss': 'https://evil.example/pool'}},
                       {'claims': {'aud': ['someone-else']}},
                       {'claims': {'aud': 'someone-else'}}):
            provider.sign_with, provider.claims = KEY, {}
            for name, value in change.items():
                setattr(provider, name, value)
            status, _, _, session = sign_in(httpd, provider)
            assert status == 401 and session is None, change
        assert _account(httpd, 'ada@example.com') is None


def test_expired_token_is_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.claims = {'exp': int(time.time()) - 3600}
        status, _, _, session = sign_in(httpd, provider)
        assert status == 401 and session is None


def test_unverified_email_with_open_signup_is_email_unverified(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.identity['email_verified'] = False
        status, _, body, session = sign_in(httpd, provider)
        assert status == 403 and session is None
        assert b'Verify your email' in body
        assert _account(httpd, 'ada@example.com') is None
        provider.identity['email_verified'] = 'false'
        assert sign_in(httpd, provider)[0] == 403
        provider.identity['email_verified'] = 'true'    # the string form is accepted
        assert sign_in(httpd, provider)[0] == 200


def test_invite_only_denies_a_stranger(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        status, _, body, session = sign_in(httpd, provider)
        assert status == 403 and session is None
        assert b'no access to this server' in body
        assert _account(httpd, 'ada@example.com') is None


def test_existing_account_claimed_by_verified_email(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        repo = AccountRepo(httpd.db)
        pending = repo.create('ada@example.com')
        MembershipRepo(httpd.db, httpd.default_workspace_id).add(pending['id'], 'member')
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        assert repo.get('ada@example.com')['firebase_uid'] == 'oidc:sub-1'


def _legacy_firebase_account(httpd, login, uid='old-firebase-uid', *, member=True):
    repo = AccountRepo(httpd.db)
    account = repo.create(login)
    if member:
        MembershipRepo(httpd.db, httpd.default_workspace_id).add(account['id'], 'member')
    with httpd.db.transaction(write=True) as tx:
        repo.link_firebase(account['id'], uid, login, True, tx)
    return repo.get(login)


def _bob(provider, verified=True):
    provider.identity = {'sub': 'sub-2', 'email': 'bob@example.com',
                         'email_verified': verified, 'name': 'Bob'}


def test_firebase_to_oidc_switch_relinks_by_verified_email(tmp_path, provider):
    """The server had Firebase; its accounts keep a Firebase uid nobody can use now."""
    with oidc_server(tmp_path / 'data', provider) as httpd:
        _legacy_firebase_account(httpd, 'bob@example.com')
        _bob(provider)
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        account = AccountRepo(httpd.db).get('bob@example.com')
        assert account['firebase_uid'] == 'oidc:sub-2'
        assert account['email'] == 'bob@example.com' and account['email_verified'] == 1
        events = AuditRepo(httpd.db, None).tail(20)
        relinks = [row for row in events if row['action'] == 'account.relink']
        assert len(relinks) == 1 and relinks[0]['target'] == 'bob@example.com'
        assert relinks[0]['detail'] == {'from': 'firebase', 'to': 'oidc'}
        # And from then on the uid itself signs in, with no second relink.
        assert sign_in(httpd, provider)[0] == 200
        assert len([r for r in AuditRepo(httpd.db, None).tail(40)
                    if r['action'] == 'account.relink']) == 1


@pytest.mark.parametrize('verified', [False, 'false'])
def test_no_relink_for_an_unverified_email(tmp_path, provider, verified):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        _legacy_firebase_account(httpd, 'bob@example.com')
        _bob(provider, verified)
        status, _, _, session = sign_in(httpd, provider)
        assert status == 403 and session is None
        assert AccountRepo(httpd.db).get('bob@example.com')['firebase_uid'] == 'old-firebase-uid'
        assert not [r for r in AuditRepo(httpd.db, None).tail(20) if r['action'] == 'account.relink']


def test_no_relink_of_an_account_linked_to_another_oidc_uid(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        _legacy_firebase_account(httpd, 'bob@example.com', uid='oidc:someone-else')
        _bob(provider)
        status, _, _, session = sign_in(httpd, provider)
        assert status == 403 and session is None
        assert AccountRepo(httpd.db).get('bob@example.com')['firebase_uid'] == 'oidc:someone-else'
        assert not [r for r in AuditRepo(httpd.db, None).tail(20) if r['action'] == 'account.relink']


def _invite_then_legacy_account(httpd, login, uid):
    """The invite exists BEFORE the account (InviteRepo.create adds an existing account
    straight to the workspace and returns no token), then the account appears with no
    membership, linked to ``uid``: the redeem path itself runs at sign-in."""
    token = InviteRepo(httpd.db, httpd.default_workspace_id).create(login, 'member', 'admin')
    assert token, 'an invite for an account that does not exist yet must return a token'
    repo = AccountRepo(httpd.db)
    account = repo.create(login)
    with httpd.db.transaction(write=True) as tx:
        repo.link_firebase(account['id'], uid, login, True, tx)
    assert InviteRepo.find_pending(httpd.db, token) is not None
    return token


def _relink_rows(httpd):
    return [row for row in AuditRepo(httpd.db, None).tail(50) if row['action'] == 'account.relink']


def test_relink_also_applies_when_redeeming_an_invite(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        token = _invite_then_legacy_account(httpd, 'bob@example.com', 'old-firebase-uid')
        _bob(provider)
        status, _, _, session = sign_in(httpd, provider, invite=token)
        assert status == 200 and session
        account = AccountRepo(httpd.db).get('bob@example.com')
        assert account['firebase_uid'] == 'oidc:sub-2'
        assert MembershipRepo(httpd.db, httpd.default_workspace_id).get(account['id']) is not None
        assert InviteRepo.find_pending(httpd.db, token) is None          # redeemed
        rows = _relink_rows(httpd)
        assert len(rows) == 1 and rows[0]['detail'] == {'from': 'firebase', 'to': 'oidc'}


def test_invite_redeem_leaves_another_oidc_uid_alone(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        token = _invite_then_legacy_account(httpd, 'bob@example.com', 'oidc:someone-else')
        _bob(provider)
        status, _, _, session = sign_in(httpd, provider, invite=token)
        assert status == 403 and session is None
        assert AccountRepo(httpd.db).get('bob@example.com')['firebase_uid'] == 'oidc:someone-else'
        assert not _relink_rows(httpd)
        assert InviteRepo.find_pending(httpd.db, token) is not None


def test_firebase_server_never_relinks_on_invite_redeem(tmp_path):
    from test_server_firebase import _live_token, _post_firebase, firebase_running
    with firebase_running(tmp_path / 'data') as httpd:
        token = _invite_then_legacy_account(httpd, 'bob@example.com', 'uid-a')
        status, headers, body = _post_firebase(
            httpd, _live_token(sub='uid-b', email='bob@example.com'), invite=token)
        assert status == 403
        assert not headers.get('Set-Cookie')                                # no session for uid-b
        repo = AccountRepo(httpd.db)
        assert repo.get('bob@example.com')['firebase_uid'] == 'uid-a'
        assert repo.by_firebase_uid('uid-b') is None
        assert not _relink_rows(httpd)
        assert InviteRepo.find_pending(httpd.db, token) is not None


def test_firebase_servers_never_relink(tmp_path):
    from test_server_firebase import _live_token, _post_firebase, firebase_running
    with firebase_running(tmp_path / 'data') as httpd:
        repo = AccountRepo(httpd.db)
        account = repo.create('bob@example.com')
        MembershipRepo(httpd.db, httpd.default_workspace_id).add(account['id'], 'member')
        with httpd.db.transaction(write=True) as tx:
            repo.link_firebase(account['id'], 'uid-a', 'bob@example.com', True, tx)
        status, _, body = _post_firebase(httpd, _live_token(sub='uid-b', email='bob@example.com'))
        assert status == 403
        assert repo.get('bob@example.com')['firebase_uid'] == 'uid-a'


def test_oidc_uid_cannot_collide_with_a_firebase_uid(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        repo = AccountRepo(httpd.db)
        other = repo.create('someone@example.com')
        with httpd.db.transaction(write=True) as tx:
            repo.link_firebase(other['id'], 'sub-1', 'someone@example.com', True, tx)
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        assert repo.get('ada@example.com')['firebase_uid'] == 'oidc:sub-1'
        assert repo.get('someone@example.com')['firebase_uid'] == 'sub-1'


def test_admin_email_becomes_server_admin_with_workspace(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     admin_emails=frozenset({'ada@example.com'})) as httpd:
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        admins = AccountRepo(httpd.db).server_admins()
        assert [a['login'] for a in admins] == ['ada@example.com']
        assert admins[0]['server_admin_source'] == 'list'
        status, payload = _session(httpd, session)
        assert payload['server_admin'] is True and len(payload['workspaces']) == 1
        events = AuditRepo(httpd.db, None).tail(20)
        assert any(row['action'] == 'account.server_admin'
                   and row['detail'].get('source') == 'admin-emails' for row in events)


def test_admin_email_works_on_invite_only_server_and_needs_verified_email(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider,
                     admin_emails=frozenset({'ada@example.com'})) as httpd:
        provider.identity['email_verified'] = False
        assert sign_in(httpd, provider)[0] == 403
        assert _account(httpd, 'ada@example.com') is None
        provider.identity['email_verified'] = True
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        assert [a['login'] for a in AccountRepo(httpd.db).server_admins()] == ['ada@example.com']
        # Not open sign-up: a second person is still refused.
        provider.identity = {'sub': 'sub-9', 'email': 'eve@example.com',
                             'email_verified': True, 'name': 'Eve'}
        assert sign_in(httpd, provider)[0] == 403


def test_signup_email_domains_restrict_open_signup(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     signup_email_domains=frozenset({'example.com'}),
                     admin_emails=frozenset({'boss@other.org'})) as httpd:
        for sub, email, ok in (
                ('s1', 'Ada@EXAMPLE.com', True),
                ('s2', 'eve@other.org', False),
                ('s3', 'eve@sub.example.com', False),
                ('s4', 'eve@notexample.com', False),
                ('s5', 'eve@example.com.evil.org', False),
                ('s6', 'boss@other.org', True)):      # the admin list is not open sign-up
            provider.identity = {'sub': sub, 'email': email, 'email_verified': True, 'name': 'X'}
            status, _, body, session = sign_in(httpd, provider)
            assert (status == 200) is ok, email
            if not ok:
                assert b'no access to this server' in body
        logins = sorted(a['login'] for a in AccountRepo(httpd.db).list())
        assert logins == ['ada@example.com', 'boss@other.org']


def test_signup_domains_do_not_block_invites(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     signup_email_domains=frozenset({'example.com'})) as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'guest@other.org', 'member', 'admin')
        provider.identity = {'sub': 'g1', 'email': 'guest@other.org',
                             'email_verified': True, 'name': 'Guest'}
        status, _, _, session = sign_in(httpd, provider, invite=invite)
        assert status == 200 and session
        # An invited outsider does not also get a free personal workspace.
        slugs = [w['slug'] for w in AccountRepo(httpd.db).workspaces('guest@other.org')]
        assert slugs == ['default']


def test_signup_domains_apply_to_firebase_too(tmp_path):
    from test_server_firebase import _live_token, _post_firebase, firebase_running
    with firebase_running(tmp_path / 'data', open_signup=True, plans=plans(),
                          signup_email_domains=frozenset({'example.com'})) as httpd:
        status, _, body = _post_firebase(
            httpd, _live_token(sub='f1', email='eve@other.org'))
        assert status == 403 and json.loads(body)['reason'] == 'no_account'
        status, _, body = _post_firebase(
            httpd, _live_token(sub='f2', email='ada@example.com'))
        assert status == 200


def test_invite_redeemed_through_state_and_never_in_provider_url(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'ada@example.com', 'member', 'admin')
        status, headers, _ = post_start(httpd, {'invite': invite, 'next': '/workspaces'})
        assert status == 302
        location = header(headers, 'Location')[0]
        assert invite not in location
        callback = provider.authorize(location)
        name, value = cookie_pair(header(headers, 'Set-Cookie')[0])
        status, _, body = finish_flow(httpd, urlsplit(callback).path + '?' + urlsplit(callback).query,
                                      f'{name}={value}')
        assert status == 200 and b'url=/workspaces"' in body
        assert all(invite not in json.dumps(item) for item in provider.requests)
        assert InviteRepo.find_pending(httpd.db, invite) is None
        account = _account(httpd, 'ada@example.com')
        assert account['firebase_uid'] == 'oidc:sub-1'
        assert MembershipRepo(httpd.db, httpd.default_workspace_id).get(account['id']) is not None


def test_invite_for_another_email_or_unverified_is_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'other@example.com', 'member', 'admin')
        status, _, body, session = sign_in(httpd, provider, invite=invite)
        assert status == 403 and session is None
        assert b'different or unverified email' in body
        assert InviteRepo.find_pending(httpd.db, invite) is not None
        provider.identity = {'sub': 'o1', 'email': 'other@example.com',
                             'email_verified': False, 'name': 'O'}
        assert sign_in(httpd, provider, invite=invite)[0] == 403
        assert InviteRepo.find_pending(httpd.db, invite) is not None


def test_get_start_refuses_an_invite_parameter(tmp_path, provider):
    """An invite token in a GET query would land in access logs: it must be POSTed."""
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        for query in ('?invite=' + 'a' * 43, '?invite=', '?invite=x&next=/'):
            status, headers, body = raw_call(httpd, START_PATH + query)
            assert status == 400 and b'Open your invite link again' in body, query
            assert not header(headers, 'Set-Cookie') and not header(headers, 'Location')
        assert not provider.requests


def test_unknown_or_malformed_invite(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        status, _, body, session = sign_in(httpd, provider, invite='a' * 43)
        assert status == 403 and session is None and b'no longer valid' in body
        assert _account(httpd, 'ada@example.com') is None
        for bad in ('short', 'a' * 200, 'has space' + 'a' * 20, 'x\x00' + 'a' * 20):
            status, _, body = post_start(httpd, {'invite': bad})
            assert status == 400 and b'no longer valid' in body, bad
        from urllib.parse import urlencode
        both = urlencode([('invite', 'a' * 43), ('invite', 'b' * 43)]).encode()
        status, _, _ = raw_call(httpd, START_PATH, 'POST', both, {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': f'http://127.0.0.1:{httpd.server_address[1]}'})
        assert status == 400


def test_post_start_content_type_origin_and_size(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        host = f'127.0.0.1:{httpd.server_address[1]}'
        form = {'Content-Type': 'application/x-www-form-urlencoded'}
        # A plain POST without invite works the same as GET.
        assert post_start(httpd, {'next': '/workspaces'})[0] == 302
        # Wrong content type.
        assert raw_call(httpd, START_PATH, 'POST', b'{}', {
            'Content-Type': 'application/json', 'Origin': f'http://{host}'})[0] == 415
        # A cross-site form (Origin or Sec-Fetch-Site) is refused, even "Origin: null".
        for extra in ({'Origin': 'http://evil.example'}, {'Origin': 'null'},
                      {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            status, headers, body = raw_call(httpd, START_PATH, 'POST', b'next=%2F', {**form, **extra})
            assert status == 403, extra
            assert not header(headers, 'Set-Cookie') and not header(headers, 'Location')
        # Same-origin browser headers are accepted; no browser headers at all too.
        assert raw_call(httpd, START_PATH, 'POST', b'next=%2F', {
            **form, 'Origin': f'http://{host}', 'Sec-Fetch-Site': 'same-origin'})[0] == 302
        assert raw_call(httpd, START_PATH, 'POST', b'next=%2F', form)[0] == 302
        # The body is capped like the other login bodies.
        status, _, _ = raw_call(httpd, START_PATH, 'POST', b'next=' + b'a' * 6000, {
            **form, 'Origin': f'http://{host}'})
        assert status == 413
        assert not provider.token_requests


def test_failed_sign_in_page_keeps_the_invite_for_try_again(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'other@example.com', 'member', 'admin')
        status, _, body, _ = sign_in(httpd, provider, invite=invite)     # wrong email
        assert status == 403
        html = body.decode()
        assert 'method="post"' in html
        assert re.search(r'name="invite" value="%s"\s*/>' % invite, html)   # enabled, escaped token
        # The provider's own error keeps it too (the browser owns this sign-in).
        status, headers, _ = post_start(httpd, {'invite': invite})
        name, value = cookie_pair(header(headers, 'Set-Cookie')[0])
        state = parse_qs(urlsplit(header(headers, 'Location')[0]).query)['state'][0]
        status, _, body = finish_flow(
            httpd, CALLBACK_PATH + '?error=access_denied&state=' + state, f'{name}={value}')
        assert status == 400 and invite in body.decode() and 'method="post"' in body.decode()
        # Without the browser's cookie nothing is shown.
        status, headers, _ = post_start(httpd, {'invite': invite})
        state = parse_qs(urlsplit(header(headers, 'Location')[0]).query)['state'][0]
        status, _, body = finish_flow(httpd, CALLBACK_PATH + '?code=x&state=' + state, None)
        assert status == 400 and invite not in body.decode()
        # A dead invite is not offered again, and a plain sign-in has no invite field.
        status, _, body, _ = sign_in(httpd, provider, invite='z' * 43)
        assert status == 403 and 'z' * 43 not in body.decode()
        provider.identity['email_verified'] = False
        status, _, body, _ = sign_in(httpd, provider)
        assert status == 403 and 'method="get"' in body.decode()


def test_next_is_kept_only_when_same_origin(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        status, _, body, session = sign_in(httpd, provider, query='?next=/workspaces')
        assert status == 200 and 'url=/workspaces"' in body.decode()
        provider.identity['sub'] = 'sub-1'
        for evil in ('https://evil.example/', '//evil.example', '/\\evil.example'):
            status, _, body, session = sign_in(httpd, provider, query='?next=' + evil)
            assert status == 200 and session and 'evil' not in body.decode()
            assert 'url=/"' in body.decode()


def test_next_is_html_escaped_in_the_redirect_page(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        status, _, body, _ = sign_in(httpd, provider, query='?next=/a%22%3E%3Cscript%3E')
        assert status == 200
        assert '<script>' not in body.decode().split('<body')[1]


def test_provider_error_param_shows_generic_sign_in_page(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, headers, _ = raw_call(httpd, START_PATH)
        name, value = cookie_pair(header(headers, 'Set-Cookie')[0])
        status, headers, body = finish_flow(
            httpd, CALLBACK_PATH + '?error=access_denied&error_description=SECRET-DETAIL&state=zzz',
            f'{name}={value}')
        assert status == 400
        assert b'cancelled or refused' in body and b'SECRET-DETAIL' not in body
        assert b'/auth/oidc/start' in body          # the button to try again
        assert not provider.token_requests


def test_provider_down_gives_503_not_500(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.fail = {'discovery': 503}
        status, _, body = raw_call(httpd, START_PATH)
        assert status == 503 and b'temporarily unavailable' in body
        # And a cooldown: a down provider is not asked on every request.
        before = len(provider.requests)
        assert raw_call(httpd, START_PATH)[0] == 503
        assert len(provider.requests) == before


def test_jwks_and_token_endpoint_down_give_503(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.fail = {'jwks': 500}
        status, _, body, session = sign_in(httpd, provider)
        assert status == 503 and session is None and b'temporarily unavailable' in body
        provider.fail = {'token': 500}
        status, _, _, session = sign_in(httpd, provider)
        assert status == 503 and session is None
        assert _account(httpd, 'ada@example.com') is None


def test_token_endpoint_refusal_is_a_failed_sign_in(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        callback, cookie = start_flow(httpd, provider)
        provider.codes.clear()                   # the provider no longer knows the code
        status, _, body = finish_flow(httpd, callback, cookie)
        assert status == 401 and b'Sign-in failed' in body


def test_discovery_issuer_must_equal_configured(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.discovery_issuer = 'https://evil.example/pool'
        status, _, _ = raw_call(httpd, START_PATH)
        assert status == 503


def test_discovery_and_jwks_are_cached(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        sign_in(httpd, provider)
        sign_in(httpd, provider)
        kinds = [kind for kind, _ in provider.requests]
        assert kinds.count('discovery') == 1 and kinds.count('jwks') == 1


def test_rotated_key_refetches_jwks_at_most_once_per_interval(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        sign_in(httpd, provider)
        provider.kid = 'rotated'                  # a token with a kid the JWKS lacks
        for _ in range(3):
            status, _, _, _ = sign_in(httpd, provider)
            assert status == 401
        assert [k for k, _ in provider.requests].count('jwks') == 1


def test_open_signup_requires_a_provider_and_limits(tmp_path, provider):
    with pytest.raises(ValueError, match='requires --firebase-project or --oidc-issuer'):
        make_server(tmp_path / 'a', port=0, open_signup=True, plans=plans())
    config = parse_oidc_config(provider.issuer, CLIENT_ID)
    with pytest.raises(ValueError, match='requires --limits'):
        make_server(tmp_path / 'b', port=0, public_url='http://127.0.0.1:9', oidc=config,
                    open_signup=True)


def test_refuses_to_start_with_firebase_identity_header_or_no_public_url(tmp_path, provider):
    from agenticml.server.firebase import FirebaseConfig
    config = parse_oidc_config(provider.issuer, CLIENT_ID)
    firebase = FirebaseConfig('demo-project', 'key', 'demo-project.firebaseapp.com')
    with pytest.raises(ValueError, match='cannot be combined with Firebase'):
        make_server(tmp_path / 'a', port=0, public_url='http://127.0.0.1:9', oidc=config,
                    firebase=firebase)
    with pytest.raises(ValueError, match='--identity-header'):
        make_server(tmp_path / 'b', port=0, public_url='http://127.0.0.1:9', oidc=config,
                    identity_header='alb', trusted_proxies=('10.0.0.0/8',))
    with pytest.raises(ValueError, match='requires --public-url'):
        make_server(tmp_path / 'c', port=0, oidc=config)
    with pytest.raises(ValueError, match='https:// public URL'):
        make_server(tmp_path / 'd', port=0, public_url='http://aml.example.com', oidc=config)


def test_routes_are_404_without_oidc(tmp_path):
    httpd = make_server(tmp_path / 'data', port=0)
    import threading
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        for path in (START_PATH, CALLBACK_PATH, '/static/oidc-login.js'):
            assert raw_call(httpd, path)[0] in (303, 404), path
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_wrong_method_is_405(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        assert raw_call(httpd, CALLBACK_PATH, 'POST', b'{}', csrf(httpd))[0] == 405
        assert raw_call(httpd, START_PATH, 'DELETE', None, csrf(httpd))[0] == 405
        assert raw_call(httpd, START_PATH, 'PUT', b'{}', csrf(httpd))[0] == 405


def test_login_invite_pages_and_password_routes(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, label='Company <SSO>') as httpd:
        for path in ('/login', '/invite'):
            status, headers, body = raw_call(httpd, path)
            html = body.decode()
            assert status == 200, path
            assert 'action="/auth/oidc/start"' in html and 'Company &lt;SSO&gt;' in html
            assert 'method="get"' in html          # a plain GET until an invite is carried
            assert re.search(r'name="invite"[^>]*disabled', html)
            assert '/static/oidc-login.js' in html
            assert 'id="key"' not in html and 'password' not in html.lower().split('<main')[1]
            assert header(headers, 'Referrer-Policy') == ['same-origin']
            assert "script-src 'self'" in header(headers, 'Content-Security-Policy')[0]
            assert 'connect-src' not in header(headers, 'Content-Security-Policy')[0]
        assert raw_call(httpd, '/static/oidc-login.js')[0] == 200
        assert raw_call(httpd, '/static/firebase-login.js')[0] != 200
        # No password, key or setup sign-in on an OIDC server.
        assert raw_call(httpd, '/setup')[0] == 404
        status, _, _ = call(httpd, '/login', 'POST', b'{"key":"x"}', csrf(httpd))
        assert status == 404
        assert call(httpd, '/auth/firebase', 'POST', b'{}', csrf(httpd))[0] != 200


def test_default_label(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        html = raw_call(httpd, '/login')[2].decode()
        assert re.search(r'id="signin"[^>]*>Sign in</button>', html)
        assert '{{' not in html


def test_logout_without_logout_url_only_ends_the_session(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        name, token = session.split('=', 1)
        status, headers, body = raw_call(httpd, '/logout', 'POST', b'', csrf(
            httpd, Cookie=session))
        assert status == 204 and body == b''
        assert _session(httpd, session)[0] == 401


def test_logout_url_redirects_through_the_provider(tmp_path, provider):
    logout = 'https://pool.auth.us-east-2.amazoncognito.com/logout'
    with oidc_server(tmp_path / 'data', provider, open_signup=True, logout_url=logout) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        status, headers, body = raw_call(httpd, '/logout', 'POST', b'', csrf(
            httpd, Cookie=session))
        assert status == 200
        target = json.loads(body)['redirect']
        parts = urlsplit(target)
        assert f'{parts.scheme}://{parts.netloc}{parts.path}' == logout
        query = parse_qs(parts.query)
        assert query['client_id'] == [CLIENT_ID]
        assert query['logout_uri'] == [provider.public_url + '/login']
        assert 'Max-Age=0' in header(headers, 'Set-Cookie')[0]
        assert _session(httpd, session)[0] == 401
        # Without a session there is nothing to redirect for.
        status, _, _ = raw_call(httpd, '/logout', 'POST', b'', csrf(httpd))
        assert status == 204


def test_whoami_surfaces_show_oidc(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     admin_emails=frozenset({'ada@example.com'})) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        status, _, body, _ = authed(httpd, '/api/admin/server-admins', cookie=session)
        assert status == 200
        rows = json.loads(body)['server_admins']
        assert rows[0]['signin'] == 'oidc' and rows[0]['pending'] is False


def test_member_reinvite_refused_like_on_firebase(tmp_path, provider):
    """24.11c on OIDC: a re-invite is a password-reset token the OIDC invite page
    never accepts, and it would sign the member out, so the server refuses it."""
    from agenticml.server.db.workspaces import WorkspaceRepo
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        slug = AccountRepo(httpd.db).workspaces('ada@example.com')[0]['slug']
        workspace_id = WorkspaceRepo(httpd.db).get(slug)['id']
        member = AccountRepo(httpd.db).create('bob@example.com')
        MembershipRepo(httpd.db, workspace_id).add(member['id'], 'member')
        generation = _account(httpd, 'bob@example.com')['generation']
        status, _, body, _ = authed(
            httpd, f'/api/w/{slug}/members/bob@example.com/invite', 'POST', b'{}', cookie=session)
        assert status == 409 and b'identity provider' in body
        assert _account(httpd, 'bob@example.com')['generation'] == generation


def test_new_member_invite_link_uses_the_fragment_form(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        slug = AccountRepo(httpd.db).workspaces('ada@example.com')[0]['slug']
        status, _, body, _ = authed(
            httpd, f'/api/w/{slug}/invites', 'POST',
            json.dumps({'login': 'new@example.com', 'role': 'member'}).encode(), cookie=session)
        assert status == 201, body
        assert '/invite#token=' in json.loads(body)['invite_url']


def test_open_signup_workspace_creation_still_works(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        status, payload = _session(httpd, session)
        assert payload['open_signup'] is True


def test_accounts_delete_hint_for_oidc_users(tmp_path, provider, capsys):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(
            'ada@example.com', 'member', 'admin')
        assert sign_in(httpd, provider, invite=invite)[0] == 200
    capsys.readouterr()
    code = cli.main(['server', 'accounts', 'delete', 'ada@example.com',
                     '--yes-delete', 'ada@example.com', '--data', str(tmp_path / 'data')])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert 'delete the user in your identity provider' in captured.out
    assert 'Firebase' not in captured.out


def test_accounts_list_marks_oidc_accounts(tmp_path, provider, capsys):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        sign_in(httpd, provider)
    capsys.readouterr()
    assert cli.main(['server', 'accounts', 'list', '--data', str(tmp_path / 'data')]) == 0
    row = [line for line in capsys.readouterr().out.splitlines() if 'ada@example.com' in line][0]
    assert 'oidc' in row


# --- CLI options -------------------------------------------------------------------

def _serve_args(monkeypatch, argv):
    seen = {}

    def fake_serve(*args, **kwargs):
        seen['args'], seen['kwargs'] = args, kwargs

    monkeypatch.setattr('agenticml.server.serve', fake_serve)
    return seen, cli.main(['server', 'serve', *argv])


def test_serve_options_and_environment(monkeypatch, tmp_path, provider):
    argv = ['--data', str(tmp_path), '--public-url', 'https://aml.example.com',
            '--oidc-issuer', ISSUER, '--oidc-client-id', 'cid',
            '--oidc-logout-url', 'https://p.example.com/logout', '--oidc-label', 'Acme',
            '--signup-email-domains', 'Example.com,@acme.io']
    seen, code = _serve_args(monkeypatch, argv)
    assert code == 0
    kwargs = seen['kwargs']
    assert kwargs['oidc'] == OidcConfig(ISSUER, 'cid', 'https://p.example.com/logout', 'Acme')
    assert kwargs['signup_email_domains'] == frozenset({'example.com', 'acme.io'})
    monkeypatch.setenv('AGENTICML_SERVER_OIDC_ISSUER', ISSUER)
    monkeypatch.setenv('AGENTICML_SERVER_OIDC_CLIENT_ID', 'env-cid')
    monkeypatch.setenv('AGENTICML_SERVER_OIDC_LOGOUT_URL', 'https://p.example.com/out')
    monkeypatch.setenv('AGENTICML_SERVER_OIDC_LABEL', 'Env label')
    monkeypatch.setenv('AGENTICML_SERVER_SIGNUP_EMAIL_DOMAINS', 'env.example')
    seen, code = _serve_args(monkeypatch, ['--data', str(tmp_path)])
    assert code == 0
    assert seen['kwargs']['oidc'] == OidcConfig(ISSUER, 'env-cid', 'https://p.example.com/out', 'Env label')
    assert seen['kwargs']['signup_email_domains'] == frozenset({'env.example'})


def test_serve_refuses_bad_combinations(monkeypatch, tmp_path, capsys):
    base = ['--data', str(tmp_path), '--public-url', 'https://aml.example.com']
    seen, code = _serve_args(monkeypatch, base + ['--oidc-issuer', ISSUER])
    assert code == 1 and 'client id is required' in capsys.readouterr().err
    seen, code = _serve_args(monkeypatch, base + [
        '--oidc-issuer', ISSUER, '--oidc-client-id', 'c', '--firebase-project', 'demo-project',
        '--firebase-api-key', 'k'])
    assert code == 1 and 'cannot be combined with Firebase' in capsys.readouterr().err
    seen, code = _serve_args(monkeypatch, base + ['--signup-email-domains', 'not a domain'])
    assert code == 1 and 'signup-email-domains' in capsys.readouterr().err
    seen, code = _serve_args(monkeypatch, base + ['--open-signup'])
    assert code == 1 and 'requires --firebase-project or --oidc-issuer' in capsys.readouterr().err


def test_admin_emails_warning_names_both_providers(tmp_path, caplog):
    caplog.set_level('WARNING')
    httpd = make_server(tmp_path / 'data', port=0, admin_emails=frozenset({'a@example.com'}))
    httpd.server_close()
    assert '--admin-emails is ignored without --firebase-project' in caplog.text
    assert '--oidc-issuer' in caplog.text


def test_systemd_unit_carries_the_oidc_options(tmp_path):
    from agenticml.server.up import write_launchd_plist, write_systemd_unit
    import plistlib
    config = OidcConfig(ISSUER, 'cid', 'https://p.example.com/logout', 'Acme SSO')
    path = write_systemd_unit(
        tmp_path / 'data', 8080, home=tmp_path, oidc=config, open_signup=True,
        limits=str(tmp_path / 'limits.json'), signup_email_domains=frozenset({'b.io', 'a.io'}),
        admin_emails=frozenset({'x@a.io'}))
    unit = path.read_text()
    for part in (f'--oidc-issuer {ISSUER}', '--oidc-client-id cid',
                 '--oidc-logout-url https://p.example.com/logout', '--oidc-label "Acme SSO"',
                 '--signup-email-domains a.io,b.io', '--open-signup'):
        assert part in unit, part
    plist = write_launchd_plist(
        tmp_path / 'data', 8080, home=tmp_path, oidc=config,
        signup_email_domains=frozenset({'a.io'}))
    args = plistlib.loads(plist.read_bytes())['ProgramArguments']
    assert args[args.index('--oidc-issuer') + 1] == ISSUER
    assert args[args.index('--signup-email-domains') + 1] == 'a.io'


# --- review round: email claim, start limits, per-state cookies ------------------------

@pytest.mark.parametrize('email', ['alice', 'a b@example.com', 'x@y', '@example.com',
                                   'a' * 250 + '@example.com', 'two@@example.com'])
def test_email_claim_must_be_an_address(email):
    claims = _verify(_token(email=email))
    assert claims.email is None and claims.uid == 'oidc:sub-1'


def test_non_address_email_cannot_create_an_account_or_match_the_admin_list(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     admin_emails=frozenset({'ada@example.com'})) as httpd:
        provider.identity = {'sub': 'sub-a', 'email': 'alice', 'email_verified': True, 'name': 'A'}
        status, _, body, session = sign_in(httpd, provider)
        assert status == 403 and session is None and b'Verify your email' in body
        assert AccountRepo(httpd.db).list() == []
        assert AccountRepo(httpd.db).server_admins() == []


def test_start_limiter_unit():
    from agenticml.server.oidc import StartLimiter
    limiter = StartLimiter(limit=3, window=60)
    assert [limiter.allow('a', 100 + i)[0] for i in range(4)] == [True, True, True, False]
    allowed, wait = limiter.allow('a', 110)
    assert not allowed and 1 <= wait <= 60
    assert limiter.allow('b', 110)[0]
    assert limiter.allow('a', 161)[0]                # the window rolled
    for index in range(50):                          # idle keys are dropped on a later call
        limiter.allow(f'k{index}', 200)
    limiter.allow('z', 400)
    assert len(limiter._hits) <= 2


def test_start_is_limited_per_address(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     trusted_proxies=('127.0.0.1',)) as httpd:
        mine = {'X-Forwarded-For': '203.0.113.7'}
        other = {'X-Forwarded-For': '203.0.113.99'}
        # A legitimate sign-in is pending while someone hammers /start.
        callback, cookie = start_flow(httpd, provider, headers=other)
        statuses = [raw_call(httpd, START_PATH, headers=mine)[0] for _ in range(32)]
        assert statuses[:30] == [302] * 30 and statuses[30:] == [429, 429]
        status, headers, body = raw_call(httpd, START_PATH, headers=mine)
        assert status == 429 and int(header(headers, 'Retry-After')[0]) >= 1
        assert b'Too many sign-in attempts' in body
        assert not header(headers, 'Set-Cookie') and not header(headers, 'Location')
        assert post_start(httpd, {'next': '/'}, headers=mine)[0] == 429     # POST counts too
        assert raw_call(httpd, START_PATH, headers=other)[0] == 302         # others unaffected
        # The pending state survived the burst.
        status, _, _ = finish_flow(httpd, callback, cookie)
        assert status == 200


def test_start_limit_rolls_over_with_the_clock(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        real = httpd.web.clock
        assert [raw_call(httpd, START_PATH)[0] for _ in range(31)][-1] == 429
        httpd.web.clock = lambda: real() + 61
        assert raw_call(httpd, START_PATH)[0] == 302


def test_state_purge_is_not_run_on_every_start(tmp_path, provider, monkeypatch):
    with oidc_server(tmp_path / 'data', provider) as httpd:
        states = OidcStates(httpd.db)
        purges = []
        original = states._purge
        monkeypatch.setattr(states, '_purge', lambda tx, now: (purges.append(now), original(tx, now)))
        for index in range(6):
            states.create(f's{index}', 'b', 'n', 'v', None, '/', now=1000 + index)
        assert len(purges) == 1                       # the first create, then not again
        monkeypatch.setattr('agenticml.server.oidc.STATE_CAP', 3)
        states.create('s-over', 'b', 'n', 'v', None, '/', now=1010)
        assert len(purges) == 2                       # the table is at its cap
        monkeypatch.setattr('agenticml.server.oidc.PURGE_INTERVAL', -1)
        states.create('s-timed', 'b', 'n', 'v', None, '/', now=1011)
        assert len(purges) == 3                       # the periodic purge


def _slots(monkeypatch, *slots):
    """Make the next sign-ins (in start order) land in the given binding cookie slots."""
    queue, seen = list(slots), {}

    def slot(state):
        if state not in seen:
            seen[state] = queue.pop(0)
        return seen[state]

    monkeypatch.setattr('agenticml.server.oidc.binding_slot', slot)


def test_two_sign_ins_in_different_slots_do_not_clobber_each_other(tmp_path, provider, monkeypatch):
    """Two tabs, a double click, Back: sign-ins in different slots keep their own cookie."""
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _slots(monkeypatch, 1, 2)
        first, cookie_first = start_flow(httpd, provider)
        second, cookie_second = start_flow(httpd, provider)
        assert cookie_first.split('=')[0] == 'aml_oidc_1'
        assert cookie_second.split('=')[0] == 'aml_oidc_2'
        jar = f'{cookie_first}; {cookie_second}'
        status, headers, _ = finish_flow(httpd, first, jar)        # the FIRST completes
        assert status == 200
        cleared = [c for c in header(headers, 'Set-Cookie') if c.startswith('aml_oidc_')]
        assert [cookie_pair(c)[0] for c in cleared] == ['aml_oidc_1']
        # The second one is still good.
        status, _, _ = finish_flow(httpd, second, jar)
        assert status == 200


def test_a_slot_collision_fails_the_older_sign_in_only(tmp_path, provider, monkeypatch):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _slots(monkeypatch, 3, 3)
        first, cookie_first = start_flow(httpd, provider)
        second, cookie_second = start_flow(httpd, provider)
        assert cookie_first.split('=')[0] == cookie_second.split('=')[0] == 'aml_oidc_3'
        # The browser keeps the newer cookie value under the shared name.
        assert finish_flow(httpd, first, cookie_second)[0] == 400
        assert finish_flow(httpd, second, cookie_second)[0] == 200


def test_many_starts_leave_a_bounded_number_of_cookies(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, trusted_proxies=('127.0.0.1',)) as httpd:
        jar = {}
        for index in range(100):
            status, headers, _ = raw_call(httpd, START_PATH, headers={
                'X-Forwarded-For': f'203.0.113.{index}'})
            assert status == 302
            name, value = cookie_pair(header(headers, 'Set-Cookie')[0])
            jar[name] = value                      # a browser keeps one cookie per name
        assert 1 < len(jar) <= 8
        assert all(re.fullmatch(r'aml_oidc_[0-7]', name) for name in jar)


def test_cookie_of_one_sign_in_does_not_bind_another(tmp_path, provider, monkeypatch):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _slots(monkeypatch, 1, 2)
        first, cookie_first = start_flow(httpd, provider)
        second, cookie_second = start_flow(httpd, provider)
        # The cookie VALUE of the first under the second's NAME is not accepted.
        forged = cookie_second.split('=')[0] + '=' + cookie_first.split('=', 1)[1]
        status, _, _ = finish_flow(httpd, second, forged)
        assert status == 400


# --- review round: unverified emails, domains, secrets in logs ---------------------------

@pytest.mark.parametrize('verified', [False, 'false'])
@pytest.mark.parametrize('open_signup', [True, False])
def test_unverified_email_cannot_claim_an_existing_account(tmp_path, provider, verified, open_signup):
    with oidc_server(tmp_path / 'data', provider, open_signup=open_signup) as httpd:
        repo = AccountRepo(httpd.db)
        account = repo.create('ada@example.com')
        MembershipRepo(httpd.db, httpd.default_workspace_id).add(account['id'], 'member')
        provider.identity['email_verified'] = verified
        status, _, body, session = sign_in(httpd, provider)
        assert status == 403 and session is None
        assert repo.get('ada@example.com')['firebase_uid'] is None
        assert repo.get('ada@example.com')['email_verified'] == 0


def _workspace_slugs(httpd, login):
    return [w['slug'] for w in AccountRepo(httpd.db).workspaces(login)]


def test_signup_domains_and_existing_accounts_personal_workspace(tmp_path, provider):
    """Accounts that exist but belong to no workspace (an account only gets the free
    personal workspace when it has no active membership)."""
    with oidc_server(tmp_path / 'data', provider, open_signup=True,
                     signup_email_domains=frozenset({'example.com'}),
                     admin_emails=frozenset({'boss@other.org'})) as httpd:
        repo = AccountRepo(httpd.db)
        for login in ('guest@other.org', 'ada@example.com', 'boss@other.org'):
            repo.create(login)
        # Out of domain: still signs in (200), but gets no personal workspace.
        provider.identity = {'sub': 'g', 'email': 'guest@other.org', 'email_verified': True, 'name': 'G'}
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session
        assert _workspace_slugs(httpd, 'guest@other.org') == []
        assert repo.get('guest@other.org')['firebase_uid'] == 'oidc:g'
        # In domain: gets one.
        provider.identity = {'sub': 'a', 'email': 'ada@example.com', 'email_verified': True, 'name': 'A'}
        assert sign_in(httpd, provider)[0] == 200
        assert len(_workspace_slugs(httpd, 'ada@example.com')) == 1
        # An admin-list email gets one whatever its domain.
        provider.identity = {'sub': 'b', 'email': 'boss@other.org', 'email_verified': True, 'name': 'B'}
        assert sign_in(httpd, provider)[0] == 200
        assert len(_workspace_slugs(httpd, 'boss@other.org')) == 1
        # The guest signing in again is still without one.
        provider.identity = {'sub': 'g', 'email': 'guest@other.org', 'email_verified': True, 'name': 'G'}
        assert sign_in(httpd, provider)[0] == 200
        assert _workspace_slugs(httpd, 'guest@other.org') == []


def test_secrets_never_reach_the_logs(tmp_path, provider, caplog):
    caplog.set_level('DEBUG')
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        _, _, _, session = sign_in(httpd, provider)
        # Failures log too: a bad nonce, a refused token, a down provider, an error callback.
        provider.claims = {'nonce': 'attacker-nonce'}
        sign_in(httpd, provider)
        provider.claims = {}
        provider.alg = 'HS256'
        sign_in(httpd, provider)
        provider.alg = 'RS256'
        callback, cookie = start_flow(httpd, provider)
        finish_flow(httpd, callback, None)
        provider.fail = {'token': 500}
        sign_in(httpd, provider)
    issued = provider.issued
    assert issued['id_token'] and issued['code'] and issued['code_verifier'] and issued['state']
    secrets_seen = [value for values in issued.values() for value in values if value]
    token_value = session.split('=', 1)[1]
    secrets_seen += [token_value] + [t.split('.')[1] for t in issued['id_token']]
    assert len(secrets_seen) > 10
    for value in secrets_seen:
        assert value not in caplog.text, value[:12]


# --- review round: provider hardening ------------------------------------------------------

def test_discovery_endpoint_over_plain_http_is_refused(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.discovery_extra = {'token_endpoint': 'http://idp.example.com/oauth2/token'}
        assert raw_call(httpd, START_PATH)[0] == 503
        assert not provider.token_requests


def test_token_endpoint_redirect_is_not_followed(tmp_path, provider):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.fail = {'token': 302}
        status, _, _, session = sign_in(httpd, provider)
        assert status == 503 and session is None
        assert [kind for kind, _ in provider.requests].count('token') == 1


@pytest.mark.parametrize('what', ['discovery', 'jwks'])
def test_oversize_provider_documents_are_refused(tmp_path, provider, what):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.oversize = {what}
        status, _, _, session = sign_in(httpd, provider) if what == 'jwks' else (
            raw_call(httpd, START_PATH)[0], None, None, None)
        assert status == 503 and session is None


def _direct_provider(provider):
    from agenticml.server.oidc import OidcProvider
    return OidcProvider(parse_oidc_config(provider.issuer, CLIENT_ID), 'https://aml.example.com')


def test_max_age_zero_is_clamped_not_uncached_or_forever(provider):
    """max-age=0 would mean a fetch per sign-in; the floor is the same 5 minutes as Firebase."""
    provider.cache_control = 'max-age=0'
    direct = _direct_provider(provider)
    count = lambda: [kind for kind, _ in provider.requests].count('discovery')
    direct.endpoints(now=10_000)
    direct.endpoints(now=10_299)
    assert count() == 1
    direct.endpoints(now=10_300)
    assert count() == 2


def test_unknown_kid_refetches_at_most_once_per_interval(provider):
    direct = _direct_provider(provider)
    jwks = lambda: [kind for kind, _ in provider.requests].count('jwks')
    assert direct._keys.get(KID, now=10_000)['kid'] == KID
    assert jwks() == 1
    for offset in (1, 10, 59):
        with pytest.raises(OidcTokenInvalid, match='unknown kid'):
            direct._keys.get('rotated', now=10_000 + offset)
    assert jwks() == 1
    with pytest.raises(OidcTokenInvalid, match='unknown kid'):
        direct._keys.get('rotated', now=10_061)
    assert jwks() == 2
    with pytest.raises(OidcTokenInvalid, match='unknown kid'):
        direct._keys.get('rotated', now=10_070)
    assert jwks() == 2


def test_hs256_signed_with_the_public_key_pem_is_refused(tmp_path, provider):
    import base64, hashlib, hmac as hmac_module
    from cryptography.hazmat.primitives import serialization
    pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

    head = b64(json.dumps({'alg': 'HS256', 'kid': KID}).encode())
    claims = {'iss': ISSUER, 'aud': CLIENT_ID, 'sub': 'evil', 'iat': NOW, 'exp': NOW + 3600,
              'nonce': 'n-1', 'email': 'ada@example.com', 'email_verified': True}
    body = b64(json.dumps(claims).encode())
    forged = f'{head}.{body}.' + b64(hmac_module.new(pem, f'{head}.{body}'.encode(), hashlib.sha256).digest())
    with pytest.raises(OidcTokenInvalid, match='HS256'):
        _verify(forged)
    # And through the whole flow.
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        provider.alg = 'HS256-PEM'
        status, _, _, session = sign_in(httpd, provider)
        assert status == 401 and session is None
        assert _account(httpd, 'ada@example.com') is None


def test_logout_url_with_a_query_gets_exactly_one_question_mark():
    from agenticml.server.oidc import logout_redirect
    config = OidcConfig(ISSUER, 'cid', 'https://idp.example.com/logout?tenant=1')
    url = logout_redirect(config, 'https://aml.example.com/')
    assert url.count('?') == 1
    query = parse_qs(urlsplit(url).query)
    assert query == {'tenant': ['1'], 'client_id': ['cid'],
                     'logout_uri': ['https://aml.example.com/login']}
    plain = logout_redirect(OidcConfig(ISSUER, 'cid', 'https://idp.example.com/logout'),
                            'https://aml.example.com')
    assert plain.count('?') == 1 and plain.startswith('https://idp.example.com/logout?client_id=cid')


# --- review round: systemd quoting -------------------------------------------------------------

def _systemd_words(line):
    """Split an ExecStart value the way systemd does: (text, was_quoted) per word."""
    words, current, quoted, inside, index = [], None, False, False, 0
    while index < len(line):
        char = line[index]
        if inside:
            if char == '\\':
                index += 1
                char = {'n': '\n', 't': '\t', 'r': '\r'}.get(line[index], line[index])
                current += char
            elif char == '"':
                inside = False
            else:
                current += char
        elif char == '"':
            inside, quoted = True, True
            current = '' if current is None else current
        elif char in ' \t':
            if current is not None:
                words.append((current, quoted))
                current, quoted = None, False
        else:
            current = (current or '') + char
        index += 1
    if current is not None:
        words.append((current, quoted))
    # Unquoted ";" separates commands; "%%" and "$$" are the escapes for "%" and "$".
    assert (';', False) not in words, 'a bare ; would split ExecStart'
    return [(text.replace('%%', '%').replace('$$', '$'), q) for text, q in words]


@pytest.mark.parametrize('label', [';', 'a b', '"x"', '%h', '$HOME', 'a;b', '; ', 'x\\y', "it's"])
def test_systemd_unit_round_trips_the_label_as_one_argument(tmp_path, label):
    from agenticml.server.up import write_launchd_plist, write_systemd_unit
    import plistlib
    config = OidcConfig(ISSUER, 'cid', None, label)
    unit = write_systemd_unit(tmp_path / 'data', 8080, home=tmp_path, oidc=config).read_text()
    exec_line = [line for line in unit.splitlines() if line.startswith('ExecStart=')][0]
    words = [text for text, _ in _systemd_words(exec_line[len('ExecStart='):])]
    assert words[words.index('--oidc-label') + 1] == label
    assert words.count('--oidc-label') == 1
    if '%' in label:
        assert '%%' in exec_line                 # never a raw specifier
    # launchd takes a plist array: one element, no quoting rules at all.
    plist = write_launchd_plist(tmp_path / 'data', 8080, home=tmp_path, oidc=config)
    args = plistlib.loads(plist.read_bytes())['ProgramArguments']
    assert args[args.index('--oidc-label') + 1] == label


# --- round 3: stored email, prefix queries --------------------------------------------------

@pytest.mark.parametrize('second', [
    {'email': 'alice'},                       # not an address
    {'email': OMIT},                          # no email claim at all
    {'email': 'other@example.com'}])          # a verified email that is not ours: kept too
def test_unusable_email_claim_does_not_wipe_the_stored_email(tmp_path, provider, second):
    with oidc_server(tmp_path / 'data', provider, open_signup=True) as httpd:
        assert sign_in(httpd, provider)[0] == 200
        repo = AccountRepo(httpd.db)
        before = repo.get('ada@example.com')
        assert before['email'] == 'ada@example.com' and before['email_verified'] == 1
        provider.claims = dict(second, email_verified=True)
        status, _, _, session = sign_in(httpd, provider)
        assert status == 200 and session                    # the linked uid still signs in
        after = repo.get('ada@example.com')
        if second['email'] == 'other@example.com':
            assert after['email'] == 'other@example.com'     # a real address refreshes as before
        else:
            assert after['email'] == 'ada@example.com' and after['email_verified'] == 1
        assert _session(httpd, session)[0] == 200


def test_dropped_email_is_never_verified():
    for email in ('alice', OMIT, None):
        claims = _verify(_token(email=email, email_verified=True))
        assert claims.email is None and claims.email_verified is False


def _settings_names(httpd):
    with httpd.db.transaction() as tx:
        return sorted(r[0] for r in tx.fetchall('SELECT name FROM server_settings'))


def test_state_queries_match_exactly_the_prefix_rows(tmp_path, provider, monkeypatch):
    """No range query on ``name``: Postgres with a non-C collation would match nothing.
    (Runs on both backends: the Postgres CI job sets AGENTICML_TEST_DATABASE_URL and the
    autouse fixture in conftest.py points make_server at it.)"""
    from agenticml.server.db.repos import SettingsRepo
    with oidc_server(tmp_path / 'data', provider) as httpd:
        others = ['oidc_statement', 'oidc_state;x', 'oidc_stateZ', 'oidc_stat', 'zz', 'Oidc_state:abc']
        settings = SettingsRepo(httpd.db)
        for name in others:
            settings.set(name, 'keep')
        states = OidcStates(httpd.db)
        monkeypatch.setattr('agenticml.server.oidc.STATE_CAP', 4)
        for index in range(6):
            states.create(f's{index}', 'b', 'n', 'v', None, '/', now=1000 + index)
        names = _settings_names(httpd)
        prefixed = [n for n in names if n.startswith('oidc_state:')]
        # The cap held (it counted only the prefix rows), and the oldest were evicted.
        assert len(prefixed) == 4
        assert all(re.fullmatch(r'oidc_state:[0-9a-f]{64}', n) for n in prefixed)
        assert states.consume('s0', 1006) is None and states.consume('s1', 1006) is None
        assert states.consume('s5', 1006) is not None
        # An expired purge removes only prefix rows; every other setting survives.
        states.create('late', 'b', 'n', 'v', None, '/', now=1000 + 10_000)
        monkeypatch.setattr('agenticml.server.oidc.PURGE_INTERVAL', -1)
        states.create('later', 'b', 'n', 'v', None, '/', now=1000 + 10_001)
        names = _settings_names(httpd)
        assert [n for n in names if n.startswith('oidc_state:')].__len__() == 2
        for name in others:
            assert name in names and settings.get(name) == 'keep'
