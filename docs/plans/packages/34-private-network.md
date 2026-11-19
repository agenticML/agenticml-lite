# Package 34: Private-network deployment (no Tailscale)

Nov 19, 2026 · Supervisor plan · Release 3.2 · Coders: Sonnet (code), Antigravity (prose) · Size: M

## Goal

A lab whose machines all sit in a private VPC, where Tailscale is not
allowed, can run the server and dashboard inside that VPC and sync to it
from every training machine over HTTPS with an internal certificate. Every
step is documented and tested.

```
training machines ──HTTPS :443──▶ TLS proxy (internal cert) ──▶ agenticml server serve
lab users' browsers ──HTTPS :443──┘        Caddy or nginx            127.0.0.1:8080, SQLite on local disk
```

Every connection starts inside the VPC and nothing reaches in from the
internet, so `agenticml server up` (Tailscale Funnel) is not used.

## What works today, and what does not (facts from the code)

What works without changes:

- **Sync is outbound request/response only.** The client calls `/v2/...` on
  one TCP port. There is no callback, websocket or long poll (`client.py`
  168-264; `docs/PROTOCOL.md` "Transport").
- **`sync --follow` rides out outages.** It retries an unreachable server
  with backoff up to 300 s and never exits on network errors (`sync.py`
  1242-1256).
- **`server serve` can run anywhere.** It takes `--host`, `--port`
  (default 8080), `--public-url`, `--trusted-proxy` and
  `--identity-header oauth2-proxy` (`cli.py` 1877-1906).
  `deploy/systemd/agenticml-server.service` binds `127.0.0.1:8080`.
- **The dashboard needs no internet.** It loads no external resources:
  fonts are local and the CSP is `default-src 'self'`. Only the Firebase
  page loads Google code, and Firebase stays off.
- **The session cookie is Secure on https.** It is `__Host-aml_session`
  with `Secure` when `--public-url` is `https://` (`server/webauth.py`
  720-731).
- **Dashboard POSTs need two CSRF checks** (`ui/server.py` 1544-1577):
  - `X-AgenticML-Request: 1`;
  - an `Origin` equal to the public URL's origin. When the Host header is
    a loopback name, `http://<Host>` is also accepted.
- **The Host check is dashboard-only.** It (403 "forbidden host") applies
  to dashboard routes only, not to `/v2` (`server/ingest.py` 128-144). It
  accepts only loopback names and the public URL's host, including any
  non-443 port (`ui/server.py` 270-282).
- **The first admin is created through a `/setup` link.** It is written
  to the server log at startup (`webauth.py` 464-481), and only while there
  is no admin user, no admin-scope key and no `AGENTICML_SERVER_KEY`
  (`db/repos.py` 434-438). The alternative is
  `server users add|invite ... --public-url` run as the service user
  (`cli.py` 736-755).

Gaps this package closes:

1. **No way to trust an internal CA.**
   - `Client` builds a plain `requests.Session` (`client.py` 38-44) and
     never sets `verify`. The only route is requests' own
     `REQUESTS_CA_BUNDLE`, which is undocumented and untested here.
   - requests quirk (confirmed on requests 2.34.2): with `trust_env` on,
     `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` override `session.verify`
     (`Session.merge_environment_settings`). Only a per-request `verify=`
     wins.
   - `verify=<file>` replaces the default roots; it does not add to them.
2. **Plain http needs `--insecure` on every command.**
   - `normalize_host` (`auth.py` 21-51) rejects non-local http unless the
     flag is given.
   - `login --insecure` saves only the key (`auth.py` 272).
   - The shipped `agenticml-sync.service` passes no flag.
3. **TLS failures are slow and unclear.**
   - `requests.exceptions.SSLError` subclasses `ConnectionError`, so
     `Client._request` retries it 3 times (about 3.5 s) before failing.
   - The messages that follow are unhelpful:
     - `login` prints "unable to log in (SSLError)";
     - `whoami` prints "unable to verify credentials (SSLError)";
     - every sync path prints `SSLError` through `_short_error`
       (`sync.py` 1196-1211); `--follow` shows "server unreachable
       (SSLError); retrying".
   - An untrusted certificate shows up as an `ssl.SSLCertVerificationError`
     in the cause chain. An `https://` URL pointing at a plain-http port
     raises an SSLError without one.
