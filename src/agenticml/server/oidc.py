"""OpenID Connect sign-in: authorization code flow with PKCE, public client.

The server is a relying party for one configured issuer (Amazon Cognito, Okta,
Entra ID, Google Workspace, any standard provider). There is no client secret.
PyJWT and cryptography are imported inside functions so the base install stays
``requests`` only. Never log ID tokens, authorization codes, PKCE verifiers,
state values or cookie values.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import quote, urlsplit

from .keycache import (
    LEEWAY, Claims, KeyCache, KeyUnavailable, TokenInvalid, http_fetch, parse_max_age,
    string_or_none, unix_time)

logger = logging.getLogger('agenticml.server.oidc')

OIDC_EXTRA = ("OIDC sign-in needs the [oidc] extra: "
              "pip install 'agenticml-lite[oidc]'")
OIDC_IDENTITY = "OIDC sign-in cannot be combined with --identity-header"
OIDC_FIREBASE = "OIDC sign-in cannot be combined with Firebase sign-in"
OIDC_PUBLIC_URL = "OIDC sign-in requires --public-url (the callback address)"
START_PATH = '/auth/oidc/start'
CALLBACK_PATH = '/auth/oidc/callback'
SCOPE = 'openid email profile'
DEFAULT_LABEL = 'Sign in'
PROVIDER = 'oidc'
UID_PREFIX = 'oidc:'


def issuer_tag(issuer):
    """Twelve hex digits that name an issuer inside a stored uid."""
    return hashlib.sha256(issuer.encode('utf-8')).hexdigest()[:12]


def uid_for(issuer, sub):
    """The stored uid of an OIDC subject: ``oidc:<issuer tag>:<sub>``.

    The tag keeps accounts of one identity pool apart from those of another,
    so a re-created pool (new subjects) can relink by verified email.
    """
    return f'{UID_PREFIX}{issuer_tag(issuer)}:{sub}'

STATE_TTL = 600
STATE_CAP = 2000
STATE_PREFIX = 'oidc_state:'
# A prefix test that does not depend on the database collation (a range query
# on ``name`` would match nothing on Postgres with a non-C collation).
_PREFIX_TEST = 'substr(name, 1, ?) = ?'
_PREFIX_ARGS = (len(STATE_PREFIX), STATE_PREFIX)
BINDING_SLOTS = 8
INVITE_PATTERN = re.compile(r'[A-Za-z0-9_-]{16,128}')
_LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})
_CONTROL = re.compile(r'[\x00-\x20\x7f]')
_NEXT_MAX = 512
_DISCOVERY = '/.well-known/openid-configuration'


class OidcTokenInvalid(TokenInvalid):
    """The ID token or a token endpoint answer failed verification."""


class OidcUnavailable(KeyUnavailable):
    """Discovery, JWKS or the token endpoint failed; callers map this to 503."""

    def __init__(self, reason='provider unavailable'):
        super().__init__(reason)


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    logout_url: str | None = None
    label: str = DEFAULT_LABEL


def oidc_available():
    try:
        import jwt  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
        return True
    except ImportError:
        return False


def _loopback_http(parts):
    return parts.scheme == 'http' and parts.hostname in _LOOPBACK


def _check_url(value, what, *, allow_query):
    """Require https (http only for loopback), a host, no credentials or fragment."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError(f"{what} must be an https:// URL")
    if _CONTROL.search(value):
        raise ValueError(f"{what} must not contain spaces or control characters")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # raises ValueError for a bad port
    except ValueError:
        raise ValueError(f"{what} is not a valid URL") from None
    if not host or not (parts.scheme == 'https' or _loopback_http(parts)):
        raise ValueError(f"{what} must be an https:// URL")
    if parts.username is not None or parts.password is not None or '#' in value:
        raise ValueError(f"{what} must not have credentials or a fragment")
    if not allow_query and (parts.query or '?' in value):
        raise ValueError(f"{what} must not have a query")
    return value


