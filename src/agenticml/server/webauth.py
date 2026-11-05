"""Dashboard key login, in-memory sessions, and the request gate."""
from __future__ import annotations

from collections import OrderedDict
from http.cookies import CookieError, SimpleCookie
import hashlib
import hmac
import ipaddress
import json
import logging
import math
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from agenticml.ui.server import (
    _DISCONNECT,
    _FILES,
    _STATIC,
    _reject_nonfinite,
)
from . import keyspage
from .keys import ClientError, KeyNameTaken, KeyStoreUnavailable, SetupClosed, validate_name

logger = logging.getLogger("agenticml.server.webauth")
SESSION_IDLE = 12 * 3600
SESSION_MAX_AGE = 7 * 86400
SESSION_CAP = 1000
LOGIN_BODY_MAX = 4 * 1024


def _bucket(address):
    """Collapse an address onto its throttle key.

    IPv4-mapped IPv6 shares the inner IPv4 bucket. Other IPv6 is keyed by
    /64 so rotating addresses in one network does not reset the delay.
    """
    try:
        addr = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return address
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network((addr, 64), strict=False))
    return str(addr)


class LoginThrottle:
    """Failed-login delays keyed by client address. Responses never sleep.

    Behind a reverse proxy every client has the proxy's address and shares
    one bucket, so someone sending bad keys can delay the admin's sign-in
    by up to 60 s at a time. The admin can still use the keys CLI. Package
    13 (trusted identity proxy) is where to key the throttle by the
    forwarded address, from configured proxy addresses only. Never trust
    X-Forwarded-For here.

    Spraying more than 1024 buckets (for example many /64s) can still
    evict entries. That is an accepted stopgap until package 13.
    """

    FREE_FAILURES = 5
    MAX_DELAY = 60.0
    EXPIRE_AFTER = 15 * 60
    MAX_ENTRIES = 1024

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = OrderedDict()

    def retry_after(self, address):
        """Whole seconds until the next attempt is allowed, or 0 if now."""
        address = _bucket(address)
        now = self._clock()
        with self._lock:
            entry = self._live(address, now)
            if entry is None:
                return 0
            wait = entry[2] - now
            if wait <= 0:
                return 0
            return math.ceil(wait)

    def record_failure(self, address):
        address = _bucket(address)
        now = self._clock()
        with self._lock:
            entry = self._live(address, now)
            n = 1 if entry is None else entry[0] + 1
            if entry is None and len(self._entries) >= self.MAX_ENTRIES:
                self._entries.popitem(last=False)
            delay = min(2 ** (n - self.FREE_FAILURES), self.MAX_DELAY) if n >= self.FREE_FAILURES else 0
            self._entries[address] = (n, now, now + delay)
            self._entries.move_to_end(address)

    def clear(self, address):
        address = _bucket(address)
        with self._lock:
            self._entries.pop(address, None)

    def _live(self, address, now):
        """Caller holds the lock. Drop the entry if it is gone or stale."""
        entry = self._entries.get(address)
        if entry is None:
            return None
        if now - entry[1] >= self.EXPIRE_AFTER:
            del self._entries[address]
            return None
        return entry


_LOGIN_STATIC = Path(__file__).with_name("static")
_ASSETS = {
    "/static/login.js": (_LOGIN_STATIC / "login.js", "text/javascript; charset=utf-8"),
    "/static/setup.js": (_LOGIN_STATIC / "setup.js", "text/javascript; charset=utf-8"),
    "/static/app.css": (_STATIC / "app.css", _FILES["app.css"]),
    "/static/InstrumentSans.woff2": (_STATIC / "InstrumentSans.woff2", _FILES["InstrumentSans.woff2"]),
    "/static/JetBrainsMono.woff2": (_STATIC / "JetBrainsMono.woff2", _FILES["JetBrainsMono.woff2"]),
}


