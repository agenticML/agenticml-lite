# Private-network install (no Tailscale)

Run the AgenticML server and dashboard inside a private network and sync to
it over HTTPS with an internal certificate. All connections start inside the
network; nothing reaches in from the internet, so `agenticml server up`
(Tailscale Funnel) is not used.

```
training machines --HTTPS :443--> TLS proxy (internal cert) --> agenticml server serve
lab users' browsers --HTTPS :443--^   Caddy or nginx            127.0.0.1:8080, SQLite on local disk
```

The server speaks plain http on loopback. TLS ends in the proxy.

Files in this directory:

- `Caddyfile`: Caddy site with an internal certificate (`tls internal`).
- `nginx.conf`: nginx server block for a certificate you already have.
- `agenticml-server.env.example`: the server's `/etc/agenticml-server.env`.

Examples below use the name `agenticml.lab.internal`; replace it everywhere
(the Caddyfile or nginx.conf, the env file, the commands).

## 1. Prerequisites

- Python 3.10 or newer on the server and on every training machine
  (`python3-venv` on Debian and Ubuntu).
- `git` on the server. Without it the server still works but lineage code
  diffs are off (the log says "git is not installed").
- NTP or chrony on every machine. Certificate checks fail when a clock is
  wrong.
- A DNS name, or an `/etc/hosts` line on every machine, for the server. It
  is the host of the public URL and the name in the certificate.

## 2. Install agenticml-lite 3.2 or newer

Install it on the server and on every training machine. On the server, use
a venv at `/opt/agenticml` (the unit expects it):

    sudo python3 -m venv /opt/agenticml
    sudo /opt/agenticml/bin/python -m pip install 'agenticml-lite>=3.2'

Machines without internet access: on a machine with the same OS and the same
Python minor version, download the wheels, copy the directory over, and
install from it:

    python3 -m pip download -d wheelhouse 'agenticml-lite>=3.2'
    # copy wheelhouse/ to the target machine, then install with that
    # environment's own pip. On the server:
    sudo /opt/agenticml/bin/python -m pip install --no-index --find-links wheelhouse 'agenticml-lite>=3.2'
    # On a training machine, use the Python environment that runs training:
    python -m pip install --no-index --find-links wheelhouse 'agenticml-lite>=3.2'

The files in this directory and in `deploy/systemd/` are not in the wheel.
Fetch them from the tag that matches the installed version
(`pip show agenticml-lite`), or clone the repository at that tag. For
example, for version 3.4.0:

    TAG=v3.4.0
    BASE=https://raw.githubusercontent.com/agenticML/agenticml-lite/$TAG/deploy
    curl -fsSLO $BASE/private-network/Caddyfile        # or nginx.conf
    curl -fsSLO $BASE/private-network/agenticml-server.env.example

## 3. Server unit

Follow "Server: system unit" in [`deploy/systemd/README.md`](../systemd/README.md)
to create the `agenticml` user and install `agenticml-server.service`. Then
install the environment file; it overrides the unit's placeholder URL:

    sudo install -m 0600 -o root -g root agenticml-server.env.example /etc/agenticml-server.env
    sudoedit /etc/agenticml-server.env      # set the public URL

`AGENTICML_SERVER_PUBLIC_URL` must be the exact URL users type, including
the port if it is not 443. `AGENTICML_SERVER_TRUSTED_PROXIES` must be the
proxy's own address as `/32` (`127.0.0.1/32` when the proxy runs on the same
machine), never a subnet. Do not set `AGENTICML_SERVER_KEY`: it turns off
the first-admin link in step 6.

    sudo systemctl daemon-reload
    sudo systemctl enable --now agenticml-server
    sudo journalctl -u agenticml-server -n 20

Check that the log shows `Dashboard: https://agenticml.lab.internal` (your
URL). On the server, `curl http://127.0.0.1:8080/healthz` must answer.

## 4. Proxy

Install Caddy or nginx and use the matching file. Both strip the identity
headers and pass the `Host` header unchanged.

**Caddy** (internal certificate, no certificate files to manage):

    sudo cp Caddyfile /etc/caddy/Caddyfile
    sudo systemctl reload caddy

**nginx** (your own certificate): copy `nginx.conf` to
`/etc/nginx/conf.d/agenticml.conf`, set `ssl_certificate` and
`ssl_certificate_key`, then:

    sudo nginx -t && sudo systemctl reload nginx

Keep the private key readable by root and the proxy only. A proxy in front
of AgenticML must:

- not redirect the sync URL (the client refuses any redirect);
- send every request body with its `Content-Length`, without re-chunking
  (the server answers 411 otherwise);
- allow object uploads (large files and artifacts) up to 4 GiB, and read and
  send timeouts of at least 3 hours (run files are capped at 16 MiB,
  appends at 64 MiB, lineage bundles at 256 MiB);
- pass the `Host` header unchanged, so it carries the public URL's host
  name (the server does not compare the port; the browser's `Origin`
  must equal the public URL, so the port in the public URL must be the
  one users type);
- drop `X-Forwarded-Email`, `X-Amzn-Oidc-Identity` and `Tailscale-User-Login`
  from clients;
- keep query strings out of access logs, because setup and invite tokens are
  in them.

SELinux (RHEL, Rocky, Fedora): let the proxy connect to the local port, keep
certificates in `/etc/pki` or `/etc/ssl`, and relabel them:

    sudo setsebool -P httpd_can_network_connect 1
    sudo restorecon -Rv /etc/ssl /etc/pki