def parse_oidc_config(issuer, client_id, logout_url=None, label=None):
    """Return ``OidcConfig`` when an issuer is set, else ``None``."""
    if issuer is None or issuer == "":
        if client_id not in (None, "") or logout_url not in (None, ""):
            raise ValueError("OIDC client id and logout URL require --oidc-issuer")
        return None
    issuer = _check_url(issuer.strip() if isinstance(issuer, str) else issuer,
                        "OIDC issuer", allow_query=False)
    if client_id is None or client_id == "":
        raise ValueError("OIDC client id is required with --oidc-issuer")
    if (not isinstance(client_id, str) or not 1 <= len(client_id) <= 256
            or not all(33 <= ord(ch) <= 126 for ch in client_id)):
        raise ValueError("OIDC client id must be printable ASCII without spaces, at most 256 characters")
    if logout_url in (None, ""):
        logout_url = None
    else:
        logout_url = _check_url(logout_url.strip() if isinstance(logout_url, str) else logout_url,
                                "OIDC logout URL", allow_query=True)
    label = DEFAULT_LABEL if label is None or not str(label).strip() else str(label).strip()
    if len(label) > 40 or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        raise ValueError("OIDC button label must be at most 40 characters without control characters")
    if not oidc_available():
        raise ValueError(OIDC_EXTRA)
    return OidcConfig(issuer=issuer, client_id=client_id, logout_url=logout_url, label=label)


def safe_next(value):
    """A same-origin path, or ``/``. Never a URL, never ``//host``."""
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//')
            or len(value) > _NEXT_MAX or '\\' in value or _CONTROL.search(value)):
        return '/'
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return '/'
    return value


def logout_redirect(config, public_url):
    """The provider sign-out URL, with ``client_id`` and ``logout_uri`` added."""
    if config.logout_url is None:
        return None
    sep = '&' if '?' in config.logout_url else '?'
    return (config.logout_url + sep + 'client_id=' + quote(config.client_id, safe='')
            + '&logout_uri=' + quote(public_url.rstrip('/') + '/login', safe=''))


