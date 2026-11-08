"""Laptop setup: Tailscale checks, first admin key, Funnel, optional user service."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading

from agenticml.store import layout
from .app import prepare_data_dir, resolve_data_dir, serve
from .keys import KeyStore
from .users import UserNameTaken, UserStore

_MAC_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_UNIT_NAME = "agenticml-server-up"
_UNIT_MARKER = "# Managed by agenticml server up"
_LAUNCHD_LABEL = "xyz.agenticml.server-up"
_FUNNEL_OFF = ("funnel", "--https=443", "off")
_NOT_INSTALLED = "Tailscale is not installed: see https://tailscale.com/download"
_NO_DNS = ("Tailscale has no MagicDNS name for this machine: "
           "enable MagicDNS in the admin console (DNS page)")
_NO_HTTPS = ("HTTPS certificates are not enabled for this tailnet: "
             "enable HTTPS in the admin console (DNS page)")
_NO_FUNNEL = ('this machine may not use Funnel: add the "funnel" node attribute '
              "in the tailnet policy (https://tailscale.com/kb/1223/funnel)")
_NOT_RUNNING = 'Tailscale is not running or not logged in: run "tailscale up"'
_INSTALL_PLATFORMS = "--install is supported on Linux (systemd) and macOS"
_NO_ADMIN_TTY = ("no admin: run `agenticml server up` once in a terminal, or "
                 "`agenticml server users add admin --role admin`")
_ADMIN_NAME_NOT_ADMIN = (
    "user admin exists but is not an admin: promote it with "
    "`agenticml server users set-role admin admin` or remove it")
_SETUP_CLOSED = ("setup is closed for this data directory: create an admin key with "
                 "`agenticml server keys create NAME --scope admin`")
_PORT_443_TAKEN = ("Tailscale already serves port 443 on this machine: "
                   "check `tailscale funnel status` and turn it off first")
_ENV_KEY_NOT_INHERITED = ("Created a file admin key because the installed service "
                          "does not inherit AGENTICML_SERVER_KEY.")
_INSTALL_ENV_DATABASE = (
    "the installed service uses the default database unless "
    "AGENTICML_SERVER_DATABASE_URL is set in its environment")
_INSTALL_NEEDS_PORT = "--install needs a fixed --port (1..65535)"
_SYSTEMD_CONTROL = "systemd unit path contains a control character"
_FUNNEL_STATUS_WARN = "warning: could not read Tailscale Funnel status; continuing"


class UpError(ValueError):
    """User-facing setup failure; str() is the exact stderr line."""


def _stdout_is_tty():
    return sys.stdout.isatty()


def find_tailscale():
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "darwin" and os.path.isfile(_MAC_TAILSCALE):
        return _MAC_TAILSCALE
    raise UpError(_NOT_INSTALLED)


def _first_line(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (str(text or "").splitlines() or [""])[0]


def run_tailscale(*args):
    binary = find_tailscale()
    try:
        return subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            text=True,
            errors="replace",
        )
    except FileNotFoundError:
        raise UpError(_NOT_INSTALLED) from None
    except subprocess.TimeoutExpired as exc:
        raise UpError(_first_line(exc.stderr) or "tailscale timed out") from None


def _not_running(state):
    shown = state if isinstance(state, str) and state else "unknown"
    return UpError(f"{_NOT_RUNNING} ({shown})")


def _node_caps(self_obj):
    caps = set()
    capmap = self_obj.get("CapMap")
    if isinstance(capmap, dict):
        caps.update(key for key in capmap if isinstance(key, str))
    capabilities = self_obj.get("Capabilities")
    if isinstance(capabilities, list):
        caps.update(item for item in capabilities if isinstance(item, str))
    return caps


def check_tailscale():
    """Return the public HTTPS URL. Does not touch the data directory."""
    result = run_tailscale("status", "--json")
    if result.returncode != 0:
        line = _first_line(result.stderr)
        if line:
            raise UpError(line)
        raise _not_running("unknown")
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise _not_running("unknown") from None
    if not isinstance(status, dict):
        raise _not_running("unknown")
    if status.get("BackendState") != "Running":
        raise _not_running(status.get("BackendState"))
    self_obj = status.get("Self")
    if not isinstance(self_obj, dict):
        self_obj = {}
    dns = self_obj.get("DNSName")
    if not isinstance(dns, str) or not dns.rstrip("."):
        raise UpError(_NO_DNS)
    certs = status.get("CertDomains")
    caps = _node_caps(self_obj)
    if not (isinstance(certs, list) and certs) and "https" not in caps:
        raise UpError(_NO_HTTPS)
    if "funnel" not in caps:
        raise UpError(_NO_FUNNEL)
    return "https://" + dns.rstrip(".")


def _invite_is_live(user):
    invite = getattr(user, "invite", None)
    if not isinstance(invite, dict):
        return False
    try:
        expires = layout.parse_timestamp(invite.get("expires_at"))
    except (TypeError, ValueError, ArithmeticError):
        return False
    return expires > datetime.now(timezone.utc)


def bootstrap_admin(data, public_url, *, install=False, database=None):
    """Create the first admin user with an invite if needed. Returns the data dir."""
    from .audit import AuditLog, cli_actor

    data = resolve_data_dir(data)
    prepare_data_dir(data)
    actor = cli_actor()
    from .db import for_server_dir
    db = for_server_dir(data / 'server', database, actor=actor)
    from .workspaces import default_workspace_id
    wid = default_workspace_id(db)
    log = AuditLog(data / 'server', db=db, workspace_id=wid)
    env_key = os.environ.get("AGENTICML_SERVER_KEY")
    keys = KeyStore(data / "server", None if install else env_key, db=db, workspace_id=wid)
    users = UserStore(data / "server", db=db, workspace_id=wid)
    named = users.get("admin")
    has_admin_user = any(user.role in ("owner", "admin") for user in users.list())
    if named is not None and named.role not in ("owner", "admin") and not keys.has_admin() and not has_admin_user:
        raise UpError(_ADMIN_NAME_NOT_ADMIN)
    if keys.has_admin() or has_admin_user:
        admin_user = named if named is not None and named.role in ("owner", "admin") else None
        if admin_user is None:
            admin_user = next((user for user in users.list() if user.role in ("owner", "admin")), None)
        if (admin_user is not None and admin_user.password is None
                and not _invite_is_live(admin_user)):
            print("no admin password yet: run `agenticml server users invite "
                  f"{admin_user.name}`", flush=True)
        return data
    if keys.setup_closed():
        raise UpError(_SETUP_CLOSED)
    if not _stdout_is_tty():
        raise UpError(_NO_ADMIN_TTY)
    try:
        users.add("admin", role="admin", created_by="server up")
    except UserNameTaken:
        raise UpError(_ADMIN_NAME_NOT_ADMIN) from None
    log.record(actor, "cli", "user.add", "admin", detail={"role": users.get("admin").role, "identity": None})
    token = users.create_invite("admin", operator=True)
    log.record(actor, "cli", "user.invite", "admin",
               detail={"expires_at": users.get("admin").invite["expires_at"]})
    print(f"Open {public_url}/invite?token={token} to choose the admin "
          "password. The link works once, for 72 hours.", flush=True)
    if install and env_key:
        print(_ENV_KEY_NOT_INHERITED, flush=True)
    return data


def funnel_on(port):
    result = run_tailscale("funnel", "--bg", "--https=443", f"http://127.0.0.1:{port}")
    if result.returncode != 0:
        raise UpError(_first_line(result.stderr) or "tailscale funnel failed")


def funnel_off():
    try:
        result = run_tailscale(*_FUNNEL_OFF)
    except Exception:
        result = None
    if result is None or result.returncode != 0:
        print('warning: could not turn Tailscale Funnel off; run '
              '"tailscale funnel --https=443 off"', file=sys.stderr)


def _serve_configs(status):
    configs = [status]
    foreground = status.get("Foreground")
    if isinstance(foreground, dict):
        configs.extend(item for item in foreground.values() if isinstance(item, dict))
    return configs


def _tcp_443(tcp):
    if not isinstance(tcp, dict):
        return None
    handler = tcp.get("443", tcp.get(443))
    return handler


def _web_443(web):
    if not isinstance(web, dict):
        return []
    return [value for key, value in web.items()
            if isinstance(key, str) and key.endswith(":443")]


def _is_our_443_proxy(tcp_handler, web_handlers, target):
    if tcp_handler is not None:
        if not isinstance(tcp_handler, dict) or tcp_handler.get("TCPForward"):
            return False
        if not tcp_handler.get("HTTPS") and not tcp_handler.get("HTTP"):
            return False
    if not web_handlers:
        return False
    for handler_set in web_handlers:
        if not isinstance(handler_set, dict):
            return False
        handlers = handler_set.get("Handlers")
        if not isinstance(handlers, dict) or set(handlers) != {"/"}:
            return False
        root = handlers.get("/")
        if not isinstance(root, dict) or root.get("Proxy") != target:
            return False
    return True


def _port_443_conflict(status, port):
    target = f"http://127.0.0.1:{port}"
    for cfg in _serve_configs(status):
        tcp_handler = _tcp_443(cfg.get("TCP"))
        web_handlers = _web_443(cfg.get("Web"))
        if tcp_handler is None and not web_handlers:
            continue
        if not _is_our_443_proxy(tcp_handler, web_handlers, target):
            return True
    return False


def ensure_funnel_443_free(port):
    try:
        result = run_tailscale("funnel", "status", "--json")
    except UpError:
        print(_FUNNEL_STATUS_WARN, file=sys.stderr)
        return
    if result.returncode != 0:
        print(_FUNNEL_STATUS_WARN, file=sys.stderr)
        return
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(_FUNNEL_STATUS_WARN, file=sys.stderr)
        return
    if not isinstance(status, dict):
        print(_FUNNEL_STATUS_WARN, file=sys.stderr)
        return
    if _port_443_conflict(status, port):
        raise UpError(_PORT_443_TAKEN)


def _systemd_quote(value):
    value = str(value)
    if any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in value):
        raise UpError(_SYSTEMD_CONTROL)
    needs_quotes = value == "" or "'" in value or re.search(r'[\s"\\]', value) is not None
    if needs_quotes:
        inner = (value.replace("\\", "\\\\").replace('"', '\\"')
                 .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
                 .replace("%", "%%").replace("$", "$$"))
        return '"' + inner + '"'
    return value.replace("%", "%%").replace("$", "$$")


def _atomic_write(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    payload = data if isinstance(data, bytes) else data.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _refuse_overwrite(path, ours, force):
    if path.exists() and not force and not ours(path):
        raise UpError(f"refusing to overwrite {path}; pass --force")


def _ours_systemd(path):
    try:
        return _UNIT_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _ours_plist(path):
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("Label") == _LAUNCHD_LABEL


def systemd_unit_path(home=None):
    return Path(home or Path.home()) / ".config/systemd/user" / f"{_UNIT_NAME}.service"


def launchd_plist_path(home=None):
    return Path(home or Path.home()) / "Library/LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def write_systemd_unit(data, port, *, home=None, force=False, database=None):
    path = systemd_unit_path(home)
    _refuse_overwrite(path, _ours_systemd, force)
    exec_start = " ".join([
        _systemd_quote(sys.executable), "-m", "agenticml", "server", "up",
        "--data", _systemd_quote(data), "--port", str(port),
    ])
    if database is not None:
        # Package 19 URLs can hold passwords; callers pass only an explicit
        # --database, never AGENTICML_SERVER_DATABASE_URL.
        exec_start += ' --database ' + _systemd_quote(database)
    body = (
        f"{_UNIT_MARKER}\n"
        "[Unit]\n"
        "Description=AgenticML server up\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    _atomic_write(path, body)
    print("systemctl --user daemon-reload && systemctl --user enable --now "
          f"{_UNIT_NAME}")
    print("loginctl enable-linger $USER")
    return path


def write_launchd_plist(data, port, *, home=None, force=False, database=None):
    home = Path(home or Path.home())
    path = launchd_plist_path(home)
    _refuse_overwrite(path, _ours_plist, force)
    log_dir = home / "Library/Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / "agenticml-server-up.log")
    payload = {
        "Label": _LAUNCHD_LABEL,
        "ProgramArguments": [
            sys.executable, "-m", "agenticml", "server", "up",
            "--data", str(data), "--port", str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if database is not None:
        # Package 19 URLs can hold passwords; callers pass only an explicit
        # --database, never AGENTICML_SERVER_DATABASE_URL.
        payload['ProgramArguments'].extend(['--database', database])
    _atomic_write(path, plistlib.dumps(payload))
    print(f"launchctl load -w {path}")
    return path


def install_unit(data, port, *, force=False, database=None):
    if sys.platform.startswith("linux"):
        return write_systemd_unit(data, port, force=force, database=database)
    if sys.platform == "darwin":
        return write_launchd_plist(data, port, force=force, database=database)
    raise UpError(_INSTALL_PLATFORMS)


def up(data=None, port=8080, *, install=False, force=False, database=None):
    if install and port == 0:
        raise UpError(_INSTALL_NEEDS_PORT)
    public_url = check_tailscale()
    explicit_database = database
    database = database if database is not None else os.environ.get('AGENTICML_SERVER_DATABASE_URL')
    data = bootstrap_admin(data, public_url, install=install,
                           **({"database": database} if database is not None else {}))
    if install:
        # Package 19 URLs can hold passwords; write --database into the unit
        # only when the operator passed it explicitly, never AGENTICML_SERVER_DATABASE_URL.
        install_unit(data, port, force=force,
                     **({'database': explicit_database} if explicit_database is not None else {}))
        if explicit_database is None and database is not None:
            print(_INSTALL_ENV_DATABASE)
        return
    funnel_attempted = False
    previous_hup = None
    sighup = getattr(signal, "SIGHUP", None)

    def on_ready(server):
        nonlocal funnel_attempted, previous_hup
        bound = server.server_address[1]
        if sighup is not None and threading.current_thread() is threading.main_thread():
            if signal.getsignal(sighup) != signal.SIG_IGN:
                def stop_hup(signum, frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()
                previous_hup = signal.signal(sighup, stop_hup)
        ensure_funnel_443_free(bound)
        funnel_attempted = True
        funnel_on(bound)
        print(f"AgenticML is live at {public_url}", flush=True)

    try:
        serve(data, "127.0.0.1", port, public_url, on_ready,
              trusted_proxies=("127.0.0.1", "::1"),
              **({"database": database} if database is not None else {}))
    finally:
        try:
            if funnel_attempted:
                funnel_off()
        finally:
            if previous_hup is not None and sighup is not None:
                signal.signal(sighup, previous_hup)
