"""Pieces shared by the Firebase and OpenID Connect ID token verifiers.

A capped HTTP body, ``Cache-Control: max-age`` parsing, the claims record, and
a signing-key cache with single-flight fetches, a failure cooldown and a limit
on unknown-key refetches. Provider modules subclass ``KeyCache`` and supply
``_load``. ``requests`` is imported inside functions so the base install stays
``requests`` only, and nothing here logs tokens, codes or key material.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import re
import threading
import time

logger = logging.getLogger('agenticml.server.keycache')

LEEWAY = 60
MIN_MAX_AGE = 300
MAX_MAX_AGE = 24 * 60 * 60
MAX_BODY = 64 * 1024
HTTP_TIMEOUT = 5
BODY_DEADLINE = 10
WAITER_TIMEOUT = 15
FAILURE_COOLDOWN = 30
UNKNOWN_KID_INTERVAL = 60
_MAX_AGE = re.compile(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)"?', re.IGNORECASE)


class TokenInvalid(ValueError):
    """Token failed verification. ``reason`` is for the server log only."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class KeyUnavailable(RuntimeError):
    """A key, discovery or token endpoint fetch failed; callers map this to 503."""

    def __init__(self, reason='fetch failed'):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Claims:
    uid: str
    email: str | None
    email_verified: bool
    name: str | None
    provider: str | None


def parse_max_age(header):
    if not isinstance(header, str):
        return MIN_MAX_AGE
    match = _MAX_AGE.search(header)
    if match is None:
        return MIN_MAX_AGE
    return max(MIN_MAX_AGE, min(int(match.group(1)), MAX_MAX_AGE))


def capped_body(response, limit=MAX_BODY, *, deadline=None, clock=time.monotonic,
                error=KeyUnavailable, reason='fetch failed'):
    """Read a streamed body, refusing more than ``limit`` bytes or past ``deadline``."""
    headers = getattr(response, 'headers', None) or {}
    raw = headers.get('Content-Length')
    if raw is None and hasattr(headers, 'get'):
        raw = headers.get('content-length')
    try:
        declared = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > limit:
        raise error(reason)
    chunks = []
    size = 0
    for chunk in response.iter_content(8192):
        if deadline is not None and clock() >= deadline:
            raise error(reason)
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            raise error(reason)
        chunks.append(chunk)
    return b''.join(chunks)


class FetchedResponse:
    """Status, headers and the capped body of one HTTP exchange."""

    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self._body = body

    def json(self):
        import json
        return json.loads(self._body)


def http_fetch(url, *, method='get', data=None, headers=None, error=KeyUnavailable,
               reason='fetch failed', label='fetch failed', limit=MAX_BODY):
    """One bounded request: no redirects, streamed capped body, short timeouts.

    Network and body failures raise ``error(reason)``; only the exception type
    is logged, never the URL (it can carry a code) or the body.
    """
    import requests
    send = requests.post if method == 'post' else requests.get
    kwargs = {'timeout': HTTP_TIMEOUT, 'allow_redirects': False, 'stream': True}
    if data is not None:
        kwargs['data'] = data
    if headers:
        kwargs['headers'] = headers
    try:
        response = send(url, **kwargs)
    except requests.RequestException as exc:
        logger.warning('%s: %s', label, type(exc).__name__)
        raise error(reason) from exc
    try:
        body = capped_body(response, limit, deadline=time.monotonic() + BODY_DEADLINE,
                           error=error, reason=reason)
        return FetchedResponse(response.status_code, response.headers, body)
    except error:
        raise
    except Exception as exc:
        logger.warning('%s: %s', label, type(exc).__name__)
        raise error(reason) from exc
    finally:
        response.close()


def string_or_none(value):
    return value if isinstance(value, str) and value else None


def unix_time(value, claim, error=TokenInvalid):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(claim)
    try:
        number = float(value)
        if not math.isfinite(number):
            raise error(claim)
    except error:
        raise
    except (OverflowError, ValueError):
        raise error(claim)
    return number


class KeyCache:
    """Cached signing keys by ``kid``. Subclasses implement ``_load``.

    HTTP runs outside ``self._lock`` with single-flight: one fetch in flight,
    other callers wait for it or use the last good keys while those are valid.
    A fetch failure is remembered for the failure cooldown so a down provider
    does not block every sign-in for 5 s. An unknown-kid refetch happens at
    most once per unknown-kid interval. ``_load`` returns ``(keys, max_age)``
    and raises ``unavailable_error``.
    """

    invalid_error = TokenInvalid
    unavailable_error = KeyUnavailable
    unavailable_reason = 'fetch failed'

    def __init__(self):
        self._lock = threading.Lock()
        self._certs = {}
        self._expires_at = 0.0
        self._last_fetch = 0.0
        self._last_failure = None
        self._inflight = None

    # Subclasses read their own module constants so each keeps its tuning
    # knobs (and tests that patch them) in one place.
    def _waiter_timeout(self):
        return WAITER_TIMEOUT

    def _failure_cooldown(self):
        return FAILURE_COOLDOWN

    def _unknown_interval(self):
        return UNKNOWN_KID_INTERVAL

    def _load(self):
        raise NotImplementedError

    def get(self, kid, now=None):
        now = time.time() if now is None else now
        event = None
        try:
            while True:
                with self._lock:
                    cached = self._cached(kid, now)
                    if cached is not None:
                        return cached
                    if now < self._expires_at and now - self._last_fetch < self._unknown_interval():
                        raise self.invalid_error('unknown kid')
                    if (now >= self._expires_at and self._last_failure is not None
                            and now - self._last_failure < self._failure_cooldown()):
                        raise self.unavailable_error(self.unavailable_reason)
                    if self._inflight is not None:
                        wait_for, is_leader = self._inflight, False
                    else:
                        wait_for = threading.Event()
                        self._inflight = wait_for
                        event = wait_for
                        is_leader = True
                if not is_leader:
                    if not wait_for.wait(timeout=self._waiter_timeout()):
                        raise self.unavailable_error(self.unavailable_reason)
                    continue
                break
            try:
                payload, max_age = self._load()
                error = None
            except KeyUnavailable as exc:
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
                    result, outcome = None, self.invalid_error('unknown kid')
                elif error is not None:
                    result, outcome = None, error
                else:
                    result, outcome = None, self.invalid_error('unknown kid')
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
