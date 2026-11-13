"""Single route table for every HTTP route the server answers.

Dashboard dispatch (webauth) matches this table. Sync (/v2) keeps its own
router in ingest.py; those rows are listed here so the isolation suite can
enumerate everything.
"""
from __future__ import annotations

import logging
import re

from .access import WorkspaceNotFound, bind_dashboard
from .users import allowed

logger = logging.getLogger("agenticml.server.routes")

_SLUG = r"(?P<slug>[a-z0-9][a-z0-9-]{0,39})"
_RUN = r"(?P<run_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
_SHA = r"(?P<sha>[0-9a-f]{64})"
_NAME = r"(?P<name>[^/]+)"
_LOGIN = r"(?P<name>[^/]+)"
_V2_ID = r"(?P<run_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
_V2_FILE = r"(?P<path>.+)"


class Route:
    __slots__ = ("method", "pattern", "scope", "action", "handler", "name", "target", "regex")

    def __init__(self, method, pattern, scope, action, handler, name, target=None):
        self.method = method
        self.pattern = pattern
        self.scope = scope
        self.action = action
        self.handler = handler
        self.name = name
        self.target = target
        self.regex = re.compile(pattern)


def _r(method, pattern, scope, action, handler, name, target=None):
    return Route(method, pattern, scope, action, handler, name, target)


def _ws(method, rest, action, handler, name, target=None):
    return _r(method, r"/api/w/" + _SLUG + r"/" + rest, "workspace", action, handler, name, target=target)


_RUN_SUBS = (
    ("", "run_meta", "/api/runs/{run_id}"),
    ("/notes", "run_notes", "/api/runs/{run_id}/notes"),
    ("/scalars", "run_scalars", "/api/runs/{run_id}/scalars"),
    ("/time", "run_time", "/api/runs/{run_id}/time"),
    ("/media", "run_media", "/api/runs/{run_id}/media"),
    ("/code", "run_code", "/api/runs/{run_id}/code"),
    ("/lineage", "run_lineage", "/api/runs/{run_id}/lineage"),
)

