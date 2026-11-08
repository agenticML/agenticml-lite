"""18.1b isolation suite: every workspace and server-admin route, two workspaces."""
from __future__ import annotations

from server_db_helpers import on_postgres

import hashlib
import io
import json
import os
import shutil

import pytest

from agenticml import lineage, sync
from agenticml.client import Client
from agenticml.server.db.workspaces import AccountRepo, MembershipRepo, WorkspaceRepo
from agenticml.server.keys import KeyStore
from agenticml.server.routes import route_table
from agenticml.server.users import UserStore
from agenticml.server.workspaces import prepare_store
from agenticml.store import RunReader, RunWriter
from test_server_ingest import request as v2
from test_server_signin import PASSWORD, password_login, running
from test_server_web import call, cookie_parts, csrf, login

ENV_KEY = "isolation-env-key-" + "x" * 16
RUN_ID = "shared-run"
ONLY_B = "only-beta"
A_SLUG, B_SLUG, S_SLUG = "alpha", "beta", "suspended"
MISSING_SLUG = "no-such-ws"
A_OBJECT = b"object-bytes-only-in-alpha"
B_OBJECT = b"object-bytes-only-in-beta"
A_SHA = hashlib.sha256(A_OBJECT).hexdigest()
B_SHA = hashlib.sha256(B_OBJECT).hexdigest()

# Example request per workspace and server-admin route. A route without an
# entry fails test_examples_cover_workspace_and_admin_routes.
EXAMPLES = {
    "workspace_page": {"method": "GET", "path": "/w/{slug}/"},
    "run_meta": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}"},
    "run_notes": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}/notes"},
    "run_scalars": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}/scalars"},
    "run_time": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}/time"},
    "run_media": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}/media"},
    "run_code": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}/code"},
    "run_lineage": {"method": "GET", "path": "/api/w/{slug}/runs/{run_id}/lineage"},
    "runs_list": {"method": "GET", "path": "/api/w/{slug}/runs"},
    "run_notes_put": {
        "method": "PUT", "path": "/api/w/{slug}/runs/{run_id}/notes",
        "body": {"text": "isolation note", "revision": None},
    },
    "compare_code": {
        "method": "GET",
        "path": "/api/w/{slug}/compare/code?base={run_id}&target={run_id}",
    },
    "lineage_graph": {"method": "GET", "path": "/api/w/{slug}/lineage?project=demo"},
    "notes_list": {"method": "GET", "path": "/api/w/{slug}/notes"},
    "object_get": {"method": "GET", "path": "/api/w/{slug}/objects/{sha}"},
    "keys_list": {"method": "GET", "path": "/api/w/{slug}/keys"},
    "keys_create": {
        "method": "POST", "path": "/api/w/{slug}/keys",
        "body": {"name": "isolation-create", "scope": "sync"},
    },
    "keys_revoke": {"method": "DELETE", "path": "/api/w/{slug}/keys/{name}"},
    "members_list": {"method": "GET", "path": "/api/w/{slug}/members"},
    "members_create": {
        "method": "POST", "path": "/api/w/{slug}/members",
        "body": {"name": "isolation-user", "role": "member"},
    },
    "members_patch": {
        "method": "PATCH", "path": "/api/w/{slug}/members/{login}",
        "body": {"role": "member"},
    },
    "members_delete": {"method": "DELETE", "path": "/api/w/{slug}/members/{login}"},
    "members_invite": {"method": "POST", "path": "/api/w/{slug}/members/{login}/invite"},
    "invites_list": {"method": "GET", "path": "/api/w/{slug}/invites"},
    "invites_create": {
        "method": "POST", "path": "/api/w/{slug}/invites",
        "body": {"login": "isolation-invite", "role": "viewer"},
    },
    "invites_revoke": {"method": "DELETE", "path": "/api/w/{slug}/invites/{login}"},
    "audit": {"method": "GET", "path": "/api/w/{slug}/audit"},
    "admin_page": {"method": "GET", "path": "/admin/"},
    "admin_workspaces_list": {"method": "GET", "path": "/api/admin/workspaces"},
    "admin_workspaces_create": {
        "method": "POST", "path": "/api/admin/workspaces",
        "body": {"slug": "nope", "name": "Nope", "owner": "bootstrap"},
    },
    "admin_workspaces_patch": {
        "method": "PATCH", "path": "/api/admin/workspaces/{slug}",
        "body": {"name": "Renamed"},
    },
    "admin_audit": {"method": "GET", "path": "/api/admin/audit"},
    "healthz": {"method": "GET", "path": "/healthz"},
    "login_get": {"method": "GET", "path": "/login"},
    "login_post": {
        "method": "POST", "path": "/login",
        "body": {"bogus": True},
    },
    "logout": {"method": "POST", "path": "/logout"},
    "setup_get": {"method": "GET", "path": "/setup"},
    "setup_post": {
        "method": "POST", "path": "/setup",
        "body": {"token": "x", "name": "nope", "password": PASSWORD},
    },
    "invite_get": {"method": "GET", "path": "/invite"},
    "invite_post": {
        "method": "POST", "path": "/invite",
        "body": {"token": "x", "password": PASSWORD},
    },
    "auth_firebase": {
        "method": "POST", "path": "/auth/firebase",
        "body": {"id_token": "x"},
    },
    "auth_firebase_config": {"method": "GET", "path": "/auth/firebase/config"},
    "static_login_js": {"method": "GET", "path": "/static/login.js"},
    "static_setup_js": {"method": "GET", "path": "/static/setup.js"},
    "static_invite_js": {"method": "GET", "path": "/static/invite.js"},
    "static_firebase_login_js": {"method": "GET", "path": "/static/firebase-login.js"},
    "static_firebase_app": {"method": "GET", "path": "/static/vendor/firebase/firebase-app-compat.js"},
    "static_firebase_auth": {"method": "GET", "path": "/static/vendor/firebase/firebase-auth-compat.js"},
    "static_app_css": {"method": "GET", "path": "/static/app.css"},
    "static_font_sans": {"method": "GET", "path": "/static/InstrumentSans.woff2"},
    "static_font_mono": {"method": "GET", "path": "/static/JetBrainsMono.woff2"},
    "home": {"method": "GET", "path": "/"},
    "workspaces_page": {"method": "GET", "path": "/workspaces"},
    "account_session": {"method": "GET", "path": "/api/account/session"},
    "account_password": {
        "method": "POST", "path": "/api/account/password",
        "body": {"new": "new-password-1"},
    },
    "account_workspaces": {"method": "GET", "path": "/api/account/workspaces"},
    "static_app_js": {"method": "GET", "path": "/static/app.js"},
    "static_charts_js": {"method": "GET", "path": "/static/charts.js"},
    "static_code_js": {"method": "GET", "path": "/static/code.js"},
    "static_codediff_js": {"method": "GET", "path": "/static/codediff.js"},
    "v2_whoami": {"method": "GET", "path": "/v2/whoami"},
    "v2_objects_missing": {
        "method": "POST", "path": "/v2/objects/missing",
        "body": {"objects": [A_SHA]},
    },
    "v2_object": {"method": "PUT", "path": "/v2/objects/{sha}"},
    "v2_run_state": {"method": "GET", "path": "/v2/runs/{run_id}/state"},
    "v2_lineage_state": {"method": "GET", "path": "/v2/runs/{run_id}/lineage"},
    "v2_lineage_put": {"method": "PUT", "path": "/v2/runs/{run_id}/lineage"},
    "v2_atomic": {"method": "PUT", "path": "/v2/runs/{run_id}/files/meta.json"},
    "v2_append": {"method": "POST", "path": "/v2/runs/{run_id}/append/events.jsonl"},
    "v2_commit": {"method": "POST", "path": "/v2/runs/{run_id}/commit", "body": {}},
}