4. **The redirect error does not say what to do** ("server redirected;
   refusing to send the API key", `client.py` 135-139).
5. **No proxy example and no private-network docs.** The proxy must meet
   rules that today live only in the code:
   - **No redirect on the sync URL.** The client refuses any 3xx.
   - **`Content-Length` on every body, no re-chunking.** Otherwise the
     server answers 411 (`ingest.py` 408-418).
   - **Bodies up to 4 GiB** (`OBJECT_BODY_LIMIT`, `app.py` 35).
   - **Long read timeouts.** The body deadline is about 30 s +
     size / 512 KiB/s, roughly 2.3 h at 4 GiB (`app.py` 40-41, 121-125).
   - **Host header with its port.** It must equal the public URL's
     authority.
   - **`--trusted-proxy` set to the proxy itself.** Then the login
     throttle and the audit log see the real client address.
   - **Identity headers stripped** unless an authenticating proxy sets them
     (`webauth.py` 45, 508-523 trust any peer in `--trusted-proxy`).
   - **No query strings in access logs.** Setup and invite tokens travel
     in the query string (`docs/SERVER.md` 244).

## Decisions (taken by the supervisor, recommended options)

- **D1. TLS stays in a proxy; no built-in TLS in `server serve`.**
  - Why: the dashboard's slow-client defences read raw sockets (header
    budgets, `read1` deadlines and writer eviction in `ui/server.py`
    1075-1660). TLS sockets would need a careful, security-sensitive
    rework.
  - Caddy gets an internal certificate in one line.
  - Built-in TLS goes to "Later".
- **D2. CA trust order, most specific first:**
  1. `--ca-bundle PATH` (on `sync`, `login`, `whoami`);
  2. the bundle saved for that exact host by `login --ca-bundle`;
  3. `AGENTICML_CA_BUNDLE`;
  4. requests' default, which still honours `REQUESTS_CA_BUNDLE`.

  There is never an option to turn verification off. A bundle replaces the
  default roots, so a machine that syncs to both a lab server and the
  hosted service saves the bundle per host instead of exporting the
  variable.
- **D3. http is allowed per saved host, not globally.**
  - `login --insecure` to a non-local `http://` host saves
    `"insecure": true` in that host's entry.
  - Later commands whose normalized host equals that saved host accept
    `http://` without the flag.
  - There is no environment variable for insecure, because it would apply
    to any host.
- **D4. No proxy-environment code.** The docs say to add the server to
  `NO_PROXY` when the machines have `HTTPS_PROXY` set.
- **D5. Offline (air-gapped) transfer is out of scope.** It goes to
  "Later" (sync bundles).
- **D6. The identity header is an advanced variant.** The default kit
  uses passwords and invites and strips every identity header. The
  oauth2-proxy chain is described, with its conditions, but it is not the
  default.

## Parts

| Part | What | Coder | Size |
| --- | --- | --- | --- |
| 34.1 † | Client trust: `--ca-bundle`, saved bundle, `AGENTICML_CA_BUNDLE`, saved `insecure` per host, TLS errors without retries and with clear text, redirect text | Sonnet | M |
| 34.2 † | Deploy kit (`deploy/private-network/`), sync unit lines, end-to-end test through a TLS proxy with a private CA | Sonnet | M |
| 34.3 | Docs: SERVER.md section and troubleshooting, THREAT_MODEL.md, PROTOCOL.md, website page, README, systemd README | Antigravity (Sonnet if unavailable); supervisor checks | S–M |
| 34.4 | Version 3.2.0: version bump, the docs require `agenticml-lite>=3.2` on the server and every training machine, the kit is fetched from the matching tag | Sonnet; owner tags and publishes | S |
| 34.5 | Owner: first lab install from the docs (Backlog owner step, already in the roadmap) | Owner | S |

† Security-sensitive (34.1: certificate trust, the credentials file, keys
sent over http; 34.2: proxy trust and identity headers). These parts need
a second review. With Codex and Cursor disabled, the supervisor does it
(memory: supervisor second review), or the part waits.

## 34.1 † Client trust (Sonnet)

Files: `src/agenticml/auth.py`, `src/agenticml/client.py`,
`src/agenticml/sync.py`, `src/agenticml/cli.py`, `tests/test_auth.py`,
`tests/test_client.py`, `tests/test_sync_follow.py`, new
`tests/test_client_tls.py`.

