"""Admin key lifecycle endpoints and bounded run-owner counts."""
from __future__ import annotations

import json
import logging
import os

from agenticml.ui.server import _reject_nonfinite
from .keys import ClientError, KeyNameTaken, KeyStoreUnavailable, LastAdminError, _NAME, validate_name
from .users import attribution

logger = logging.getLogger("agenticml.server.keyspage")
RUN_SCAN_MAX = 10000


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


def _run_counts(root):
    counts = {}
    try:
        with os.scandir(root / "runs") as entries:
            for index, entry in enumerate(entries):
                if index >= RUN_SCAN_MAX:
                    return None
                if not entry.is_dir(follow_symlinks=False):
                    continue
                owner, owner_key = attribution(entry.path)
                name = owner_key or owner
                if name is not None:
                    counts[name] = counts.get(name, 0) + 1
    except FileNotFoundError:
        pass
    except OSError:
        return None
    return counts


def handle(webauth, handler, path):
    from .webauth import LOGIN_BODY_MAX

    try:
        if handler.command in ("GET", "DELETE") and (
                handler.headers.get_all("Content-Length") or handler.headers.get_all("Transfer-Encoding")):
            handler.close_connection = True
        if path == "/api/server/keys" and handler.command == "GET":
            records = sorted(webauth.keys.list(), key=lambda record: (
                record.name == "env", record.created_at, record.name))
            counts = _run_counts(handler.server.root)
            rows = [{**record.to_public(), "runs": None if counts is None else counts.get(record.name, 0),
                     "revocable": record.name != "env"} for record in records]
            handler._json({"keys": rows, **command_host(webauth, handler)})
        elif path == "/api/server/keys" and handler.command == "POST":
            payload = read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            name, scope = payload.get("name"), payload.get("scope")
            validate_name(name)
            if not isinstance(scope, str) or scope not in ("sync", "admin"):
                raise ClientError("invalid key scope")
            try:
                actor = handler.principal.user
                # The env user is not stored; fall through to the default-user rule.
                key_user = None if actor == "env" else actor
                record, value = webauth.keys.create(
                    name, scope, created_by=actor, user=key_user)
            except KeyNameTaken:
                handler._json({"error": "key name already exists"}, 409)
                return
            except (KeyStoreUnavailable, OSError):
                handler._json({"error": "key store unavailable"}, 503)
                return
            logger.info("created key %s scope %s", name, scope)
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
            if name == "env":
                handler._json({"error": "environment key: remove AGENTICML_SERVER_KEY and restart"}, 409)
                return
            try:
                record = webauth.keys.revoke(name, keep_admin=True)
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
            signed_out = handler.session["name"] == name and handler.session["sha256"] == record.sha256
            headers = {"Set-Cookie": webauth._cookie("", max_age=0)} if signed_out else None
            handler._json({"revoked": name, "signed_out": signed_out}, headers=headers)
        else:
            handler.close_connection = True
            handler._json({"error": "method not allowed"}, 405)
    except ValueError as exc:
        if not getattr(handler, "_started", False):
            handler._error({"error": str(exc) if isinstance(exc, ClientError) else "invalid request"}, 400)