_ROUTES = []
_ROUTES.extend([
    _r("GET", r"/healthz", "public", None, "health", "healthz"),
    _r("GET", r"/login", "public", None, "login_page", "login_get"),
    _r("POST", r"/login", "public", None, "login", "login_post"),
    _r("POST", r"/logout", "public", None, "logout", "logout"),
    _r("GET", r"/setup", "public", None, "setup_page", "setup_get"),
    _r("POST", r"/setup", "public", None, "setup", "setup_post"),
    _r("GET", r"/invite", "public", None, "invite_page", "invite_get"),
    _r("POST", r"/invite", "public", None, "invite", "invite_post"),
    _r("POST", r"/auth/firebase", "public", None, "firebase_auth", "auth_firebase"),
    _r("GET", r"/auth/firebase/config", "public", None, "firebase_config", "auth_firebase_config"),
    _r("GET", r"/static/login.js", "public", None, "public_asset", "static_login_js"),
    _r("GET", r"/static/setup.js", "public", None, "public_asset", "static_setup_js"),
    _r("GET", r"/static/invite.js", "public", None, "public_asset", "static_invite_js"),
    _r("GET", r"/static/firebase-login.js", "public", None, "public_asset", "static_firebase_login_js"),
    _r("GET", r"/static/vendor/firebase/firebase-app-compat.js", "public", None, "public_asset", "static_firebase_app"),
    _r("GET", r"/static/vendor/firebase/firebase-auth-compat.js", "public", None, "public_asset", "static_firebase_auth"),
    _r("GET", r"/static/app.css", "public", None, "public_asset", "static_app_css"),
    _r("GET", r"/static/InstrumentSans.woff2", "public", None, "public_asset", "static_font_sans"),
    _r("GET", r"/static/JetBrainsMono.woff2", "public", None, "public_asset", "static_font_mono"),
    _r("GET", r"/v2/whoami", "sync", "sync", "whoami", "v2_whoami"),
    _r("POST", r"/v2/objects/missing", "sync", "sync", "missing", "v2_objects_missing"),
    _r("PUT", r"/v2/objects/" + _SHA, "sync", "sync", "object", "v2_object"),
    _r("GET", r"/v2/runs/" + _V2_ID + r"/state", "sync", "sync", "state", "v2_run_state"),
    _r("GET", r"/v2/runs/" + _V2_ID + r"/lineage", "sync", "sync", "lineage_state", "v2_lineage_state"),
    _r("PUT", r"/v2/runs/" + _V2_ID + r"/lineage", "sync", "sync", "lineage", "v2_lineage_put"),
    _r("PUT", r"/v2/runs/" + _V2_ID + r"/files/" + _V2_FILE, "sync", "sync", "atomic", "v2_atomic"),
    _r("POST", r"/v2/runs/" + _V2_ID + r"/append/" + _V2_FILE, "sync", "sync", "append", "v2_append"),
    _r("POST", r"/v2/runs/" + _V2_ID + r"/commit", "sync", "sync", "commit", "v2_commit"),
    _r("GET", r"/", "account", None, "home", "home"),
    _r("GET", r"/workspaces", "account", None, "workspaces_page", "workspaces_page"),
    _r("GET", r"/api/account/session", "account", None, "session", "account_session"),
    _r("POST", r"/api/account/password", "account", "password", "account_password", "account_password"),
    _r("GET", r"/api/account/workspaces", "account", None, "account_workspaces", "account_workspaces"),
    _r("POST", r"/api/account/workspaces", "account", None, "account_workspaces_create", "account_workspaces_create"),
    _r("GET", r"/static/app.js", "account", "read", "static_file", "static_app_js", target="app.js"),
    _r("GET", r"/static/charts.js", "account", "read", "static_file", "static_charts_js", target="charts.js"),
    _r("GET", r"/static/code.js", "account", "read", "static_file", "static_code_js", target="code.js"),
    _r("GET", r"/static/codediff.js", "account", "read", "static_file", "static_codediff_js", target="codediff.js"),
    _r("GET", r"/w/" + _SLUG + r"/?", "workspace", "read", "dashboard_get", "workspace_page", target="/"),
    _r("GET", r"/admin/?", "server_admin", None, "dashboard_get", "admin_page", target="/"),
    _r("GET", r"/api/admin/workspaces", "server_admin", None, "admin_workspaces", "admin_workspaces_list"),
    _r("POST", r"/api/admin/workspaces", "server_admin", None, "admin_workspaces", "admin_workspaces_create"),
    _r("PATCH", r"/api/admin/workspaces/" + _SLUG, "server_admin", None, "admin_workspaces", "admin_workspaces_patch"),
    _r("GET", r"/api/admin/server-admins", "server_admin", None,
       "admin_server_admins", "admin_server_admins_list"),
    _r("POST", r"/api/admin/server-admins", "server_admin", None,
       "admin_server_admins", "admin_server_admins_add"),
    _r("DELETE", r"/api/admin/server-admins/" + _LOGIN, "server_admin", None,
       "admin_server_admins", "admin_server_admins_remove"),
    _r("GET", r"/api/admin/audit", "server_admin", None, "admin_audit", "admin_audit"),
])
for sub, name, target in _RUN_SUBS:
    _ROUTES.append(_ws("GET", r"runs/" + _RUN + sub, "read", "dashboard_get", name, target))
_ROUTES.extend([
    _ws("GET", r"runs", "read", "dashboard_get", "runs_list", "/api/runs"),
    _ws("GET", r"metrics", "read", "dashboard_get", "metrics_list", "/api/metrics"),
    _ws("PUT", r"runs/" + _RUN + r"/notes", "notes", "dashboard_put", "run_notes_put", "/api/runs/{run_id}/notes"),
    _ws("GET", r"compare/code", "read", "dashboard_get", "compare_code", "/api/compare/code"),
    _ws("GET", r"lineage", "read", "dashboard_get", "lineage_graph", "/api/lineage"),
    _ws("GET", r"notes", "read", "dashboard_get", "notes_list", "/api/notes"),
    _ws("GET", r"objects/" + _SHA, "read", "dashboard_get", "object_get", "/api/objects/{sha}"),
    _ws("GET", r"usage", "read", "usage", "workspace_usage"),
    _ws("GET", r"keys", "own_keys", "keys", "keys_list"),
    _ws("POST", r"keys", "own_keys", "keys", "keys_create"),
    _ws("DELETE", r"keys/" + _NAME, "own_keys", "keys", "keys_revoke"),
    _ws("GET", r"members", "users", "members", "members_list"),
    _ws("POST", r"members", "users", "members", "members_create"),
    _ws("PATCH", r"members/" + _LOGIN, "users", "members", "members_patch"),
    _ws("DELETE", r"members/" + _LOGIN, "users", "members", "members_delete"),
    _ws("POST", r"members/" + _LOGIN + r"/invite", "users", "members", "members_invite"),
    _ws("GET", r"invites", "users", "invites", "invites_list"),
    _ws("POST", r"invites", "users", "invites", "invites_create"),
    _ws("DELETE", r"invites/" + _LOGIN, "users", "invites", "invites_revoke"),
    _ws("GET", r"audit", "audit", "audit", "audit"),
])
ROUTES = tuple(_ROUTES)