def _scoped_names(*scopes):
    return {route.name for route in route_table() if route.scope in scopes}


def _examples(*scopes, slug=False):
    wanted = _scoped_names(*scopes) if scopes else set(EXAMPLES)
    for name, example in EXAMPLES.items():
        if name not in wanted:
            continue
        if slug and "{slug}" not in example["path"]:
            continue
        yield name, example


def _probe_methods(example):
    methods = ["GET", "HEAD", "OPTIONS"]
    if example["method"] not in methods:
        methods.append(example["method"])
    return methods


def _fill(path, slug=A_SLUG):
    return path.format(
        slug=slug, run_id=RUN_ID, sha=A_SHA, name="twin", login="member-b")


def _unknown_path(example):
    path = example["path"]
    if path.startswith("/w/") or path.startswith("/admin"):
        return "/no-such-page"
    return "/api/no-such"


_SKIP_HEADERS = {"date", "server", "set-cookie"}


def _snapshot(status, headers, body):
    filtered = {key.lower(): value for key, value in headers.items()
                if key.lower() not in _SKIP_HEADERS}
    return status, filtered, body


def _ask(server, method, path, *, cookie=None, extra=None, body=None):
    headers = {}
    if cookie:
        name, token, _ = cookie_parts(cookie)
        headers["Cookie"] = f"{name}={token}"
    if extra:
        headers.update(extra)
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        for key, value in csrf(server).items():
            headers.setdefault(key, value)
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    return call(server, path, method, body, headers)


def _password_cookie(server, login_name):
    status, _, hdrs = password_login(server, login_name, PASSWORD)
    assert status == 200, login_name
    return hdrs["Set-Cookie"]


def _key_cookie(server, key):
    status, _, hdrs = login(server, key)
    assert status == 200
    return hdrs["Set-Cookie"]


def _users(server, workspace_id):
    return UserStore(server.data_dir / "server", db=server.db, workspace_id=workspace_id)


def _keys(server, workspace_id):
    return KeyStore(server.data_dir / "server", db=server.db, workspace_id=workspace_id)


def _prepare_account(server, login_name, role, workspace_id, *, identity=None):
    store = _users(server, workspace_id)
    store.add(login_name, role)
    store.set_password(login_name, PASSWORD, operator=True)
    if identity is not None:
        store.set_identity(login_name, identity, operator=True)
    return store


def _drop_default_membership(server, login_name):
    account = AccountRepo(server.db).get(login_name)
    MembershipRepo(server.db, server.default_workspace_id).remove(account["id"])


def _write_run(root, run_id, marker):
    writer = RunWriter.create(root, run_id=run_id, meta={"project": "demo", "name": marker})
    writer.write_scalars([(0, {"loss": 1.0})])
    blob = root / "blob.bin"
    blob.write_bytes(marker.encode())
    writer.add_artifact(blob, "weights", "model", {}, copy=True)
    if shutil.which("git") is not None:
        source = root / "src"
        source.mkdir()
        (source / "train.py").write_text("print(%r)\n" % marker)
        info = lineage.snapshot(root, "demo", source, run_id)
        assert info.get("error") is None
        writer._update_meta({"lineage": info})
    writer.finish("finished")
    writer.close()
    return writer