class WebAuth:
    """Owns /login, /logout, /api/server/session, and the session cookie gate."""

    def __init__(self, keys, public_url=None, clock=time.time, throttle_clock=time.monotonic):
        self.keys = keys
        self.public_url = public_url
        self.clock = clock
        self.throttle = LoginThrottle(clock=throttle_clock)
        self._lock = threading.Lock()
        self._sessions = {}
        self.setup_token = None
        self._setup_used = False
        self._setup_lock = threading.Lock()

    def arm_setup(self, base_url):
        if not self.keys.prepare_setup():
            return
        self.setup_token = secrets.token_urlsafe(32)
        self._setup_used = False
        logger.warning(
            "No admin key yet. Open %s/setup?token=%s to create one, or run: "
            "agenticml server keys create --name admin --scope admin",
            base_url.rstrip("/"), self.setup_token,
        )

    def _setup_armed(self):
        """Caller holds _setup_lock; observing an admin permanently disarms setup."""
        if self.keys.has_admin() or self.keys.setup_closed():
            self._setup_used = True
            self.setup_token = None
        return self.setup_token is not None and not self._setup_used

    def _setup_matches(self, token):
        try:
            return hmac.compare_digest(token.encode("ascii"), self.setup_token.encode("ascii"))
        except UnicodeEncodeError:
            return False

    def handle(self, handler) -> bool:
        """Answer login routes and rejected requests. False means continue."""
        if handler.command in ("GET", "HEAD", "DELETE") and (
                handler.headers.get_all("Content-Length") or handler.headers.get_all("Transfer-Encoding")):
            handler.close_connection = True
        path = self._path(handler)
        try:
            if path == "/setup":
                with self._setup_lock:
                    self._setup_armed()
                if handler.command == "GET":
                    self._setup_page(handler)
                elif handler.command == "POST":
                    self._setup(handler)
                else:
                    handler.close_connection = True
                    handler._json({"error": "method not allowed"}, 405)
                return True
            if handler.command == "GET" and path in _ASSETS:
                self._asset(handler, path)
                return True
            if handler.command == "GET" and path == "/login":
                self._login_page(handler)
                return True
            if handler.command == "POST" and path == "/login":
                self._login(handler)
                return True
            if handler.command == "POST" and path == "/logout":
                self._logout(handler)
                return True
            if handler.command == "GET" and path == "/api/server/session":
                self._session_info(handler)
                return True
            session = self._session(handler)
            if session is None:
                self._refuse(handler, path)
                return True
            handler.session = session
            if path == "/api/server/keys" or (path or "").startswith("/api/server/keys/"):
                keyspage.handle(self, handler, path)
                return True
            if (path or "").startswith("/api/server/") and path != "/api/server/session":
                handler.close_connection = True
                handler._json({"error": "not found"}, 404)
                return True
            return False
        except _DISCONNECT:
            handler.close_connection = True
            return True
        except (TimeoutError, socket.timeout):
            handler.close_connection = True
            if not getattr(handler, "_started", False):
                try:
                    handler._error({"error": "request timeout"}, 408)
                except OSError:
                    handler.close_connection = True
            return True
        except KeyStoreUnavailable:
            if not getattr(handler, "_started", False):
                handler._error({"error": "key store unavailable"}, 503)
            return True
        except ValueError:
            if not getattr(handler, "_started", False):
                handler._error({"error": "invalid request"}, 400)
            return True
        except (FileNotFoundError, NotADirectoryError):
            if not getattr(handler, "_started", False):
                handler._error({"error": "not found"}, 404)
            return True

    def _path(self, handler):
        try:
            return unquote(urlsplit(handler.path).path, errors="strict")
        except (UnicodeDecodeError, ValueError, LookupError):
            return None

    def _secure(self) -> bool:
        return isinstance(self.public_url, str) and urlsplit(self.public_url).scheme == "https"

    def _cookie_name(self) -> str:
        return "__Host-aml_session" if self._secure() else "aml_session"

    def _cookie(self, token: str, max_age: int = SESSION_MAX_AGE) -> str:
        parts = [f"{self._cookie_name()}={token}"]
        if self._secure():
            parts.append("Secure")
        parts.extend(["HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={max_age}"])
        return "; ".join(parts)

    def _read_cookie(self, handler):
        raw = handler.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(self._cookie_name())
        if morsel is None:
            return None
        value = morsel.value
        return value or None

    def _session(self, handler):
        token = self._read_cookie(handler)
        if token is None:
            return None
        try:
            digest = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        except UnicodeEncodeError:
            return None
        with self._lock:
            entry = self._sessions.get(digest)
            if entry is None:
                return None
            now = self.clock()
            if now - entry["last_seen"] >= SESSION_IDLE or now - entry["created"] >= SESSION_MAX_AGE:
                del self._sessions[digest]
                return None
            name, sha256 = entry["name"], entry["sha256"]
        record = self.keys.current(name, sha256)
        if record is None or record.scope != "admin":
            with self._lock:
                self._sessions.pop(digest, None)
            return None
        with self._lock:
            current = self._sessions.get(digest)
            if current is None:
                return None
            if self.clock() - current["last_seen"] >= 60:
                current["last_seen"] = self.clock()
            return current

    def _create_session(self, record):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self.clock()
        entry = {"name": record.name, "sha256": record.sha256, "created": now, "last_seen": now}
        with self._lock:
            while len(self._sessions) >= SESSION_CAP:
                oldest = min(self._sessions, key=lambda key: self._sessions[key]["last_seen"])
                del self._sessions[oldest]
            self._sessions[digest] = entry
        return token, entry

    def _delete_session(self, handler):
        token = self._read_cookie(handler)
        if token is None:
            return
        try:
            digest = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        except UnicodeEncodeError:
            return
        with self._lock:
            self._sessions.pop(digest, None)

    def drop_sessions(self, name, sha256):
        with self._lock:
            for digest, entry in list(self._sessions.items()):
                if entry["name"] == name and entry["sha256"] == sha256:
                    del self._sessions[digest]

    def _setup_page(self, handler):
        tokens = parse_qs(urlsplit(handler.path).query, keep_blank_values=True).get("token", [])
        with self._setup_lock:
            allowed = self._setup_armed() and len(tokens) == 1 and self._setup_matches(tokens[0])
        if not allowed:
            handler._json({"error": "not found"}, 404)
            return
        handler._send(
            (_LOGIN_STATIC / "setup.html").read_bytes(), "text/html; charset=utf-8",
            headers=self._headers({"Referrer-Policy": "no-referrer"}),
        )

    def _setup(self, handler):
        try:
            payload = keyspage.read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            token, name = payload.get("token"), payload.get("name", "admin")
            if not isinstance(token, str) or not isinstance(name, str):
                raise ClientError("invalid setup body")
            validate_name(name)
            with self._setup_lock:
                if not self._setup_armed() or not self._setup_matches(token):
                    handler._json({"error": "not found"}, 404)
                    return
                if any(record.name == name for record in self.keys.list()):
                    handler._json({"error": "key name already exists"}, 409)
                    return
                try:
                    record, value = self.keys.create(
                        name, "admin", created_by="setup", only_if_no_admin=True)
                except SetupClosed:
                    self._setup_used = True
                    self.setup_token = None
                    handler._json({"error": "not found"}, 404)
                    return
                except KeyNameTaken:
                    handler._json({"error": "key name already exists"}, 409)
                    return
                except (KeyStoreUnavailable, OSError):
                    handler._json({"error": "key store unavailable"}, 503)
                    return
                self._setup_used = True
            cookie, entry = self._create_session(record)
            handler.session = entry
            handler._json(
                {"ok": True, "key": name, "value": value, **keyspage.command_host(self, handler)},
                201, self._headers({"Set-Cookie": self._cookie(cookie)}),
            )
        except ValueError as exc:
            if not getattr(handler, "_started", False):
                handler._error({"error": str(exc) if isinstance(exc, ClientError) else "invalid request"}, 400)

    def _headers(self, extra=None):
        headers = {"Cache-Control": "no-store"}
        if extra:
            headers.update(extra)
        return headers

    def _asset(self, handler, path):
        file, content_type = _ASSETS[path]
        handler._send(file.read_bytes(), content_type, headers={"Cache-Control": "no-store"})

    def _login_page(self, handler):
        if self._session(handler) is not None:
            handler._send(b"", "text/plain; charset=utf-8", 303, self._headers({"Location": "/"}))
            return
        body = (_LOGIN_STATIC / "login.html").read_bytes()
        handler._send(body, "text/html; charset=utf-8", headers=self._headers())

    def _login(self, handler):
        if not handler._csrf_ok():
            handler.close_connection = True
            handler._error({"error": "cross-site request refused"}, 403)
            return
        address = handler.client_address[0]
        retry_after = self.throttle.retry_after(address)
        if retry_after:
            handler.close_connection = True
            handler._json(
                {"error": "too many failed logins; try again later"},
                429, self._headers({"Retry-After": str(retry_after)}),
            )
            return
        length = handler._put_length(LOGIN_BODY_MAX)
        if length is None:
            return
        if length == 0:
            body = handler._read_put_body(length)
        else:
            if not handler.server._admit_body(handler.connection, 2):
                handler.close_connection = True
                handler._send(
                    b"", "application/json", 503,
                    handler._with_cookie_vary(self._headers()),
                )
                return
            try:
                body = handler._read_put_body(length)
            finally:
                handler.server._set_connection_state(handler.connection, "busy")
        if len(body) != length:
            handler.close_connection = True
            raise ValueError("short body")
        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_reject_nonfinite)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ValueError("invalid login body") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("key"), str):
            raise ValueError("invalid login body")
        record = self.keys.verify(payload["key"])
        if record is None:
            logger.info("login failed")
            self.throttle.record_failure(address)
            handler._json({"error": "invalid key"}, 401, self._headers())
            return
        if record.scope != "admin":
            logger.info("login failed")
            handler._json(
                {"error": "this is a sync key; sign in with an admin key"}, 403, self._headers())
            return
        self.throttle.clear(address)
        token, entry = self._create_session(record)
        self.keys.touch(record)
        handler.session = entry
        handler._json(
            {"ok": True, "key": record.name},
            headers=self._headers({"Set-Cookie": self._cookie(token)}),
        )

    def _logout(self, handler):
        if not handler._csrf_ok():
            handler.close_connection = True
            handler._error({"error": "cross-site request refused"}, 403)
            return
        self._delete_session(handler)
        handler._send(
            b"", "text/plain; charset=utf-8", 204,
            self._headers({"Set-Cookie": self._cookie("", max_age=0)}),
        )

    def _session_info(self, handler):
        session = self._session(handler)
        if session is None:
            handler.close_connection = True
            handler._json({"error": "login required"}, 401, self._headers())
            return
        handler.session = session
        handler._json(
            {"server": True, "key": session["name"], "scope": "admin"},
            headers=self._headers(),
        )

    def _refuse(self, handler, path):
        handler.close_connection = True
        if handler.command == "GET" and not (path or "").startswith("/api/"):
            handler._send(b"", "text/plain; charset=utf-8", 303, self._headers({"Location": "/login"}))
            return
        handler._json({"error": "login required"}, 401, self._headers())
