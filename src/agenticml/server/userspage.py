"""Admin user lifecycle endpoints for the dashboard Team page."""
from __future__ import annotations

import logging

from .db import DatabaseError
from .keys import ClientError, KeyStoreUnavailable, LastAdminError, _NAME, validate_name
from .keyspage import command_host, read_json
from .users import (
    CROSS_WORKSPACE_CREDENTIALS, IdentityTaken, ROLES, UserNameTaken, UserNotFound, allowed,
)

logger = logging.getLogger("agenticml.server.userspage")
_SELF_INVITE = "you cannot re-invite yourself; change your password from the Account menu"


def _user_name(name):
    try:
        validate_name(name)
    except ClientError:
        raise ClientError("invalid or reserved user name")


def _user_run_counts(handler):
    index = getattr(handler.server, "run_index", None)
    if index is None:
        return {}
    try:
        return index.owner_user_counts()
    except DatabaseError:
        return None


def _invite_url(webauth, handler, token):
    return command_host(webauth, handler)["host"].rstrip("/") + "/invite?token=" + token


def _audit(handler, principal, action, target, detail=None):
    handler.server.audit.record(
        actor=principal.user, via=principal.via, action=action, target=target,
        client=handler.server.web.client_address(handler), detail=detail)


def _public_row(record, key_counts, run_counts):
    return {
        **record.to_public(),
        # Temporary 2.2 API compatibility until the workspace dashboard lands.
        "role": "admin" if record.role == "owner" else record.role,
        "keys": key_counts.get(record.name, 0),
        "runs": None if run_counts is None else run_counts.get(record.name, 0),
    }