def route_table():
    """Return the module-level route table (method, pattern, scope, action, handler)."""
    return ROUTES


def match_routes(path):
    found = []
    for route in ROUTES:
        matched = route.regex.fullmatch(path)
        if matched is not None:
            found.append((route, matched))
    return found


def _not_found(handler):
    handler._error({"error": "not found"}, 404)
    return True


def _forbidden(handler, action):
    handler.close_connection = True
    if action == "notes":
        handler._error({"error": "read-only"}, 403)
    else:
        handler._json({"error": "forbidden"}, 403)
    return True


def _method_not_allowed(handler, methods):
    handler.close_connection = True
    allow = ", ".join(dict.fromkeys(methods))
    handler._json({"error": "method not allowed"}, 405, {"Allow": allow})
    return True


def _key_workspace_id(handler):
    principal = getattr(handler, "principal", None)
    session = getattr(handler, "session", None) or {}
    if principal is None or principal.via != "key":
        return None
    return session.get("workspace_id")


def _bind_membership(webauth, handler, match):
    slug = match.groupdict().get("slug") or "default"
    try:
        ctx = bind_dashboard(
            handler.server, slug, handler.principal,
            key_workspace_id=_key_workspace_id(handler))
    except WorkspaceNotFound:
        return None
    handler.workspace = ctx
    handler.principal = ctx.principal
    return ctx


def _require_action(handler, route):
    if route.action and not allowed(handler.principal, route.action):
        raise PermissionError(route.action)


def _dashboard_get(webauth, handler, route, match):
    groups = {key: value for key, value in match.groupdict().items() if key != "slug" and value is not None}
    handler._effective_path = route.target.format(**groups) if route.target else "/"
    handler._get()


def _dashboard_put(webauth, handler, route, match):
    groups = {key: value for key, value in match.groupdict().items() if key != "slug" and value is not None}
    handler._effective_path = route.target.format(**groups)
    handler._put()


def _keys(webauth, handler, route, match):
    from . import keyspage
    groups = match.groupdict()
    path = "/api/server/keys" if "name" not in groups else "/api/server/keys/" + groups["name"]
    keyspage.handle(webauth, handler, path)


def _members(webauth, handler, route, match):
    from . import userspage
    groups = match.groupdict()
    rest = groups.get("name") or ""
    path = "/api/server/users"
    if rest:
        path += "/" + rest
        if route.name.endswith("invite"):
            path += "/invite"
    userspage.handle(webauth, handler, path, show_owner=True)


def _audit(webauth, handler, route, match):
    from . import audit
    audit.handle(webauth, handler)


def _session(webauth, handler, route, match):
    webauth._session_info(handler)


def _account_password(webauth, handler, route, match):
    webauth._account_password(handler)


def _account_workspaces(webauth, handler, route, match):
    handler._json({"workspaces": webauth._account_workspaces(handler)}, headers=webauth._headers())


