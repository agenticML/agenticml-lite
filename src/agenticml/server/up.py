"""Laptop setup: Tailscale checks, first admin key, Funnel, optional user service."""
from __future__ import annotations

from datetime import datetime, timezone
try:
    import grp
    import pwd
except ImportError:
    grp = None
    pwd = None
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import stat
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
_ENV_PATH_CONTROL = "environment file path contains a control character"
_ENV_PATH_GLOB_CHARS = "*?[]\\"
_ENV_PATH_GLOB = (
    "environment file path contains a systemd glob character "
    "(asterisk, question mark, brackets, or backslash); "
    "use a different home or config directory")
_UNSAFE_DIR_SYMLINK = "directory is a symlink"
_UNSAFE_DIR_NOT_DIR = "not a directory"
_UNSAFE_DIR_OWNER = "not owned by the current user"
_UNSAFE_DIR_WRITABLE = (
    "directory is world-writable, or group-writable by a group that is "
    "not the current user's private group")
_FUNNEL_STATUS_WARN = "warning: could not read Tailscale Funnel status; continuing"
_DATABASE_URL_NEWLINE = "database URL must not contain a newline or NUL"
_ENV_DATABASE_KEY = "AGENTICML_SERVER_DATABASE_URL"
_ENV_FILE_MARKER = (
    "# agenticml-server environment; written by agenticml server up --install")
_SHELL_NEED_ESCAPE = '"\\`$'


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


def bootstrap_admin(data, public_url, *, install=False, database=None, firebase=None):
    """Create the first admin user with an invite if needed. Returns the data dir."""
    from .audit import AuditLog, cli_actor

    data = resolve_data_dir(data)
    prepare_data_dir(data)
    if firebase is not None:
        print("run `agenticml server accounts add <your-email> --server-admin`, "
              "then sign in with Firebase.", flush=True)
        return data
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


def _refuse_untrusted_dir(path, reason):
    raise UpError(f"refusing to use {path}: {reason}")


def _is_user_private_group(gid):
    """True if *gid* is the current user's private group.

    Ubuntu's default umask 002 with user-private groups creates directories
    0775 owned by a group named after the user, with no other listed
    members. Those directories are as private as 0755 under umask 022.
    Shared groups (staff, users, ...) stay refused even when gid matches.
    """
    if pwd is None or grp is None:
        return False
    if gid != os.getgid():
        return False
    try:
        user = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(gid)
    except (KeyError, OSError):
        return False
    if group.gr_name != user.pw_name:
        return False
    return all(name == user.pw_name for name in group.gr_mem)


def _require_private_dir(path, st):
    if stat.S_ISLNK(st.st_mode):
        _refuse_untrusted_dir(path, _UNSAFE_DIR_SYMLINK)
    if not stat.S_ISDIR(st.st_mode):
        _refuse_untrusted_dir(path, _UNSAFE_DIR_NOT_DIR)
    if st.st_uid != os.geteuid():
        _refuse_untrusted_dir(path, _UNSAFE_DIR_OWNER)
    mode = stat.S_IMODE(st.st_mode)
    # World-writable is always refused. Group-writable is allowed only for
    # the user's private group (see _is_user_private_group).
    if mode & 0o002:
        _refuse_untrusted_dir(path, _UNSAFE_DIR_WRITABLE)
    if mode & 0o020 and not _is_user_private_group(st.st_gid):
        _refuse_untrusted_dir(path, _UNSAFE_DIR_WRITABLE)


def _lineage_from_home(path, home):
    """Yield each component from $HOME (inclusive) down to path."""
    path = Path(os.path.abspath(os.fspath(path)))
    home = Path(os.path.abspath(os.fspath(home)))
    if path != home and home not in path.parents:
        _refuse_untrusted_dir(path, "not under the home directory")
    yield home
    if path == home:
        return
    current = home
    for part in path.relative_to(home).parts:
        current = current / part
        yield current


