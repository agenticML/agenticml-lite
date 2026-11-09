#!/bin/sh
set -eu
# Python validates the comma-separated CIDRs and execs the server directly as PID 1.
exec python - "$@" <<'PYTHON'
import ipaddress
import os
import sys
args = ['agenticml', 'server', 'serve', '--data', '/data', '--host', '0.0.0.0', '--port', '8080']
raw = os.environ.get('AGENTICML_SERVER_TRUSTED_PROXY', '')
if raw:
    for item in raw.split(','):
        try:
            cidr = str(ipaddress.ip_network(item.strip(), strict=True))
        except ValueError:
            sys.exit('AGENTICML_SERVER_TRUSTED_PROXY must contain comma-separated CIDRs')
        args.extend(['--trusted-proxy', cidr])
args.extend(sys.argv[1:])
os.execvp(args[0], args)
PYTHON
