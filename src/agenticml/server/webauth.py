"""Dashboard key login, in-memory sessions, and the request gate."""
from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
import hashlib
import json
import logging
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import unquote, urlsplit

from agenticml.ui.server import (
    _DISCONNECT,
    _FILES,
    _STATIC,
    _reject_nonfinite,
)

logger = logging.getLogger("agenticml.server.webauth")
SESSION_IDLE = 12 * 3600
SESSION_MAX_AGE = 7 * 86400
SESSION_CAP = 1000
LOGIN_BODY_MAX = 4 * 1024
_LOGIN_STATIC = Path(__file__).with_name("static")
_ASSETS = {
    "/static/login.js": (_LOGIN_STATIC / "login.js", "text/javascript; charset=utf-8"),
    "/static/app.css": (_STATIC / "app.css", _FILES["app.css"]),
    "/static/InstrumentSans.woff2": (_STATIC / "InstrumentSans.woff2", _FILES["InstrumentSans.woff2"]),
    "/static/JetBrainsMono.woff2": (_STATIC / "JetBrainsMono.woff2", _FILES["JetBrainsMono.woff2"]),
}


class WebAuth:
    """Owns /login, /logout, /api/server/session, and the session cookie gate."""

    def __init__(self, keys, public_url=None, clock=time.time):
        self.keys = keys
        self.public_url = public_url
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions = {}

    def handle(self, handler) -> bool:
        """Answer login routes and rejected requests. False means continue."""
        path = self._path(handler)
        try:
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
            handler._json({"error": "invalid key"}, 401, self._headers())
            return
        if record.scope != "admin":
            logger.info("login failed")
            handler._json(
                {"error": "this is a sync key; sign in with an admin key"}, 403, self._headers())
            return
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