def _ensure_private_directory(path, home):
    """Create missing components at 0700; refuse symlink / foreign / writable dirs.

    Every component from $HOME (inclusive) through *path* is lstat'd and
    must be a real directory owned by the current user, not world-writable,
    and not group-writable unless the directory's group is the current
    user's private group (gid == os.getgid(), group name equals the user
    name, and grp.getgrgid(gid).gr_mem is empty or only that user).
    Ancestors of $HOME are not walked (they may be root-owned).
    """
    for component in _lineage_from_home(path, home):
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            try:
                os.mkdir(component, 0o700)
            except FileExistsError:
                pass
            st = os.lstat(component)
        _require_private_dir(component, st)


def _require_private_lineage(path, home):
    """Same lstat checks as _ensure_private_directory, without creating."""
    for component in _lineage_from_home(path, home):
        _require_private_dir(component, os.lstat(component))


def _open_private_dir(path):
    # Residual: the check-then-open window is only exploitable by someone
    # who can already write to the user's own directories, which these
    # checks refuse.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        dir_fd = os.open(path, flags)
    except OSError as exc:
        try:
            st = os.lstat(path)
        except OSError:
            raise UpError(f"refusing to use {path}: not a private directory") from exc
        _require_private_dir(path, st)
        raise UpError(f"refusing to use {path}: not a private directory") from exc
    try:
        _require_private_dir(path, os.fstat(dir_fd))
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _atomic_write(path, data, mode=0o600, *, home=None):
    path = Path(path)
    parent = path.parent
    home = Path(home or Path.home())
    _ensure_private_directory(parent, home)
    payload = data if isinstance(data, bytes) else data.encode("utf-8")
    tmp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    dir_fd = _open_private_dir(parent)
    created = False
    try:
        fd = os.open(tmp_name, flags, mode, dir_fd=dir_fd)
        created = True
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        created = False
    except BaseException:
        if created:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(dir_fd)


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


def _ours_env(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return False
    return first.rstrip("\n\r") == _ENV_FILE_MARKER


def _remove_our_env_file(path, home=None):
    path = Path(path)
    home = Path(home or Path.home())
    try:
        _require_private_lineage(path.parent, home)
    except FileNotFoundError:
        return
    dir_fd = _open_private_dir(path.parent)
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError:
            return
        try:
            raw = os.read(fd, 512)
        finally:
            os.close(fd)
        first = raw.split(b"\n", 1)[0].rstrip(b"\r")
        if first != _ENV_FILE_MARKER.encode("utf-8"):
            return
        os.unlink(path.name, dir_fd=dir_fd)
    except OSError:
        return
    finally:
        os.close(dir_fd)


def systemd_unit_path(home=None):
    return Path(home or Path.home()) / ".config/systemd/user" / f"{_UNIT_NAME}.service"


def systemd_env_path(home=None):
    return Path(home or Path.home()) / ".config/agenticml/server.env"


def launchd_plist_path(home=None):
    return Path(home or Path.home()) / "Library/LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _require_single_line_database(url):
    if "\n" in url or "\r" in url or "\0" in url:
        raise UpError(_DATABASE_URL_NEWLINE)


def _environment_file_line(path):
    # EnvironmentFile= takes an unquoted absolute path. ExecStart quoting
    # (double quotes, $$) makes systemd reject the path as not absolute.
    # systemd 249 globs the path (* ? [ ] \\), so refuse those rather than
    # trying to escape them.
    path = str(path)
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in path):
        raise UpError(_ENV_PATH_CONTROL)
    if any(char in _ENV_PATH_GLOB_CHARS for char in path):
        raise UpError(_ENV_PATH_GLOB)
    return "EnvironmentFile=" + path.replace("%", "%%") + "\n"


def _environment_file_assignment(name, value):
    # systemd 249 EnvironmentFile: double-quote and escape SHELL_NEED_ESCAPE
    # (" \ ` $) so the assignment is one line; refuse newline/NUL first.
    _require_single_line_database(value)
    escaped = "".join("\\" + char if char in _SHELL_NEED_ESCAPE else char
                      for char in value)
    return f'{name}="{escaped}"\n'