A missing setting shows as HTTP 502 from the proxy while
`curl http://127.0.0.1:8080/healthz` still works on the server.

## 5. CA certificate

Every training machine and every browser must trust the proxy's CA.

With Caddy's `tls internal`, the root certificate is, for the packaged
service, `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`
(to confirm in 34.5). Read it on the proxy host with
`sudo cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`
and copy the output to the machines. With your own PKI, use your CA
certificate.

1. Copy it to `/etc/ssl/certs/lab-ca.pem` on each training machine. The file
   and its directory must be owned by the user or root and not writable by
   group or others.
2. Check it:

       openssl x509 -in /etc/ssl/certs/lab-ca.pem -noout -subject -enddate

3. Install it in the browsers or the OS store of the lab users' machines
   before their first visit.
4. Back up Caddy's data directory (`/var/lib/caddy`). A rebuilt proxy gets a
   new root and every machine would need the new one.

## 6. First admin

Do this before the user network can reach port 443. The server writes a
one-time setup link to its log at start, only while there is no admin user,
no admin-scope key and no `AGENTICML_SERVER_KEY`:

    sudo journalctl -u agenticml-server | grep 'setup?token'

Open the link in a browser that trusts the CA and create the admin. Anyone
who can read the journal can read the link, so restrict journal access. The
link is written at every start while no admin exists; if you lost it,
restart the service and read the journal again.

Do not create an admin-scope key before the first admin: that closes the
link. If it already happened, create the admin and its invite link on the
server instead:

    sudo -u agenticml /opt/agenticml/bin/agenticml server users add NAME --role admin --public-url https://agenticml.lab.internal --data /var/lib/agenticml-server

The command prints an invite link; open it to set the password. Add further
users the same way (`--role admin|member|viewer`). To issue a new link for an
existing user, use `server users invite NAME` with the same `--public-url`
and `--data`. Always run these commands as the `agenticml` user, never as
root, so the data directory keeps its owner.

## 7. Sync keys

Create one sync key per training machine, after adding the person who trains
on it (step 6). From the dashboard Keys page, or on the server:

    sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name box-1 --scope sync --user NAME --data /var/lib/agenticml-server

A key belongs to a server user, and the runs synced with it are owned by
that user, so use the person who trains on that machine. A key created on
the dashboard Keys page belongs to the signed-in person. Without `--user`,
the command assigns the key to the only user if there is one, and fails with
"--user is required when the server has more than one user" otherwise.

The key is printed once. Store it where only that machine's user can read it.

## 8. Each training machine

    agenticml login --host https://agenticml.lab.internal --ca-bundle /etc/ssl/certs/lab-ca.pem

`login` asks for the key and saves it with the bundle for that host. Running
`login` for the host again replaces the entry, so give `--ca-bundle` again.
Then check the connection and sync:

    agenticml whoami
    agenticml sync --all

For a long-running sync, install the user unit `agenticml-sync.service` (see
`deploy/systemd/README.md`). It needs no extra setting after `login
--ca-bundle`. If you did not use `--ca-bundle` at login, uncomment the
`AGENTICML_CA_BUNDLE` line in the unit instead.

If the machine sets `HTTPS_PROXY`, add the server to `NO_PROXY`
(for example `NO_PROXY=agenticml.lab.internal`) in the shell and in the sync
unit, so sync goes directly to the lab server.

Use `https://` everywhere. The client refuses redirects, so a port-80
redirect does not help sync.

## 9. Firewall

- The training subnet and the user network reach port 443 on the proxy
  (and 80 if you keep the redirect for browsers).
- Port 8080 stays on loopback. Do not open it.
- The machine runs only AgenticML and has no untrusted local users: the
  server trusts the proxy address you configured.

## 10. Backups

Run a backup every night as the service user, for example from
`/etc/cron.d/agenticml-backup` (`%` must be escaped in cron; `/backups`
must be writable by `agenticml`, and the target directory must not exist):

    17 2 * * * agenticml /opt/agenticml/bin/agenticml server backup --to /backups/$(date +\%F) --data /var/lib/agenticml-server

The server keeps running during a backup. Old backups are not removed;
prune them yourself. To restore, see "Backups and restore" in
[`docs/SERVER.md`](../../docs/SERVER.md).

## 11. Variant: single sign-on with oauth2-proxy

The default above uses passwords and invites. To sign in through your
identity provider instead, put oauth2-proxy between the TLS proxy and the
server:

```
client -> TLS proxy -> oauth2-proxy -> agenticml server serve
```

Conditions:

- Run the server with `--identity-header oauth2-proxy` (or
  `AGENTICML_SERVER_IDENTITY_HEADER=oauth2-proxy`) and set
  `AGENTICML_SERVER_TRUSTED_PROXIES` to oauth2-proxy's exact address as
  `/32`. The server then trusts `X-Forwarded-Email` from that address only.
- The TLS proxy must not forward a client-sent `X-Forwarded-Email` (the
  provided files already strip it), and nothing but oauth2-proxy may reach
  the server's port.
- oauth2-proxy must skip authentication for `/v2/` and `/healthz`. Sync uses
  Bearer API keys and cannot do a browser login, so sync breaks otherwise.
  (`--skip-auth-route` in current oauth2-proxy; to confirm in 34.5.)
- Each account needs its identity mapped to the email oauth2-proxy sends:

      sudo -u agenticml /opt/agenticml/bin/agenticml server users set-identity NAME name@example.org --data /var/lib/agenticml-server
