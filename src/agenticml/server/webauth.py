"""Dashboard login, in-memory sessions, identity proxy, and the request gate."""
from __future__ import annotations

from collections import OrderedDict
from html import escape as html_escape
from http.cookies import CookieError, SimpleCookie
import hashlib
import hmac
import ipaddress
import itertools
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
from . import audit, keyspage, userspage
from .keys import ClientError, KeyStoreUnavailable, validate_name
from .users import (
    InviteInvalid, Principal, ScryptUnavailable, UserNameTaken, UserStore,
    allowed, resolve, scrypt_available, validate_password,
)

logger = logging.getLogger("agenticml.server.webauth")
SESSION_IDLE = 12 * 3600
SESSION_MAX_AGE = 7 * 86400
SESSION_CAP = 1000
LOGIN_BODY_MAX = 4 * 1024
IDENTITY_PRESETS = {
    "alb": ("X-Amzn-Oidc-Identity", "exact"),
    "tailscale": ("Tailscale-User-Login", "casefold"),
    "oauth2-proxy": ("X-Forwarded-Email", "casefold"),
}
def _canonical_ip(value):
    addr = ipaddress.ip_address(str(value).split("%")[0])
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _strip_forwarded_port(entry):
    """Return the host from an X-Forwarded-For hop, dropping IPv4:port or [IPv6]:port."""
    text = str(entry).strip()
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return text
        host, rest = text[1:end], text[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return text
        return host
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            return host
    return text


def _trusted_network(net):
    if isinstance(net, ipaddress.IPv6Network):
        mapped = net.network_address.ipv4_mapped
        if mapped is not None and net.prefixlen >= 96:
            net = ipaddress.ip_network((mapped, net.prefixlen - 96))
    if net.prefixlen == 0:
        raise ValueError("refusing to trust every address")
    return net


def parse_trusted_proxies(values):
    """Parse CIDR/address strings (plain addresses are /32 or /128)."""
    networks = []
    for raw in values or ():
        if isinstance(raw, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            net = raw
        else:
            text = str(raw).strip()
            if not text:
                continue
            try:
                net = (ipaddress.ip_network(text, strict=False) if "/" in text
                       else ipaddress.ip_network(_canonical_ip(text)))
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy: {text}") from exc
        networks.append(_trusted_network(net))
    return tuple(networks)


def parse_identity_header(value):
    if value is None or value == "":
        return None
    if value not in IDENTITY_PRESETS:
        raise ValueError("identity header must be alb, tailscale, or oauth2-proxy")
    return value


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
    """Failed-login delays keyed by a caller-supplied bucket. Responses never sleep.

    Dashboard and ingest pass ``WebAuth.client_address``: the TCP peer,
    or the rightmost untrusted ``X-Forwarded-For`` hop when that peer is a
    configured trusted proxy. Password attempts also use a ``user:<name>``
    bucket. Never trust ``X-Forwarded-For`` from an untrusted peer.

    Spraying more than 1024 buckets (for example many /64s) can still
    evict entries. About 6 truly simultaneous valid-key requests from one
    bucket can get a short spurious 429 (clients retry 429). Accepted.
    """

    FREE_FAILURES = 5
    MAX_DELAY = 60.0
    EXPIRE_AFTER = 15 * 60
    MAX_ENTRIES = 1024

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets = itertools.count()
        # address -> (failures, last_failure, holds). holds maps each pending
        # ticket to its own deadline. While every bucket holds a ticket, the
        # table can exceed MAX_ENTRIES, bounded by in-flight connections.
        self._entries = OrderedDict()

    def _delay(self, n):
        if n < self.FREE_FAILURES:
            return 0
        return min(2 ** (n - self.FREE_FAILURES), self.MAX_DELAY)

    def _deadline(self, failures, last_failure, holds):
        base = last_failure + self._delay(failures) if failures else 0
        return max(base, *holds.values()) if holds else base

    def begin(self, address):
        """Reserve a slot. Returns (retry_after, ticket); ticket is None if refused."""
        address = _bucket(address)
        now = self._clock()
        with self._lock:
            entry = self._live(address, now)
            if entry is None:
                failures, last_failure, holds = 0, 0, {}
                count = 0
            else:
                failures, last_failure, holds = entry
                count = failures + len(holds)
                deadline = self._deadline(failures, last_failure, holds)
                if count >= self.FREE_FAILURES and now < deadline:
                    return math.ceil(deadline - now), None
            if entry is None and len(self._entries) >= self.MAX_ENTRIES:
                self._evict()
            ticket = (address, next(self._tickets))
            # Hold this ticket's slot as if the attempt will fail, so concurrent
            # callers see it taken before verify returns.
            holds[ticket] = now + self._delay(count + 1)
            self._entries[address] = (failures, last_failure, holds)
            return 0, ticket

    def finish(self, ticket, outcome):
        if ticket is None:
            return
        now = self._clock()
        with self._lock:
            try:
                address = ticket[0]
            except (TypeError, IndexError):
                return
            entry = self._entries.get(address)
            if entry is None:
                return
            failures, last_failure, holds = entry
            if ticket not in holds:
                return
            del holds[ticket]
            if outcome == "failed":
                failures += 1
                last_failure = now
                self._entries.move_to_end(address)
            elif outcome == "success":
                failures = 0
            if failures == 0 and not holds:
                self._entries.pop(address, None)
                return
            self._entries[address] = (failures, last_failure, holds)

    def _evict(self):
        """Caller holds the lock. Drop the oldest last-failure with no holds.

        While every bucket holds a ticket, the table can exceed MAX_ENTRIES,
        bounded by in-flight connections.
        """
        victim = None
        for address, entry in self._entries.items():
            if not entry[2]:
                victim = address
                break
        if victim is not None:
            del self._entries[victim]

    def _live(self, address, now):
        """Caller holds the lock. Drop the entry if it is gone or stale.

        A bucket with holds is never expired or evicted; dropping it would
        leak the slot and lock the address out.
        """
        entry = self._entries.get(address)
        if entry is None:
            return None
        if entry[2]:
            return entry
        if now - entry[1] >= self.EXPIRE_AFTER:
            del self._entries[address]
            return None
        return entry


_LOGIN_STATIC = Path(__file__).with_name("static")
_ASSETS = {
    "/static/login.js": (_LOGIN_STATIC / "login.js", "text/javascript; charset=utf-8"),
    "/static/setup.js": (_LOGIN_STATIC / "setup.js", "text/javascript; charset=utf-8"),
    "/static/invite.js": (_LOGIN_STATIC / "invite.js", "text/javascript; charset=utf-8"),
    "/static/app.css": (_STATIC / "app.css", _FILES["app.css"]),
    "/static/InstrumentSans.woff2": (_STATIC / "InstrumentSans.woff2", _FILES["InstrumentSans.woff2"]),
    "/static/JetBrainsMono.woff2": (_STATIC / "JetBrainsMono.woff2", _FILES["JetBrainsMono.woff2"]),
}


class WebAuth:
    """Owns /login, /logout, /invite, /setup, sessions, and the request gate."""

    def __init__(self, keys, public_url=None, clock=time.time, throttle_clock=time.monotonic,
                 users=None, trusted_proxies=(), identity_header=None):
        self.keys = keys
        self.users = users if users is not None else UserStore(keys.server_dir)
        self.public_url = public_url
        self.clock = clock
        self.throttle = LoginThrottle(clock=throttle_clock)
        self.trusted_proxies = parse_trusted_proxies(trusted_proxies)
        self.identity_header = parse_identity_header(identity_header)
        if self.identity_header is not None and not self.trusted_proxies:
            raise ValueError("identity header requires a trusted proxy")
        self._identity_name, self._identity_match = (
            IDENTITY_PRESETS[self.identity_header] if self.identity_header else (None, None))
        self._lock = threading.Lock()
        self._sessions = {}
        self.setup_token = None
        self._setup_used = False
        self._setup_lock = threading.Lock()

    def arm_setup(self, base_url):
        if not self.keys.prepare_setup():
            return
        if self._has_admin_user():
            return
        self.setup_token = secrets.token_urlsafe(32)
        self._setup_used = False
        logger.warning(
            "No admin user yet. Open %s/setup?token=%s to create one, or run: "
            "agenticml server users add admin --role admin",
            base_url.rstrip("/"), self.setup_token,
        )

    def _has_admin_user(self):
        return any(user.role == "admin" for user in self.users.list())

    def _setup_armed(self):
        """Caller holds _setup_lock; observing an admin permanently disarms setup."""
        if self.keys.has_admin() or self.keys.setup_closed() or self._has_admin_user():
            self._setup_used = True
            self.setup_token = None
        return self.setup_token is not None and not self._setup_used

    def _peer_ip(self, handler):
        try:
            return _canonical_ip(handler.client_address[0])
        except (ValueError, TypeError, IndexError):
            return None

    def _ip_trusted(self, addr):
        for network in self.trusted_proxies:
            try:
                if addr in network:
                    return True
            except TypeError:
                continue
        return False

    def _peer_trusted(self, handler):
        addr = self._peer_ip(handler)
        return addr is not None and self._ip_trusted(addr)

    def client_address(self, handler) -> str:
        """TCP peer, or the forwarded client when the peer is a trusted proxy."""
        peer = handler.client_address[0]
        if not self.trusted_proxies or not self._peer_trusted(handler):
            return peer
        values = handler.headers.get_all("X-Forwarded-For") or []
        if not values:
            return peer
        parts = [part.strip() for part in ",".join(values).split(",")]
        for entry in reversed(parts):
            try:
                addr = _canonical_ip(_strip_forwarded_port(entry))
            except (ValueError, TypeError):
                return peer
            if not self._ip_trusted(addr):
                return str(addr)
        return peer

    def proxy_identity(self, handler):
        """Identity header value when the peer is trusted; else None."""
        if self._identity_name is None or not self._peer_trusted(handler):
            return None
        if (self.identity_header == "tailscale"
                and handler.headers.get_all("Tailscale-Funnel-Request")):
            return None
        values = handler.headers.get_all(self._identity_name) or []
        if len(values) != 1:
            return None
        value = values[0].strip()
        if (not 1 <= len(value) <= 256
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
            return None
        return value

    def _user_for_identity(self, identity):
        matches = []
        folded = identity.casefold()
        exact = self._identity_match == "exact"
        for user in self.users.list():
            if user.identity is None:
                continue
            if exact:
                if user.identity == identity:
                    matches.append(user)
            elif user.identity.casefold() == folded:
                matches.append(user)
            if len(matches) > 1:
                return None
        return matches[0] if len(matches) == 1 else None

    def _setup_matches(self, token):
        try:
            return hmac.compare_digest(token.encode("ascii"), self.setup_token.encode("ascii"))
        except UnicodeEncodeError:
            return False

    def _rollback_setup(self, created):
        if created is None:
            return
        self.users.discard(created.name, created.id)
        try:
            self.keys.clear_setup_closed()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not clear setup-closed after failed setup")

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
            if path == "/invite":
                if handler.command == "GET":
                    self._invite_page(handler)
                elif handler.command == "POST":
                    self._invite(handler)
                else:
                    handler.close_connection = True
                    handler._json({"error": "method not allowed"}, 405)
                return True
            if handler.command == "GET" and path in _ASSETS:
                self._asset(handler, path)
                return True
            if handler.command == "POST" and path == "/login":
                self._login(handler)
                return True
            if handler.command == "POST" and path == "/logout":
                self._logout(handler)
                return True
            principal, unknown = self._principal_for(handler)
            if handler.command == "GET" and path == "/login":
                if principal is not None:
                    handler._send(b"", "text/plain; charset=utf-8", 303,
                                  self._headers({"Location": "/"}))
                elif unknown is not None:
                    self._unknown_identity(handler, path, unknown)
                else:
                    self._login_page(handler)
                return True
            if principal is None:
                if unknown is not None:
                    self._unknown_identity(handler, path, unknown)
                else:
                    self._refuse(handler, path)
                return True
            self._bind(handler, principal)
            if handler.command == "GET" and path == "/api/server/session":
                self._session_info(handler)
                return True
            if handler.command == "POST" and path == "/api/server/account/password":
                self._account_password(handler)
                return True
            if path == "/api/server/keys" or (path or "").startswith("/api/server/keys/"):
                keyspage.handle(self, handler, path)
                return True
            if path == "/api/server/users" or (path or "").startswith("/api/server/users/"):
                userspage.handle(self, handler, path)
                return True
            if path == "/api/server/audit":
                audit.handle(self, handler)
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

    def _principal_for(self, handler):
        """Return (principal, unknown_identity).

        Matching proxy identity wins; else a valid session cookie; else an
        unknown proxy identity is 403; else the 2.0 refusal.
        """
        identity = self.proxy_identity(handler)
        if identity is not None:
            user = self._user_for_identity(identity)
            if user is not None:
                return Principal(user.name, user.role, "proxy"), None
        session = self._session(handler)
        if session is not None:
            principal = self._principal_from_session(session)
            if principal is None:
                self._delete_session(handler)
            else:
                return principal, None
        if identity is not None:
            return None, identity
        return None, None

    def _bind(self, handler, principal):
        handler.principal = principal
        if principal.via == "proxy":
            handler.session = {"name": "", "sha256": "", "user": principal.user}
            return
        session = self._session(handler)
        handler.session = session if session is not None else {
            "name": principal.key or "", "sha256": "", "user": principal.user,
        }

    def _principal_from_session(self, session):
        user_name = session.get("user")
        via = session.get("via") or "key"
        if user_name == "env":
            record = self.keys.current(
                session.get("key") or session.get("name"),
                session.get("key_sha256") or session.get("sha256"))
            principal = resolve(self.keys, self.users, record)
            if principal is None or principal.user != "env":
                return None
            return Principal("env", "admin", "key", "env")
        user = self.users.get(user_name)
        if (user is None or user.id != session.get("id")
                or user.generation != session.get("generation")):
            return None
        if via == "key":
            record = self.keys.current(
                session.get("key") or session.get("name"),
                session.get("key_sha256") or session.get("sha256"))
            principal = resolve(self.keys, self.users, record)
            if (record is None or record.scope != "admin"
                    or principal is None or principal.role != "admin"):
                return None
            return Principal(user.name, user.role, "key", record.name)
        return Principal(user.name, user.role, via, None)

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
            snapshot = dict(entry)
        if self._principal_from_session(snapshot) is None:
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

    def _attach(self, handler, session, principal=None):
        """Set handler.session and handler.principal from a valid session.

        Returns False when the session is no longer valid. Drops it; caller 401s.
        """
        if principal is None:
            principal = self._principal_from_session(session)
        if principal is None:
            self._delete_session(handler)
            return False
        handler.session = session
        handler.principal = principal
        return True

    def _between_verify_and_session(self, user):
        """Hook for tests: runs after credential verification, before minting a session."""

    def _create_session(self, record=None, *, user=None, generation=None, via="key",
                        key=None, key_sha256=None, expected=None):
        user_id = None
        if record is not None:
            user = record.user if record.name != "env" else "env"
            key = record.name
            key_sha256 = record.sha256
            via = "key"
            found = None if user == "env" else self.users.get(user)
            user_id = None if found is None else found.id
            generation = 0 if found is None else found.generation
        elif user == "env":
            generation = 0 if generation is None else generation
        else:
            found = self.users.get(user)
            if expected is not None:
                exp_id, exp_gen = expected
                if (found is None or found.id != exp_id
                        or found.generation != exp_gen):
                    return None, None
            if found is None:
                generation = 0 if generation is None else generation
            else:
                user_id = found.id
                generation = found.generation
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self.clock()
        entry = {
            "user": user, "id": user_id, "generation": generation, "via": via,
            "key": key, "key_sha256": key_sha256,
            "name": key or "", "sha256": key_sha256 or "",
            "created": now, "last_seen": now,
        }
        with self._lock:
            while len(self._sessions) >= SESSION_CAP:
                oldest = min(self._sessions, key=lambda item: self._sessions[item]["last_seen"])
                del self._sessions[oldest]
            self._sessions[digest] = entry
        return token, entry

    def _session_cookie_for(self, user, via, key=None, key_sha256=None, expected=None):
        if user == "env":
            return self._create_session(
                user=user, generation=0, via=via, key=key, key_sha256=key_sha256)
        return self._create_session(
            user=user, via=via, key=key, key_sha256=key_sha256, expected=expected)

    def _refuse_stale_credentials(self, handler):
        logger.info("login failed")
        handler._json({"error": "invalid user name or password"}, 401, self._headers())

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
                key_name = entry.get("key") or entry.get("name")
                key_hash = entry.get("key_sha256") or entry.get("sha256")
                if key_name == name and key_hash == sha256:
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

    def _throttle_or_refuse(self, handler, address):
        retry_after, ticket = self.throttle.begin(address)
        if ticket is None:
            handler.close_connection = True
            handler._json(
                {"error": "too many failed logins; try again later"},
                429, self._headers({"Retry-After": str(retry_after)}),
            )
            return None
        return ticket

    def _setup(self, handler):
        address = self.client_address(handler)
        ticket = None
        outcome = "ignored"
        try:
            if not handler._csrf_ok():
                handler.close_connection = True
                handler._error({"error": "cross-site request refused"}, 403)
                return
            ticket = self._throttle_or_refuse(handler, address)
            if ticket is None:
                return
            payload = keyspage.read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            token, name = payload.get("token"), payload.get("name", "admin")
            password = payload.get("password")
            if not isinstance(token, str) or not isinstance(name, str):
                raise ClientError("invalid setup body")
            if not isinstance(password, str):
                raise ClientError("invalid setup body")
            validate_name(name)
            validate_password(password)
            if not scrypt_available():
                handler._json({"error": ScryptUnavailable().args[0]}, 503, self._headers())
                return
            with self._setup_lock:
                if not self._setup_armed() or not self._setup_matches(token):
                    outcome = "failed"
                    handler._json({"error": "not found"}, 404)
                    return
                if self.users.get(name) is not None:
                    handler._json({"error": "user name already exists"}, 409)
                    return
                created = None
                try:
                    created = self.users.add(name, role="admin", created_by="setup")
                    user = self.users.set_password(name, password)
                    self.keys._write_setup_closed()
                except UserNameTaken:
                    handler._json({"error": "user name already exists"}, 409)
                    return
                except ScryptUnavailable as exc:
                    if created is not None:
                        self._rollback_setup(created)
                        handler._json(
                            {"error": "setup failed; retry or use agenticml server users"},
                            503, self._headers())
                    else:
                        handler._json({"error": str(exc)}, 503, self._headers())
                    return
                except (KeyStoreUnavailable, OSError):
                    if created is not None:
                        self._rollback_setup(created)
                        handler._json(
                            {"error": "setup failed; retry or use agenticml server users"},
                            503, self._headers())
                    else:
                        handler._json({"error": "key store unavailable"}, 503)
                    return
                except (ClientError, ValueError):
                    if created is not None:
                        self._rollback_setup(created)
                        handler._json(
                            {"error": "setup failed; retry or use agenticml server users"},
                            503, self._headers())
                        return
                    raise
                self._setup_used = True
                self.setup_token = None
            self._between_verify_and_session(user)
            cookie, entry = self._session_cookie_for(
                user.name, "setup", expected=(user.id, user.generation))
            if entry is None:
                outcome = "failed"
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            self._attach(handler, entry, Principal(user.name, user.role, "setup"))
            handler._json(
                {"ok": True, "user": user.name, "role": user.role},
                201, self._headers({"Set-Cookie": self._cookie(cookie)}),
            )
        except ValueError as exc:
            if not getattr(handler, "_started", False):
                handler._error({"error": str(exc) if isinstance(exc, ClientError) else "invalid request"}, 400)
        finally:
            if ticket is not None:
                self.throttle.finish(ticket, outcome)

    def _headers(self, extra=None):
        headers = {"Cache-Control": "no-store"}
        if extra:
            headers.update(extra)
        return headers

    def _asset(self, handler, path):
        file, content_type = _ASSETS[path]
        handler._send(file.read_bytes(), content_type, headers={"Cache-Control": "no-store"})

    def _login_page(self, handler):
        body = (_LOGIN_STATIC / "login.html").read_bytes()
        handler._send(body, "text/html; charset=utf-8", headers=self._headers())

    def _read_login_payload(self, handler):
        length = handler._put_length(LOGIN_BODY_MAX)
        if length is None:
            return None
        if length == 0:
            body = handler._read_put_body(length)
        else:
            if not handler.server._admit_body(handler.connection, 2):
                handler.close_connection = True
                handler._send(
                    b"", "application/json", 503,
                    handler._with_cookie_vary(self._headers()),
                )
                return None
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
        if not isinstance(payload, dict):
            raise ValueError("invalid login body")
        return payload

    def _login(self, handler):
        if not handler._csrf_ok():
            handler.close_connection = True
            handler._error({"error": "cross-site request refused"}, 403)
            return
        address = self.client_address(handler)
        retry_after, ticket = self.throttle.begin(address)
        if ticket is None:
            handler.close_connection = True
            handler._json(
                {"error": "too many failed logins; try again later"},
                429, self._headers({"Retry-After": str(retry_after)}),
            )
            return
        outcome = "ignored"
        try:
            payload = self._read_login_payload(handler)
            if payload is None:
                return
            if isinstance(payload.get("user"), str) and isinstance(payload.get("password"), str):
                address_ticket = ticket
                ticket = None
                self._login_password(handler, payload, address, address_ticket)
                return
            if not isinstance(payload.get("key"), str):
                raise ValueError("invalid login body")
            record = self.keys.verify(payload["key"])
            principal = resolve(self.keys, self.users, record)
            if record is None or principal is None:
                logger.info("login failed")
                outcome = "failed"
                handler._json({"error": "invalid key"}, 401, self._headers())
                return
            if record.scope != "admin":
                logger.info("login failed")
                handler._json(
                    {"error": "this is a sync key; sign in with an admin key"}, 403, self._headers())
                return
            if principal.role != "admin":
                logger.info("login failed")
                handler._json(
                    {"error": "this key cannot sign in"}, 403, self._headers())
                return
            self._between_verify_and_session(principal)
            expected = (
                None if principal.id is None
                else (principal.id, principal.generation))
            token, entry = self._session_cookie_for(
                principal.user, "key", key=record.name, key_sha256=record.sha256,
                expected=expected)
            if entry is None:
                outcome = "failed"
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            self.keys.touch(record)
            self._attach(handler, entry, principal)
            handler._json(
                {"ok": True, "user": principal.user, "role": principal.role},
                headers=self._headers({"Set-Cookie": self._cookie(token)}),
            )
        finally:
            if ticket is not None:
                self.throttle.finish(ticket, outcome)

    def _login_password(self, handler, payload, address, address_ticket):
        name, password = payload["user"], payload["password"]
        user_ticket = None
        outcome = "ignored"
        try:
            retry_after, user_ticket = self.throttle.begin("user:" + name)
            if user_ticket is None:
                self.throttle.finish(address_ticket, "ignored")
                address_ticket = None
                handler.close_connection = True
                handler._json(
                    {"error": "too many failed logins; try again later"},
                    429, self._headers({"Retry-After": str(retry_after)}),
                )
                return
            if not scrypt_available():
                handler._json({"error": ScryptUnavailable().args[0]}, 503, self._headers())
                return
            try:
                user = self.users.verify_password(name, password)
            except ClientError:
                logger.info("login failed")
                outcome = "failed"
                handler._json({"error": "invalid password"}, 400, self._headers())
                return
            if user is None:
                logger.info("login failed")
                outcome = "failed"
                handler._json(
                    {"error": "invalid user name or password"}, 401, self._headers())
                return
            self._between_verify_and_session(user)
            token, entry = self._session_cookie_for(
                user.name, "password", expected=(user.id, user.generation))
            if entry is None:
                outcome = "failed"
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            self._attach(handler, entry, Principal(user.name, user.role, "password"))
            handler._json(
                {"ok": True, "user": user.name, "role": user.role},
                headers=self._headers({"Set-Cookie": self._cookie(token)}),
            )
        finally:
            if address_ticket is not None:
                self.throttle.finish(address_ticket, outcome)
            if user_ticket is not None:
                self.throttle.finish(user_ticket, outcome)

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

    def _session_payload(self, handler):
        principal = handler.principal
        user = None if principal.user == "env" else self.users.get(principal.user)
        key = principal.key if principal.via == "key" else None
        return {
            "server": True,
            "user": principal.user,
            "role": principal.role,
            "via": principal.via,
            "key": key,
            "scope": "admin" if principal.via == "key" else None,
            "can": {
                "notes": allowed(principal, "notes"),
                "own_keys": allowed(principal, "own_keys"),
                "all_keys": allowed(principal, "all_keys"),
                "users": allowed(principal, "users"),
                "audit": allowed(principal, "audit"),
            },
            "password_set": bool(user is not None and user.password is not None),
            "identity_proxy": self.identity_header is not None,
        }

    def _session_info(self, handler):
        handler._json(self._session_payload(handler), headers=self._headers())

    def _check_current_password(self, handler, user, current):
        """Throttle the current-password check. False means a response was sent."""
        address = self.client_address(handler)
        address_ticket = self._throttle_or_refuse(handler, address)
        if address_ticket is None:
            return False
        retry_after, user_ticket = self.throttle.begin("user:" + user.name)
        if user_ticket is None:
            self.throttle.finish(address_ticket, "ignored")
            handler.close_connection = True
            handler._json(
                {"error": "too many failed logins; try again later"},
                429, self._headers({"Retry-After": str(retry_after)}),
            )
            return False
        outcome = "ignored"
        try:
            if not isinstance(current, str):
                outcome = "failed"
                handler._json({"error": "invalid current password"}, 401, self._headers())
                return False
            try:
                verified = self.users.verify_password(user.name, current)
            except ClientError:
                outcome = "failed"
                handler._json({"error": "invalid password"}, 400, self._headers())
                return False
            if verified is None:
                outcome = "failed"
                handler._json({"error": "invalid current password"}, 401, self._headers())
                return False
            outcome = "success"
            return True
        finally:
            self.throttle.finish(address_ticket, outcome)
            self.throttle.finish(user_ticket, outcome)

    def _account_password(self, handler):
        principal = handler.principal
        if principal.user == "env":
            handler.close_connection = True
            handler._json({"error": "environment user cannot set a password"}, 403, self._headers())
            return
        try:
            payload = keyspage.read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            new = payload.get("new")
            current = payload.get("current")
            if not isinstance(new, str):
                raise ClientError("invalid password")
            user = self.users.get(principal.user)
            if user is None:
                handler.close_connection = True
                handler._json({"error": "login required"}, 401, self._headers())
                return
            if principal.via != "proxy":
                session = handler.session or {}
                if (user.id != session.get("id")
                        or user.generation != session.get("generation")):
                    handler.close_connection = True
                    handler._json({"error": "login required"}, 401, self._headers())
                    return
            if user.password is not None:
                if not self._check_current_password(handler, user, current):
                    return
            if not scrypt_available():
                handler._json({"error": ScryptUnavailable().args[0]}, 503, self._headers())
                return
            updated = self.users.set_password(user.name, new)
            token, entry = self._create_session(
                user=updated.name, via="password",
                expected=(updated.id, updated.generation))
            if entry is None:
                handler._json({"error": "invalid current password"}, 401, self._headers())
                return
            self._attach(handler, entry, Principal(updated.name, updated.role, "password"))
            handler._json(
                {"ok": True, "user": updated.name, "role": updated.role},
                headers=self._headers({"Set-Cookie": self._cookie(token)}),
            )
        except ScryptUnavailable as exc:
            if not getattr(handler, "_started", False):
                handler._json({"error": str(exc)}, 503, self._headers())
        except ValueError as exc:
            if not getattr(handler, "_started", False):
                handler._error(
                    {"error": str(exc) if isinstance(exc, ClientError) else "invalid request"}, 400)

    def _invite_pending(self, token):
        from datetime import datetime, timezone

        from agenticml.store import layout as store_layout

        if not isinstance(token, str) or not token:
            return False
        try:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            return False
        now = datetime.now(timezone.utc)
        found = False
        for user in self.users.list():
            invite = user.invite
            if not isinstance(invite, dict) or not isinstance(invite.get("sha256"), str):
                continue
            stored = invite["sha256"]
            if len(stored) != len(digest) or not hmac.compare_digest(stored, digest):
                continue
            try:
                expires = store_layout.parse_timestamp(invite.get("expires_at"))
            except (TypeError, ValueError):
                continue
            if expires > now:
                found = True
        return found

    def _invite_page(self, handler):
        tokens = parse_qs(urlsplit(handler.path).query, keep_blank_values=True).get("token", [])
        token = tokens[0] if len(tokens) == 1 else None
        if not self._invite_pending(token):
            handler._json({"error": "not found"}, 404)
            return
        handler._send(
            (_LOGIN_STATIC / "invite.html").read_bytes(), "text/html; charset=utf-8",
            headers=self._headers({"Referrer-Policy": "no-referrer"}),
        )

    def _invite(self, handler):
        if not handler._csrf_ok():
            handler.close_connection = True
            handler._error({"error": "cross-site request refused"}, 403)
            return
        address = self.client_address(handler)
        ticket = self._throttle_or_refuse(handler, address)
        if ticket is None:
            return
        outcome = "ignored"
        try:
            payload = keyspage.read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            token, password = payload.get("token"), payload.get("password")
            if not isinstance(token, str) or not isinstance(password, str):
                raise ClientError("invalid invite body")
            if not scrypt_available():
                handler._json({"error": ScryptUnavailable().args[0]}, 503, self._headers())
                return
            try:
                user = self.users.redeem_invite(token, password)
            except InviteInvalid:
                outcome = "failed"
                handler._json({"error": "not found"}, 404)
                return
            except ScryptUnavailable as exc:
                handler._json({"error": str(exc)}, 503, self._headers())
                return
            self._between_verify_and_session(user)
            cookie, entry = self._session_cookie_for(
                user.name, "invite", expected=(user.id, user.generation))
            if entry is None:
                outcome = "failed"
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            self._attach(handler, entry, Principal(user.name, user.role, "invite"))
            handler._json(
                {"ok": True, "user": user.name, "role": user.role},
                headers=self._headers({"Set-Cookie": self._cookie(cookie)}),
            )
        except ValueError as exc:
            if not getattr(handler, "_started", False):
                handler._error(
                    {"error": str(exc) if isinstance(exc, ClientError) else "invalid request"}, 400)
        finally:
            if ticket is not None:
                self.throttle.finish(ticket, outcome)

    def _unknown_identity(self, handler, path, identity):
        handler.close_connection = True
        if handler.command == "GET" and not (path or "").startswith("/api/"):
            escaped = html_escape(identity, quote=True)
            text = (
                f"Signed in as {escaped} by your identity proxy, but no AgenticML user "
                f"has that identity. Ask an admin to run "
                f"<code>agenticml server users set-identity NAME {escaped}</code>."
            )
            body = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
                "<title>agenticml · No user for this identity</title>"
                "<link rel=\"stylesheet\" href=\"/static/app.css\"/></head>"
                "<body class=\"login-page\"><main class=\"login-card\">"
                "<p class=\"eyebrow\">agenticml</p><h1>No user for this identity</h1>"
                f"<p>{text}</p></main></body></html>"
            ).encode("utf-8")
            handler._send(body, "text/html; charset=utf-8", 403, self._headers())
            return
        handler._json({"error": "no user for this identity"}, 403, self._headers())

    def _refuse(self, handler, path):
        handler.close_connection = True
        if handler.command == "GET" and not (path or "").startswith("/api/"):
            handler._send(b"", "text/plain; charset=utf-8", 303, self._headers({"Location": "/login"}))
            return
        handler._json({"error": "login required"}, 401, self._headers())