def _sync_run(server, key, writer):
    url = "http://127.0.0.1:%d" % server.server_address[1]
    result = sync.SyncResult(writer.run_id)
    with Client(url, key, timeout=5, max_retries=0) as client:
        sync.sync_run(RunReader(writer.root, writer.run_id), client.base_url, client, result)
        assert not result.failed, result.warnings
    return result


def _put_object(server, key, blob):
    url = "http://127.0.0.1:%d" % server.server_address[1]
    sha = hashlib.sha256(blob).hexdigest()
    with Client(url, key, timeout=5, max_retries=0) as client:
        client.put_object(sha, io.BytesIO(blob), len(blob))
    return sha


def _write_note(server, slug, cookie, text):
    status, _, body = _ask(
        server, "PUT", "/api/w/%s/runs/%s/notes" % (slug, RUN_ID),
        cookie=cookie, body={"text": text, "revision": None})
    assert status == 200, body
    return json.loads(body)


@pytest.fixture(scope="function" if on_postgres() else "module")
def iso(tmp_path_factory):
    previous_key = os.environ.pop("AGENTICML_SERVER_KEY", None)
    previous_config = os.environ.get("AGENTICML_CONFIG_DIR")
    os.environ["AGENTICML_CONFIG_DIR"] = str(tmp_path_factory.mktemp("config"))
    data = tmp_path_factory.mktemp("data")
    local = tmp_path_factory.mktemp("local")
    try:
        with running(
                data, env_key=ENV_KEY, trusted_proxies=("127.0.0.1",),
                identity_header="tailscale") as server:
            server.users.add("bootstrap", "admin")
            server.users.set_password("bootstrap", PASSWORD, operator=True)
            assert AccountRepo(server.db).get("bootstrap")["server_admin"]

            for slug_owner, slug in (("owner-a", A_SLUG), ("owner-b", B_SLUG), ("owner-s", S_SLUG)):
                server.users.add(slug_owner, "admin")
                server.users.set_password(slug_owner, PASSWORD, operator=True)
            a = WorkspaceRepo(server.db).create(A_SLUG, name="Alpha", owner="owner-a")
            b = WorkspaceRepo(server.db).create(B_SLUG, name="Beta", owner="owner-b")
            s = WorkspaceRepo(server.db).create(S_SLUG, name="Suspended", owner="owner-s")
            for ws in (a, b, s):
                prepare_store(server.data_dir, ws["id"])
            for login_name in ("owner-a", "owner-b", "owner-s"):
                _drop_default_membership(server, login_name)

            for login_name, role in (("admin-a", "admin"), ("member-a", "member"), ("viewer-a", "viewer")):
                _prepare_account(server, login_name, role, a["id"],
                                 identity="%s@example.com" % login_name)
            _users(server, a["id"]).set_identity("owner-a", "owner-a@example.com", operator=True)

            for login_name, role in (("admin-b", "admin"), ("member-b", "member"), ("viewer-b", "viewer")):
                _prepare_account(server, login_name, role, b["id"],
                                 identity="%s@example.com" % login_name)
            _users(server, b["id"]).set_identity("owner-b", "owner-b@example.com", operator=True)

            _prepare_account(server, "x-user", "admin", a["id"])
            _users(server, b["id"]).add("x-user", "viewer")

            keys_a, keys_b, keys_s = _keys(server, a["id"]), _keys(server, b["id"]), _keys(server, s["id"])
            _, sync_a = keys_a.create("sync", "sync", user="member-a")
            _, admin_a = keys_a.create("admin-ui", "admin", user="admin-a")
            _, twin_a = keys_a.create("twin", "sync", user="member-a")
            _, sync_b = keys_b.create("sync", "sync", user="member-b")
            _, admin_b = keys_b.create("admin-ui", "admin", user="admin-b")
            _, twin_b = keys_b.create("twin", "sync", user="member-b")
            _, sync_s = keys_s.create("sync", "sync", user="owner-s")

            writer_a = _write_run(local / "a", RUN_ID, "alpha")
            writer_b = _write_run(local / "b", RUN_ID, "beta")
            writer_only_b = _write_run(local / "only-b", ONLY_B, "only-beta")
            _sync_run(server, sync_a, writer_a)
            _sync_run(server, sync_b, writer_b)
            _sync_run(server, sync_b, writer_only_b)
            _put_object(server, sync_a, A_OBJECT)
            _put_object(server, sync_b, B_OBJECT)

            owner_a_cookie = _password_cookie(server, "owner-a")
            owner_b_cookie = _password_cookie(server, "owner-b")
            _write_note(server, A_SLUG, owner_a_cookie, "note in alpha")
            _write_note(server, B_SLUG, owner_b_cookie, "note in beta")

            WorkspaceRepo(server.db).update(S_SLUG, status="suspended")

            cookies = {
                "password:owner-a": owner_a_cookie,
                "password:admin-a": _password_cookie(server, "admin-a"),
                "password:member-a": _password_cookie(server, "member-a"),
                "password:viewer-a": _password_cookie(server, "viewer-a"),
                "password:x-user": _password_cookie(server, "x-user"),
                "password:owner-s": _password_cookie(server, "owner-s"),
                "password:bootstrap": _password_cookie(server, "bootstrap"),
                "key-login:a": _key_cookie(server, admin_a),
                "key-login:env": _key_cookie(server, ENV_KEY),
                "password:admin-b": _password_cookie(server, "admin-b"),
                "password:member-b": _password_cookie(server, "member-b"),
                "password:viewer-b": _password_cookie(server, "viewer-b"),
                "password:owner-b": owner_b_cookie,
            }
            yield {
                "server": server,
                "a": a, "b": b, "s": s,
                "sync_a": sync_a, "sync_b": sync_b, "sync_s": sync_s,
                "admin_a": admin_a, "twin_a": twin_a, "twin_b": twin_b,
                "cookies": cookies,
                "keys_a": keys_a, "keys_b": keys_b,
            }
    finally:
        if previous_config is None:
            os.environ.pop("AGENTICML_CONFIG_DIR", None)
        else:
            os.environ["AGENTICML_CONFIG_DIR"] = previous_config
        if previous_key is None:
            os.environ.pop("AGENTICML_SERVER_KEY", None)
        else:
            os.environ["AGENTICML_SERVER_KEY"] = previous_key