def _account_workspaces_create(webauth, handler, route, match):
    from .db import DatabaseError
    from .db.workspaces import AccountRepo, WorkspaceRepo, validate_slug
    from .keys import ClientError
    from .keyspage import read_json
    from .limits import LimitReached, free_workspace_limit_error
    from .webauth import LOGIN_BODY_MAX
    from .workspaces import prepare_store

    if not webauth.open_signup:
        handler.close_connection = True
        handler._json({"error": "not found"}, 404, webauth._headers())
        return
    payload = read_json(handler, LOGIN_BODY_MAX)
    if payload is None:
        return
    slug, name = payload.get("slug"), payload.get("name")
    if not isinstance(slug, str) or not isinstance(name, str):
        handler._json({"error": "invalid request"}, 400, webauth._headers())
        return
    account = AccountRepo(handler.server.db).get(handler.principal.user)
    if account is None:
        handler.close_connection = True
        handler._json({"error": "not found"}, 404, webauth._headers())
        return
    if not account.get("email_verified"):
        handler._json({"error": "verify your email"}, 403, webauth._headers())
        return
    try:
        validate_slug(slug)
    except ClientError as exc:
        handler._json({"error": str(exc)}, 400, webauth._headers())
        return
    retry_after, ticket = webauth.throttle.begin("slug:" + account["id"])
    if ticket is None:
        handler.close_connection = True
        handler._json(
            {"error": "too many attempts; try again later"},
            429, webauth._headers({"Retry-After": str(retry_after)}),
        )
        return
    outcome = "ignored"
    try:
        repo = WorkspaceRepo(handler.server.db)
        plans = getattr(handler.server, "plans", None)
        try:
            with handler.server.db.transaction(write=True) as tx:
                tx.advisory_lock("users")
                limit = None if plans is None else plans.free_workspaces_per_account
                if limit is not None and repo.owned_free_count(account["id"], tx) >= limit:
                    raise free_workspace_limit_error(limit)
                row = repo.create(slug, name, account["login"], tx, plan="free", personal=0)
        except LimitReached as exc:
            handler._json({"error": str(exc)}, 403, webauth._headers())
            return
        except ClientError as exc:
            message = str(exc)
            status = 409 if message == "workspace slug already exists" else 400
            if status == 409:
                outcome = "failed"
            handler._json({"error": message}, status, webauth._headers())
            return
        try:
            prepare_store(handler.server.data_dir, row["id"])
        except (OSError, DatabaseError):
            logger.exception("could not create workspace store")
            try:
                repo.delete(row["slug"])
                from .workspaces import discard_store
                discard_store(handler.server.data_dir, row["id"])
            except Exception:
                logger.exception("could not delete workspace after store failure")
            handler._json({"error": "could not create workspace"}, 503, webauth._headers())
            return
        try:
            webauth._audit(
                handler, "workspace.create", row["id"],
                {"slug": row["slug"], "name": row["name"]},
                via="dashboard")
        except Exception:
            pass
        outcome = "success"
        handler._json({"slug": row["slug"], "name": row["name"]}, 201, webauth._headers())
    finally:
        webauth.throttle.finish(ticket, outcome)


def _usage(webauth, handler, route, match):
    from datetime import timedelta, timezone

    from .db.usage import UsageRepo
    from .db.workspaces import WorkspaceRepo

    plans = getattr(handler.server, "plans", None)
    if plans is None:
        handler._json({"limits": None})
        return
    workspace = handler.workspace
    row = WorkspaceRepo(handler.server.db).by_id(workspace.id)
    if row is None:
        from .access import WorkspaceNotFound
        raise WorkspaceNotFound()
    limits = plans.limits_for(row)
    usage_repo = UsageRepo(handler.server.db, workspace.id)
    counters = usage_repo.get()
    now = plans.clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    day = now.date().isoformat()
    resets = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    handler._json({
        "plan": row["plan"],
        "storage": {"used": int(counters.get("stored_bytes") or 0), "limit": limits["storage_bytes"]},
        "uploads_today": {
            "used": _uploads_today(counters, day),
            "limit": limits["upload_bytes_per_day"],
            "resets_at": resets.strftime("%Y-%m-%dT00:00:00Z"),
        },
        "requests_per_minute": {"limit": limits["requests_per_minute"]},
        "people": {"used": usage_repo.people(now=now), "limit": limits["people"]},
    })


def _home(webauth, handler, route, match):
    workspaces = webauth._account_workspaces(handler)
    cookie_slug = webauth._workspace_cookie(handler)
    slug = None
    if cookie_slug and any(row["slug"] == cookie_slug for row in workspaces):
        slug = cookie_slug
    elif len(workspaces) == 1:
        slug = workspaces[0]["slug"]
    location = "/w/" + slug + "/" if slug else "/workspaces"
    handler._send(b"", "text/plain; charset=utf-8", 303, webauth._headers({"Location": location}))


def _workspaces_page(webauth, handler, route, match):
    from html import escape as html_escape

    workspaces = webauth._account_workspaces(handler)
    if workspaces:
        items = "".join(
            '<li><a class="btn btn-ghost workspace-switcher-item" href="/w/{slug}/">'
            '<span class="workspace-switcher-name">{name}</span>{role}</a></li>'.format(
                slug=html_escape(row["slug"], quote=True),
                name=html_escape(row["name"] or row["slug"], quote=True),
                role=(
                    '<span class="workspace-switcher-role">{0}</span>'.format(
                        html_escape(row["role"], quote=True))
                    if row.get("role") else ""),
            )
            for row in workspaces)
        body_inner = '<ul class="workspace-list" aria-label="Workspaces">' + items + "</ul>"
    else:
        body_inner = (
            "<p class=\"muted\">You are not a member of any workspace; "
            "ask an admin to invite you.</p>")
    admin_link = (
        '<a class="btn btn-ghost btn-sm" href="/admin/">Admin</a>'
        if getattr(handler.principal, "server_admin", False) else "")
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        "<meta name=\"color-scheme\" content=\"light dark\"/>"
        "<title>agenticml · Workspaces</title>"
        "<link rel=\"stylesheet\" href=\"/static/app.css\"/></head>"
        "<body class=\"picker-page\">"
        "<header class=\"topbar\">"
        "<a href=\"/workspaces\" class=\"breadcrumb\">agenticml / workspaces</a>"
        + admin_link +
        "</header>"
        "<main id=\"app\"><div class=\"workspace stack\">"
        "<div class=\"heading\"><h1>Workspaces</h1></div>"
        + body_inner +
        "</div></main></body></html>"
    ).encode("utf-8")
    handler._send(body, "text/html; charset=utf-8", headers=webauth._headers())