def _sha256(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _jwt():
    try:
        import jwt
        return jwt
    except ImportError:
        raise RuntimeError(OIDC_EXTRA) from None


def _endpoint(doc, name):
    value = doc.get(name)
    try:
        return _check_url(value, name, allow_query=True)
    except ValueError:
        raise OidcUnavailable(f'bad {name}') from None


class _Discovery(KeyCache):
    """The provider's discovery document, cached by its ``max-age``."""

    invalid_error = OidcTokenInvalid
    unavailable_error = OidcUnavailable
    unavailable_reason = 'discovery failed'

    def __init__(self, config, fetch=None):
        super().__init__()
        self._config = config
        self._fetch = fetch

    def _load(self):
        url = self._config.issuer.rstrip('/') + _DISCOVERY
        fetch = self._fetch or (lambda: http_fetch(
            url, error=OidcUnavailable, reason='discovery failed',
            label='oidc discovery failed', headers={'Accept': 'application/json'}))
        response = fetch()
        if getattr(response, 'status_code', None) != 200:
            logger.warning('oidc discovery failed')
            raise OidcUnavailable('discovery failed')
        try:
            doc = response.json()
            if not isinstance(doc, dict):
                raise ValueError('not an object')
        except Exception as exc:
            logger.warning('oidc discovery failed: %s', type(exc).__name__)
            raise OidcUnavailable('discovery failed') from exc
        if doc.get('issuer') != self._config.issuer:
            logger.warning('oidc discovery failed: issuer mismatch')
            raise OidcUnavailable('discovery issuer mismatch')
        algs = doc.get('id_token_signing_alg_values_supported')
        if isinstance(algs, list) and 'RS256' not in algs:
            raise OidcUnavailable('provider does not sign with RS256')
        endpoints = {name: _endpoint(doc, name)
                     for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri')}
        return {'doc': endpoints}, parse_max_age(_cache_control(response))


def _cache_control(response):
    headers = getattr(response, 'headers', None) or {}
    value = headers.get('Cache-Control')
    if value is None and hasattr(headers, 'get'):
        value = headers.get('cache-control')
    return value


class _Jwks(KeyCache):
    """RS256 signing keys by ``kid``, from the discovery document's ``jwks_uri``."""

    invalid_error = OidcTokenInvalid
    unavailable_error = OidcUnavailable
    unavailable_reason = 'key fetch failed'

    def __init__(self, discovery, fetch=None):
        super().__init__()
        self._discovery = discovery
        self._fetch = fetch

    def _load(self):
        url = self._discovery.get('doc')['jwks_uri']
        fetch = self._fetch or (lambda: http_fetch(
            url, error=OidcUnavailable, reason='key fetch failed',
            label='oidc key fetch failed', headers={'Accept': 'application/json'}))
        response = fetch()
        if getattr(response, 'status_code', None) != 200:
            logger.warning('oidc key fetch failed')
            raise OidcUnavailable('key fetch failed')
        try:
            body = response.json()
            entries = body['keys']
            if not isinstance(entries, list):
                raise ValueError('keys is not a list')
        except Exception as exc:
            logger.warning('oidc key fetch failed: %s', type(exc).__name__)
            raise OidcUnavailable('key parse failed') from exc
        keys = {}
        for entry in entries:
            if (isinstance(entry, dict) and entry.get('kty') == 'RSA'
                    and isinstance(entry.get('kid'), str) and entry['kid']
                    and isinstance(entry.get('n'), str) and isinstance(entry.get('e'), str)
                    and entry.get('use') in (None, 'sig') and entry.get('alg') in (None, 'RS256')):
                keys[entry['kid']] = {name: entry[name] for name in ('kty', 'kid', 'n', 'e')}
        if not keys:
            raise OidcUnavailable('no usable signing keys')
        return keys, parse_max_age(_cache_control(response))


def _public_key(jwk):
    jwt = _jwt()
    try:
        key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        if key.key_size < 2048:
            raise ValueError('short key')
        return key
    except Exception as exc:
        raise OidcTokenInvalid('invalid key') from exc


def _verified_flag(value):
    if value is True or value == 'true':
        return True
    return False


def verify_id_token(token, config, *, nonce, now=None, keys=None) -> Claims:
    """Verify an ID token from the configured issuer. ``keys`` is a key source."""
    jwt = _jwt()
    now = time.time() if now is None else now
    if not isinstance(token, (str, bytes)) or not token:
        raise OidcTokenInvalid('malformed')
    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.InvalidTokenError as exc:
        raise OidcTokenInvalid('malformed') from exc
    alg = header.get('alg')
    if alg != 'RS256':
        raise OidcTokenInvalid(f'alg {alg!r} refused')
    kid = header.get('kid')
    if not isinstance(kid, str) or not kid:
        raise OidcTokenInvalid('missing kid')
    jwk = keys.get(kid, now=now)
    try:
        payload = jwt.decode(
            token, _public_key(jwk), algorithms=['RS256'],
            options={'verify_signature': True, 'verify_exp': False, 'verify_nbf': False,
                     'verify_iat': False, 'verify_aud': False, 'require': []})
    except jwt.exceptions.InvalidSignatureError as exc:
        logger.debug('oidc token invalid: signature')
        raise OidcTokenInvalid('signature') from exc
    except jwt.exceptions.DecodeError as exc:
        logger.debug('oidc token invalid: decode')
        raise OidcTokenInvalid('decode') from exc
    except jwt.exceptions.InvalidTokenError as exc:
        logger.debug('oidc token invalid: claim')
        raise OidcTokenInvalid('claim') from exc
    if not isinstance(payload, dict):
        raise OidcTokenInvalid('claim')
    if payload.get('iss') != config.issuer:
        raise OidcTokenInvalid('iss')
    aud = payload.get('aud')
    if isinstance(aud, str):
        if aud != config.client_id:
            raise OidcTokenInvalid('aud')
    elif isinstance(aud, list):
        # Several audiences: ours must be listed and the token must say it was
        # issued to us (azp), so a token minted for another client is refused.
        if config.client_id not in aud or payload.get('azp') != config.client_id:
            raise OidcTokenInvalid('aud')
    else:
        raise OidcTokenInvalid('aud')
    exp = unix_time(payload.get('exp'), 'exp', OidcTokenInvalid)
    iat = unix_time(payload.get('iat'), 'iat', OidcTokenInvalid)
    if exp + LEEWAY <= now:
        raise OidcTokenInvalid('expired')
    if iat > now + LEEWAY:
        raise OidcTokenInvalid('iat')
    token_nonce = payload.get('nonce')
    if (not isinstance(token_nonce, str) or not isinstance(nonce, str) or not nonce
            or not hmac.compare_digest(token_nonce.encode('utf-8'), nonce.encode('utf-8'))):
        raise OidcTokenInvalid('nonce')
    use = payload.get('token_use')
    if use is not None and use != 'id':
        raise OidcTokenInvalid('token_use')
    sub = payload.get('sub')
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise OidcTokenInvalid('sub')
    from .db.workspaces import is_email_login
    email = payload.get('email')
    email = email.lower() if isinstance(email, str) else None
    verified = _verified_flag(payload.get('email_verified'))
    if email is not None and not is_email_login(email):
        # Not an address (no @, spaces, over 254 characters): no email claim at
        # all, so it cannot create an account or match the admin list.
        email = None
    if email is None:
        verified = False
    return Claims(
        uid=uid_for(config.issuer, sub), email=email,
        email_verified=verified,
        name=string_or_none(payload.get('name')), provider=PROVIDER)


class OidcProvider:
    """One configured issuer: discovery, keys, authorization URL and code exchange."""

    def __init__(self, config, public_url, *, discovery_fetch=None, jwks_fetch=None):
        self.config = config
        self.redirect_uri = public_url.rstrip('/') + CALLBACK_PATH
        self.logout_url = logout_redirect(config, public_url)
        self._discovery = _Discovery(config, discovery_fetch)
        self._keys = _Jwks(self._discovery, jwks_fetch)

    def endpoints(self, now=None):
        return self._discovery.get('doc', now=now)

    def authorization_url(self, state, nonce, verifier, now=None):
        query = {
            'response_type': 'code',
            'client_id': self.config.client_id,
            'redirect_uri': self.redirect_uri,
            'scope': SCOPE,
            'state': state,
            'nonce': nonce,
            'code_challenge': pkce_challenge(verifier),
            'code_challenge_method': 'S256',
        }
        base = self.endpoints(now)['authorization_endpoint']
        sep = '&' if '?' in base else '?'
        return base + sep + '&'.join(
            f'{name}={quote(value, safe="")}' for name, value in query.items())

    def exchange_code(self, code, verifier, now=None):
        """POST the code to the token endpoint and return the ``id_token`` string."""
        url = self.endpoints(now)['token_endpoint']
        response = http_fetch(
            url, method='post', error=OidcUnavailable, reason='token endpoint failed',
            label='oidc token request failed', headers={'Accept': 'application/json'},
            data={'grant_type': 'authorization_code', 'code': code,
                  'client_id': self.config.client_id, 'redirect_uri': self.redirect_uri,
                  'code_verifier': verifier})
        status = response.status_code
        if 400 <= status < 500:
            logger.info('oidc token endpoint refused the code')
            raise OidcTokenInvalid('token endpoint refused')
        if status != 200:
            logger.warning('oidc token endpoint failed')
            raise OidcUnavailable('token endpoint failed')
        try:
            body = response.json()
            token = body['id_token']
        except Exception as exc:
            logger.warning('oidc token endpoint failed: %s', type(exc).__name__)
            raise OidcTokenInvalid('token endpoint answer') from exc
        if not isinstance(token, str) or not token:
            raise OidcTokenInvalid('token endpoint answer')
        return token

    def verify(self, token, nonce, now=None):
        return verify_id_token(token, self.config, nonce=nonce, now=now, keys=self._keys)


PURGE_INTERVAL = 30
START_LIMIT = 30
START_WINDOW = 60


def binding_slot(state):
    """Which of ``BINDING_SLOTS`` binding cookies this sign-in uses.

    A few fixed slots, not one cookie per state: a page that keeps starting
    sign-ins cannot make a browser hold more than ``BINDING_SLOTS`` of them. Two
    tabs usually land in different slots; on a collision the older pending
    sign-in fails and starts again.
    """
    return int(_sha256(state)[:12], 16) % BINDING_SLOTS


class StartLimiter:
    """At most ``START_LIMIT`` starts per ``START_WINDOW`` seconds per key (process-local).

    Idle keys are dropped, so the map is bounded by the addresses seen in one window.
    """

    def __init__(self, limit=None, window=None):
        self.limit = START_LIMIT if limit is None else limit
        self.window = START_WINDOW if window is None else window
        self._hits = {}
        self._lock = threading.Lock()
        self._swept = 0.0

    def allow(self, key, now):
        """``(True, 0)`` or ``(False, seconds to wait)``."""
        with self._lock:
            if now - self._swept >= self.window:
                self._swept = now
                for name in [n for n, hits in self._hits.items()
                             if not hits or now - hits[-1] >= self.window]:
                    del self._hits[name]
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False, max(1, int(self.window - (now - hits[0])) + 1)
            hits.append(now)
            self._hits[key] = hits
            return True, 0


class OidcStates:
    """Pending sign-ins: ``state`` -> nonce, PKCE verifier, invite, next, cookie binding.

    Rows live in ``server_settings`` under a hash of ``state`` (never the state
    itself), expire after ``STATE_TTL`` and are consumed once. Oldest rows go
    first above ``STATE_CAP`` so anonymous ``/auth/oidc/start`` calls cannot
    grow the table without bound. Expired rows are purged when the table holds
    ``STATE_CAP`` rows or at most every ``PURGE_INTERVAL`` seconds per process,
    not on every start.
    """

    def __init__(self, db):
        self.db = db
        self._last_purge = None

    @staticmethod
    def _name(state):
        return STATE_PREFIX + _sha256(state)

    @staticmethod
    def _decode(value):
        try:
            record = json.loads(value)
        except (TypeError, ValueError, RecursionError):
            return None
        return record if isinstance(record, dict) else None

    def create(self, state, binding, nonce, verifier, invite, next_path, now):
        record = {'binding': _sha256(binding), 'nonce': nonce, 'verifier': verifier,
                  'invite': invite, 'next': next_path, 'created': now,
                  'expires': now + STATE_TTL}
        tick = time.monotonic()
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('oidc_state')
            count = tx.fetchone(
                'SELECT COUNT(*) FROM server_settings WHERE ' + _PREFIX_TEST, _PREFIX_ARGS)[0]
            if count >= STATE_CAP or self._last_purge is None or tick - self._last_purge >= PURGE_INTERVAL:
                self._purge(tx, now)
                self._last_purge = tick
            tx.execute(
                self.db.dialect.upsert('server_settings', ('name', 'value'), ('name',), ('value',)),
                (self._name(state), json.dumps(record)))

    def _purge(self, tx, now):
        rows = tx.fetchall(
            'SELECT name, value FROM server_settings WHERE ' + _PREFIX_TEST, _PREFIX_ARGS)
        live = []
        for row in rows:
            stored = self._decode(row[1])
            if stored is None or not isinstance(stored.get('expires'), (int, float)) \
                    or stored['expires'] <= now:
                tx.execute('DELETE FROM server_settings WHERE name=?', (row[0],))
            else:
                live.append((stored.get('created', 0), row[0]))
        live.sort()
        for _, name in live[:max(0, len(live) - STATE_CAP + 1)]:
            tx.execute('DELETE FROM server_settings WHERE name=?', (name,))

    def consume(self, state, now):
        """The stored record for ``state`` once, or ``None`` (unknown, used, expired)."""
        if not isinstance(state, str) or not state or len(state) > 256 or not state.isascii():
            return None
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('oidc_state')
            name = self._name(state)
            row = tx.fetchone('SELECT value FROM server_settings WHERE name=?', (name,))
            if row is None:
                return None
            tx.execute('DELETE FROM server_settings WHERE name=?', (name,))
        record = self._decode(row[0])
        if record is None or not isinstance(record.get('expires'), (int, float)) \
                or record['expires'] <= now:
            return None
        return record

    @staticmethod
    def binding_matches(record, cookie):
        if not isinstance(cookie, str) or not cookie or not cookie.isascii():
            return False
        return hmac.compare_digest(record.get('binding', ''), _sha256(cookie))


def new_secret(nbytes=32):
    return secrets.token_urlsafe(nbytes)