1. **Resolution helper** (`auth.py`). Keep `resolve()` and `_resolve()` with
   their current signatures and return values (tests compare
   `auth.resolve()` to 2-tuples, e.g. `tests/test_auth.py` 128-152 and 346).
   - Add `resolve_connection(host=None, api_key=None, *, insecure=False,
     ca_bundle=None, use_env=True)`, returning a namedtuple
     `Connection(host, key, source, http_ok, ca_bundle)`.
   - It calls `read_credentials()` once, applies the existing host and key
     precedence, and normalizes with `insecure=True`. It then rejects a
     non-local `http` host unless the `insecure` argument is set or the
     saved entry for that exact normalized host has `insecure is True`,
     using today's error text.
   - It ignores a saved entry that is not a dict, as the existing
     `isinstance(entry, dict)` guard does.
   - `_resolve` and `resolve` become thin wrappers over it.
   - `ca_bundle` in the result is the *unvalidated* choice by D2 order:
     flag, then a saved string, then `AGENTICML_CA_BUNDLE`, then None.
2. **Bundle validation** in a new `load_ca_bundle(path)` (`auth.py`). It is
   called only where a `Client` is about to be created for an `https`
   host: never for `whoami --offline`, `sync --dry-run`, `sync --reset`,
   `logout` or http hosts.
   - **Absolute path.** Expand `~`. A saved value must be an absolute
     string; a relative saved value is an error.
   - **Regular file.** Resolve symlinks to the final file, which must be a
     regular file you can read.
   - **Owner and mode (POSIX).** The file and its parent directory must be
     owned by the current uid or root, and neither may be group- or
     world-writable (`mode & 0o022 == 0`). Same rule as `_stat_problem`.
   - **Fixed-text errors.** `CA bundle not found or unreadable: <path>` and
     `CA bundle is writable by others: <path>; chmod go-w`. The path is the
     user's own, so it is safe to print.
3. **`Client(..., ca_bundle=None)`** (`client.py`).
   - When set, store it and pass `verify=<path>` on every
     `self.session.request(...)` in `_request` (about line 122). Never rely
     on `session.verify`.
   - When None, pass nothing, so requests' defaults stay.
   - In `_request`, catch `requests.exceptions.SSLError` *before* the
     `ConnectionError` clause and re-raise it at once (no retry).
4. **`tls_failure(exc)`** (`client.py`). It walks `exc`, `.args`,
   `.reason` and `__cause__`/`__context__`. It returns `"cert"` when it
   finds `ssl.SSLCertVerificationError`, `"tls"` for any other
   `requests.exceptions.SSLError`, and None otherwise.
   - Fixed hint texts, never the exception text:
     - `"cert"`: `TLS certificate check failed (untrusted CA, wrong host
       name, expired, or a wrong clock); pass --ca-bundle PATH or set
       AGENTICML_CA_BUNDLE, and use the name in the certificate`;
     - `"tls"`: `TLS handshake failed; is this an https port?`.
   - Use it in `sync._short_error`, before the type-name fallback, so every
     sync path is covered, including follow's
     `server unreachable (...); retrying in Ns` line, whose format stays.
   - Use it in `auth._report` for `login` and `whoami`.
   - Follow keeps retrying, because a certificate can be fixed or rotated
     while it runs.
5. **`login`** (`auth.py` 253-303) gains `--ca-bundle`.
   - Its trust comes only from its own flags (`--insecure`, `--ca-bundle`)
     plus `AGENTICML_CA_BUNDLE` for the verification call. It never reads
     the saved entry's `insecure` or `ca_bundle`.
   - It saves `{"api_key"}` plus `"ca_bundle"` (absolute) only when
     `--ca-bundle` was given.
   - It saves `"insecure": true` only when `--insecure` was given and the
     normalized host is non-local `http`.
   - `--no-verify --ca-bundle X` still validates `X` before saving.
   - Re-running `login` to a host rewrites the entry, so it needs the same
     flags again (documented).
6. **`sync` and `whoami`** gain `--ca-bundle`.
   - Every `Client(...)` call site (`auth.py` 266, 344; `sync.py` 1424,
     1623) goes through `resolve_connection`. It passes
     `insecure=conn.http_ok` and `ca_bundle=load_ca_bundle(...)`, the
     latter only for https.
   - `sync --reset` and `--dry-run` use the same resolution but do not load
     the bundle.