def _a_only_principals(iso):
    cookies = iso["cookies"]
    principals = []
    for role in ("owner-a", "admin-a", "member-a", "viewer-a"):
        principals.append(("password:" + role, cookies["password:" + role], None))
    principals.append(("key-login:a", cookies["key-login:a"], None))
    principals.append(("key-login:env", cookies["key-login:env"], None))
    for role in ("owner-a", "admin-a", "member-a", "viewer-a"):
        principals.append((
            "proxy:" + role, None, {"Tailscale-User-Login": "%s@example.com" % role}))
    return principals


def _example_request(iso, example, slug, cookie=None, extra=None):
    return _ask(
        iso["server"], example["method"], _fill(example["path"], slug),
        cookie=cookie, extra=extra, body=example.get("body"))


def test_examples_cover_every_route():
    expected = {route.name for route in route_table()}
    assert set(EXAMPLES) == expected, (
        "missing %s extra %s" % (sorted(expected - set(EXAMPLES)), sorted(set(EXAMPLES) - expected)))


def _slug_answers(iso, example, slugs, cookie=None, extra=None, method=None):
    verb = example["method"] if method is None else method
    body = None if verb in ("GET", "HEAD", "OPTIONS") else example.get("body")
    return [
        _snapshot(*_ask(
            iso["server"], verb, _fill(example["path"], slug),
            cookie=cookie, extra=extra, body=body))
        for slug in slugs
    ]


def test_workspace_routes_foreign_missing_and_suspended_are_identical(iso):
    failures = []
    slugs = (B_SLUG, MISSING_SLUG, S_SLUG)
    for name, example in _examples("workspace", slug=True):
        for label, cookie, extra in _a_only_principals(iso):
            for method in _probe_methods(example):
                body = None if method in ("GET", "HEAD", "OPTIONS") else example.get("body")
                answers = _slug_answers(iso, example, slugs, cookie, extra, method)
                if not (answers[0] == answers[1] == answers[2]):
                    failures.append((name, label, method, "slugs", answers))
                    break
                unknown = _snapshot(*_ask(
                    iso["server"], method, _unknown_path(example),
                    cookie=cookie, extra=extra, body=body))
                if answers[0] != unknown:
                    failures.append((name, label, method, "unknown", answers[0], unknown))
                    break
                if method == "HEAD" or method == example["method"]:
                    if answers[0][0] != 404:
                        failures.append((name, label, method, "status", answers[0]))
                        break
            if failures:
                break
        if failures:
            break
    assert not failures, failures[:5]

    owner_s = iso["cookies"]["password:owner-s"]
    for name, example in _examples("workspace", slug=True):
        for method in _probe_methods(example):
            body = None if method in ("GET", "HEAD", "OPTIONS") else example.get("body")
            answers = _slug_answers(
                iso, example, (S_SLUG, MISSING_SLUG), owner_s, method=method)
            assert answers[0] == answers[1], (name, method, answers)
            unknown = _snapshot(*_ask(
                iso["server"], method, _unknown_path(example),
                cookie=owner_s, body=body))
            assert answers[0] == unknown, (name, method, answers[0], unknown)
            if method == "HEAD":
                assert answers[0][0] == 404, (name, method, answers[0])
            if method == example["method"]:
                assert answers[0][0] == 404, (name, method, answers[0])


def test_workspace_members_options_is_405_with_allow(iso):
    cookie = iso["cookies"]["password:owner-a"]
    for name, example in _examples("workspace", slug=True):
        status, headers, body = _ask(
            iso["server"], "OPTIONS", _fill(example["path"], A_SLUG), cookie=cookie)
        assert status == 405, (name, status, body)
        allow = headers.get("Allow") or headers.get("allow")
        assert allow and example["method"] in allow, (name, allow)
        foreign = _snapshot(*_ask(
            iso["server"], "OPTIONS", _fill(example["path"], B_SLUG), cookie=cookie))
        unknown = _snapshot(*_ask(
            iso["server"], "OPTIONS", _unknown_path(example), cookie=cookie))
        assert foreign == unknown and foreign[0] == 404, (name, foreign, unknown)


def _not_in_default_principals(iso):
    cookies = iso["cookies"]
    return [
        ("key-login:a", cookies["key-login:a"], None),
        ("password:member-a", cookies["password:member-a"], None),
        ("password:member-b", cookies["password:member-b"], None),
        ("proxy:member-a", None, {"Tailscale-User-Login": "member-a@example.com"}),
        ("proxy:member-b", None, {"Tailscale-User-Login": "member-b@example.com"}),
    ]