def _uploads_today(usage, day):
    if usage.get("upload_day") and usage["upload_day"] >= day:
        return int(usage.get("upload_bytes") or 0)
    return 0


def _admin_usage_fields(handler, row):
    from .db.usage import UsageRepo, utc_day

    usage = UsageRepo(handler.server.db, row["id"])
    counters = usage.get()
    return {
        "plan": row.get("plan") or "free",
        "stored_bytes": int(counters.get("stored_bytes") or 0),
        "uploads_today": _uploads_today(counters, utc_day()),
        "people": usage.people(),
    }


def _decoded_limits(row):
    import json
    raw = row.get("custom_limits") if row else None
    return json.loads(raw) if raw else None


def _admin_public(row, extra=None):
    payload = {
        "slug": row["slug"], "name": row["name"], "status": row["status"],
        "members": row.get("members", 0), "created_at": row["created_at"],
        "plan": row.get("plan") or "free",
        "stored_bytes": 0, "uploads_today": 0, "people": 0,
        "limits": _decoded_limits(row),
    }
    if extra:
        payload.update(extra)
    return payload


def _plan_values(row):
    return {
        "plan": row["plan"],
        "limits": _decoded_limits(row),
    }


def _merged_custom_limits(handler, current, limits):
    from .limits import UNLIMITED
    decoded = _decoded_limits(current)
    if current is not None and current.get("plan") == "custom" and decoded is not None:
        merged = dict(decoded)
    else:
        plans = getattr(handler.server, "plans", None)
        merged = dict(plans.free) if plans is not None else dict(UNLIMITED)
    if limits is None:
        return merged
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    merged.update(limits)
    return merged


def _server_audit(handler):
    from .audit import AuditLog
    return AuditLog(handler.server.data_dir / "server", db=handler.server.db, workspace_id=None)


def _discard_pending_owner(handler, accounts, login):
    """Remove an owner account this request created, if it is still unused."""
    from .db import DatabaseError

    try:
        with handler.server.db.transaction(write=True) as tx:
            tx.advisory_lock("users")
            account = accounts.get(login, tx)
            if account is not None and not accounts.can_sign_in(account):
                accounts.discard_unreferenced(account["id"], account["login"], tx)
    except (DatabaseError, OSError):
        logger.warning("could not remove the unused owner account after a failed workspace create")


