"""A local fake OpenID Connect provider and server helpers (package 35.1).

Stdlib HTTP server: discovery, JWKS from a generated RSA key, an authorize
endpoint that approves at once, and a token endpoint that checks the PKCE
verifier, redirect URI and client id. Tests and browser tests share it.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from agenticml.server.app import make_server
from agenticml.server.limits import UNLIMITED, Plans
from agenticml.server.oidc import parse_oidc_config

CLIENT_ID = 'test-client-id'
KID = 'fake-kid'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def _jwk(key, kid):
    numbers = key.public_key().public_numbers()

    def integer(number):
        return _b64(number.to_bytes((number.bit_length() + 7) // 8, 'big'))

    return {'kty': 'RSA', 'kid': kid, 'use': 'sig', 'alg': 'RS256',
            'n': integer(numbers.n), 'e': integer(numbers.e)}


def plans(free_workspaces=1):
    return Plans({'default_plan': 'free', 'free_workspaces_per_account': free_workspaces,
                  'plans': {'free': dict(UNLIMITED)}})


class FakeProvider:
    """One issuer. Set ``identity`` before a sign-in; the rest are failure knobs."""

    def __init__(self, host='127.0.0.1'):
        self.identity = {'sub': 'sub-1', 'email': 'ada@example.com',
                         'email_verified': True, 'name': 'Ada'}
        self.claims = {}          # overrides merged into the next ID tokens
        self.sign_with = KEY      # key used to sign
        self.alg = 'RS256'
        self.kid = KID
        self.fail = {}            # 'discovery' | 'jwks' | 'token' -> HTTP status
        self.discovery_issuer = None
        self.codes = {}
        self.requests = []        # (kind, details) in order
        self.token_requests = []
        self.cache_control = 'max-age=300'
        self.discovery_extra = {}  # merged into the discovery document
        self.oversize = set()      # 'discovery' | 'jwks': answer with a huge body
        self.issued = {'id_token': [], 'code': [], 'code_verifier': [], 'state': []}
        self.public_url = None
        self._http = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.port = self._http.server_address[1]
        self.base = f'http://{host}:{self.port}'
        self.issuer = f'{self.base}/pool'
        self._thread = threading.Thread(
            target=self._http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self._thread.start()

    def close(self):
        self._http.shutdown()
        self._thread.join(timeout=5)
        self._http.server_close()

    # -- tokens ------------------------------------------------------------
    def id_token(self, nonce, **overrides):
        now = int(time.time())
        claims = {'iss': self.issuer, 'aud': CLIENT_ID, 'sub': self.identity['sub'],
                  'iat': now, 'exp': now + 3600, 'nonce': nonce, 'token_use': 'id',
                  'email': self.identity.get('email'),
                  'email_verified': self.identity.get('email_verified', True),
                  'name': self.identity.get('name')}
        claims.update(self.claims)
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not _OMIT}
        if self.alg == 'none':
            header = _b64(json.dumps({'alg': 'none', 'kid': self.kid}).encode())
            return f'{header}.{_b64(json.dumps(claims).encode())}.'
        if self.alg == 'HS256-PEM':
            # Classic algorithm confusion: HMAC with the RSA public key's PEM as the secret.
            from cryptography.hazmat.primitives import serialization
            pem = KEY.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            head = _b64(json.dumps({'alg': 'HS256', 'kid': self.kid}).encode())
            body = _b64(json.dumps(claims).encode())
            mac = hmac.new(pem, f'{head}.{body}'.encode(), hashlib.sha256).digest()
            return f'{head}.{body}.{_b64(mac)}'
        if self.alg == 'HS256':
            return jwt.encode(claims, 'secret' * 8, algorithm='HS256', headers={'kid': self.kid})
        return jwt.encode(claims, self.sign_with, algorithm='RS256', headers={'kid': self.kid})

    # -- the browser's part: approve at the authorize endpoint ----------------
    def authorize(self, location):
        """GET the authorization URL; return the callback URL it redirects to."""
        parts = urlsplit(location)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', parts.path + '?' + parts.query)
            response = conn.getresponse()
            response.read()
            return response.getheader('Location')
        finally:
            conn.close()

    # -- HTTP -----------------------------------------------------------------
    def _handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype='application/json', headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == '/pool/.well-known/openid-configuration':
                    provider.requests.append(('discovery', {}))
                    if 'discovery' in provider.fail:
                        return self._send(provider.fail['discovery'], {})
                    if 'discovery' in provider.oversize:
                        return self._send(200, b'{"pad":"' + b'x' * 200_000 + b'"}')
                    return self._send(200, {
                        'issuer': provider.discovery_issuer or provider.issuer,
                        'authorization_endpoint': provider.base + '/oauth2/authorize',
                        'token_endpoint': provider.base + '/oauth2/token',
                        'jwks_uri': provider.base + '/pool/.well-known/jwks.json',
                        'id_token_signing_alg_values_supported': ['RS256'],
                        **provider.discovery_extra,
                    }, headers={'Cache-Control': provider.cache_control})
                if parts.path == '/pool/.well-known/jwks.json':
                    provider.requests.append(('jwks', {}))
                    if 'jwks' in provider.fail:
                        return self._send(provider.fail['jwks'], {})
                    if 'jwks' in provider.oversize:
                        return self._send(200, b'{"keys":[],"pad":"' + b'x' * 200_000 + b'"}')
                    return self._send(200, {'keys': [_jwk(KEY, KID)]},
                                      headers={'Cache-Control': provider.cache_control})
                if parts.path == '/oauth2/authorize':
                    provider.requests.append(('authorize', query))
                    code = f'code-{len(provider.codes) + 1}'
                    provider.issued['state'].append(query.get('state'))
                    provider.codes[code] = {
                        'challenge': query.get('code_challenge'),
                        'method': query.get('code_challenge_method'),
                        'nonce': query.get('nonce'),
                        'client_id': query.get('client_id'),
                        'redirect_uri': query.get('redirect_uri'),
                    }
                    target = (query['redirect_uri'] + '?code=' + code
                              + '&state=' + query.get('state', ''))
                    return self._send(302, b'', 'text/plain', {'Location': target})
                if parts.path == '/logout':
                    provider.requests.append(('logout', query))
                    return self._send(302, b'', 'text/plain',
                                      {'Location': query.get('logout_uri', '/')})
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                if self.path != '/oauth2/token':
                    return self._send(404, {})
                provider.token_requests.append(form)
                provider.requests.append(('token', form))
                provider.issued['code'].append(form.get('code'))
                provider.issued['code_verifier'].append(form.get('code_verifier'))
                if 'token' in provider.fail:
                    status = provider.fail['token']
                    extra = {'Location': provider.base + '/elsewhere'} if 300 <= status < 400 else {}
                    return self._send(status, {'error': 'server_error'}, headers=extra)
                stored = provider.codes.pop(form.get('code'), None)
                if stored is None:
                    return self._send(400, {'error': 'invalid_grant'})
                verifier = form.get('code_verifier', '')
                challenge = _b64(hashlib.sha256(verifier.encode()).digest())
                if (stored['method'] != 'S256' or challenge != stored['challenge']
                        or form.get('client_id') != CLIENT_ID
                        or form.get('redirect_uri') != stored['redirect_uri']
                        or form.get('grant_type') != 'authorization_code'):
                    return self._send(400, {'error': 'invalid_grant'})
                token = provider.id_token(stored['nonce'])
                provider.issued['id_token'].append(token)
                return self._send(200, {'id_token': token,
                                        'token_type': 'Bearer', 'expires_in': 3600})

        return Handler


_OMIT = object()
OMIT = _OMIT


def free_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def oidc_server(data, provider, **kwargs):
    """A running server whose public URL is its own address and issuer the fake."""
    port = free_port()
    public_url = f'http://127.0.0.1:{port}'
    provider.public_url = public_url
    config = parse_oidc_config(
        provider.issuer, CLIENT_ID, kwargs.pop('logout_url', None), kwargs.pop('label', None))
    if kwargs.get('open_signup') and 'plans' not in kwargs:
        kwargs['plans'] = plans()
    httpd = make_server(data, port=port, public_url=public_url, oidc=config, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def raw_call(server, path, method='GET', body=None, headers=None):
    """(status, headers as a list of pairs, body): keeps repeated Set-Cookie lines."""
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


def header(headers, name):
    return [value for key, value in headers if key.lower() == name.lower()]


def cookie_pair(set_cookie):
    first = set_cookie.split(';', 1)[0]
    name, value = first.split('=', 1)
    return name, value


def post_start(server, fields, headers=None):
    """The sign-in page's form POST to /auth/oidc/start (an invite travels this way)."""
    from urllib.parse import urlencode
    host = f'127.0.0.1:{server.server_address[1]}'
    send = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': f'http://{host}'}
    send.update(headers or {})
    return raw_call(server, '/auth/oidc/start', 'POST', urlencode(fields).encode(), send)