def test_server_admin_routes_match_unknown_path(iso):
    cookies = iso["cookies"]
    non_admins = [
        ("anon", None, None),
        ("password:viewer-a", cookies["password:viewer-a"], None),
        ("password:member-a", cookies["password:member-a"], None),
        ("password:admin-a", cookies["password:admin-a"], None),
        ("password:owner-a", cookies["password:owner-a"], None),
        ("password:x-user", cookies["password:x-user"], None),
        ("key-login:a", cookies["key-login:a"], None),
        ("key-login:env", cookies["key-login:env"], None),
        ("proxy:admin-a", None, {"Tailscale-User-Login": "admin-a@example.com"}),
    ]
    for name, example in _examples("server_admin"):
        kind = "/no-such-page" if example["path"].startswith("/admin") else "/api/no-such"
        for label, cookie, extra in non_admins:
            got = _snapshot(*_ask(
                iso["server"], example["method"], _fill(example["path"], B_SLUG),
                cookie=cookie, extra=extra, body=example.get("body")))
            unknown = _snapshot(*_ask(
                iso["server"], example["method"], kind,
                cookie=cookie, extra=extra, body=example.get("body")))
            assert got == unknown, (name, label, got, unknown)

    bootstrap = cookies["password:bootstrap"]
    for path in ("/w/%s/" % B_SLUG, "/api/w/%s/runs" % B_SLUG):
        denied = _snapshot(*_ask(iso["server"], "GET", path, cookie=bootstrap))
        missing = _snapshot(*_ask(
            iso["server"], "GET", path.replace(B_SLUG, MISSING_SLUG), cookie=bootstrap))
        assert denied == missing and denied[0] == 404, (path, denied, missing)


def test_sync_key_cannot_see_or_write_the_other_workspace(iso):
    server = iso["server"]
    ka, kb, ks = iso["sync_a"], iso["sync_b"], iso["sync_s"]
    sync_names = _scoped_names("sync")
    seen = set()

    status, whoami, _ = v2(server, "/v2/whoami", key=ka)
    assert status == 200 and whoami["workspace"] == A_SLUG
    seen.add("v2_whoami")
    assert v2(server, "/v2/whoami", key=kb)[1]["workspace"] == B_SLUG

    status, payload, _ = v2(server, "/v2/runs/%s/state" % RUN_ID, key=ka)
    assert status == 200 and payload.get("error") != "unknown run"
    seen.add("v2_run_state")
    other = v2(server, "/v2/runs/%s/state" % RUN_ID, key=kb)[1]
    assert payload != other
    status, payload, _ = v2(server, "/v2/runs/%s/state" % ONLY_B, key=ka)
    assert status == 404 and payload == {"error": "unknown run"}
    status, payload, _ = v2(server, "/v2/runs/%s/state" % ONLY_B, key=kb)
    assert status == 200

    extra = b"alpha-append\n"
    status, payload, _ = v2(
        server, "/v2/runs/%s/append/events.jsonl?offset=0" % RUN_ID, "POST", extra, ka)
    seen.add("v2_append")
    assert status == 409
    assert "size" in payload or payload.get("error") == "run is not running"
    from agenticml.server.workspaces import store_root
    b_events = store_root(server.data_dir, iso["b"]["id"]) / "runs" / RUN_ID / "events.jsonl"
    a_events = store_root(server.data_dir, iso["a"]["id"]) / "runs" / RUN_ID / "events.jsonl"
    if b_events.exists():
        assert extra not in b_events.read_bytes()
    if a_events.exists():
        assert extra not in a_events.read_bytes()

    state = v2(server, "/v2/runs/%s/state" % RUN_ID, key=ka)[1]
    sizes = {name: info["size"] for name, info in (state.get("files") or {}).items()}
    commit = json.dumps({"status": "finished", "sizes": sizes, "fingerprint": "a" * 64}).encode()
    status, _, _ = v2(server, "/v2/runs/%s/commit" % RUN_ID, "POST", commit, ka)
    seen.add("v2_commit")
    assert status == 200

    status, payload, _ = v2(
        server, "/v2/objects/missing", "POST", json.dumps({"objects": [B_SHA]}).encode(), ka)
    seen.add("v2_objects_missing")
    assert status == 200 and payload == {"missing": [B_SHA]}
    assert v2(server, "/v2/objects/missing", "POST",
              json.dumps({"objects": [B_SHA]}).encode(), kb)[1] == {"missing": []}
    assert v2(server, "/v2/objects/missing", "POST",
              json.dumps({"objects": [A_SHA]}).encode(), kb)[1] == {"missing": [A_SHA]}

    status, _, _ = v2(server, "/v2/objects/" + A_SHA, "PUT", A_OBJECT, ka)
    seen.add("v2_object")
    assert status == 200
    assert v2(server, "/v2/objects/missing", "POST",
              json.dumps({"objects": [A_SHA]}).encode(), kb)[1] == {"missing": [A_SHA]}

    status, payload, _ = v2(server, "/v2/runs/%s/lineage" % ONLY_B, key=ka)
    assert status == 404 and payload == {"error": "unknown run"}

    created = "created-in-alpha"
    meta = json.dumps({"run_id": created, "status": "finished"}).encode()
    status, payload, _ = v2(server, "/v2/runs/%s/files/meta.json" % created, "PUT", meta, ka)
    seen.add("v2_atomic")
    assert status == 200, payload
    only_b_meta = store_root(server.data_dir, iso["b"]["id"]) / "runs" / ONLY_B / "meta.json"
    assert only_b_meta.exists()
    assert json.loads(only_b_meta.read_bytes()).get("name") != "alpha"
    assert v2(server, "/v2/runs/%s/state" % created, key=kb)[0] == 404
    b_only_state = v2(server, "/v2/runs/%s/state" % ONLY_B, key=kb)[1]
    assert b_only_state.get("error") != "unknown run"

    for path, method, body in (
            ("/v2/whoami", "GET", None),
            ("/v2/objects/missing", "POST", json.dumps({"objects": [A_SHA]}).encode()),
            ("/v2/objects/" + A_SHA, "PUT", A_OBJECT),
            ("/v2/runs/%s/state" % RUN_ID, "GET", None),
            ("/v2/runs/%s/lineage" % RUN_ID, "GET", None),
            ("/v2/runs/%s/lineage" % RUN_ID, "PUT", b"x"),
            ("/v2/runs/%s/files/meta.json" % RUN_ID, "PUT", meta),
            ("/v2/runs/%s/append/events.jsonl?offset=0" % RUN_ID, "POST", b"x"),
            ("/v2/runs/%s/commit" % RUN_ID, "POST", b"{}"),
    ):
        status, payload, _ = v2(server, path, method, body, ks)
        assert status == 403 and payload == {"error": "workspace suspended"}, (path, status, payload)

    lineage_names = {"v2_lineage_state", "v2_lineage_put"}
    for name in sync_names - lineage_names:
        assert name in seen, "sync route %s was not exercised" % name
    if shutil.which("git") is None:
        pytest.skip("git is required for lineage isolation checks")

    status, payload, _ = v2(server, "/v2/runs/%s/lineage" % RUN_ID, key=ka)
    seen.add("v2_lineage_state")
    assert status == 200
    before_b = v2(server, "/v2/runs/%s/lineage" % RUN_ID, key=kb)[1]
    status, _, _ = v2(server, "/v2/runs/%s/lineage" % RUN_ID, "PUT", b"not a bundle", ka)
    seen.add("v2_lineage_put")
    after_b = v2(server, "/v2/runs/%s/lineage" % RUN_ID, key=kb)[1]
    assert after_b == before_b
    for name in lineage_names:
        assert name in seen, "sync route %s was not exercised" % name


