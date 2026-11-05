# User systemd unit for `agenticml sync --follow`

Copy `agenticml-sync.service` to `~/.config/systemd/user/`. Set `ExecStart` to
the output of `which agenticml` (the example uses `%h/.local/bin/agenticml`).

    systemctl --user daemon-reload
    systemctl --user enable --now agenticml-sync
    loginctl enable-linger $USER   # keep running after logout

Logs: `journalctl --user -u agenticml-sync -f`. Stop: `systemctl --user stop agenticml-sync`.

For a non-default store, add `Environment=AGENTICML_DIR=...` under `[Service]`.
Exit status 3 means the API key was rejected; fix it with `agenticml login`.
The unit does not restart in that case.