def _admin_workspaces(webauth, handler, route, match):
    from .db.workspaces import AccountRepo, WorkspaceRepo, is_email_login
    from .keys import ClientError
    from .keyspage import read_json
    from .webauth import LOGIN_BODY_MAX
    from .workspaces import prepare_store

    repo = WorkspaceRepo(handler.server.db)
    principal = handler.principal
    if handler.command == "GET":
        handler._json({"workspaces": [
            _admin_public(row, _admin_usage_fields(handler, row)) for row in repo.list()]},
                      headers=webauth._headers())
        return
    if handler.command == "POST":
        payload = read_json(handler, LOGIN_BODY_MAX)
        if payload is None:
            return
        slug, name, owner = payload.get("slug"), payload.get("name"), payload.get("owner")
        accounts = AccountRepo(handler.server.db)
        owner_created = False
        owner_login = owner
        try:
            # One transaction: a refused workspace never leaves a new owner
            # account behind. A new owner is created only for an email, and
            # only when Firebase can later link it.
            with handler.server.db.transaction(write=True) as tx:
                tx.advisory_lock("users")
                owner_created = (
                    webauth.firebase is not None and is_email_login(owner)
                    and accounts.get(owner, tx) is None)
                row = repo.create(
                    slug, name, owner, tx,
                    create_owner=principal.user if owner_created else None)
                owner_account = accounts.get(owner, tx)
                owner_login = owner_account["login"] if owner_account else owner
        except ClientError as exc:
            handler._json({"error": str(exc)}, 400, webauth._headers())
            return
        try:
            prepare_store(handler.server.data_dir, row["id"])
        except OSError:
            try:
                repo.delete(row["slug"])
                from .workspaces import discard_store
                discard_store(handler.server.data_dir, row["id"])
            except ClientError:
                pass
            if owner_created:
                _discard_pending_owner(handler, accounts, owner_login)
            handler._json({"error": "could not create workspace"}, 503, webauth._headers())
            return
        try:
            log = _server_audit(handler)
            client = webauth.client_address(handler)
            if owner_created:
                log.record(principal.user, principal.via, "account.create", owner_login,
                           client, {"pending": True})
            log.record(principal.user, principal.via, "workspace.create", row["id"], client,
                       {"slug": row["slug"], "owner": owner_login, "name": row["name"]})
        except Exception:
            pass
        handler._json({"workspace": _admin_public(
            {**row, "members": 1}, _admin_usage_fields(handler, row)),
            "owner_created": owner_created}, 201, webauth._headers())
        return
    if handler.command == "PATCH":
        payload = read_json(handler, LOGIN_BODY_MAX)
        if payload is None:
            return
        slug = match.groupdict()["slug"]
        name, new_slug, status = payload.get("name"), payload.get("slug"), payload.get("status")
        plan, limits = payload.get("plan"), payload.get("limits")
        if (plan is not None or "limits" in payload) and (
                name is not None or new_slug is not None or status is not None):
            handler._json(
                {"error": "cannot change plan or limits together with name, slug, or status"},
                400, webauth._headers())
            return
        if name is None and new_slug is None and status is None and plan is None:
            handler._json({"error": "invalid request"}, 400, webauth._headers())
            return
        if plan is None and "limits" in payload:
            handler._json({"error": "invalid request"}, 400, webauth._headers())
            return
        row = None
        if plan is not None:
            if plan == "free" and limits is not None:
                handler._json({"error": "free plan cannot have custom limits"}, 400,
                              webauth._headers())
                return
            try:
                if plan == "custom":
                    current = repo.get(slug)
                    if current is None:
                        raise ClientError("unknown workspace")
                    limits = _merged_custom_limits(handler, current, limits)
                old, row = repo.set_plan(slug, plan, None if plan == "free" else limits)
            except ClientError as exc:
                message = str(exc)
                handler._json({"error": message}, 404 if message == "unknown workspace" else 400,
                              webauth._headers())
                return
            except ValueError as exc:
                handler._json({"error": str(exc)}, 400, webauth._headers())
                return
            try:
                _server_audit(handler).record(
                    principal.user, principal.via, "workspace.plan", row["id"],
                    webauth.client_address(handler),
                    {"slug": row["slug"], "old": _plan_values(old), "new": _plan_values(row)})
            except Exception:
                pass
            slug = row["slug"]
        if name is not None or new_slug is not None or status is not None:
            try:
                before = repo.get(slug)
                row = repo.update(slug, name=name, new_slug=new_slug, status=status)
            except ClientError as exc:
                message = str(exc)
                handler._json({"error": message}, 404 if message == "unknown workspace" else 400,
                              webauth._headers())
                return
            try:
                log = _server_audit(handler)
                client = webauth.client_address(handler)
                if new_slug not in (None, slug) or name not in (None, before["name"] if before else name):
                    log.record(principal.user, principal.via, "workspace.rename", row["id"], client,
                               {"slug": row["slug"], "name": row["name"]})
                if status is not None and before is not None and status != before["status"]:
                    action = "workspace.suspend" if status == "suspended" else "workspace.resume"
                    log.record(principal.user, principal.via, action, row["id"], client,
                               {"slug": row["slug"], "status": row["status"]})
            except Exception:
                pass
        if row is None:
            row = repo.get(slug)
        members = next((item.get("members", 0) for item in repo.list() if item["id"] == row["id"]), 0)
        handler._json({"workspace": _admin_public(
            {**row, "members": members}, _admin_usage_fields(handler, row))},
                      headers=webauth._headers())
        return
    handler.close_connection = True
    handler._json({"error": "method not allowed"}, 405, webauth._headers())


_LIST_RETURN_NOTE = (
    "An email still on the admin email list gets server admin back at its next sign-in.")


def _server_admin_public(row):
    from .db.workspaces import is_email_login

    login = row["login"]
    email = row.get("email") or (login if is_email_login(login) else None)
    return {
        "login": login,
        "display_name": row.get("display_name") or login,
        "email": email,
        "source": "list" if row.get("server_admin_source") == "list" else "manual",
        "signin": ("firebase" if row.get("firebase_uid") is not None
                   else "identity" if row.get("identity") is not None
                   else "password" if row.get("password") is not None else "pending"),
        "firebase": row.get("firebase_uid") is not None,
        "pending": (row.get("password") is None and row.get("identity") is None
                    and row.get("firebase_uid") is None),
        "created_at": row["created_at"],
    }