7. **Warning:** when a key is sent over non-local `http://`, print once
   per command to stderr: `warning: sending the API key over plain http to
   <host>`. For `--follow`, print it once at start.
8. **Redirect text** (`client.py` 135-139): `server redirected to another
   URL; use the final https:// URL as --host (refusing to send the API key
   on a redirect)`. Do not print `Location`.
9. **No change** to the credentials file mode, path checks or key rules.

Tests:

- **Certificates.** A session fixture generates two throwaway CAs and a
  server certificate with the `openssl` CLI.
  - CAs: `basicConstraints=critical,CA:TRUE` and
    `keyUsage=critical,keyCertSign,cRLSign`.
  - Server certificate, signed by CA 1:
    `subjectAltName=DNS:localhost,IP:127.0.0.1` and
    `extendedKeyUsage=serverAuth`. Use EC or RSA 2048 with a validity that
    includes now, created with `-addext` or an extfile.
  - Skip with a reason if `openssl` is missing or older than 1.1.1.
- **Clean environment.** Every test deletes `REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE` and `AGENTICML_CA_BUNDLE` with `monkeypatch.delenv(...,
  raising=False)` unless it sets them on purpose.
- **Stub server.** `http.server` plus `ssl` on loopback, answering
  `/v2/whoami`.
- Cases:
  - `whoami --ca-bundle ca1` succeeds. Without it, the `"cert"` hint
    prints, exit is 1, and there is no retry delay.
  - **Precedence:** the flag beats the saved bundle, which beats
    `AGENTICML_CA_BUNDLE`.
  - **The requests quirk:** `REQUESTS_CA_BUNDLE` set to CA 2's PEM (a
    valid but wrong CA, never a missing path) does not override
    `--ca-bundle ca1`.
  - `login --ca-bundle` saves an absolute path, and `sync`/`whoami` then
    work with no flag. `login` with only `AGENTICML_CA_BUNDLE` saves no
    `ca_bundle`.
  - **Bad bundles:** a missing path, a directory, a 0666 file, and a
    relative saved path each give their fixed error and exit 1. A deleted
    saved bundle with `whoami --offline` still exits 0.
  - **Saved insecure:** `login --insecure --no-verify` to
    `http://10.0.0.5:8080` saves `insecure`. `whoami` to that saved host
    works without the flag (stub the HTTP call as existing tests do).
    `http://10.0.0.6:8080` is still refused. `login --insecure` to an
    https or localhost host saves no `insecure`. A hand-edited
    `"insecure": "false"` is not honoured. The plain-http warning prints
    once. Update the existing test at `tests/test_auth.py` 338-346 to the
    new behaviour.
  - An `https://` URL pointing at a plain-http port gives the `"tls"`
    hint.
  - **Redirect:** update `tests/test_client.py` about line 425 to the new
    text, using `fake_http(handler)` from `tests/test_client.py` 67-78
    with a 302 and a `Location` header. Assert that the `Location` value
    is not in `str(exc)`.
  - **Follow:** `sync --follow` against an untrusted certificate prints
    the hint inside `server unreachable (...); retrying in` and keeps
    going.
    - Use `follow_ctx`, `spawn`, `wait_until`, `stop_follow`,
      `join_follow` and `capture_prints` from `tests/test_sync_follow.py`
      (49-140).
    - Set `--interval 0.03`, and monkeypatch `sync.BACKOFF_MAX = 0.08` and
      `sync.random.uniform` as at lines 134-136.

## 34.2 † Deploy kit and end-to-end test (Sonnet)

Files:

- new `deploy/private-network/`: `Caddyfile`, `nginx.conf`,
  `agenticml-server.env.example`, `README.md`;
- `deploy/systemd/agenticml-sync.service`;
- new `tests/test_private_network.py`.

1. **Caddyfile.**
   - The site `agenticml.lab.internal` uses `tls internal`, or, commented,
     `tls /etc/ssl/lab.crt /etc/ssl/lab.key` for labs with their own PKI.
     It has `reverse_proxy 127.0.0.1:8080` and strips every identity header
     (`header_up -X-Forwarded-Email`, `-X-Amzn-Oidc-Identity`,
     `-Tailscale-User-Login`).
   - Comment: Caddy also opens :80 to redirect browsers, which is fine.
     Sync and login must use the `https://` URL. If port 80 is blocked or
     in use, add the global option `auto_https disable_redirects`.
   - Comment: Caddy logs no requests unless `log` is added. If it is, filter
     out the query string, because it carries setup and invite tokens.
   - Comment: do not set `trusted_proxies` in Caddy unless another proxy
     sits in front.
