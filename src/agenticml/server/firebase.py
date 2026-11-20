"""Firebase ID token verification for hosted sign-in.

PyJWT is imported inside functions so the base install stays `requests` only.
Never log ID tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import threading
import time

from . import keycache
from .keycache import (  # noqa: F401  (re-exported: other modules and tests import them here)
    BODY_DEADLINE, FAILURE_COOLDOWN, HTTP_TIMEOUT, LEEWAY, MAX_MAX_AGE, MIN_MAX_AGE,
    UNKNOWN_KID_INTERVAL, WAITER_TIMEOUT, Claims, KeyCache, KeyUnavailable, TokenInvalid)

logger = logging.getLogger('agenticml.server.firebase')

CERTS_URL = ('https://www.googleapis.com/robot/v1/metadata/x509/'
             'securetoken@system.gserviceaccount.com')
FIREBASE_EXTRA = ("Firebase sign-in needs the [firebase] extra: "
                  "pip install 'agenticml-lite[firebase]'")
FIREBASE_IDENTITY = "Firebase sign-in cannot be combined with --identity-header"
OPEN_SIGNUP_REQUIRES_FIREBASE = "open sign-up requires --firebase-project or --oidc-issuer"
OPEN_SIGNUP_REQUIRES_LIMITS = "open sign-up requires --limits"


def require_open_signup(open_signup, firebase=None, plans=None, oidc=None):
    if not open_signup:
        return
    if firebase is None and oidc is None:
        raise ValueError(OPEN_SIGNUP_REQUIRES_FIREBASE)
    if plans is None:
        raise ValueError(OPEN_SIGNUP_REQUIRES_LIMITS)


AUTH_MAX_AGE = 600
MAX_CERTS_BODY = keycache.MAX_BODY
TRUSTED_EMAIL_PROVIDERS = frozenset({'google.com', 'password'})
# The OIDC provider (``oidc.py``) is a trust root of its own: the configured
# issuer vouches for ``email_verified``.
OIDC_PROVIDER = 'oidc'
_PROJECT_ID = re.compile(r'[a-z0-9-]{4,40}')
_HOSTNAME = re.compile(
    r'(?=.{1,253}$)'
    r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+'
    r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    r'|localhost'
)

_default_source = None
_default_lock = threading.Lock()


class FirebaseTokenInvalid(TokenInvalid):
    """Token failed verification. ``reason`` is for the server log only."""


class FirebaseUnavailable(KeyUnavailable):
    """Certificate fetch or parse failed; callers map this to 503."""

    def __init__(self, reason='certificate fetch failed'):
        super().__init__(reason)


@dataclass(frozen=True)
class FirebaseConfig:
    project: str
    api_key: str
    auth_domain: str


def firebase_available():
    try:
        import jwt  # noqa: F401
        return True
    except ImportError:
        return False


def _printable_ascii(value, limit):
    return (isinstance(value, str) and 1 <= len(value) <= limit
            and all(32 <= ord(ch) <= 126 for ch in value))


def parse_firebase_config(project, api_key, auth_domain):
    """Return ``FirebaseConfig`` when a project id is set, else ``None``."""
    if project is None or project == "":
        if api_key not in (None, "") or auth_domain not in (None, ""):
            raise ValueError("Firebase API key and auth domain require --firebase-project")
        return None
    if not isinstance(project, str) or not _PROJECT_ID.fullmatch(project):
        raise ValueError("Firebase project id must be 4–40 characters [a-z0-9-]")
    if api_key is None or api_key == "":
        raise ValueError("Firebase API key is required with --firebase-project")
    if not _printable_ascii(api_key, 100):
        raise ValueError("Firebase API key must be printable ASCII of at most 100 characters")
    if auth_domain is None or auth_domain == "":
        auth_domain = f"{project}.firebaseapp.com"
    if not isinstance(auth_domain, str) or not _HOSTNAME.fullmatch(auth_domain):
        raise ValueError("Firebase auth domain must be a hostname")
    if not firebase_available():
        raise ValueError(FIREBASE_EXTRA)
    return FirebaseConfig(project=project, api_key=api_key, auth_domain=auth_domain.lower())


def _jwt():
    try:
        import jwt
        return jwt
    except ImportError:
        raise RuntimeError(FIREBASE_EXTRA) from None


def _parse_max_age(header):
    return keycache.parse_max_age(header)


def _capped_body(response, limit=MAX_CERTS_BODY, *, deadline=None, clock=time.monotonic):
    return keycache.capped_body(
        response, limit, deadline=deadline, clock=clock,
        error=FirebaseUnavailable, reason='certificate fetch failed')


_HttpCerts = keycache.FetchedResponse


def _http_fetch():
    return keycache.http_fetch(
        CERTS_URL, error=FirebaseUnavailable, reason='certificate fetch failed',
        label='firebase certificate fetch failed', limit=MAX_CERTS_BODY)


class CertificateSource(KeyCache):
    """Cached Google x509 certs. ``fetch`` is injectable so tests stay offline.

    The cache (single-flight fetch, failure cooldown, unknown-kid interval) is
    ``keycache.KeyCache``; this class loads and validates the certificate map.
    """

    invalid_error = FirebaseTokenInvalid
    unavailable_error = FirebaseUnavailable
    unavailable_reason = 'certificate fetch failed'

    def __init__(self, fetch=None):
        super().__init__()
        self._fetch = fetch

    # The tuning constants are read from this module so they can be patched here.
    def _waiter_timeout(self):
        return WAITER_TIMEOUT

    def _failure_cooldown(self):
        return FAILURE_COOLDOWN

    def _unknown_interval(self):
        return UNKNOWN_KID_INTERVAL

    def _load(self):
        try:
            response = (self._fetch or _http_fetch)()
        except FirebaseUnavailable:
            raise
        except Exception as exc:
            logger.warning('firebase certificate fetch failed: %s', type(exc).__name__)
            raise FirebaseUnavailable('certificate fetch failed') from exc
        status = getattr(response, 'status_code', None)
        if status is not None and status != 200:
            logger.warning('firebase certificate fetch failed')
            raise FirebaseUnavailable('certificate fetch failed')
        try:
            payload = response.json() if hasattr(response, 'json') else response
            if not isinstance(payload, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in payload.items()):
                raise ValueError('invalid certs')
            headers = getattr(response, 'headers', {}) or {}
            cache_control = headers.get('Cache-Control')
            if cache_control is None and hasattr(headers, 'get'):
                cache_control = headers.get('cache-control')
        except FirebaseUnavailable:
            raise
        except Exception as exc:
            logger.warning('firebase certificate fetch failed: %s', type(exc).__name__)
            raise FirebaseUnavailable('certificate parse failed') from exc
        return payload, _parse_max_age(cache_control)


def _default_keys():
    global _default_source
    with _default_lock:
        if _default_source is None:
            _default_source = CertificateSource()
        return _default_source


def _public_key(pem):
    try:
        from cryptography import x509
    except ImportError:
        raise RuntimeError(FIREBASE_EXTRA) from None
    try:
        data = pem.encode() if isinstance(pem, str) else pem
        return x509.load_pem_x509_certificate(data).public_key()
    except Exception as exc:
        raise FirebaseTokenInvalid('invalid key') from exc


_string_or_none = keycache.string_or_none


def _unix_time(value, claim):
    return keycache.unix_time(value, claim, FirebaseTokenInvalid)


def email_verified_for_link(claims):
    """True when the token email can be trusted for linking or invite acceptance."""
    return bool(claims.email_verified
                and (claims.provider in TRUSTED_EMAIL_PROVIDERS or claims.provider == OIDC_PROVIDER))


def verify_id_token(token, project, *, now=None, keys=None) -> Claims:
    jwt = _jwt()
    now = time.time() if now is None else now
    if not isinstance(token, (str, bytes)) or not token:
        raise FirebaseTokenInvalid('malformed')
    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.InvalidTokenError as exc:
        raise FirebaseTokenInvalid('malformed') from exc
    alg = header.get('alg')
    if alg != 'RS256':
        raise FirebaseTokenInvalid(f'alg {alg!r} refused')
    kid = header.get('kid')
    if not isinstance(kid, str) or not kid:
        raise FirebaseTokenInvalid('missing kid')
    source = keys if keys is not None else _default_keys()
    pem = source.get(kid, now=now)
    try:
        payload = jwt.decode(
            token, _public_key(pem), algorithms=['RS256'],
            options={'verify_signature': True, 'verify_exp': False, 'verify_nbf': False,
                     'verify_iat': False, 'verify_aud': False, 'require': []})
    except jwt.exceptions.InvalidSignatureError as exc:
        logger.debug('firebase token invalid: signature')
        raise FirebaseTokenInvalid('signature') from exc
    except jwt.exceptions.DecodeError as exc:
        logger.debug('firebase token invalid: decode')
        raise FirebaseTokenInvalid('decode') from exc
    except jwt.exceptions.InvalidTokenError as exc:
        logger.debug('firebase token invalid: claim')
        raise FirebaseTokenInvalid('claim') from exc
    if payload.get('iss') != f'https://securetoken.google.com/{project}':
        raise FirebaseTokenInvalid('iss')
    if payload.get('aud') != project:
        raise FirebaseTokenInvalid('aud')
    exp = _unix_time(payload.get('exp'), 'exp')
    iat = _unix_time(payload.get('iat'), 'iat')
    auth_time = _unix_time(payload.get('auth_time'), 'auth_time')
    if exp + LEEWAY <= now:
        raise FirebaseTokenInvalid('expired')
    if iat > now + LEEWAY:
        raise FirebaseTokenInvalid('iat')
    if auth_time > now + LEEWAY or now - auth_time > AUTH_MAX_AGE:
        raise FirebaseTokenInvalid('auth_time')
    sub = payload.get('sub')
    if not isinstance(sub, str) or not sub or len(sub) > 128:
        raise FirebaseTokenInvalid('sub')
    email = payload.get('email')
    email = email.lower() if isinstance(email, str) else None
    verified = payload.get('email_verified', False)
    firebase = payload.get('firebase')
    provider = firebase.get('sign_in_provider') if isinstance(firebase, dict) else None
    return Claims(
        uid=sub, email=email,
        email_verified=verified is True,
        name=_string_or_none(payload.get('name')),
        provider=_string_or_none(provider),
    )
