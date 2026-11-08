"""Admin key lifecycle endpoints and bounded run-owner counts."""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlsplit

from agenticml.ui.server import _reject_nonfinite
from .keys import ClientError, KeyNameTaken, KeyStoreUnavailable, LastAdminError, UserRequired, _NAME, validate_name
from .db import DatabaseError
from .users import UserNotFound, allowed, resolve

logger = logging.getLogger("agenticml.server.keyspage")


def can_see(principal, record) -> bool:
    return allowed(principal, "all_keys") or (
        allowed(principal, "own_keys") and record.user == principal.user)


def can_create(principal, user, scope) -> str | None:
    """Check role permissions; user existence/scope validity are store checks."""
    if not allowed(principal, "own_keys"):
        return "forbidden"
    if user != principal.user and not allowed(principal, "all_keys"):
        return "you can only create keys for yourself"
    if scope == "admin" and not allowed(principal, "admin_keys"):
        return "only admins can create admin keys"
    return None


def can_revoke(principal, record) -> bool:
    return can_see(principal, record)


def command_host(webauth, handler):
    if webauth.public_url:
        return {"host": webauth.public_url, "host_source": "public_url"}
    return {"host": "http://" + handler.headers["Host"], "host_source": "request"}


def read_json(handler, limit):
    """Read an admitted JSON body; None means an error was already sent."""
    if not handler._csrf_ok():
        handler.close_connection = True
        handler._error({"error": "cross-site request refused"}, 403)
        return None
    if handler.headers.get_content_type() != "application/json":
        handler.close_connection = True
        handler._error({"error": "application/json required"}, 415)
        return None
    length = handler._put_length(limit)
    if length is None:
        return None
    if length == 0:
        body = handler._read_put_body(length)
    else:
        if not handler.server._admit_body(handler.connection, 2):
            handler.close_connection = True
            handler._send(
                b"", "application/json", 503,
                handler._with_cookie_vary({"Cache-Control": "no-store"}),
            )
            return None
        try:
            body = handler._read_put_body(length)
        finally:
            handler.server._set_connection_state(handler.connection, "busy")
    if len(body) != length:
        handler.close_connection = True
        raise ClientError("short body")
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ClientError("invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise ClientError("invalid JSON body")
    return payload


def _stores(webauth, handler):
    ws = getattr(handler, "workspace", None)
    if ws is not None:
        return ws.keys, ws.users
    return webauth.keys, webauth.users


def _run_counts(handler):
    ws = getattr(handler, "workspace", None)
    index = ws.run_index if ws is not None else getattr(handler.server, "run_index", None)
    if index is None:
        return {}
    try:
        return index.owner_key_counts()
    except DatabaseError:
        return None


def _audit(webauth, handler, action, record):
    try:
        principal = handler.principal
        target = record.user if action == "user.add" else record.name
        detail = ({"role": "admin", "identity": None} if action == "user.add"
                  else {"user": record.user, "scope": record.scope})
        ws = getattr(handler, "workspace", None)
        log = ws.audit if ws is not None else handler.server.audit
        log.record(
            principal.user, principal.via, action, target,
            webauth.client_address(handler), detail)
    except Exception:
        logger.error("could not record audit event")


def handle(webauth, handler, path):
    from .webauth import LOGIN_BODY_MAX

    try:
        principal = handler.principal
        keys, users = _stores(webauth, handler)
        if not allowed(principal, "own_keys"):
            handler.close_connection = True
            handler._json({"error": "forbidden"}, 403)
            return
        if handler.command in ("GET", "DELETE") and (
                handler.headers.get_all("Content-Length") or handler.headers.get_all("Transfer-Encoding")):
            handler.close_connection = True
        if path == "/api/server/keys" and handler.command == "GET":
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
            user = query.get("user", [None])[0]
            if user is not None:
                validate_name(user)
            records = sorted(keys.list(), key=lambda record: (
                record.name == "env", record.created_at, record.name))
            counts = _run_counts(handler)
            rows = [{**record.to_public(), "runs": None if counts is None else counts.get((record.name, record.user), 0),
                     "revocable": can_revoke(principal, record) and record.name != "env",
                     "active": (resolved := resolve(keys, users, record)) is not None
                     and allowed(resolved, "sync")
                     and (record.scope != "admin" or allowed(resolved, "admin_keys"))}
                    for record in records if can_see(principal, record)
                    and (user is None or record.user == user)]
            handler._json({"keys": rows, "user": principal.user, "role": "admin" if principal.role == "owner" else principal.role,
                           **command_host(webauth, handler)})
        elif path == "/api/server/keys" and handler.command == "POST":
            payload = read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            name, scope = payload.get("name"), payload.get("scope")
            validate_name(name)
            if not isinstance(scope, str) or scope not in ("sync", "admin"):
                raise ClientError("invalid key scope")
            user = payload.get("user", principal.user)
            error = can_create(principal, user, scope)
            if error is not None:
                handler._json({"error": error}, 403)
                return
            env_default = principal.user == "env" and "user" not in payload
            if not env_default:
                validate_name(user)
            try:
                actor = principal.user
                # The env user is not stored; fall through to the default-user rule.
                key_user = None if env_default else user
                created_users = []
                record, value = keys.create(
                    name, scope, created_by=actor, user=key_user,
                    created_users=created_users)
            except KeyNameTaken:
                message = ("key name already exists" if allowed(principal, "all_keys")
                           else "key name is not available")
                handler._json({"error": message}, 409)
                return
            except UserNotFound:
                handler._json({"error": "unknown user"}, 404)
                return
            except (KeyStoreUnavailable, OSError):
                handler._json({"error": "key store unavailable"}, 503)
                return
            logger.info("created key %s scope %s", name, scope)
            if created_users:
                _audit(webauth, handler, "user.add", record)
            _audit(webauth, handler, "key.create", record)
            handler._json({"key": value, "record": record.to_public(),
                           **command_host(webauth, handler)}, 201)
        elif path.startswith("/api/server/keys/") and handler.command == "DELETE":
            if not handler._csrf_ok():
                handler.close_connection = True
                handler._error({"error": "cross-site request refused"}, 403)
                return
            name = path[len("/api/server/keys/"):]
            if not _NAME.fullmatch(name) or name in (".", ".."):
                raise ClientError("invalid or reserved key name")
            record = next((record for record in keys.list() if record.name == name), None)
            if (record is None or not can_see(principal, record)
                    or resolve(keys, users, record) is None):
                handler._json({"error": "not found"}, 404)
                return
            if not can_revoke(principal, record):
                handler._json({"error": "forbidden"}, 403)
                return
            if name == "env":
                handler._json({"error": "environment key: remove AGENTICML_SERVER_KEY and restart"}, 409)
                return
            try:
                record = keys.revoke(name, keep_admin=True, expected=record)
            except LastAdminError as exc:
                handler._json({"error": str(exc)}, 409)
                return
            except (KeyStoreUnavailable, OSError):
                handler._json({"error": "key store unavailable"}, 503)
                return
            if record is None:
                handler._json({"error": "not found"}, 404)
                return
            webauth.drop_sessions(name, record.sha256)
            _audit(webauth, handler, "key.revoke", record)
            session = getattr(handler, "session", None) or {}
            signed_out = (session.get("name") == name
                          and session.get("sha256") == record.sha256)
            headers = {"Set-Cookie": webauth._cookie("", max_age=0)} if signed_out else None
            handler._json({"revoked": name, "signed_out": signed_out}, headers=headers)
        else:
            handler.close_connection = True
            handler._json({"error": "method not allowed"}, 405)
    except ValueError as exc:
        if not getattr(handler, "_started", False):
            message = str(exc) if isinstance(exc, ClientError) else "invalid request"
            if isinstance(exc, UserRequired):
                message = "user is required when the server has more than one user"
            handler._error({"error": message}, 400)