def test_key_session_url_matrix_and_revoke_is_scoped(iso):
    server = iso["server"]
    x = iso["cookies"]["password:x-user"]
    status, _, body = _ask(server, "GET", "/api/w/%s/keys" % A_SLUG, cookie=x)
    assert status == 200
    names = {row["name"] for row in json.loads(body)["keys"]}
    assert "sync" in names and "twin" in names
    status, _, body = _ask(
        server, "POST", "/api/w/%s/keys" % A_SLUG, cookie=x,
        body={"name": "x-laptop", "scope": "sync"})
    assert status == 201 and json.loads(body)["record"]["name"] == "x-laptop"

    for path in ("/api/w/%s/keys" % B_SLUG, "/api/w/%s/members" % B_SLUG,
                 "/api/w/%s/invites" % B_SLUG, "/api/w/%s/audit" % B_SLUG):
        status, _, body = _ask(server, "GET", path, cookie=x)
        assert status == 403, (path, status, body)
        assert json.loads(body)["error"] in ("forbidden", "read-only")

    status, _, body = _ask(server, "GET", "/api/w/%s/members" % A_SLUG, cookie=x)
    assert status == 200
    status, _, body = _ask(server, "GET", "/api/w/%s/invites" % A_SLUG, cookie=x)
    assert status == 200
    status, _, body = _ask(server, "GET", "/api/w/%s/audit" % A_SLUG, cookie=x)
    assert status == 200

    assert v2(server, "/v2/whoami", key=iso["twin_b"])[0] == 200
    status, _, body = _ask(server, "DELETE", "/api/w/%s/keys/twin" % A_SLUG, cookie=x)
    assert status == 200
    assert v2(server, "/v2/whoami", key=iso["twin_a"])[0] == 401
    assert v2(server, "/v2/whoami", key=iso["twin_b"])[0] == 200


def test_two_workspace_end_to_end_account_x(iso):
    server = iso["server"]
    x = iso["cookies"]["password:x-user"]
    status, _, body = _ask(server, "GET", "/w/%s/" % A_SLUG, cookie=x)
    assert status == 200 and b"<!doctype html>" in body.lower()
    status, _, body = _ask(server, "GET", "/w/%s/" % B_SLUG, cookie=x)
    assert status == 200 and b"<!doctype html>" in body.lower()

    status, _, body = _ask(
        server, "GET", "/api/w/%s/runs/%s/notes" % (A_SLUG, RUN_ID), cookie=x)
    assert status == 200
    revision = json.loads(body)["revision"]
    status, _, body = _ask(
        server, "PUT", "/api/w/%s/runs/%s/notes" % (A_SLUG, RUN_ID),
        cookie=x, body={"text": "x edited alpha", "revision": revision})
    assert status == 200 and json.loads(body)["text"] == "x edited alpha"
    status, _, body = _ask(
        server, "PUT", "/api/w/%s/runs/%s/notes" % (B_SLUG, RUN_ID),
        cookie=x, body={"text": "x cannot edit beta", "revision": None})
    assert status == 403 and json.loads(body) == {"error": "read-only"}

    status, _, body = _ask(server, "GET", "/api/account/session", cookie=x)
    assert status == 200
    payload = json.loads(body)
    by_slug = {row["slug"]: row for row in payload["workspaces"]}
    assert by_slug[A_SLUG]["role"] == "admin"
    assert by_slug[B_SLUG]["role"] == "viewer"
    assert payload["user"] == "x-user"
    assert payload["server_admin"] is False