def _admin_server_admins(webauth, handler, route, match):
    from .db.workspaces import AccountRepo, LastServerAdminError, is_email_login
    from .keys import ClientError
    from .keyspage import read_json
    from .webauth import LOGIN_BODY_MAX

    headers = webauth._headers()
    accounts = AccountRepo(handler.server.db)
    principal = handler.principal
    if handler.command == "GET":
        handler._json({"server_admins": [
            _server_admin_public(row) for row in accounts.server_admins()]}, headers=headers)
        return
    if handler.command == "POST":
        payload = read_json(handler, LOGIN_BODY_MAX)
        if payload is None:
            return
        login = payload.get("login")
        if not isinstance(login, str) or not login.strip() or len(login) > 254:
            handler._json({"error": "invalid request"}, 400, headers)
            return
        login = login.strip()
        created = False
        try:
            with handler.server.db.transaction(write=True) as tx:
                tx.advisory_lock("users")
                account = accounts.get(login, tx)
                if account is None:
                    if webauth.firebase is None or not is_email_login(login):
                        raise ClientError("unknown account")
                    account = accounts.create(login, None, principal.user, tx)
                    created = True
                row = accounts.set_server_admin(account["login"], True, source="manual", tx=tx)
        except ClientError as exc:
            message = str(exc)
            handler._json({"error": message}, 404 if message == "unknown account" else 400,
                          headers)
            return
        try:
            log = _server_audit(handler)
            client = webauth.client_address(handler)
            if created:
                log.record(principal.user, principal.via, "account.create", row["login"],
                           client, {"pending": True})
            log.record(principal.user, principal.via, "account.server_admin", row["login"],
                       client, {"enabled": True, "source": "web"})
        except Exception:
            pass
        handler._json({"server_admin": _server_admin_public(row), "account_created": created},
                      201 if created else 200, headers)
        return
    if handler.command == "DELETE":
        if not handler._csrf_ok():
            handler.close_connection = True
            handler._error({"error": "cross-site request refused"}, 403)
            return
        login = match.groupdict().get("name")
        try:
            with handler.server.db.transaction(write=True) as tx:
                tx.advisory_lock("users")
                account = accounts.get(login, tx)
                if account is None or not account["server_admin"]:
                    raise ClientError("unknown account")
                row = accounts.set_server_admin(account["login"], False, tx=tx)
        except LastServerAdminError as exc:
            handler._json({"error": str(exc)}, 409, headers)
            return
        except ClientError as exc:
            handler._json({"error": str(exc)}, 404, headers)
            return
        try:
            _server_audit(handler).record(
                principal.user, principal.via, "account.server_admin", row["login"],
                webauth.client_address(handler), {"enabled": False, "source": "web"})
        except Exception:
            pass
        handler._json({"removed": row["login"], "note": _LIST_RETURN_NOTE}, headers=headers)
        return
    handler.close_connection = True
    handler._json({"error": "method not allowed"}, 405, headers)


def _admin_audit(webauth, handler, route, match):
    import re
    from urllib.parse import parse_qs, urlsplit

    from .audit import AuditLog, _TIMESTAMP
    from .db import DatabaseError

    headers = webauth._headers()
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    try:
        limits = query.get("limit", ["50"])
        if len(limits) != 1 or re.fullmatch(r"[0-9]+", limits[0]) is None:
            raise ValueError("invalid limit")
        limit = int(limits[0])
        if limit < 1:
            raise ValueError("invalid limit")
    except ValueError:
        handler._json({"error": "invalid limit"}, 400, headers)
        return
    cursors = query.get("before", [])
    if len(cursors) > 1 or (cursors and _TIMESTAMP.fullmatch(cursors[0]) is None):
        handler._json({"error": "invalid before"}, 400, headers)
        return
    before = cursors[0] if cursors else None
    try:
        events = AuditLog(
            handler.server.data_dir / "server", db=handler.server.db, workspace_id=None
        ).repo.tail(min(limit, 1000), before)
    except (OSError, ValueError, DatabaseError):
        handler._json({"error": "audit log unavailable"}, 503, headers)
        return
    handler._json({"events": events, "skipped": 0}, 200, headers)


def _static_file(webauth, handler, route, match):
    handler._effective_path = "/static/" + (route.target or match.groupdict().get("name", ""))
    handler._get()