def handle(webauth, handler, path):
    from .webauth import LOGIN_BODY_MAX

    try:
        principal = handler.principal
        if not allowed(principal, "users"):
            handler.close_connection = True
            handler._json({"error": "forbidden"}, 403)
            return
        if handler.command in ("GET", "DELETE") and (
                handler.headers.get_all("Content-Length") or handler.headers.get_all("Transfer-Encoding")):
            handler.close_connection = True
        if path == "/api/server/users" and handler.command == "GET":
            try:
                records = sorted(webauth.users.list(), key=lambda record: (record.created_at, record.name))
                key_counts = {}
                for key in webauth.keys.list():
                    key_counts[key.user] = key_counts.get(key.user, 0) + 1
            except (KeyStoreUnavailable, OSError):
                handler._json({"error": "key store unavailable"}, 503)
                return
            run_counts = _user_run_counts(handler)
            handler._json({
                "users": [_public_row(record, key_counts, run_counts) for record in records],
                "me": principal.user,
            })
        elif path == "/api/server/users" and handler.command == "POST":
            payload = read_json(handler, LOGIN_BODY_MAX)
            if payload is None:
                return
            name, role = payload.get("name"), payload.get("role", "member")
            identity = payload.get("identity")
            _user_name(name)
            from .db.workspaces import AccountRepo
            existing_account = AccountRepo(webauth.keys.db).get(name)
            try:
                record = webauth.users.add(
                    name, role=role, identity=identity, created_by=principal.user)
            except UserNameTaken:
                handler._json({"error": "user name already exists"}, 409)
                return
            except IdentityTaken:
                handler._json({"error": "identity already in use"}, 409)
                return
            except (KeyStoreUnavailable, OSError):
                handler._json({"error": "key store unavailable"}, 503)
                return
            if existing_account is not None:
                _audit(handler, principal, 'user.add', name, {'role': record.role, 'identity': record.identity})
                handler._json({'user': record.to_public(), 'invite_url': None, 'expires_at': None}, 201)
                return
            try:
                token = webauth.users.create_invite(record.name)
                updated = webauth.users.get(record.name)
            except (KeyStoreUnavailable, OSError):
                webauth.users.discard(record.name, record.id)
                handler._json({"error": "key store unavailable"}, 503)
                return
            expires_at = updated.invite["expires_at"] if updated is not None and updated.invite else None
            logger.info("created user %s role %s", name, role)
            _audit(handler, principal, "user.add", name,
                   {"role": record.role, "identity": record.identity})
            _audit(handler, principal, "user.invite", name, {"expires_at": expires_at})
            handler._json({
                "user": (updated or record).to_public(),
                "invite_url": _invite_url(webauth, handler, token),
                "expires_at": expires_at,
            }, 201)
        elif path.startswith("/api/server/users/"):
            rest = path[len("/api/server/users/"):]
            if rest.endswith("/invite"):
                name = rest[:-len("/invite")]
                if handler.command != "POST":
                    handler.close_connection = True
                    handler._json({"error": "method not allowed"}, 405)
                    return
                if not _NAME.fullmatch(name) or name in (".", ".."):
                    raise ClientError("invalid or reserved user name")
                payload = read_json(handler, LOGIN_BODY_MAX)
                if payload is None:
                    return
                if name == principal.user:
                    handler._json({"error": _SELF_INVITE}, 409)
                    return
                try:
                    token = webauth.users.create_invite(name)
                    expires_at = None
                    try:
                        updated = webauth.users.get(name)
                        expires_at = updated.invite["expires_at"] if updated is not None and updated.invite else None
                    finally:
                        # The invite is already persisted, even if its read-back fails.
                        _audit(handler, principal, "user.invite", name, {"expires_at": expires_at})
                except UserNotFound:
                    handler._json({"error": "not found"}, 404)
                    return
                except (KeyStoreUnavailable, OSError):
                    handler._json({"error": "key store unavailable"}, 503)
                    return
                logger.info("created invite for %s", name)
                handler._json({
                    "invite_url": _invite_url(webauth, handler, token),
                    "expires_at": expires_at,
                })
                return
            name = rest
            if not _NAME.fullmatch(name) or name in (".", ".."):
                raise ClientError("invalid or reserved user name")
            if handler.command == "PATCH":
                payload = read_json(handler, LOGIN_BODY_MAX)
                if payload is None:
                    return
                if "role" not in payload and "identity" not in payload:
                    raise ClientError("invalid request")
                signed_out = False
                try:
                    found = webauth.users.get(name)
                    if found is None:
                        handler._json({"error": "not found"}, 404)
                        return
                    if "role" in payload:
                        role = payload.get("role")
                        if role not in ROLES[:-1]:
                            raise ClientError("invalid role")
                        if found.role == "owner":
                            handler._json({"error": "last admin"}, 409)
                            return
                        if found.role == "admin" and role != "admin":
                            if not any(item.role in ("owner", "admin") and item.name != name
                                       for item in webauth.users.list()):
                                handler._json({"error": "last admin"}, 409)
                                return
                    updated = found
                    if "identity" in payload:
                        updated = webauth.users.set_identity(name, payload.get("identity"))
                        _audit(handler, principal, "user.identity", name,
                               {"identity": updated.identity})
                    if "role" in payload:
                        previous = updated.role
                        updated = webauth.users.set_role(name, payload.get("role"))
                        _audit(handler, principal, "user.role", name,
                               {"from": previous, "to": updated.role})
                        if name == principal.user:
                            signed_out = True
                except UserNotFound:
                    handler._json({"error": "not found"}, 404)
                    return
                except LastAdminError:
                    handler._json({"error": "last admin"}, 409)
                    return
                except IdentityTaken:
                    handler._json({"error": "identity already in use"}, 409)
                    return
                except (KeyStoreUnavailable, OSError):
                    handler._json({"error": "key store unavailable"}, 503)
                    return
                headers = {"Set-Cookie": webauth._cookie("", max_age=0)} if signed_out else None
                handler._json(
                    {"user": updated.to_public(), "signed_out": signed_out},
                    headers=headers)
            elif handler.command == "DELETE":
                if not handler._csrf_ok():
                    handler.close_connection = True
                    handler._error({"error": "cross-site request refused"}, 403)
                    return
                if name == principal.user:
                    handler._json({"error": "you cannot remove yourself"}, 409)
                    return
                found = webauth.users.get(name)
                if found is None:
                    handler._json({"error": "not found"}, 404)
                    return
                try:
                    revoked = [record for record in webauth.keys.list() if record.user == name]
                    keys_revoked = webauth.users.remove(name)
                except UserNotFound:
                    handler._json({"error": "not found"}, 404)
                    return
                except LastAdminError:
                    handler._json({"error": "last admin"}, 409)
                    return
                except (KeyStoreUnavailable, OSError):
                    handler._json({"error": "key store unavailable"}, 503)
                    return
                logger.info("removed user %s (%s keys)", name, keys_revoked)
                _audit(handler, principal, "user.remove", name, {"keys_revoked": keys_revoked})
                for record in revoked:
                    webauth.drop_sessions(record.name, record.sha256)
                    _audit(handler, principal, "key.revoke", record.name,
                           {"user": record.user, "scope": record.scope})
                handler._json({"removed": name, "keys_revoked": keys_revoked})
            else:
                handler.close_connection = True
                handler._json({"error": "method not allowed"}, 405)
        else:
            handler.close_connection = True
            handler._json({"error": "method not allowed"}, 405)
    except KeyStoreUnavailable:
        if not getattr(handler, "_started", False):
            handler._error({"error": "key store unavailable"}, 503)
    except ValueError as exc:
        if not getattr(handler, "_started", False):
            message = str(exc) if isinstance(exc, ClientError) else "invalid request"
            if message == "invalid or reserved key name":
                message = "invalid or reserved user name"
            status = 403 if message == CROSS_WORKSPACE_CREDENTIALS else 400
            handler._error({"error": message}, status)