_WORKSPACE_LEAKS = (
    "/w/" + A_SLUG, "/w/" + B_SLUG, "/w/" + S_SLUG,
    RUN_ID, ONLY_B, A_SHA, B_SHA, "note in alpha", "note in beta",
)


def test_account_routes_key_login_and_env_cannot_escalate(iso):
    server = iso["server"]
    _, x_key = iso["keys_a"].create("x-ui", "admin", user="x-user")
    x_cookie = _key_cookie(server, x_key)
    cases = (
        ("key-login:a", iso["cookies"]["key-login:a"], {A_SLUG}),
        ("key-login:x", x_cookie, {A_SLUG}),
        ("key-login:env", iso["cookies"]["key-login:env"], {"default"}),
    )
    for label, cookie, allowed_slugs in cases:
        for path in ("/api/account/password",):
            for body in ({"new": "new-password-1"},
                         {"current": PASSWORD, "new": "new-password-1"}):
                status, _, raw = _ask(server, "POST", path, cookie=cookie, body=body)
                assert status == 403, (label, path, body, status, raw)
                assert json.loads(raw) == {"error": "this session cannot set a password"}
        for path in ("/api/account/session",
                     "/api/account/workspaces"):
            status, _, raw = _ask(server, "GET", path, cookie=cookie)
            assert status == 200, (label, path, status, raw)
            payload = json.loads(raw)
            slugs = {row["slug"] for row in payload["workspaces"]}
            assert slugs == allowed_slugs, (label, path, slugs)
        status, _, raw = _ask(server, "GET", "/workspaces", cookie=cookie)
        assert status == 200, (label, status)
        for slug in (A_SLUG, B_SLUG, S_SLUG, "default"):
            href = ("/w/%s/" % slug).encode()
            if slug in allowed_slugs:
                assert href in raw, (label, slug)
            else:
                assert href not in raw, (label, slug)


def test_public_routes_never_expose_workspace_data(iso):
    cookies = iso["cookies"]
    viewers = (
        ("anon", None),
        ("password:owner-a", cookies["password:owner-a"]),
        ("password:owner-b", cookies["password:owner-b"]),
        ("key-login:a", cookies["key-login:a"]),
        ("key-login:env", cookies["key-login:env"]),
    )
    needles = [marker.encode() for marker in _WORKSPACE_LEAKS]
    for name, example in _examples("public"):
        for label, cookie in viewers:
            method = example["method"]
            if method != "GET" and label != "anon":
                continue
            body = None if method in ("GET", "HEAD", "OPTIONS") else example.get("body")
            status, _, raw = _ask(
                iso["server"], method, example["path"], cookie=cookie, body=body)
            assert status not in (500, 503), (name, label, status, raw[:200])
            for needle in needles:
                assert needle not in raw, (name, label, needle, raw[:200])


_VARIANT_MARKERS = frozenset({
    A_SLUG, B_SLUG, S_SLUG, RUN_ID, ONLY_B, "twin", "member-b",
})