2. **nginx.conf** server block:
   - `listen 443 ssl` with the lab certificate paths;
   - `client_max_body_size 4g`, `proxy_request_buffering off`,
     `proxy_http_version 1.1`;
   - `proxy_read_timeout 3h` and `proxy_send_timeout 3h`;
   - `proxy_set_header Host $http_host` (keeps a non-443 port);
   - `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for` and
     `X-Forwarded-Proto https`;
   - `proxy_set_header X-Forwarded-Email ""`, `X-Amzn-Oidc-Identity ""` and
     `Tailscale-User-Login ""`;
   - `log_format agenticml '$remote_addr [$time_local] "$request_method
     $uri" $status $body_bytes_sent'` with `access_log ... agenticml`, and
     a comment explaining why;
   - a comment that a port-80 redirect is fine for browsers, but sync and
     login use the `https://` URL.
3. **Env example:**
   - `AGENTICML_SERVER_PUBLIC_URL=https://agenticml.lab.internal`, with
     a comment to include a non-443 port if one is used;
   - `AGENTICML_SERVER_TRUSTED_PROXIES=127.0.0.1/32`, with a comment: the
     proxy's exact address as `/32`, never a subnet;
   - no `AGENTICML_SERVER_KEY`, because it disables `/setup`;
   - no active identity header line, only a pointer to the README's
     oauth2-proxy variant.
4. **README.md**, a numbered checklist:
   1. **Prerequisites.**
      - Python 3.10+ (and `python3-venv` on Debian/Ubuntu);
      - `git` on the server (without it, lineage diffs are off,
        `app.py` 300-303);
      - NTP or chrony on every machine;
      - a DNS name (or `/etc/hosts` entry) for the server, used for the
        public URL and in the certificate.
   2. **Install `agenticml-lite>=3.2`** on the server and every training
      machine.
      - Offline: on a machine with the same OS and Python minor version,
        `python3 -m pip download -d wheelhouse 'agenticml-lite>=3.2'`, copy
        it over, then `pip install --no-index --find-links wheelhouse
        agenticml-lite`.
      - Fetch the kit from the tag that matches the installed version.
   3. **Server unit.** Install the server unit and
      `/etc/agenticml-server.env` (it overrides the unit's placeholder
      URL). Check the printed `Dashboard:` line.
   4. **Proxy.** Set up Caddy or nginx and keep the key readable by the
      proxy only.
      - SELinux: `setsebool -P httpd_can_network_connect 1`, keep
        certificates in `/etc/pki` or `/etc/ssl`, and run `restorecon`.
      - Symptom of a missing setting: 502 while
        `curl http://127.0.0.1:8080/healthz` works on the server.
   5. **CA certificate.** With `tls internal`, Caddy's root is at
      `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`
      for the packaged service. Mark this "to confirm in 34.5".
      - Copy it to `/etc/ssl/certs/lab-ca.pem` on each training machine.
      - Check it with `openssl x509 -noout -subject -enddate`.
      - Install it in browsers or the OS store before the first visit.
      - Back up Caddy's data directory: a rebuilt proxy has a new root.
   6. **First admin.** Before the user network can reach port 443, run
      `sudo journalctl -u agenticml-server | grep 'setup?token'` and open
      the link.
      - The link exists only at start, while there is no admin; restrict
        journal access.
      - Do not create an admin-scope key first. If that already happened,
        run `sudo -u agenticml agenticml server users invite <name>
        --public-url https://...`.
      - Run CLI admin commands as the `agenticml` user, never as root.
   7. **Sync keys** per machine, from the dashboard Keys page or with
      `sudo -u agenticml agenticml server keys create ... --scope sync`.
   8. **On each training machine:**
      `agenticml login --host https://agenticml.lab.internal --ca-bundle
      /etc/ssl/certs/lab-ca.pem`, then the sync unit. Add the server to
      `NO_PROXY` if `HTTPS_PROXY` is set.
   9. **Firewall.** The training subnet and the user network reach 443
      only (and 80 if redirects are kept). Port 8080 stays on loopback, and
      the VM is single-purpose with no untrusted local users.
   10. **Backups.** A nightly `sudo -u agenticml agenticml server backup
       --to /backups/$(date +%F) --data /var/lib/agenticml-server` from a
       timer or cron, with a pointer to the restore section in SERVER.md.
   11. **Variant: single sign-on with oauth2-proxy.**
       - The chain is client → TLS proxy → oauth2-proxy → `server serve`,
         and `--trusted-proxy` is oauth2-proxy's address.
       - The TLS proxy must not forward a client-sent `X-Forwarded-Email`.
       - oauth2-proxy must skip authentication for `/v2/` and `/healthz`
         (Bearer keys), or sync breaks.
       - Each account needs `users set-identity`.