def write_systemd_unit(data, port, *, home=None, force=False, database=None, firebase=None):
    home = Path(home or Path.home())
    path = systemd_unit_path(home)
    env_path = systemd_env_path(home)
    env_line = ""
    if database is not None:
        # Passwords belong in a 0600 EnvironmentFile, never ExecStart/argv.
        _require_single_line_database(database)
        env_line = _environment_file_line(env_path)
    _refuse_overwrite(path, _ours_systemd, force)
    if database is not None:
        _refuse_overwrite(env_path, _ours_env, force)
    exec_start = " ".join([
        _systemd_quote(sys.executable), "-m", "agenticml", "server", "up",
        "--data", _systemd_quote(data), "--port", str(port),
    ])
    if firebase is not None:
        exec_start += ' --firebase-project ' + _systemd_quote(firebase.project)
        exec_start += ' --firebase-api-key ' + _systemd_quote(firebase.api_key)
        exec_start += ' --firebase-auth-domain ' + _systemd_quote(firebase.auth_domain)
    body = (
        f"{_UNIT_MARKER}\n"
        "[Unit]\n"
        "Description=AgenticML server up\n"
        "\n"
        "[Service]\n"
        f"{env_line}"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    _atomic_write(path, body, home=home)
    if database is not None:
        _atomic_write(
            env_path,
            f"{_ENV_FILE_MARKER}\n"
            f"{_environment_file_assignment(_ENV_DATABASE_KEY, database)}",
            home=home)
    else:
        _remove_our_env_file(env_path, home=home)
    print("systemctl --user daemon-reload && systemctl --user enable --now "
          f"{_UNIT_NAME}")
    print("loginctl enable-linger $USER")
    return path


def write_launchd_plist(data, port, *, home=None, force=False, database=None, firebase=None):
    home = Path(home or Path.home())
    if database is not None:
        _require_single_line_database(database)
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
        payload["EnvironmentVariables"] = {_ENV_DATABASE_KEY: database}
    if firebase is not None:
        payload['ProgramArguments'].extend([
            '--firebase-project', firebase.project,
            '--firebase-api-key', firebase.api_key,
            '--firebase-auth-domain', firebase.auth_domain,
        ])
    _atomic_write(path, plistlib.dumps(payload), home=home)
    print(f"launchctl load -w {path}")
    return path


def install_unit(data, port, *, force=False, database=None, firebase=None):
    extra = {}
    if firebase is not None:
        extra['firebase'] = firebase
    if sys.platform.startswith("linux"):
        return write_systemd_unit(data, port, force=force, database=database, **extra)
    if sys.platform == "darwin":
        return write_launchd_plist(data, port, force=force, database=database, **extra)
    raise UpError(_INSTALL_PLATFORMS)


def up(data=None, port=8080, *, install=False, force=False, database=None, firebase=None):
    if install and port == 0:
        raise UpError(_INSTALL_NEEDS_PORT)
    public_url = check_tailscale()
    explicit_database = database
    database = database if database is not None else os.environ.get('AGENTICML_SERVER_DATABASE_URL')
    extra_bootstrap = {}
    if database is not None:
        extra_bootstrap["database"] = database
    if firebase is not None:
        extra_bootstrap["firebase"] = firebase
    data = bootstrap_admin(data, public_url, install=install, **extra_bootstrap)
    if install:
        # Package 19 URLs can hold passwords; persist an explicit --database
        # in the service environment, never argv or AGENTICML_SERVER_DATABASE_URL.
        install_extra = {}
        if explicit_database is not None:
            install_extra['database'] = explicit_database
        if firebase is not None:
            install_extra['firebase'] = firebase
        install_unit(data, port, force=force, **install_extra)
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
        serve_extra = {}
        if database is not None:
            serve_extra["database"] = database
        if firebase is not None:
            serve_extra["firebase"] = firebase
        serve(data, "127.0.0.1", port, public_url, on_ready,
              trusted_proxies=("127.0.0.1", "::1"),
              **serve_extra)
    finally:
        try:
            if funnel_attempted:
                funnel_off()
        finally:
            if previous_hup is not None and sighup is not None:
                signal.signal(sighup, previous_hup)