def start_flow(server, provider, *, query='', invite=None, next_path=None, headers=None):
    """Run /auth/oidc/start (POST when an invite is carried) and the authorize step.

    Return (callback path, Cookie header for it).
    """
    if invite is not None:
        fields = {'invite': invite}
        if next_path:
            fields['next'] = next_path
        status, resp_headers, _ = post_start(server, fields, headers)
    else:
        status, resp_headers, _ = raw_call(
            server, '/auth/oidc/start' + query, headers=headers or {})
    assert status == 302, status
    location = header(resp_headers, 'Location')[0]
    name, value = cookie_pair(header(resp_headers, 'Set-Cookie')[0])
    callback = provider.authorize(location)
    parts = urlsplit(callback)
    return parts.path + '?' + parts.query, f'{name}={value}'


def finish_flow(server, callback, cookie):
    return raw_call(server, callback, headers={'Cookie': cookie} if cookie else {})


def sign_in(server, provider, *, query='', invite=None, next_path=None, headers=None):
    """A complete sign-in; return (status, headers, body, session cookie pair or None)."""
    callback, cookie = start_flow(
        server, provider, query=query, invite=invite, next_path=next_path, headers=headers)
    status, resp_headers, body = finish_flow(server, callback, cookie)
    session = None
    for value in header(resp_headers, 'Set-Cookie'):
        name, token = cookie_pair(value)
        if name.endswith('aml_session') and token:
            session = f'{name}={token}'
    return status, resp_headers, body, session