def _variant_paths(path):
    base, sep, query = path.partition("?")
    suffix = sep + query
    parts = base.split("/")
    variants = [
        ("trailing-slash", base.rstrip("/") + "/" + suffix),
        ("doubled-slash", "/".join(parts[:2] + [""] + parts[2:]) + suffix),
        ("case", base.upper() + suffix),
    ]
    idx = next((i for i, part in enumerate(parts) if part in _VARIANT_MARKERS), None)
    if idx is None:
        idx = next((i for i in range(len(parts) - 1, -1, -1) if parts[i]), None)
    if idx:
        part = parts[idx]
        cut = max(1, len(part) // 2)

        def swapped(new):
            updated = list(parts)
            updated[idx] = new
            return "/".join(updated) + suffix

        variants.extend([
            ("pct-2f", swapped(part[:cut] + "%2F" + part[cut:])),
            ("pct-2e", swapped(part[:cut] + "%2e" + part[cut:])),
            ("pct-252f", swapped(part[:cut] + "%252F" + part[cut:])),
            ("trailing-dot", swapped(part + ".")),
        ])
    return variants


def test_path_variants_match_unknown_path(iso):
    """Trailing slash, doubled slash, case, percent-encoding and trailing-dot of every /api/w/* route."""
    principals = _not_in_default_principals(iso)
    failures = []
    for principal, cookie, extra in principals:
        home = A_SLUG if "member-a" in principal or principal == "key-login:a" else B_SLUG
        unknown = _snapshot(*_ask(
            iso["server"], "GET", "/api/no-such", cookie=cookie, extra=extra))
        for name, example in _examples("workspace"):
            if not example["path"].startswith("/api/w/"):
                continue
            slug = home if example["path"].startswith("/api/w/") else A_SLUG
            filled = _fill(example["path"], slug)
            unknown_write = unknown if example["method"] in ("GET", "HEAD") else _snapshot(*_ask(
                iso["server"], example["method"], "/api/no-such",
                cookie=cookie, extra=extra, body=example.get("body")))
            for label, variant in _variant_paths(filled):
                if variant == filled:
                    continue
                got = _snapshot(*_ask(
                    iso["server"], example["method"], variant,
                    cookie=cookie, extra=extra, body=example.get("body")))
                if got != unknown_write:
                    failures.append((principal, name, label, variant, got, unknown_write))
                    break
            if failures:
                break
        if failures:
            break
    assert not failures, failures[:3]


def test_beta_only_account_signs_in_by_proxy_and_password(iso):
    extra = {"Tailscale-User-Login": "member-b@example.com"}
    status, _, body = _ask(iso["server"], "GET", "/w/%s/" % B_SLUG, extra=extra)
    assert status == 200 and b"<!doctype html>" in body.lower()
    denied = _snapshot(*_ask(iso["server"], "GET", "/w/default/", extra=extra))
    missing = _snapshot(*_ask(iso["server"], "GET", "/w/%s/" % MISSING_SLUG, extra=extra))
    assert denied == missing and denied[0] == 404

    cookie = iso["cookies"]["password:member-b"]
    status, _, body = _ask(iso["server"], "GET", "/w/%s/" % B_SLUG, cookie=cookie)
    assert status == 200 and b"<!doctype html>" in body.lower()
    denied = _snapshot(*_ask(iso["server"], "GET", "/w/default/", cookie=cookie))
    missing = _snapshot(*_ask(iso["server"], "GET", "/w/%s/" % MISSING_SLUG, cookie=cookie))
    assert denied == missing and denied[0] == 404


def test_proxy_server_admin_reaches_admin_api(iso):
    iso["server"].users.set_identity("bootstrap", "bootstrap@example.com", operator=True)
    extra = {"Tailscale-User-Login": "bootstrap@example.com"}
    status, _, body = _ask(iso["server"], "GET", "/api/admin/workspaces", extra=extra)
    assert status == 200
    slugs = {row["slug"] for row in json.loads(body)["workspaces"]}
    assert {A_SLUG, B_SLUG, "default"} <= slugs


def test_account_login_audit_is_server_level(iso):
    server = iso["server"]
    status, _, hdrs = password_login(server, "member-b", PASSWORD)
    assert status == 200
    bootstrap = iso["cookies"]["password:bootstrap"]
    status, _, body = _ask(server, "GET", "/api/admin/audit", cookie=bootstrap)
    assert status == 200
    events = json.loads(body)["events"]
    assert any(event["action"] == "login.ok" and event["target"] == "member-b" for event in events)
    status, _, body = _ask(server, "GET", "/api/w/default/audit", cookie=bootstrap)
    assert status == 200
    events = json.loads(body)["events"]
    assert not any(event["action"] == "login.ok" and event["target"] == "member-b" for event in events)


def test_a_only_principals_are_not_members_of_default(iso):
    default_id = iso["server"].default_workspace_id
    for login_name in ("owner-a", "admin-a", "member-a", "viewer-a"):
        account = AccountRepo(iso["server"].db).get(login_name)
        assert MembershipRepo(iso["server"].db, default_id).get(account["id"]) is None, login_name


def test_runs_list_never_includes_other_workspace_run(iso):
    cookie = iso["cookies"]["password:owner-a"]
    status, _, body = _ask(iso["server"], "GET", "/api/w/%s/runs" % A_SLUG, cookie=cookie)
    assert status == 200
    ids = {row["run_id"] for row in json.loads(body)}
    assert RUN_ID in ids
    assert ONLY_B not in ids
    cookie_b = iso["cookies"]["password:owner-b"]
    status, _, body = _ask(iso["server"], "GET", "/api/w/%s/runs" % B_SLUG, cookie=cookie_b)
    assert status == 200
    ids = {row["run_id"] for row in json.loads(body)}
    assert RUN_ID in ids and ONLY_B in ids


def test_shared_run_id_has_different_bodies_in_each_workspace(iso):
    x = iso["cookies"]["password:x-user"]
    status, _, a_body = _ask(
        iso["server"], "GET", "/api/w/%s/runs/%s" % (A_SLUG, RUN_ID), cookie=x)
    status_b, _, b_body = _ask(
        iso["server"], "GET", "/api/w/%s/runs/%s" % (B_SLUG, RUN_ID), cookie=x)
    assert status == 200 and status_b == 200
    a_row, b_row = json.loads(a_body), json.loads(b_body)
    assert a_row["run_id"] == b_row["run_id"] == RUN_ID
    assert a_body != b_body
    assert a_row.get("name") != b_row.get("name")


def test_server_admin_without_membership_password_login(iso):
    server = iso["server"]
    server.users.add("solo-admin", "member")
    server.users.set_password("solo-admin", PASSWORD, operator=True)
    AccountRepo(server.db).set_server_admin("solo-admin", True)
    MembershipRepo(server.db, server.default_workspace_id).remove(
        AccountRepo(server.db).get("solo-admin")["id"])
    status, _, hdrs = password_login(server, "solo-admin", PASSWORD)
    assert status == 200
    cookie = hdrs["Set-Cookie"]
    status, _, body = _ask(server, "GET", "/api/admin/workspaces", cookie=cookie)
    assert status == 200
    denied = _snapshot(*_ask(server, "GET", "/w/default/", cookie=cookie))
    missing = _snapshot(*_ask(server, "GET", "/w/%s/" % MISSING_SLUG, cookie=cookie))
    assert denied == missing and denied[0] == 404
