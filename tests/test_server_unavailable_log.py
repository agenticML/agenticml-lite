"""The 503s for an unavailable database are logged (once per 10 seconds), without secrets."""
import logging

import pytest
from test_server_keys_web import api, call, login, running

from agenticml.server import ratelog
from agenticml.server.db import DatabaseError


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(ratelog, '_unavailable', {})


def test_dashboard_503_is_logged_without_cookie_or_query(tmp_path, monkeypatch, caplog):
    with running(tmp_path, env_key='e' * 32) as httpd:
        httpd.cookie = login(httpd, 'e' * 32)

        def fail(*args, **kwargs):
            raise DatabaseError('private SQL or database URL')

        monkeypatch.setattr(httpd.keys, 'current', fail)
        with caplog.at_level(logging.WARNING, logger='agenticml.server.webauth'):
            status, _, body = api(httpd, '/api/w/default/keys?token=secret-query')
            api(httpd)  # inside the same 10 seconds: counted, not logged
    assert status == 503 and body == {'error': 'key store unavailable'}
    lines = [r for r in caplog.records if r.name == 'agenticml.server.webauth']
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert lines[0].getMessage() == (
        'dashboard request failed: database unavailable (DatabaseError) GET /api/w/default/keys')
    for secret in (httpd.cookie.split('=', 1)[1], 'secret-query', 'private SQL'):
        assert secret not in caplog.text


def test_sync_503_is_logged(tmp_path, monkeypatch, caplog):
    from test_server_ingest import request
    with running(tmp_path) as server:
        _, secret = server.keys.create('box')

        def fail(*args, **kwargs):
            raise DatabaseError('private SQL or database URL')

        monkeypatch.setattr(server.keys.verifier, 'by_digest', fail)
        with caplog.at_level(logging.WARNING, logger='agenticml.server'):
            code, body, _ = request(server, '/v2/whoami', key=secret)
    assert code == 503 and body == {'error': 'key store unavailable'}
    assert [r.getMessage() for r in caplog.records if 'unavailable' in r.getMessage()] == [
        'sync request failed: key store unavailable (KeyStoreUnavailable) GET /v2/whoami']
    assert 'private SQL' not in caplog.text and secret not in caplog.text
