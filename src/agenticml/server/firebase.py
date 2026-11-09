"""Firebase ID token verification for hosted sign-in.

PyJWT is imported inside functions so the base install stays `requests` only.
Never log ID tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import re
import threading
import time

logger = logging.getLogger('agenticml.server.firebase')

CERTS_URL = ('https://www.googleapis.com/robot/v1/metadata/x509/'
             'securetoken@system.gserviceaccount.com')
FIREBASE_EXTRA = ("Firebase sign-in needs the [firebase] extra: "
                  "pip install 'agenticml-lite[firebase]'")
FIREBASE_IDENTITY = "Firebase sign-in cannot be combined with --identity-header"
OPEN_SIGNUP_REQUIRES_FIREBASE = "open sign-up requires --firebase-project"
OPEN_SIGNUP_REQUIRES_LIMITS = "open sign-up requires --limits"


def require_open_signup(open_signup, firebase=None, plans=None):
    if not open_signup:
        return
    if firebase is None:
        raise ValueError(OPEN_SIGNUP_REQUIRES_FIREBASE)
    if plans is None:
        raise ValueError(OPEN_SIGNUP_REQUIRES_LIMITS)
LEEWAY = 60
AUTH_MAX_AGE = 600
MIN_MAX_AGE = 300
MAX_MAX_AGE = 24 * 60 * 60
MAX_CERTS_BODY = 64 * 1024
HTTP_TIMEOUT = 5
BODY_DEADLINE = 10
WAITER_TIMEOUT = 15
FAILURE_COOLDOWN = 30
UNKNOWN_KID_INTERVAL = 60
TRUSTED_EMAIL_PROVIDERS = frozenset({'google.com', 'password'})
_MAX_AGE = re.compile(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)"?', re.IGNORECASE)
_PROJECT_ID = re.compile(r'[a-z0-9-]{4,40}')
_HOSTNAME = re.compile(
    r'(?=.{1,253}$)'
    r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+'
    r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    r'|localhost'
)

_default_source = None
_default_lock = threading.Lock()


class FirebaseTokenInvalid(ValueError):
    """Token failed verification. ``reason`` is for the server log only."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class FirebaseUnavailable(RuntimeError):
    """Certificate fetch or parse failed; callers map this to 503."""

    def __init__(self, reason='certificate fetch failed'):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Claims:
    uid: str
    email: str | None
    email_verified: bool
    name: str | None
    provider: str | None


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
    if not isinstance(header, str):
        return MIN_MAX_AGE
    match = _MAX_AGE.search(header)
    if match is None:
        return MIN_MAX_AGE
    return max(MIN_MAX_AGE, min(int(match.group(1)), MAX_MAX_AGE))


def _capped_body(response, limit=MAX_CERTS_BODY, *, deadline=None, clock=time.monotonic):
    headers = getattr(response, 'headers', None) or {}
    raw = headers.get('Content-Length')
    if raw is None and hasattr(headers, 'get'):
        raw = headers.get('content-length')
    try:
        declared = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > limit:
        raise FirebaseUnavailable('certificate fetch failed')
    chunks = []
    size = 0
    for chunk in response.iter_content(8192):
        if deadline is not None and clock() >= deadline:
            raise FirebaseUnavailable('certificate fetch failed')
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            raise FirebaseUnavailable('certificate fetch failed')
        chunks.append(chunk)
    return b''.join(chunks)


class _HttpCerts:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self._body = body

    def json(self):
        return json.loads(self._body)


def _http_fetch():
    import requests
    try:
        response = requests.get(
            CERTS_URL, timeout=HTTP_TIMEOUT, allow_redirects=False, stream=True)
    except requests.RequestException as exc:
        logger.warning('firebase certificate fetch failed: %s', type(exc).__name__)
        raise FirebaseUnavailable('certificate fetch failed') from exc
    try:
        body = _capped_body(response, deadline=time.monotonic() + BODY_DEADLINE)
        return _HttpCerts(response.status_code, response.headers, body)
    except FirebaseUnavailable:
        raise
    except Exception as exc:
        logger.warning('firebase certificate fetch failed: %s', type(exc).__name__)
        raise FirebaseUnavailable('certificate fetch failed') from exc
    finally:
        response.close()


class CertificateSource:
    """Cached Google x509 certs. ``fetch`` is injectable so tests stay offline.

    HTTP runs outside ``self._lock`` with single-flight: one fetch in flight,
    other callers wait for it or use the last good certs while those are
    valid. A fetch failure is remembered for ``FAILURE_COOLDOWN`` seconds so
    a down Google does not block every ``POST /auth/firebase`` for 5 s.
    Unknown-kid refetch stays at most once per ``UNKNOWN_KID_INTERVAL``.
    """

    def __init__(self, fetch=None):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._certs = {}
        self._expires_at = 0.0
        self._last_fetch = 0.0
        self._last_failure = None
        self._inflight = None

    def get(self, kid, now=None):
        now = time.time() if now is None else now
        event = None
        try:
            while True:
                with self._lock:
                    cached = self._cached(kid, now)
                    if cached is not None:
                        return cached
                    if now < self._expires_at and now - self._last_fetch < UNKNOWN_KID_INTERVAL:
                        raise FirebaseTokenInvalid('unknown kid')
                    if (now >= self._expires_at and self._last_failure is not None
                            and now - self._last_failure < FAILURE_COOLDOWN):
                        raise FirebaseUnavailable('certificate fetch failed')
                    if self._inflight is not None:
                        wait_for, is_leader = self._inflight, False
                    else:
                        wait_for = threading.Event()
                        self._inflight = wait_for
                        event = wait_for
                        is_leader = True
                if not is_leader:
                    if not wait_for.wait(timeout=WAITER_TIMEOUT):
                        raise FirebaseUnavailable('certificate fetch failed')
                    continue
                break
            try:
                payload, max_age = self._load()
                error = None
            except FirebaseUnavailable as exc:
                payload, max_age, error = None, None, exc
            with self._lock:
                self._last_fetch = now
                if error is None:
                    self._certs = payload
                    self._expires_at = now + max_age
                    self._last_failure = None
                else:
                    self._last_failure = now
                if self._inflight is event:
                    self._inflight = None
                if kid in self._certs and now < self._expires_at:
                    result = self._certs[kid]
                    outcome = None
                elif now < self._expires_at:
                    result, outcome = None, FirebaseTokenInvalid('unknown kid')
                elif error is not None:
                    result, outcome = None, error
                else:
                    result, outcome = None, FirebaseTokenInvalid('unknown kid')
            event.set()
            event = None
            if outcome is not None:
                raise outcome
            return result
        finally:
            if event is not None:
                with self._lock:
                    if self._inflight is event:
                        self._inflight = None
                event.set()

    def _cached(self, kid, now):
        if now < self._expires_at and kid in self._certs:
            return self._certs[kid]
        return None

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


def _string_or_none(value):
    return value if isinstance(value, str) and value else None


def _unix_time(value, claim):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FirebaseTokenInvalid(claim)
    try:
        number = float(value)
        if not math.isfinite(number):
            raise FirebaseTokenInvalid(claim)
    except FirebaseTokenInvalid:
        raise
    except (OverflowError, ValueError):
        raise FirebaseTokenInvalid(claim)
    return number


def email_verified_for_link(claims):
    """True when the token email can be trusted for linking or invite acceptance."""
    return bool(claims.email_verified and claims.provider in TRUSTED_EMAIL_PROVIDERS)


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
