"""The ``--contact-email`` option: address check, session payload, picker link (package 37.2)."""
import json

import pytest

import agenticml
from agenticml import cli
from agenticml.server.app import make_server
from agenticml.server.webauth import DEFAULT_CONTACT_EMAIL, resolve_contact_email
from test_server_firebase import PROJECT, firebase_running
from test_server_signin import running
from test_server_web import authed, call


def test_default_address_is_the_published_one():
    assert DEFAULT_CONTACT_EMAIL == 'contact@agenticml.xyz'


@pytest.mark.parametrize('value', [None, '', '  ', '\t'])
def test_resolve_unset_or_blank_is_the_project_default(value):
    assert resolve_contact_email(value) == (DEFAULT_CONTACT_EMAIL, True)


@pytest.mark.parametrize('value', ['off', 'OFF', ' Off ', 'oFf'])
def test_resolve_off_in_any_case(value):
    assert resolve_contact_email(value) is None


@pytest.mark.parametrize('value', ['contact@agenticml.xyz', 'a.b+c@sub.example.org', 'A_b-c@Example.ORG'])
def test_resolve_accepts_plain_addresses(value):
    assert resolve_contact_email(value) == (value, False)
    assert resolve_contact_email(f'  {value} ') == (value, False)


@pytest.mark.parametrize('value', [
    'a b@x.org', 'a@x.org?cc=b@y.org', 'a@x.org&bcc=b', 'a%40x@y.org', 'a,b@x.org',
    '"a"@x.org', 'a@@x.org', 'a@x', 'a@x.org\n', '\na@x.org', 'a@x.org;b@y.org', 'a@x..org',
    '@x.org', 'a@.org', 'a@x.org.', 'a@x_y.org', 'a@x.org\x00', 'a@x.org#frag', 'nonsense', 'offf',
    'a' * 249 + '@x.org',  # 255 characters
])
def test_resolve_rejects_everything_else(value):
    with pytest.raises(ValueError, match='--contact-email'):
        resolve_contact_email(value)


def test_resolve_length_limit_is_254():
    local = 'a' * (254 - len('@x.org'))
    assert resolve_contact_email(local + '@x.org') == (local + '@x.org', False)


def test_cli_contact_email_flag_env_and_default(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr('agenticml.server.serve', lambda *a, **kw: seen.update(kw))
    monkeypatch.delenv('AGENTICML_SERVER_CONTACT_EMAIL', raising=False)
    argv = ['server', 'serve', '--data', str(tmp_path), '--port', '0']
    assert cli.main(argv) == 0
    assert 'contact_email' not in seen
    monkeypatch.setenv('AGENTICML_SERVER_CONTACT_EMAIL', '')
    assert cli.main(argv) == 0
    assert 'contact_email' not in seen
    monkeypatch.setenv('AGENTICML_SERVER_CONTACT_EMAIL', 'env@example.org')
    assert cli.main(argv) == 0
    assert seen['contact_email'] == 'env@example.org'
    assert cli.main(argv + ['--contact-email', 'flag@example.org']) == 0
    assert seen['contact_email'] == 'flag@example.org'
    assert cli.main(argv + ['--contact-email', 'off']) == 0
    assert seen['contact_email'] == 'off'
    monkeypatch.setenv('AGENTICML_SERVER_CONTACT_EMAIL', 'off')
    seen.clear()
    assert cli.main(argv) == 0
    assert seen['contact_email'] == 'off'


@pytest.mark.parametrize('bad', ['a b@x.org', 'a@x.org?cc=b@y.org', 'nope'])
def test_cli_bad_contact_email_exits_1(tmp_path, monkeypatch, capsys, bad):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.delenv('AGENTICML_SERVER_CONTACT_EMAIL', raising=False)
    monkeypatch.setattr('agenticml.server.serve', lambda *a, **kw: pytest.fail('must not start'))
    argv = ['server', 'serve', '--data', str(tmp_path / 'd'), '--port', '0']
    assert cli.main(argv + ['--contact-email', bad]) == 1
    assert '--contact-email' in capsys.readouterr().err
    monkeypatch.setenv('AGENTICML_SERVER_CONTACT_EMAIL', bad)
    assert cli.main(argv) == 1
    assert '--contact-email' in capsys.readouterr().err


def test_make_server_rejects_a_bad_address(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with pytest.raises(ValueError, match='--contact-email'):
        make_server(tmp_path / 'data', port=0, contact_email='a@x.org?cc=b@y.org')


def _session(httpd):
    _, httpd.admin_key = httpd.keys.create('admin', 'admin')
    status, _, body, _ = authed(httpd, '/api/account/session')
    assert status == 200
    return json.loads(body)


@pytest.mark.parametrize('kwargs, expected', [
    ({}, {'email': DEFAULT_CONTACT_EMAIL, 'default': True}),
    ({'contact_email': ''}, {'email': DEFAULT_CONTACT_EMAIL, 'default': True}),
    ({'contact_email': 'lab@example.org'}, {'email': 'lab@example.org', 'default': False}),
    ({'contact_email': 'off'}, None),
    ({'contact_email': 'OFF'}, None),
])
def test_session_payload_has_contact_and_version(tmp_path, monkeypatch, kwargs, expected):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data', **kwargs) as httpd:
        data = _session(httpd)
    assert data['contact'] == expected
    assert data['version'] == agenticml.__version__


def test_session_payload_contact_is_for_signed_in_users_only(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data', contact_email='lab@example.org') as httpd:
        status, _, body = call(httpd, '/api/account/session')
        assert status == 401
        assert b'lab@example.org' not in body


def test_firebase_config_has_contact_email_only_in_operator_mode(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    plain = {'projectId': PROJECT, 'apiKey': 'test-api-key-1',
             'authDomain': f'{PROJECT}.firebaseapp.com'}
    with firebase_running(tmp_path / 'op', contact_email='lab@example.org') as httpd:
        assert json.loads(call(httpd, '/auth/firebase/config')[2]) == {
            **plain, 'contact_email': 'lab@example.org'}
    for i, value in enumerate((None, '', 'off', 'OFF')):
        with firebase_running(tmp_path / f'other-{i}', contact_email=value) as httpd:
            assert json.loads(call(httpd, '/auth/firebase/config')[2]) == plain
    # Choosing the default address explicitly is operator mode, like the hosted service.
    with firebase_running(tmp_path / 'explicit', contact_email=DEFAULT_CONTACT_EMAIL) as httpd:
        assert json.loads(call(httpd, '/auth/firebase/config')[2])['contact_email'] == DEFAULT_CONTACT_EMAIL


def _picker(httpd):
    _, httpd.admin_key = httpd.keys.create('admin', 'admin')
    status, _, body, _ = authed(httpd, '/workspaces')
    assert status == 200
    return body.decode()


def test_picker_has_a_mailto_link_only_in_operator_mode(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'op', contact_email='lab@example.org') as httpd:
        page = _picker(httpd)
    assert ('<a class="btn btn-ghost btn-sm" '
            'href="mailto:lab@example.org?subject=agenticml%3A%20workspaces">Contact us</a>') in page
    for i, value in enumerate((None, '', 'off')):
        with running(tmp_path / f'other-{i}', contact_email=value) as httpd:
            assert 'mailto:' not in _picker(httpd)