def _invites(webauth, handler, route, match):
    from .keyspage import command_host, read_json
    from .keys import ClientError
    from .webauth import LOGIN_BODY_MAX

    principal = handler.principal
    repo = handler.workspace.invites
    if handler.command == "GET":
        handler._json({"invites": repo.list()})
        return
    if handler.command == "POST":
        payload = read_json(handler, LOGIN_BODY_MAX)
        if payload is None:
            return
        login, role = payload.get("login", payload.get("name")), payload.get("role", "member")
        try:
            token = repo.create(login, role, principal.user, plans=getattr(handler.server, "plans", None))
        except ClientError as exc:
            from .limits import LimitReached
            handler._json({"error": str(exc)}, 403 if isinstance(exc, LimitReached) else 400)
            return
        rows = [row for row in repo.list() if row["login"] == login]
        expires_at = rows[0]["expires_at"] if rows else None
        try:
            handler.workspace.audit.record(
                principal.user, principal.via, "invite.create", login,
                webauth.client_address(handler), {"role": role, "expires_at": expires_at})
        except Exception:
            pass
        if token is None:
            handler._json({"added": True, "login": login, "role": role,
                           "invite_url": None, "expires_at": None}, 201)
            return
        url = command_host(webauth, handler)["host"].rstrip("/") + "/invite?token=" + token
        handler._json({"added": False, "login": login, "role": role,
                       "invite_url": url, "expires_at": expires_at}, 201)
        return
    if handler.command == "DELETE":
        if not handler._csrf_ok():
            handler.close_connection = True
            handler._error({"error": "cross-site request refused"}, 403)
            return
        login = match.groupdict().get("name")
        row = repo.revoke(login)
        if row is None:
            handler._json({"error": "not found"}, 404)
            return
        try:
            handler.workspace.audit.record(
                principal.user, principal.via, "invite.revoke", login,
                webauth.client_address(handler), {})
        except Exception:
            pass
        handler._json({"revoked": login})
        return
    handler.close_connection = True
    handler._json({"error": "method not allowed"}, 405)


HANDLERS = {
    "dashboard_get": _dashboard_get,
    "dashboard_put": _dashboard_put,
    "keys": _keys,
    "members": _members,
    "invites": _invites,
    "audit": _audit,
    "session": _session,
    "account_password": _account_password,
    "account_workspaces": _account_workspaces,
    "account_workspaces_create": _account_workspaces_create,
    "home": _home,
    "workspaces_page": _workspaces_page,
    "admin_workspaces": _admin_workspaces,
    "admin_server_admins": _admin_server_admins,
    "admin_audit": _admin_audit,
    "static_file": _static_file,
    "usage": _usage,
}


def dispatch_dashboard(webauth, handler, path):
    """Match and run a dashboard route. True means this request was answered."""
    if path is None:
        return _not_found(handler)
    matches = match_routes(path)
    dashboard = [(route, matched) for route, matched in matches
                 if route.scope not in ("public", "sync")]
    # Non-admins must not learn that server-admin routes exist: a POST to
    # /api/admin/workspaces would otherwise 404 while POST /api/no-such is 405.
    if not getattr(getattr(handler, "principal", None), "server_admin", False):
        dashboard = [(route, matched) for route, matched in dashboard
                     if route.scope != "server_admin"]
    if not dashboard:
        return False
    # Membership before method selection: a non-member gets the unknown-path
    # 404 for every method, never a 405 with Allow.
    workspace_matches = [(route, matched) for route, matched in dashboard
                         if route.scope == "workspace"]
    if workspace_matches:
        if _bind_membership(webauth, handler, workspace_matches[0][1]) is None:
            return _not_found(handler)
        plans = getattr(handler.server, 'plans', None)
        if plans is not None:
            try:
                retry = plans.request(handler.server.db, handler.workspace.id)
            except WorkspaceNotFound:
                return _not_found(handler)
            if retry is not None:
                handler.close_connection = True
                handler._json({'error': 'too many requests'}, 429, {'Retry-After': str(retry)})
                return True
    allowed_methods = [route.method for route, _ in dashboard]
    chosen = next(((route, matched) for route, matched in dashboard if route.method == handler.command), None)
    if chosen is None:
        return _method_not_allowed(handler, allowed_methods)
    route, matched = chosen
    try:
        if route.scope == "workspace":
            _require_action(handler, route)
        elif route.scope == "server_admin":
            if not getattr(handler.principal, "server_admin", False):
                return _not_found(handler)
        HANDLERS[route.handler](webauth, handler, route, matched)
    except PermissionError as exc:
        action = exc.args[0] if exc.args else route.action
        return _forbidden(handler, action)
    except WorkspaceNotFound:
        return _not_found(handler)
    return True
