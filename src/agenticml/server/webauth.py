"""Dashboard login, persistent sessions, identity proxy, and the request gate."""
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
    _CSP,
    _DISCONNECT,
    _FILES,
    _STATIC,
    _reject_nonfinite,
    _reset_request_workspace,
)
from . import keyspage
from .db import DatabaseError
from .keys import ClientError, KeyStoreUnavailable, validate_name
from .users import (
    InviteInvalid, Principal, ScryptUnavailable, UserNameTaken, UserRecord, UserStore,
    _check_password, _dummy_verify, allowed, resolve, scrypt_available, validate_password,
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


class MemoryBuckets:
    def __init__(self):
        self.entries = OrderedDict()

    def transaction(self, expiry, now, released_ticket=None, outcome=None, bucket=None):
        from contextlib import nullcontext
        return nullcontext()


class DbBuckets(MemoryBuckets):
    def __init__(self, db):
        super().__init__()
        from .db.repos import ThrottleRepo
        self.db = db
        self.repo = ThrottleRepo(db)

    def transaction(self, expiry, now, released_ticket=None, outcome=None, bucket=None):
        from contextlib import contextmanager

        @contextmanager
        def manage():
            # Tickets (holds) stay process-local; durable counts are shared.
            # Each begin/finish is one write transaction under
            # advisory_lock('throttle') that writes the buckets it touches
            # and deletes expired rows (skipping any bucket with a live hold
            # in this process), so a success cannot erase another process's
            # failures. Unsaved failure counts stay in memory and are merged
            # (max) with the database until a save succeeds, so a persistence
            # failure never lowers the effective delay.
            original = OrderedDict((k, (v[0], v[1], dict(v[2])))
                                   for k, v in self.entries.items())
            applied = False
            persisted = False
            touched = set() if bucket is None else {bucket}
            try:
                with self.db.transaction(write=True) as tx:
                    tx.advisory_lock('throttle')
                    rows = self.repo.load(tx=tx)
                    holds = {k: dict(v[2]) for k, v in original.items() if v[2]}
                    unsaved = {k: (v[0], v[1]) for k, v in original.items() if v[0]}
                    self.entries.clear()
                    previous = {key: (failures, last) for key, failures, last in rows}
                    merged = {}
                    for key, failures, last in rows:
                        mem = unsaved.pop(key, None)
                        if mem is not None:
                            failures, last = max(failures, mem[0]), max(last, mem[1])
                        merged[key] = (failures, last)
                    merged.update(unsaved)
                    for key, (failures, last) in merged.items():
                        if key in holds or now - last < expiry:
                            self.entries[key] = (failures, last, holds.pop(key, {}))
                    for key, pending in holds.items():
                        self.entries[key] = (0, 0, pending)
                    before_failures = {key: self.entries[key][0] for key in self.entries}
                    before_keys = set(self.entries)
                    yield
                    applied = True
                    touched.update(before_keys - set(self.entries))
                    touched.update(key for key, value in self.entries.items()
                                   if before_failures.get(key) != value[0])
                    if bucket is not None:
                        touched.add(bucket)
                    held = tuple(key for key, value in self.entries.items() if value[2])
                    self.repo.save(self.entries, previous, tx, touched=touched,
                                   before_failures=before_failures,
                                   expire_before=now - expiry, keep=held)
                # COMMIT succeeded. A commit failure must keep unsaved counts
                # in memory (merged by max on the next load).
                persisted = True
            except BaseException:
                if not applied or released_ticket is None:
                    # Load failed, or begin() could not persist: the caller
                    # never received a ticket, so drop the unissued hold.
                    self.entries.clear()
                    self.entries.update(original)
                    if (not applied and outcome == "failed"
                            and isinstance(released_ticket, tuple) and released_ticket):
                        # finish("failed") never ran; count it in memory so
                        # the next begin still delays. Unsaved, merged by max.
                        address = released_ticket[0]
                        entry = self.entries.get(address)
                        if entry is not None and released_ticket in entry[2]:
                            failures, _, holds = entry
                            self.entries[address] = (
                                failures + 1, now, dict(holds))
                        elif entry is None:
                            self.entries[address] = (1, now, {})
                raise
            finally:
                # A finishing attempt releases its ticket even if its failure
                # count could not be committed. Never leave an orphaned hold.
                if isinstance(released_ticket, tuple) and released_ticket:
                    entry = self.entries.get(released_ticket[0])
                    if entry is not None:
                        entry[2].pop(released_ticket, None)
                for key in list(self.entries):
                    failures, _, holds = self.entries[key]
                    if holds:
                        continue
                    if (not failures) or (persisted and key in touched):
                        del self.entries[key]
        return manage()


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

    def __init__(self, clock=time.monotonic, store=None):
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets = itertools.count()
        # address -> (failures, last_failure, holds). holds maps each pending
        # ticket to its own deadline. While every bucket holds a ticket, the
        # table can exceed MAX_ENTRIES, bounded by in-flight connections.
        self._store = store if store is not None else MemoryBuckets()
        self._entries = self._store.entries

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
        with self._lock, self._store.transaction(self.EXPIRE_AFTER, now, bucket=address):
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
        try:
            address = ticket[0]
        except (TypeError, IndexError):
            address = None
        with self._lock, self._store.transaction(
                self.EXPIRE_AFTER, now, released_ticket=ticket, outcome=outcome,
                bucket=address):
            if address is None:
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
_JS = "text/javascript; charset=utf-8"
_FIREBASE_ASSETS = {
    "/static/firebase-login.js": (_LOGIN_STATIC / "firebase-login.js", _JS),
    "/static/vendor/firebase/firebase-app-compat.js": (
        _LOGIN_STATIC / "vendor/firebase/firebase-app-compat.js", _JS),
    "/static/vendor/firebase/firebase-auth-compat.js": (
        _LOGIN_STATIC / "vendor/firebase/firebase-auth-compat.js", _JS),
}
_ASSETS = {
    "/static/login.js": (_LOGIN_STATIC / "login.js", _JS),
    "/static/setup.js": (_LOGIN_STATIC / "setup.js", _JS),
    "/static/invite.js": (_LOGIN_STATIC / "invite.js", _JS),
    "/static/app.css": (_STATIC / "app.css", _FILES["app.css"]),
    "/static/InstrumentSans.woff2": (_STATIC / "InstrumentSans.woff2", _FILES["InstrumentSans.woff2"]),
    "/static/JetBrainsMono.woff2": (_STATIC / "JetBrainsMono.woff2", _FILES["JetBrainsMono.woff2"]),
    **_FIREBASE_ASSETS,
}


def firebase_login_csp(auth_domain):
    """Dashboard CSP plus the Google hosts the Firebase sign-in pages need."""
    return (
        _CSP.replace("script-src 'self'", "script-src 'self' https://apis.google.com")
        + "; connect-src 'self' https://identitytoolkit.googleapis.com"
        " https://securetoken.googleapis.com https://www.googleapis.com https://apis.google.com"
        + f"; frame-src https://{auth_domain} https://apis.google.com"
    )


class WebAuth:
    """Owns /login, /logout, /invite, /setup, sessions, and the request gate."""

    def __init__(self, keys, public_url=None, clock=time.time, throttle_clock=time.time,
                 users=None, trusted_proxies=(), identity_header=None,
                 firebase=None, cert_source=None, plans=None, open_signup=False,
                 admin_emails=frozenset()):
        self.keys = keys
        self.users = users if users is not None else UserStore(keys.server_dir, db=keys.db, workspace_id=keys.workspace_id or '0' * 32)
        self.public_url = public_url
        self.clock = clock
        self.throttle = LoginThrottle(clock=throttle_clock, store=DbBuckets(keys.db))
        self.trusted_proxies = parse_trusted_proxies(trusted_proxies)
        self.identity_header = parse_identity_header(identity_header)
        if self.identity_header is not None and not self.trusted_proxies:
            raise ValueError("identity header requires a trusted proxy")
        self._identity_name, self._identity_match = (
            IDENTITY_PRESETS[self.identity_header] if self.identity_header else (None, None))
        self.firebase = firebase
        self.cert_source = cert_source
        self.open_signup = bool(open_signup)
        self.admin_emails = frozenset(
            email.lower() for email in (admin_emails or ()) if isinstance(email, str) and email)
        if self.admin_emails and self.firebase is None:
            logger.warning("--admin-emails is ignored without --firebase-project")
            self.admin_emails = frozenset()
        if self.admin_emails:
            logger.info("server admin emails configured: %d", len(self.admin_emails))
        if self.firebase is not None:
            from .firebase import FIREBASE_EXTRA, FIREBASE_IDENTITY, firebase_available
            if self.identity_header is not None:
                raise ValueError(FIREBASE_IDENTITY)
            if not firebase_available():
                raise ValueError(FIREBASE_EXTRA)
            if self.cert_source is None:
                from .firebase import CertificateSource
                self.cert_source = CertificateSource()
        from .firebase import require_open_signup
        require_open_signup(self.open_signup, self.firebase, plans)
        self._lock = threading.Lock()
        from .db.repos import SessionRepo
        self.sessions = SessionRepo(keys.db)
        self.setup_token = None
        self._setup_used = False
        self._setup_lock = threading.Lock()

    def arm_setup(self, base_url):
        if self.firebase is not None:
            return
        if self.keys.workspace_id is None:
            return
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
        return any(user.role in ("owner", "admin") for user in self.users.list())

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
        from .db.workspaces import AccountRepo
        account = AccountRepo(self.keys.db).by_identity(
            identity, exact=self._identity_match == "exact")
        if account is None:
            return None
        return Principal(
            account["login"], "viewer", "proxy", None, account["id"],
            account["generation"], server_admin=bool(account["server_admin"]))

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
        _reset_request_workspace(handler)
        if handler.command in ("GET", "HEAD", "DELETE") and (
                handler.headers.get_all("Content-Length") or handler.headers.get_all("Transfer-Encoding")):
            handler.close_connection = True
        path = self._path(handler)
        try:
            if self.firebase is not None and path == "/auth/firebase/config":
                if handler.command == "GET":
                    self._firebase_config(handler)
                else:
                    handler.close_connection = True
                    handler._json({"error": "method not allowed"}, 405)
                return True
            if self.firebase is not None and path == "/auth/firebase":
                if handler.command == "POST":
                    self._firebase_auth(handler)
                else:
                    handler.close_connection = True
                    handler._json({"error": "method not allowed"}, 405)
                return True
            if path == "/setup":
                if self.firebase is not None:
                    handler.close_connection = True
                    handler._json({"error": "not found"}, 404)
                    return True
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
                if self.firebase is not None:
                    if handler.command == "GET":
                        self._firebase_login_page(handler)
                    elif handler.command == "POST":
                        handler.close_connection = True
                        handler._json({"error": "not found"}, 404)
                    else:
                        handler.close_connection = True
                        handler._json({"error": "method not allowed"}, 405)
                    return True
                if handler.command == "GET":
                    self._invite_page(handler)
                elif handler.command == "POST":
                    self._invite(handler)
                else:
                    handler.close_connection = True
                    handler._json({"error": "method not allowed"}, 405)
                return True
            if handler.command == "GET" and path in _ASSETS:
                if path not in _FIREBASE_ASSETS or self.firebase is not None:
                    self._asset(handler, path)
                    return True
            if handler.command == "POST" and path == "/login":
                if self.firebase is not None:
                    handler.close_connection = True
                    handler._json({"error": "not found"}, 404)
                    return True
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
            from .routes import dispatch_dashboard
            if dispatch_dashboard(self, handler, path):
                return True
            handler._error({"error": "not found"}, 404)
            return True
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
        except (KeyStoreUnavailable, DatabaseError):
            if not getattr(handler, "_started", False):
                handler._error({"error": "key store unavailable"}, 503)
            return True
        except ValueError as exc:
            from agenticml.store.notes import NoteTooLarge
            if isinstance(exc, NoteTooLarge):
                raise
            if not getattr(handler, "_started", False):
                handler._error({"error": "invalid request"}, 400)
            return True
        except (FileNotFoundError, NotADirectoryError):
            if not getattr(handler, "_started", False):
                handler._error({"error": "not found"}, 404)
            return True

    def _audit(self, handler, action, target, detail=None, *, actor=None, via=None):
        try:
            principal = getattr(handler, "principal", None)
            from .audit import AuditLog
            AuditLog(
                handler.server.data_dir / "server", db=handler.server.db, workspace_id=None
            ).record(
                actor if actor is not None else principal.user,
                via if via is not None else principal.via,
                action, target, self.client_address(handler), {} if detail is None else detail)
        except Exception:
            logger.error("could not record audit event")

    def _audit_login_failed(self, handler, name=None, *, method, provider=None):
        try:
            validate_name(name)
        except ClientError:
            name = None
        detail = {"method": method}
        if method == "firebase":
            detail["provider"] = provider
        self._audit(handler, "login.failed", name, detail,
                    actor="anonymous", via=method)

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
            principal = self._user_for_identity(identity)
            if principal is not None:
                return principal, None
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

    def _stores_for(self, workspace_id):
        if workspace_id == self.keys.workspace_id:
            return self.keys, self.users
        from .keys import KeyStore
        from .users import UserStore
        users = UserStore(self.keys.server_dir, db=self.keys.db, workspace_id=workspace_id)
        keys = KeyStore(self.keys.server_dir, db=self.keys.db, workspace_id=workspace_id)
        return keys, users

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
            return Principal("env", "admin", "key", "env", workspace_id=record.workspace_id if record else None)
        if via == "key":
            record = self.keys.current(
                session.get("key") or session.get("name"),
                session.get("key_sha256") or session.get("sha256"))
            if record is None:
                return None
            keys, users = self._stores_for(record.workspace_id)
            principal = resolve(keys, users, record)
            if (record.scope != "admin"
                    or principal is None or principal.role not in ("owner", "admin")):
                return None
            if (principal.id != session.get("id")
                    or principal.generation != session.get("generation")):
                return None
            return principal
        user = self.users.get(user_name)
        if user is None:
            from .db.workspaces import AccountRepo
            account = AccountRepo(self.keys.db).get(user_name)
            if (account is None or account["id"] != session.get("id")
                    or account["generation"] != session.get("generation")):
                return None
            return Principal(account["login"], "viewer", via, None, account["id"],
                             account["generation"], server_admin=bool(account["server_admin"]))
        if (user.id != session.get("id")
                or user.generation != session.get("generation")):
            return None
        from .db.workspaces import AccountRepo
        account = AccountRepo(self.keys.db).get(user.name)
        return Principal(
            user.name, user.role, via, None, user.id, user.generation,
            server_admin=bool(account["server_admin"]) if account else False)

    def _session(self, handler):
        token = self._read_cookie(handler)
        if token is None:
            return None
        try:
            digest = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        except UnicodeEncodeError:
            return None
        with self._lock:
            entry = self.sessions.get(digest)
            if entry is None:
                return None
            now = self.clock()
            if now - entry["last_seen"] >= SESSION_IDLE or now - entry["created"] >= SESSION_MAX_AGE:
                self.sessions.delete(digest)
                return None
            snapshot = dict(entry)
        if self._principal_from_session(snapshot) is None:
            with self._lock:
                self.sessions.delete(digest)
            return None
        with self._lock:
            current = self.sessions.get(digest)
            if current is None:
                return None
            if self.clock() - current["last_seen"] >= 60:
                self.sessions.touch(digest, self.clock())
                current = self.sessions.get(digest)
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
            if user == "env":
                found = None
            else:
                _, users = self._stores_for(record.workspace_id)
                found = users.get(user)
                if expected is not None:
                    exp_id, exp_gen = expected
                    if (found is None or found.id != exp_id
                            or found.generation != exp_gen):
                        return None, None
            user_id = None if found is None else found.id
            generation = 0 if found is None else found.generation
        elif user == "env":
            generation = 0 if generation is None else generation
        else:
            _, found = self._users_for_login(user)
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
            "workspace_id": (record.workspace_id if record is not None else self.keys.workspace_id) if via == "key" else None,
        }
        with self._lock:
            self.sessions.prune(now, SESSION_IDLE, SESSION_MAX_AGE)
            self.sessions.create(digest, entry, SESSION_CAP)
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
            self.sessions.delete(digest)

    def drop_sessions(self, name, sha256):
        self.sessions.delete_for_key(name, sha256)

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
                    user = self.users.set_password(name, password, operator=True)
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
            self._audit(handler, "user.add", user.name,
                        {"role": user.role, "identity": user.identity},
                        actor=user.name, via="setup")
            self._audit(handler, "user.password", user.name, {"method": "setup"},
                        actor=user.name, via="setup")
            self._audit(handler, "setup", user.name, actor=user.name, via="setup")
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
                {"ok": True, "user": user.name, "role": "admin" if user.role == "owner" else user.role},
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
        if self.firebase is not None:
            self._firebase_login_page(handler)
            return
        body = (_LOGIN_STATIC / "login.html").read_bytes()
        handler._send(body, "text/html; charset=utf-8", headers=self._headers())

    def _firebase_login_page(self, handler):
        handler._send(
            (_LOGIN_STATIC / "firebase-login.html").read_bytes(),
            "text/html; charset=utf-8",
            headers=self._headers({
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": firebase_login_csp(self.firebase.auth_domain),
            }),
        )

    def _firebase_config(self, handler):
        if self.firebase is None:
            handler._json({"error": "not found"}, 404)
            return
        handler._json(
            {"projectId": self.firebase.project, "apiKey": self.firebase.api_key,
             "authDomain": self.firebase.auth_domain},
            headers=self._headers(),
        )

    def _firebase_auth(self, handler):
        from .db.workspaces import AccountRepo, InviteRepo
        from .firebase import (
            FirebaseTokenInvalid, FirebaseUnavailable, email_verified_for_link, verify_id_token)
        from .users import InviteAccountExists, InviteEmailMismatch, InviteInvalid

        if self.firebase is None:
            handler.close_connection = True
            handler._json({"error": "not found"}, 404)
            return
        if not handler._csrf_ok():
            handler.close_connection = True
            logger.info("login refused: cross-site")
            handler._error({"error": "cross-site request refused"}, 403)
            return
        address = self.client_address(handler)
        ticket = self._throttle_or_refuse(handler, address)
        if ticket is None:
            return
        uid_ticket = None
        outcome = "ignored"
        try:
            payload = self._read_login_payload(handler)
            if payload is None:
                return
            token, invite = payload.get("id_token"), payload.get("invite")
            if not isinstance(token, str):
                raise ValueError("invalid login body")
            if invite is not None and not isinstance(invite, str):
                raise ValueError("invalid login body")
            if invite == "":
                invite = None
            try:
                claims = verify_id_token(
                    token, self.firebase.project, now=self.clock(), keys=self.cert_source)
            except FirebaseTokenInvalid as exc:
                logger.info("login failed")
                logger.debug("firebase token invalid: %s", exc.reason)
                outcome = "failed"
                self._audit_login_failed(handler, method="firebase")
                handler._json({"error": "sign-in failed"}, 401, self._headers())
                return
            except FirebaseUnavailable:
                handler._json({"error": "sign-in unavailable"}, 503, self._headers())
                return
            retry_after, uid_ticket = self.throttle.begin("uid:" + claims.uid)
            if uid_ticket is None:
                self.throttle.finish(ticket, "ignored")
                ticket = None
                handler.close_connection = True
                handler._json(
                    {"error": "too many failed logins; try again later"},
                    429, self._headers({"Retry-After": str(retry_after)}),
                )
                return
            pending = InviteRepo.find_pending(self.keys.db, invite) if invite else None
            if invite is not None and pending is None:
                logger.info("login failed")
                outcome = "failed"
                self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                handler._json(
                    {"error": "this invite is no longer valid", "reason": "invite_invalid"},
                    403, self._headers())
                return
            if pending is not None:
                from .db.workspaces import emails_equal, is_email_login
                if (not email_verified_for_link(claims) or not is_email_login(pending["login"])
                        or not emails_equal(claims.email, pending["login"])):
                    logger.info("login failed")
                    outcome = "failed"
                    self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                    handler._json(
                        {"error": "this invite is for a different or unverified email"},
                        403, self._headers())
                    return
            accounts = AccountRepo(self.keys.db)
            account = None
            if pending is not None:
                try:
                    account = InviteRepo.redeem_firebase(
                        self.keys.db, invite, claims.uid, claims.email,
                        email_verified_for_link(claims), claims.name)
                except InviteEmailMismatch:
                    logger.info("login failed")
                    outcome = "failed"
                    self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                    handler._json(
                        {"error": "this invite is for a different or unverified email"},
                        403, self._headers())
                    return
                except InviteAccountExists:
                    logger.info("login failed")
                    outcome = "failed"
                    self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                    handler._json(
                        {"error": "an account with this email already exists; "
                                 "sign in with it or ask a server admin",
                         "reason": "account_exists"},
                        403, self._headers())
                    return
                except InviteInvalid:
                    logger.info("login failed")
                    outcome = "failed"
                    self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                    handler._json(
                        {"error": "this invite is no longer valid", "reason": "invite_invalid"},
                        403, self._headers())
                    return
            signed_up = False
            created_workspace = None
            unverified = False
            listed = bool(
                self.admin_emails and email_verified_for_link(claims) and claims.email
                and claims.email.lower() in self.admin_emails)
            admin_created = False
            if account is None:
                with self.keys.db.transaction(write=True) as tx:
                    tx.cross_workspace("sign-up creation")
                    tx.advisory_lock("users")
                    account = self._firebase_find_or_claim(accounts, claims, tx)
                    plans = getattr(handler.server, "plans", None)
                    if account is None and listed:
                        account = self._firebase_admin_create(accounts, claims, tx)
                        admin_created = account is not None
                        if admin_created and self.open_signup:
                            created_workspace = self._create_signup_workspace(
                                account, claims, tx, plans)
                            self._mark_personal_workspace(account, created_workspace, tx)
                    elif account is None and self.open_signup:
                        if email_verified_for_link(claims) and claims.email:
                            account, created_workspace = self._firebase_open_signup(
                                accounts, claims, tx, plans)
                            signed_up = account is not None
                        else:
                            unverified = True
                    elif account is not None and self.open_signup:
                        created_workspace = self._personal_workspace_if_none(
                            account, claims, tx, plans)
            if unverified:
                logger.info("login failed")
                outcome = "failed"
                self._audit_login_failed(
                    handler, method="firebase", provider=claims.provider)
                handler._json(
                    {"error": "verify your email", "reason": "email_unverified"},
                    403, self._headers())
                return
            if account is None:
                logger.info("login failed")
                outcome = "failed"
                self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                handler._json(
                    {"error": "no access", "reason": "no_account"}, 403, self._headers())
                return
            created_workspace = self._prepare_created_workspace(
                handler, created_workspace, account)
            if signed_up:
                self._audit(handler, "account.signup", account["login"],
                            actor=account["login"], via="signup")
            if created_workspace is not None:
                self._audit(
                    handler, "workspace.create", created_workspace["id"],
                    {"slug": created_workspace["slug"], "name": created_workspace["name"]},
                    actor=account["login"], via="signup")
            if admin_created:
                self._audit(handler, "account.create", account["login"],
                            {"created_by": "admin-emails"},
                            actor=account["login"], via="admin-emails")
            admin_change = None
            with self.keys.db.transaction(write=True) as tx:
                tx.advisory_lock("users")
                accounts.refresh_firebase_email(
                    account["id"], claims.email, email_verified_for_link(claims), tx)
                admin_change = self._apply_admin_list(accounts, account, listed, tx)
                account = dict(tx.fetchone(
                    "SELECT * FROM accounts WHERE id=?", (account["id"],)))
            if admin_change is not None:
                self._audit(handler, "account.server_admin", account["login"],
                            {"enabled": admin_change, "source": "admin-emails"},
                            actor=account["login"], via="admin-emails")
            self._between_verify_and_session(account)
            cookie, entry = self._session_cookie_for(
                account["login"], "firebase",
                expected=(account["id"], account["generation"]))
            if entry is None:
                outcome = "failed"
                self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            if not self._attach(handler, entry):
                outcome = "failed"
                self._audit_login_failed(handler, method="firebase", provider=claims.provider)
                handler._json({"error": "sign-in failed"}, 401, self._headers())
                return
            self._audit(handler, "login.ok", account["login"],
                        {"method": "firebase", "provider": claims.provider},
                        actor=account["login"], via="firebase")
            handler._json(
                {"ok": True, "user": account["login"]},
                headers=self._headers({"Set-Cookie": self._cookie(cookie)}),
            )
        finally:
            if ticket is not None:
                self.throttle.finish(ticket, outcome)
            if uid_ticket is not None:
                self.throttle.finish(uid_ticket, outcome)

    def _firebase_find_or_claim(self, accounts, claims, tx):
        from .db.workspaces import emails_equal
        from .firebase import email_verified_for_link

        found = accounts.by_firebase_uid(claims.uid, tx)
        if found is not None:
            return found
        if not (email_verified_for_link(claims) and claims.email and accounts.claimable(claims.email, tx)):
            return None
        account = accounts.get(claims.email, tx)
        if account is None or not emails_equal(account["login"], claims.email):
            return None
        try:
            accounts.link_firebase(
                account["id"], claims.uid, claims.email, email_verified_for_link(claims), tx)
        except (ClientError, DatabaseError):
            found = accounts.by_firebase_uid(claims.uid, tx)
            if found is not None:
                return found
            raise
        return accounts.by_firebase_uid(claims.uid, tx) or accounts.get(claims.email, tx)

    def _firebase_open_signup(self, accounts, claims, tx, plans):
        if accounts.get(claims.email, tx) is not None:
            return None, None
        display = claims.name if isinstance(claims.name, str) and claims.name.strip() else (
            claims.email.split("@", 1)[0] or "workspace")
        account = accounts.create(claims.email, display, "signup", tx)
        accounts.link_firebase(account["id"], claims.uid, claims.email, True, tx)
        account = accounts.by_firebase_uid(claims.uid, tx) or accounts.get(claims.email, tx)
        return account, self._create_signup_workspace(account, claims, tx, plans)

    def _firebase_admin_create(self, accounts, claims, tx):
        """Create the account of a listed, verified email (the caller adds the workspace)."""
        if accounts.get(claims.email, tx) is not None:
            return None
        display = claims.name if isinstance(claims.name, str) and claims.name.strip() else (
            claims.email.split("@", 1)[0] or "workspace")
        account = accounts.create(claims.email, display, "admin-emails", tx)
        accounts.link_firebase(account["id"], claims.uid, claims.email, True, tx)
        return accounts.by_firebase_uid(claims.uid, tx) or accounts.get(claims.email, tx)

    def _apply_admin_list(self, accounts, account, listed, tx):
        """Grant or revoke list-sourced server admin; return the new state or None."""
        from .db.workspaces import LastServerAdminError

        row = tx.fetchone(
            "SELECT login, server_admin, server_admin_source FROM accounts WHERE id=?",
            (account["id"],))
        if row is None:
            return None
        if listed and not row["server_admin"]:
            accounts.set_server_admin(row["login"], True, source="list", tx=tx)
            return True
        if not listed and row["server_admin"] and row["server_admin_source"] == "list":
            try:
                accounts.set_server_admin(row["login"], False, tx=tx)
            except LastServerAdminError:
                logger.warning(
                    "kept server admin that left the admin email list: it is the last one")
                return None
            return False
        return None

    def _signup_pending_key(self, account_id):
        return f"signup_pending:{account_id}"

    def _personal_workspace_key(self, account_id):
        return f"personal_workspace:{account_id}"

    def _mark_personal_workspace(self, account, workspace, tx):
        from .db.repos import SettingsRepo

        if workspace is not None:
            SettingsRepo(self.keys.db).set(
                self._personal_workspace_key(account["id"]), "1", tx)

    def _personal_workspace_if_none(self, account, claims, tx, plans):
        """Open sign-up: a verified sign-in of an account without any membership
        may get the free personal workspace (the limit still applies).

        Sign-up accounts only recover after a failed store preparation (the
        signup_pending marker), so a workspace an admin deleted stays deleted.
        Other accounts (list admins, CLI or /admin created) get one at most once
        (the personal_workspace marker).
        """
        from .db.repos import SettingsRepo
        from .firebase import email_verified_for_link

        if not email_verified_for_link(claims):
            return None
        # A membership in an archived workspace does not count: the workspace
        # is out of the owner's way, so they may get the free personal one.
        if tx.fetchone(
                "SELECT 1 FROM memberships m JOIN workspaces w ON w.id=m.workspace_id "
                "WHERE m.account_id=? AND w.archived_at IS NULL", (account["id"],)):
            return None
        settings = SettingsRepo(self.keys.db)
        if account.get("created_by") == "signup":
            if settings.get(self._signup_pending_key(account["id"]), tx) is None:
                return None
            return self._create_signup_workspace(account, claims, tx, plans)
        if settings.get(self._personal_workspace_key(account["id"]), tx) is not None:
            return None
        workspace = self._create_signup_workspace(account, claims, tx, plans)
        self._mark_personal_workspace(account, workspace, tx)
        return workspace

    def _create_signup_workspace(self, account, claims, tx, plans):
        from .db.workspaces import WorkspaceRepo, slug_from_email_local

        workspaces = WorkspaceRepo(self.keys.db)
        # A limits file with free_workspaces_per_account=0 creates the account
        # but no workspace; the dashboard shows the "no workspaces" state.
        limit = None if plans is None else plans.free_workspaces_per_account
        if limit is not None and workspaces.owned_free_count(account["id"], tx) >= limit:
            return None
        login = account["login"]
        display = claims.name if isinstance(claims.name, str) and claims.name.strip() else (
            login.split("@", 1)[0] or "workspace")
        slug = workspaces.allocate_slug(slug_from_email_local(login), tx)
        suffix = "'s workspace"
        name = display + suffix
        if len(name) > 256:
            name = display[:256 - len(suffix)].rstrip() + suffix
        return workspaces.create(
            slug, name, login, tx, plan="free", personal=1)

    def _prepare_created_workspace(self, handler, created_workspace, account):
        from .db.repos import SettingsRepo
        from .db.workspaces import WorkspaceRepo
        from .workspaces import discard_store, prepare_store

        if created_workspace is None:
            return None
        marker = self._signup_pending_key(account["id"])
        try:
            prepare_store(handler.server.data_dir, created_workspace["id"])
        except (OSError, DatabaseError):
            logger.exception("could not create workspace store")
            dropped = False
            try:
                with self.keys.db.transaction(write=True) as tx:
                    tx.cross_workspace("signup workspace rollback")
                    tx.advisory_lock("users")
                    row = WorkspaceRepo(self.keys.db).get(created_workspace["slug"], tx)
                    if row is not None and row["slug"] != "default":
                        tx.execute("DELETE FROM workspaces WHERE id=?", (row["id"],))
                        dropped = True
                    SettingsRepo(self.keys.db).set(marker, "1", tx)
                    SettingsRepo(self.keys.db).delete(
                        self._personal_workspace_key(account["id"]), tx)
            except Exception:
                logger.exception("could not delete workspace after store failure")
            if dropped:
                discard_store(handler.server.data_dir, created_workspace["id"])
            return None
        SettingsRepo(self.keys.db).delete(marker)
        return created_workspace

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
            logger.info("login refused: cross-site")
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
            if record is None:
                principal = None
            else:
                keys, users = self._stores_for(record.workspace_id)
                principal = resolve(keys, users, record)
            if record is None or principal is None:
                logger.info("login failed")
                outcome = "failed"
                self._audit_login_failed(handler, method="key")
                handler._json({"error": "invalid key"}, 401, self._headers())
                return
            if record.scope != "admin":
                logger.info("login failed")
                self._audit_login_failed(handler, method="key")
                handler._json(
                    {"error": "this is a sync key; sign in with an admin key"}, 403, self._headers())
                return
            if principal.role not in ("owner", "admin"):
                logger.info("login failed")
                self._audit_login_failed(handler, method="key")
                handler._json(
                    {"error": "this key cannot sign in"}, 403, self._headers())
                return
            self._between_verify_and_session(principal)
            expected = (
                None if principal.id is None
                else (principal.id, principal.generation))
            token, entry = self._create_session(record, expected=expected)
            if entry is None:
                outcome = "failed"
                self._audit_login_failed(handler, method="key")
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            self.keys.touch(record)
            self._attach(handler, entry, principal)
            self._audit(handler, "login.ok", principal.user, {"method": "key"})
            handler._json(
                {"ok": True, "user": principal.user, "role": "admin" if principal.role == "owner" else principal.role},
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
                user = self._verify_login_password(name, password)
            except ClientError:
                logger.info("login failed")
                outcome = "failed"
                handler._json({"error": "invalid password"}, 400, self._headers())
                return
            if user is None:
                logger.info("login failed")
                outcome = "failed"
                self._audit_login_failed(handler, name, method="password")
                handler._json(
                    {"error": "invalid user name or password"}, 401, self._headers())
                return
            self._between_verify_and_session(user)
            token, entry = self._session_cookie_for(
                user.name, "password", expected=(user.id, user.generation))
            if entry is None:
                outcome = "failed"
                self._audit_login_failed(handler, name, method="password")
                self._refuse_stale_credentials(handler)
                return
            outcome = "success"
            self._attach(handler, entry, Principal(user.name, user.role, "password"))
            self._audit(handler, "login.ok", user.name, {"method": "password"})
            handler._json(
                {"ok": True, "user": user.name, "role": "admin" if user.role == "owner" else user.role},
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
        session = self._session(handler)
        self._delete_session(handler)
        if session is not None:
            self._audit(handler, "logout", session["user"],
                        actor=session["user"], via=session["via"])
        handler._send(
            b"", "text/plain; charset=utf-8", 204,
            self._headers({"Set-Cookie": self._cookie("", max_age=0)}),
        )

    def _workspace_cookie(self, handler):
        raw = handler.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get("aml_ws")
        if morsel is None or not morsel.value:
            return None
        from .db.workspaces import validate_slug
        try:
            return validate_slug(morsel.value)
        except ClientError:
            return None

    def _account_workspaces(self, handler):
        principal = handler.principal
        from .db.workspaces import AccountRepo, WorkspaceRepo
        if principal.user == "env":
            workspace = WorkspaceRepo(self.keys.db).get("default")
            if workspace is None or workspace["status"] != "active":
                return []
            return [{"slug": workspace["slug"], "name": workspace["name"], "role": "admin"}]
        rows = AccountRepo(self.keys.db).workspaces(principal.user)
        session = getattr(handler, "session", None) or {}
        bound = session.get("workspace_id") if principal.via == "key" else None
        if bound:
            workspace = WorkspaceRepo(self.keys.db).by_id(bound)
            slug = None if workspace is None else workspace["slug"]
            return [row for row in rows if row["slug"] == slug]
        return rows

    def _user_from_account(self, account, role="viewer"):
        raw = account.get("password")
        try:
            password = json.loads(raw) if raw else None
        except (ValueError, TypeError, RecursionError):
            password = None
        return UserRecord(
            account["login"], account["id"], role, account.get("identity"),
            password, None, int(account.get("generation") or 0),
            account.get("created_at") or "", account.get("created_by") or "")

    def _users_for_login(self, login):
        user = self.users.get(login)
        if user is not None:
            return self.users, user
        from .db.workspaces import AccountRepo, WorkspaceRepo
        slugs = AccountRepo(self.keys.db).workspace_slugs(login)
        if slugs:
            workspace = WorkspaceRepo(self.keys.db).get(slugs[0])
            if workspace is not None:
                from .users import UserStore
                users = UserStore(self.keys.server_dir, db=self.keys.db, workspace_id=workspace["id"])
                found = users.get(login)
                if found is not None:
                    return users, found
        account = AccountRepo(self.keys.db).get(login)
        if account is None:
            return None, None
        return None, self._user_from_account(account)

    def _verify_login_password(self, name, password):
        users, found = self._users_for_login(name)
        if users is not None:
            return users.verify_password(name, password)
        if found is None or found.password is None or not isinstance(password, str):
            _dummy_verify(password if isinstance(password, str) else "")
            return None
        return found if _check_password(password, found.password) else None

    def _session_payload(self, handler):
        principal = handler.principal
        user = None if principal.user == "env" else self._users_for_login(principal.user)[1]
        key = principal.key if principal.via == "key" else None
        return {
            "server": True,
            "user": principal.user,
            "role": "admin" if principal.role == "owner" else principal.role,
            "via": principal.via,
            "auth": "firebase" if self.firebase is not None else "password",
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
            "workspaces": self._account_workspaces(handler),
            "server_admin": bool(principal.server_admin),
            "open_signup": bool(self.open_signup),
        }

    def _session_info(self, handler):
        handler._json(self._session_payload(handler), headers=self._headers())

    def _check_current_password(self, handler, user, current, users=None):
        """Throttle the current-password check. False means a response was sent."""
        store = self.users if users is None else users
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
                verified = store.verify_password(user.name, current)
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
        # Key-login and env sessions are workspace-bound; setup/invite proved
        # the account token and may change the password they just set.
        if principal.via == "key" or principal.user == "env":
            handler.close_connection = True
            handler._json({"error": "this session cannot set a password"}, 403, self._headers())
            return
        try:
            payload = keyspage.read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            new = payload.get("new")
            current = payload.get("current")
            if not isinstance(new, str):
                raise ClientError("invalid password")
            users, user = self._users_for_login(principal.user)
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
                if not self._check_current_password(handler, user, current, users):
                    return
            if not scrypt_available():
                handler._json({"error": ScryptUnavailable().args[0]}, 503, self._headers())
                return
            updated = users.set_password(user.name, new, acting_account_id=user.id)
            self._audit(handler, "user.password", updated.name, {"method": "account"})
            token, entry = self._create_session(
                user=updated.name, via="password",
                expected=(updated.id, updated.generation))
            if entry is None:
                handler._json({"error": "invalid current password"}, 401, self._headers())
                return
            self._attach(handler, entry, Principal(updated.name, updated.role, "password"))
            handler._json(
                {"ok": True, "user": updated.name, "role": "admin" if updated.role == "owner" else updated.role},
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
        from .db.workspaces import InviteRepo

        if InviteRepo.find_pending(self.keys.db, token) is not None:
            return True
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

    def _invite_existing_page(self, handler):
        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
            "<title>agenticml · Sign in</title>"
            "<link rel=\"stylesheet\" href=\"/static/app.css\"/>"
            "<script type=\"module\" src=\"/static/invite.js\"></script></head>"
            "<body class=\"login-page\"><main class=\"login-card\">"
            "<p class=\"eyebrow\">agenticml</p><h1>You already have an account</h1>"
            "<p>You already have an account; join this workspace, then sign in with it.</p>"
            "<form id=\"invite-existing\">"
            "<button class=\"btn btn-primary\" type=\"submit\">Join workspace</button>"
            "</form>"
            "<p><a href=\"/login\">Sign in</a></p>"
            "<p id=\"error\" class=\"error\" role=\"alert\" hidden></p>"
            "</main></body></html>"
        ).encode("utf-8")
        handler._send(body, "text/html; charset=utf-8", headers=self._headers({"Referrer-Policy": "no-referrer"}))

    def _invite_kind(self, token):
        """Return (pending_row, kind) where kind is new, existing, or reset."""
        from .db.workspaces import AccountRepo, InviteRepo, MembershipRepo

        pending = InviteRepo.find_pending(self.keys.db, token)
        if pending is None:
            return None, None
        account = AccountRepo(self.keys.db).get(pending["login"])
        if account is None:
            return pending, "new"
        member = MembershipRepo(self.keys.db, pending["workspace_id"]).get(account["id"])
        return pending, "reset" if member is not None else "existing"

    def _redeem_workspace_invite(self, token, password=None):
        from .db.workspaces import AccountRepo, InviteRepo
        from .users import InviteExistingAccount, InviteInvalid, UserStore

        pending = InviteRepo.find_pending(self.keys.db, token)
        if pending is None:
            raise InviteInvalid("invalid invite")
        InviteRepo(self.keys.db, pending["workspace_id"]).redeem(token, password)
        users = UserStore(
            self.keys.server_dir, db=self.keys.db, workspace_id=pending["workspace_id"])
        user = users.get(pending["login"])
        if user is None:
            account = AccountRepo(self.keys.db).get(pending["login"])
            if account is None:
                raise InviteInvalid("invalid invite")
            raise InviteExistingAccount(account["id"])
        return user

    def _invite_page(self, handler):
        tokens = parse_qs(urlsplit(handler.path).query, keep_blank_values=True).get("token", [])
        token = tokens[0] if len(tokens) == 1 else None
        _, kind = self._invite_kind(token)
        if kind == "existing":
            self._invite_existing_page(handler)
            return
        if kind not in ("new", "reset") and not self._invite_pending(token):
            handler._json({"error": "not found"}, 404)
            return
        handler._send(
            (_LOGIN_STATIC / "invite.html").read_bytes(), "text/html; charset=utf-8",
            headers=self._headers({"Referrer-Policy": "no-referrer"}),
        )

    def _finish_invite_login(self, handler, user):
        self._audit(handler, "user.password", user.name, {"method": "invite"},
                    actor=user.name, via="invite")
        self._between_verify_and_session(user)
        cookie, entry = self._session_cookie_for(
            user.name, "invite", expected=(user.id, user.generation))
        if entry is None:
            self._refuse_stale_credentials(handler)
            return False
        self._attach(handler, entry, Principal(user.name, user.role, "invite"))
        self._audit(handler, "login.ok", user.name, {"method": "invite"})
        handler._json(
            {"ok": True, "user": user.name, "role": "admin" if user.role == "owner" else user.role},
            headers=self._headers({"Set-Cookie": self._cookie(cookie)}),
        )
        return True

    def _invite(self, handler):
        from .users import InviteExistingAccount, InviteInvalid

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
            if not isinstance(token, str):
                raise ClientError("invalid invite body")
            pending, kind = self._invite_kind(token)
            if kind == "existing":
                try:
                    self._redeem_workspace_invite(token)
                except InviteExistingAccount:
                    outcome = "success"
                    handler._json(
                        {"error": "you already have an account; sign in with it"}, 409,
                        self._headers())
                    return
                except InviteInvalid:
                    outcome = "failed"
                    handler._json({"error": "not found"}, 404)
                    return
                handler._json({"error": "not found"}, 404)
                return
            if not isinstance(password, str):
                raise ClientError("invalid invite body")
            if not scrypt_available():
                handler._json({"error": ScryptUnavailable().args[0]}, 503, self._headers())
                return
            if kind == "new":
                try:
                    user = self._redeem_workspace_invite(token, password)
                except InviteExistingAccount:
                    outcome = "success"
                    handler._json(
                        {"error": "you already have an account; sign in with it"}, 409,
                        self._headers())
                    return
                except InviteInvalid:
                    outcome = "failed"
                    handler._json({"error": "not found"}, 404)
                    return
                except ScryptUnavailable as exc:
                    handler._json({"error": str(exc)}, 503, self._headers())
                    return
                if not self._finish_invite_login(handler, user):
                    outcome = "failed"
                    return
                outcome = "success"
                return
            try:
                if pending is not None:
                    from .users import UserStore
                    users = UserStore(
                        self.keys.server_dir, db=self.keys.db,
                        workspace_id=pending["workspace_id"])
                    user = users.redeem_invite(token, password)
                else:
                    user = self.users.redeem_invite(token, password)
            except InviteInvalid:
                outcome = "failed"
                handler._json({"error": "not found"}, 404)
                return
            except ScryptUnavailable as exc:
                handler._json({"error": str(exc)}, 503, self._headers())
                return
            if not self._finish_invite_login(handler, user):
                outcome = "failed"
                return
            outcome = "success"
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