5. **Sync unit:** add commented lines
   `# Environment=AGENTICML_CA_BUNDLE=/etc/ssl/certs/lab-ca.pem` (only if
   `login --ca-bundle` was not used) and `# Environment=NO_PROXY=...`.
6. **End-to-end test** `tests/test_private_network.py`, reusing the 34.1
   certificate fixture with an extra `DNS:agenticml.lab.internal` SAN. Order
   matters:
   1. **Bind the proxy first.** A minimal TLS-terminating TCP proxy
      (listening socket wrapped with the CA 1 server certificate) is bound
      first, to learn its port.
   2. **Start the real server:** `make_server(data, host="127.0.0.1",
      port=0, public_url=f"https://agenticml.lab.internal:{port}",
      trusted_proxies=("127.0.0.1/32",))`, in a thread, as `running(data)`
      does in `tests/test_sync.py` 34-57.
   3. **Start the copy threads.** Per connection: TLS handshake, connect to
      `httpd.server_address[1]`, copy bytes both ways until either side
      closes. Teardown closes every socket and joins the threads.
   4. **Sync through the proxy.**
      - Create the key with `httpd.keys.create("box", scope="sync")` and
        the run with `writer_at(tmp_path, ...)` from `tests/test_sync.py`
        59-75.
      - With `AGENTICML_CONFIG_DIR` monkeypatched, run `cli.main(["login",
        "--host", f"https://localhost:{port}", "--key", key, "--ca-bundle",
        ca1])`, then `cli.main(["sync", "--all", "--dir",
        str(writer.root)])`.
      - Assert with `identical((httpd, SimpleNamespace(base_url=url,
        api_key=key)), writer)`.
   5. **Dashboard through the proxy.**
      - Create a password user with `httpd.users.add("alice",
        role="admin")` and `httpd.users.set_password(...)`.
      - With `http.client.HTTPSConnection("localhost", port,
        context=<ca1>)`, POST `/login` with JSON `{user, password}` and the
        headers `Host: agenticml.lab.internal:{port}`,
        `X-AgenticML-Request: 1` and
        `Origin: https://agenticml.lab.internal:{port}`. Expect 200 and
        `Set-Cookie: __Host-aml_session=...; Secure` (reuse
        `cookie_parts` from `tests/test_server_web.py`).
      - Repeat with `Origin: http://agenticml.lab.internal:{port}` and with
        `Origin: https://evil.example`. Expect 403 "cross-site request
        refused" for both.
   6. **Static checks of the kit.**
      - `nginx.conf` contains `client_max_body_size 4g`,
        `proxy_request_buffering off`, `proxy_http_version 1.1`, a read
        timeout of at least `3h`, `Host $http_host`,
        `X-Forwarded-Email ""`, and an access log format without
        `$request`.
      - The `Caddyfile` contains `reverse_proxy 127.0.0.1:8080` and
        `-X-Forwarded-Email`.
      - The env example has no prefix shorter than `/32` and no active
        `AGENTICML_SERVER_KEY` or identity header line.

## 34.3 Docs (Antigravity; supervisor checks against the code)

- **`docs/SERVER.md`.**
  - New section "Private network (no Tailscale)" after "Production systemd
    service": the diagram, the README checklist in short form, the proxy
    rules (gap 5), sign-in options (passwords and invites; oauth2-proxy
    variant with its conditions; Firebase off), `NO_PROXY`,
    `--ca-bundle` and why a bundle is saved per host.
  - Troubleshooting entries:
    - the TLS certificate check hint and the TLS handshake hint (check the
      CA, name, clock and `openssl s_client -connect host:443 -CAfile
      ca.pem`);
    - "server redirected to another URL";
    - HTTP 411, 413 and 408 from the proxy;
    - "cross-site request refused" (public URL scheme or host mismatch);
    - 403 forbidden host;
    - 502 under SELinux;
    - browser certificate warnings.
  - Configuration reference: `AGENTICML_CA_BUNDLE` and `--ca-bundle`.
