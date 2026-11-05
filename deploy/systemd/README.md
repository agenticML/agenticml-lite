# systemd units

## Sync: user unit

The unit files are not included in the PyPI wheel. Fetch the sync user unit:

    mkdir -p ~/.config/systemd/user
    curl -fsSL -o ~/.config/systemd/user/agenticml-sync.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v2.0.0/deploy/systemd/agenticml-sync.service

Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).

Cloning the repo and copying `deploy/systemd/agenticml-sync.service` to
`~/.config/systemd/user/` also works. Set `ExecStart` to the output of
`which agenticml` (the example uses `%h/.local/bin/agenticml`).

    systemctl --user daemon-reload
    systemctl --user enable --now agenticml-sync
    loginctl enable-linger $USER   # keep running after logout

Logs: `journalctl --user -u agenticml-sync -f`. Stop: `systemctl --user stop agenticml-sync`.

For a non-default store, add `Environment=AGENTICML_DIR=...` under `[Service]`.
Exit status 3 means the API key was rejected; fix it with `agenticml login`.
The unit does not restart in that case.

## Server: system unit

Install the package into a venv and create the service user:

    sudo python3 -m venv /opt/agenticml
    sudo /opt/agenticml/bin/python -m pip install agenticml-lite
    sudo useradd --system --home /var/lib/agenticml-server --shell /usr/sbin/nologin agenticml
    sudo curl -fsSL -o /etc/systemd/system/agenticml-server.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v2.0.0/deploy/systemd/agenticml-server.service

Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).

Cloning the repo and copying `deploy/systemd/agenticml-server.service` to
`/etc/systemd/system/` also works; the unit is not included in the PyPI wheel.

Adjust `ExecStart` if the venv lives elsewhere, and set
`AGENTICML_SERVER_PUBLIC_URL` in the unit to your public HTTPS URL. The unit's
`StateDirectory` creates `/var/lib/agenticml-server` for the service user.
Optionally set `AGENTICML_SERVER_KEY` in `/etc/agenticml-server.env` (root-owned,
mode 0600); otherwise the server prints a first-admin setup line on first start.

    sudo systemctl daemon-reload
    sudo systemctl enable --now agenticml-server
    sudo journalctl -u agenticml-server

Read the first-admin setup line in the journal to sign in, or create an admin
key as the service user. Create a sync key for a training machine the same way:

    sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name admin --scope admin --data /var/lib/agenticml-server
    sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name training --scope sync --data /var/lib/agenticml-server

Put `tailscale serve --bg 8080` or Caddy in front of the loopback listener to provide HTTPS. Logs: `sudo journalctl -u agenticml-server -f`.
Stop: `sudo systemctl stop agenticml-server`.
