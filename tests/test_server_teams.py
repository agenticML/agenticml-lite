"""Release 2.2 exit criteria: two users with different roles share one server.

HTTP only, except creating the first admin with the CLI.
"""
from contextlib import contextmanager
import io
import json
import os
import sys
import threading

import pytest

from agenticml import cli, sync
from agenticml.client import Client
from agenticml.server.app import make_server
from test_server_web import authed, call, csrf
from test_sync import push, writer_at

PASSWORD = "password1234"


@contextmanager
def running(data):
    server = make_server(data, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def cookie_header(hdrs):
    return hdrs["Set-Cookie"].split(";", 1)[0]


def api(server, path, method="GET", payload=None, cookie=None):
    headers = csrf(server) if method in ("POST", "PATCH", "DELETE", "PUT") else {}
    if cookie:
        headers["Cookie"] = cookie
    status, hdrs, body = call(
        server, path, method, None if payload is None else json.dumps(payload), headers)
    return status, hdrs, json.loads(body) if body else None


def password_login(server, user, password):
    status, hdrs, body = call(
        server, "/login", "POST", json.dumps({"user": user, "password": password}).encode(),
        csrf(server))
    payload = json.loads(body) if body else None
    assert status == 200 and payload["ok"] is True and payload["user"] == user
    return cookie_header(hdrs), payload


def redeem(server, invite_url, password):
    token = invite_url.split("token=", 1)[1]
    status, hdrs, body = call(
        server, "/invite", "POST",
        json.dumps({"token": token, "password": password}).encode(), csrf(server))
    payload = json.loads(body) if body else None
    assert status == 200, payload
    return cookie_header(hdrs), payload


def add_admin(data):
    assert cli.main(["server", "users", "add", "admin", "--role", "admin",
                     "--no-invite", "--data", str(data)]) == 0
    stdin = sys.stdin
    sys.stdin = io.StringIO(PASSWORD + "\n")
    try:
        assert cli.main(["server", "users", "set-password", "admin", "--stdin",
                         "--data", str(data)]) == 0
    finally:
        sys.stdin = stdin


def steal(url, key, writer):
    with Client(url, key, timeout=5, max_retries=0) as client:
        try:
            push((None, client), writer)
        except sync.OwnershipError as exc:
            return exc
    return None


@pytest.fixture(scope="module")
def teams(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    config = tmp_path_factory.mktemp("config")
    local = tmp_path_factory.mktemp("local")
    previous_key = os.environ.pop("AGENTICML_SERVER_KEY", None)
    previous_config = os.environ.get("AGENTICML_CONFIG_DIR")
    os.environ["AGENTICML_CONFIG_DIR"] = str(config)
    try:
        add_admin(data)
        with running(data) as httpd:
            url = f"http://127.0.0.1:{httpd.server_address[1]}"
            admin_cookie, admin_session = password_login(httpd, "admin", PASSWORD)
            assert admin_session["role"] == "admin"

            status, _, bob_created = api(
                httpd, "/api/w/default/members", "POST",
                {"name": "bob", "role": "member"}, admin_cookie)
            assert status == 201 and bob_created["user"]["role"] == "member"
            status, _, vera_created = api(
                httpd, "/api/w/default/members", "POST",
                {"name": "vera", "role": "viewer"}, admin_cookie)
            assert status == 201 and vera_created["user"]["role"] == "viewer"

            bob_cookie, bob_session = redeem(httpd, bob_created["invite_url"], PASSWORD)
            vera_cookie, vera_session = redeem(httpd, vera_created["invite_url"], PASSWORD)
            assert bob_session == {"ok": True, "user": "bob", "role": "member"}
            assert vera_session == {"ok": True, "user": "vera", "role": "viewer"}

            status, _, admin_key = api(
                httpd, "/api/w/default/keys", "POST",
                {"name": "admin-laptop", "scope": "sync"}, admin_cookie)
            assert status == 201 and admin_key["record"]["user"] == "admin"
            status, _, bob_key = api(
                httpd, "/api/w/default/keys", "POST",
                {"name": "bob-laptop", "scope": "sync"}, bob_cookie)
            assert status == 201 and bob_key["record"]["user"] == "bob"
            status, _, bob_ci_key = api(
                httpd, "/api/w/default/keys", "POST",
                {"name": "bob-ci", "scope": "sync", "user": "bob"}, admin_cookie)
            assert status == 201 and bob_ci_key["record"]["user"] == "bob"
            bob_admin_status, _, bob_admin_body = api(
                httpd, "/api/w/default/keys", "POST",
                {"name": "bad-admin", "scope": "admin"}, bob_cookie)
            bob_other_status, _, bob_other_body = api(
                httpd, "/api/w/default/keys", "POST",
                {"name": "bad-other", "scope": "sync", "user": "admin"}, bob_cookie)

            admin_writer = writer_at(local / "admin", run_id="admin-run")
            admin_writer.close()
            bob_writer = writer_at(local / "bob", run_id="bob-run")
            bob_writer.close()
            with Client(url, admin_key["key"], timeout=5, max_retries=0) as client:
                assert push((httpd, client), admin_writer).complete
            with Client(url, bob_key["key"], timeout=5, max_retries=0) as client:
                assert push((httpd, client), bob_writer).complete

            note = json.dumps({"text": "hello from bob", "revision": None}).encode()
            forbidden = json.dumps({"text": "nope", "revision": None}).encode()
            yield {
                "httpd": httpd,
                "data": data,
                "admin_cookie": admin_cookie,
                "bob_cookie": bob_cookie,
                "vera_cookie": vera_cookie,
                "bob_created": bob_created,
                "vera_created": vera_created,
                "bob_admin_key": (bob_admin_status, bob_admin_body),
                "bob_other_key": (bob_other_status, bob_other_body),
                "bob_steals_admin": steal(url, bob_key["key"], admin_writer),
                "admin_steals_bob": steal(url, admin_key["key"], bob_writer),
                "runs": api(httpd, "/api/w/default/runs", cookie=vera_cookie),
                "vera_note": authed(
                    httpd, "/api/w/default/runs/admin-run/notes", "PUT", forbidden, cookie=vera_cookie),
                "vera_keys": api(httpd, "/api/w/default/keys", cookie=vera_cookie),
                "vera_users": api(httpd, "/api/w/default/members", cookie=vera_cookie),
                "bob_note": authed(
                    httpd, "/api/w/default/runs/admin-run/notes", "PUT", note, cookie=bob_cookie),
            }
    finally:
        if previous_key is not None:
            os.environ["AGENTICML_SERVER_KEY"] = previous_key
        if previous_config is None:
            os.environ.pop("AGENTICML_CONFIG_DIR", None)
        else:
            os.environ["AGENTICML_CONFIG_DIR"] = previous_config


def test_two_users_share_one_server(teams):
    assert "/invite?token=" in teams["bob_created"]["invite_url"]
    assert "/invite?token=" in teams["vera_created"]["invite_url"]

    status, body = teams["bob_admin_key"]
    assert status == 403 and body == {"error": "only admins can create admin keys"}
    status, body = teams["bob_other_key"]
    assert status == 403 and body == {"error": "you can only create keys for yourself"}

    assert isinstance(teams["bob_steals_admin"], sync.OwnershipError)
    assert isinstance(teams["admin_steals_bob"], sync.OwnershipError)

    runs_status, _, runs = teams["runs"]
    assert runs_status == 200
    by_id = {row["run_id"]: row for row in runs}
    assert by_id["admin-run"]["owner"] == "admin"
    assert by_id["bob-run"]["owner"] == "bob"

    vera_note_status, _, vera_note_body, _ = teams["vera_note"]
    assert vera_note_status == 403 and json.loads(vera_note_body) == {"error": "read-only"}
    assert teams["vera_keys"][0] == 403 and teams["vera_keys"][2] == {"error": "forbidden"}
    assert teams["vera_users"][0] == 403 and teams["vera_users"][2] == {"error": "forbidden"}

    bob_note_status, _, bob_note_body, _ = teams["bob_note"]
    assert bob_note_status == 200
    assert json.loads(bob_note_body)["text"] == "hello from bob"


def test_audit_lines_for_team_writes(teams):
    from server_db_helpers import audit_rows
    events = audit_rows(teams['data'] / 'server')
    writes = [
        (event["action"], event["actor"], event["target"])
        for event in events
        if not event["actor"].startswith("cli:") and event["action"] != "login.ok"
    ]
    assert writes == [
        ("user.add", "admin", "bob"),
        ("user.invite", "admin", "bob"),
        ("user.add", "admin", "vera"),
        ("user.invite", "admin", "vera"),
        ("user.password", "bob", "bob"),
        ("user.password", "vera", "vera"),
        ("key.create", "admin", "admin-laptop"),
        ("key.create", "bob", "bob-laptop"),
        ("key.create", "admin", "bob-ci"),
        ("run.create", "admin", "admin-run"),
        ("run.finish", "admin", "admin-run"),
        ("run.create", "bob", "bob-run"),
        ("run.finish", "bob", "bob-run"),
        ("note.write", "bob", "admin-run"),
    ]
    logins = [
        (event["actor"], event["target"], event["detail"].get("method"))
        for event in events if event["action"] == "login.ok"
    ]
    assert logins == [
        ("admin", "admin", "password"),
        ("bob", "bob", "invite"),
        ("vera", "vera", "invite"),
    ]
    assert not any(event["action"] == "login.failed" for event in events)
    assert not any(event["action"] == "key.revoke" for event in events)
    assert [event["target"] for event in events if event["action"] == "key.create"] == [
        "admin-laptop", "bob-laptop", "bob-ci"]
    bob_ci_event, = [event for event in events
                     if event["action"] == "key.create" and event["target"] == "bob-ci"]
    assert bob_ci_event["detail"]["user"] == "bob"


def test_team_flow_writes_only_database(teams):
    directory = teams['data'] / 'server'
    assert all(path.name.startswith('agenticml.db')
               or path.name in {'server.lock', 'tmp'} or path.name.endswith('.2x')
               for path in directory.iterdir())