- **`docs/THREAT_MODEL.md`:**
  - the client trust controls: verification is never off, saved http
    consent is per exact host, bundles have owner and mode checks,
    redirects are not followed, TLS errors use fixed text;
  - the trusted-proxy and identity-header conditions on private networks.
- **`docs/PROTOCOL.md`:** a client TLS section (bundle order, no
  redirects, per-host insecure).
- **Website.** New `website/src/content/docs/server/private-network.md`
  with a `sidebar.order` front-matter value (the server section is
  autogenerated, so there is no `astro.config.mjs` change), linked from
  `server/install.md` and `server/sync.md`. Add a facts test in
  `tests/test_website_facts.py`: the page mentions `--ca-bundle`,
  `AGENTICML_CA_BUNDLE` and `client_max_body_size 4g`.
- **`deploy/systemd/README.md`:** the new sync unit lines.
- **`README.md`:** one line under the server section pointing to the page.

## 34.4 Version 3.2.0 (Sonnet; owner tags and publishes)

- Bump `pyproject.toml` to 3.2.0, or to the next free minor if 3.1.0 has
  shipped in the meantime, and adjust the Releases page.
- The docs from 34.2 and 34.3 say `agenticml-lite>=3.2` and fetch the kit
  from the matching tag.
- The owner pushes the tag; the existing Publish workflow uploads to PyPI.
- 34.5 waits for this.

## 34.5 Owner: first lab install

The roadmap's Backlog owner step "First lab install on a private network"
already exists. When 34.4 lands, update its "Done when" with the version.
The owner follows `deploy/private-network/README.md` on the lab's VM and
confirms the Caddy root path. Each mismatch becomes a fix with a test.

## Constraints and conventions

- The SDK and the local store do not change; `agenticml ui` is untouched.
- The server stays standard-library only; the client keeps `requests`.
- No option ever disables certificate verification.
- Never print an API key, an `Authorization` header, a `Location` header
  or raw `requests` exception text (`_exposes_header`, `_report`).
- Credentials file rules (`0600`, private directory) unchanged.
- No dashboard page changes, so the UI suite is not required unless 34.2
  touches `ui/`.

## Edge cases

- Hosts saved by older versions with `login --insecure` carry no flag.
  They need one more `login --insecure`, or `--insecure` on each command
  (documented).
- `AGENTICML_HOST` set to an http host that is saved with `insecure` is
  allowed, because the normalized hosts match. A different port or name is
  refused.
- IPv6 literal hosts are compared in `normalize_host` form (`[fd00::5]`).
- `REQUESTS_CA_BUNDLE` set system-wide, as some labs do, is still honoured
  when no agenticml bundle applies.
- A proxy that strips `User-Agent` skips the server's client-version check
  instead of failing it (existing behaviour; documented).

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
  - The new tests skip cleanly without `openssl` (macOS is not a CI
    target; CI is Ubuntu).
  - Run the Postgres job's database URL locally too if the e2e test
    touches the database layer (it follows `running()`, which honours
    `AGENTICML_SERVER_DATABASE_URL`).
- Docs CI (`website`): the link check and the 375px overflow check.
- The supervisor reviews every part and does the † second reviews for
  34.1 and 34.2.

## Done when

- **34.1:** the tests above pass, and a sync unit works with a bundle saved
  by `login` and no extra flags.
- **34.2:** the end-to-end test syncs a run and signs in through a TLS proxy
  with a private CA, and the static checks pass.
- **34.3:** the docs build in the PR, and every command in them was checked
  against the code.
- **34.4:** version 3.2.0 is on PyPI.
- **34.5:** the owner's lab install is recorded in the Backlog entry.

## Later (moved out of this package)

- **Built-in TLS for `server serve`** (`--tls-cert`, `--tls-key`). It needs
  the slow-client defences reworked for TLS sockets (D1).
- **Sync bundles for air-gapped labs** (D5).
  - `agenticml sync --to-bundle FILE` and `agenticml server import FILE`
    carry runs, objects, lineage and the owner.
  - Today's `server reindex` copy-in loses the owner, objects, lineage
    and notes.
