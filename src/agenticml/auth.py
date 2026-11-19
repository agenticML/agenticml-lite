"""Host-scoped CLI credentials, separate from the local training data root."""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

_LOCAL_HTTP = frozenset({"localhost", "127.0.0.1", "::1"})
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\)|[@-Z\\-_])")


def normalize_host(host: str, *, insecure: bool = False) -> str:
    """Canonical origin: lowercase scheme and host, default ports dropped.

    ``http://`` is rejected except for localhost, 127.0.0.1, and ::1, unless
    ``insecure`` is set. Non-http(s) schemes are rejected.
    """
    if not isinstance(host, str):
        raise ValueError("host must be a URL")
    text = host.strip()
    if not text:
        raise ValueError("host must be a URL")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {scheme or 'missing'}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    hostname = parts.hostname
    if not hostname:
        raise ValueError("host must be a URL")
    hostname = hostname.lower()
    if scheme == "http" and hostname not in _LOCAL_HTTP and not insecure:
        raise ValueError("refusing non-local http host; pass --insecure")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def config_dir() -> Path:
    """Credential directory, never the training-data root.

    ``$AGENTICML_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/agenticml``, else
    ``~/.config/agenticml``.
    """
    override = os.environ.get("AGENTICML_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "agenticml"
    return Path.home() / ".config" / "agenticml"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _posix() -> bool:
    return os.name != "nt" and hasattr(os, "getuid")


def _stat_problem(path: Path) -> str | None:
    """Why an existing path cannot be trusted, or None when it is missing or private."""
    if not _posix():
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    hint = path if stat.S_ISDIR(info.st_mode) else path.parent
    if info.st_uid != os.getuid():
        return f"{path} is not owned by you; chmod 700 {hint}"
    if stat.S_IMODE(info.st_mode) & 0o022:
        return f"{path} is group- or world-writable; chmod 700 {hint}"
    return None


def _require_private(path: Path) -> None:
    """Refuse a config directory others can write, or one we do not own."""
    reason = _stat_problem(path)
    if reason:
        raise PermissionError(f"refusing to write credentials in {path}: {reason}")


def _prepare_config_dir(path: Path) -> None:
    """Create the credential directory at 0700, or tighten one we already own."""
    if path.exists():
        if _posix():
            info = path.stat()
            if info.st_uid != os.getuid():
                raise PermissionError(
                    f"refusing to write credentials in {path}: directory is not owned by you; "
                    f"chmod 700 {path}"
                )
            if stat.S_IMODE(info.st_mode) & 0o022:
                os.chmod(path, 0o700)
        _require_private(path)
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    _require_private(path)


def _parse_credentials(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"hosts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("hosts", {}), dict):
        raise ValueError("invalid credentials file")
    data.setdefault("hosts", {})
    return data


def _ignore_credentials(reason: str) -> dict:
    print(f"agenticml: ignoring credentials: {reason}", file=sys.stderr)
    return {"hosts": {}}


def read_credentials() -> dict:
    path = credentials_path()
    if not path.exists():
        return {"hosts": {}}
    if _posix():
        for candidate in (path.parent, path):
            reason = _stat_problem(candidate)
            if reason:
                return _ignore_credentials(reason)
    return _parse_credentials(path)


def write_credentials(data: dict) -> None:
    path = credentials_path()
    _prepare_config_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rejected_key_char(ch: str) -> bool:
    if ch.isspace():
        return True
    return unicodedata.category(ch) == "Cc"


def normalize_api_key(key):
    """Strip surrounding whitespace. Reject internal whitespace and control characters.

    The ValueError text is fixed and never includes the key. Request libraries
    put header values in ``InvalidHeader`` messages.
    """
    if not isinstance(key, str):
        raise ValueError("invalid API key")
    stripped = key.strip()
    if not stripped or any(_rejected_key_char(ch) for ch in stripped):
        raise ValueError("invalid API key")
    return stripped


Connection = namedtuple(
    "Connection", ("host", "key", "source", "http_ok", "ca_bundle", "ca_source"), defaults=(None,)
)


class CABundleError(ValueError):
    """A CA bundle that cannot be used. The text is fixed and safe to print."""


def _saved_entry(data, host):
    entry = data["hosts"].get(host, {})
    return entry if isinstance(entry, dict) else {}


def resolve_connection(host=None, api_key=None, *, insecure=False, ca_bundle=None,
                       use_env=True, use_saved=True) -> Connection:
    """Host, key, and trust settings for one command.

    ``ca_bundle`` is the unvalidated choice, most specific first: the argument,
    the bundle saved for this exact host, ``AGENTICML_CA_BUNDLE``, else None.
    ``http_ok`` is true when the argument or the host's saved entry allows plain
    http. ``use_saved=False`` ignores the saved entry's trust settings (login).
    """
    data = read_credentials()
    env_host = os.environ.get("AGENTICML_HOST")
    file_host = data.get("default_host")
    if host is not None:
        raw_host, host_source = host, "flag"
    elif env_host:
        raw_host, host_source = env_host, "env"
    elif file_host:
        raw_host, host_source = file_host, "file"
    else:
        raise ValueError("no server host; run `agenticml login --host URL`")
    resolved_host = normalize_host(raw_host, insecure=True)
    entry = _saved_entry(data, resolved_host)
    http_ok = bool(insecure) or (use_saved and entry.get("insecure") is True)
    if not http_ok:
        # Raises the usual refusal for a non-local http host.
        normalize_host(raw_host, insecure=False)
    if api_key is not None:
        key, source = api_key, "flag"
    elif use_env and "AGENTICML_API_KEY" in os.environ:
        key, source = os.environ["AGENTICML_API_KEY"], "env"
    else:
        key = entry.get("api_key")
        source = "file"
    if source in ("flag", "env") and host_source == "file":
        print(
            f"warning: sending API key to {resolved_host} (default host from credentials file)",
            file=sys.stderr,
            flush=True,
        )
    saved_bundle = entry.get("ca_bundle") if use_saved else None
    env_bundle = os.environ.get("AGENTICML_CA_BUNDLE")
    if ca_bundle is not None:
        chosen, ca_source = ca_bundle, "flag"
    elif isinstance(saved_bundle, str) and saved_bundle:
        chosen, ca_source = saved_bundle, "saved"
    elif env_bundle:
        chosen, ca_source = env_bundle, "env"
    else:
        chosen, ca_source = None, None
    return Connection(resolved_host, key, source, http_ok, chosen, ca_source)


def _resolve(host=None, api_key=None, *, insecure=False, use_env=True):
    conn = resolve_connection(host, api_key, insecure=insecure, use_env=use_env)
    return conn.host, conn.key, conn.source


def resolve(host=None, api_key=None, *, insecure=False, use_env=True) -> tuple[str, str | None]:
    resolved_host, key, _source = _resolve(host, api_key, insecure=insecure, use_env=use_env)
    return resolved_host, key


def _absolute_path(path) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def load_ca_bundle(path, *, saved=False) -> str:
    """Validate a CA bundle and return the path of the final regular file.

    Call this only where a Client is about to be created for an https host.
    ``saved=True`` marks a value from the credentials file, which must be absolute.
    """
    if not isinstance(path, str) or not path:
        raise CABundleError("CA bundle not found or unreadable: ?")
    shown = _clean_detail(path)
    expanded = os.path.expanduser(path)
    if saved and not os.path.isabs(expanded):
        raise CABundleError(
            f"CA bundle path in the credentials file is not absolute: {shown}; "
            "run `agenticml login --ca-bundle PATH` again"
        )
    absolute = os.path.abspath(expanded)
    final = os.path.realpath(absolute)
    try:
        info = os.stat(final)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        with open(final, "rb"):
            pass
        parent = os.stat(os.path.dirname(final))
    except OSError:
        raise CABundleError(f"CA bundle not found or unreadable: {shown}") from None
    if _posix():
        folder = _clean_detail(os.path.dirname(final))
        for item, what in ((info, "file"), (parent, "directory")):
            if item.st_uid not in (os.getuid(), 0):
                if what == "file":
                    raise CABundleError(f"CA bundle is not owned by you or root: {shown}")
                raise CABundleError(
                    f"CA bundle directory is not owned by you or root: {folder}")
            if stat.S_IMODE(item.st_mode) & 0o022:
                if what == "file":
                    raise CABundleError(f"CA bundle is writable by others: {shown}; chmod go-w")
                raise CABundleError(
                    f"CA bundle directory is writable by others: {folder}; chmod go-w {folder}")
    return final


def warn_plain_http(host: str) -> None:
    """Say once per command that the key travels unencrypted to a non-local host."""
    parts = urlsplit(host)
    if parts.scheme == "http" and (parts.hostname or "") not in _LOCAL_HTTP:
        print(f"warning: sending the API key over plain http to {host}",
              file=sys.stderr, flush=True)


def trusted_bundle(conn: Connection) -> str | None:
    """The validated bundle path for an https connection, else None."""
    if conn.ca_bundle is None or not conn.host.startswith("https://"):
        return None
    return load_ca_bundle(conn.ca_bundle, saved=conn.ca_source == "saved")


def key_prefix(key: str) -> str:
    # Even unusually short keys must never be printed in full.
    return key[:min(8, max(0, len(key) - 1))] + "…"


def _clean_detail(text) -> str:
    """Strip control characters from a server-supplied label before printing."""
    if not isinstance(text, str):
        text = "?"
    text = _ANSI_RE.sub("", text)
    text = "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7f)
    return text[:200] or "?"


def _exposes_header(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    return any("Header" in cls.__name__ for cls in type(exc).__mro__)


def _report(command, exc):
    from .client import CABundleUnavailable, tls_hint

    if isinstance(exc, CABundleUnavailable):
        print(f"agenticml {command}: {exc}", file=sys.stderr)
        return 1
    hint = tls_hint(exc)
    if hint:
        print(f"agenticml {command}: {hint}", file=sys.stderr)
        return 1
    if not _exposes_header(exc) and isinstance(exc, (PermissionError, ValueError)):
        print(f"agenticml {command}: {exc}", file=sys.stderr)
        return 1
    # Request errors can contain URLs, header values, or other sensitive details.
    print(f"agenticml {command}: unable to log in ({type(exc).__name__})"
          if command == "login" else
          f"agenticml {command}: unable to update credentials ({type(exc).__name__})"
          if command == "logout" else
          f"agenticml {command}: unable to verify credentials ({type(exc).__name__})",
          file=sys.stderr)
    return 1


def cmd_login(args):
    from .client import Client, ProtocolError

    info = None
    try:
        # Login does not adopt AGENTICML_API_KEY. A flag key still warns in resolve
        # when the host is taken from the credentials file.
        # Login trusts only its own flags (plus AGENTICML_CA_BUNDLE), never the saved entry.
        flag_bundle = getattr(args, "ca_bundle", None)
        conn = resolve_connection(
            args.host, args.key, insecure=args.insecure, ca_bundle=flag_bundle,
            use_env=False, use_saved=False,
        )
        host = conn.host
        https = host.startswith("https://")
        # A bundle given to login is validated and saved even with --no-verify.
        bundle = None
        if https and (flag_bundle is not None or not args.no_verify):
            bundle = trusted_bundle(conn)
        key = args.key
        if key is None:
            key = getpass.getpass("API key: ") if sys.stdin.isatty() else sys.stdin.readline()
        key = normalize_api_key(key)
        if not args.no_verify:
            warn_plain_http(host)
            client = Client(base_url=host, api_key=key, insecure=conn.http_ok, ca_bundle=bundle)
            try:
                info = client.whoami()
            finally:
                client.close()
        entry = {"api_key": key}
        if https and flag_bundle is not None:
            entry["ca_bundle"] = _absolute_path(flag_bundle)
        elif not https and args.insecure and urlsplit(host).hostname not in _LOCAL_HTTP:
            entry["insecure"] = True
        data = read_credentials()
        data["hosts"][host] = entry
        data["default_host"] = host
        write_credentials(data)
    except KeyboardInterrupt:
        return 130
    except ProtocolError:
        print(
            f"agenticml login: {host} is not an agenticml server "
            f"with protocol 2 (agenticml server 2.0 or newer)",
            file=sys.stderr,
        )
        return 1
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 401:
            print("agenticml login: invalid API key", file=sys.stderr)
            return 1
        return _report("login", exc)
    except Exception as exc:
        return _report("login", exc)
    if info is None:
        print(f"Logged in to {host} with key {key_prefix(key)}")
        return 0
    print(
        f"Logged in to {host} as {_clean_detail(info.get('key', '?'))} "
        f"(scope {_clean_detail(info.get('scope', '?'))}) "
        f"with key {key_prefix(key)}"
    )
    if info.get("scope") == "admin":
        print(
            "warning: this is an admin key; training machines should use a sync key",
            file=sys.stderr,
        )
    return 0


def cmd_logout(args):
    try:
        data = read_credentials()
        # Logout defaults to the saved default, regardless of an env host override.
        # insecure=True: removing a saved http host must not require the flag again.
        raw_host = args.host or data.get("default_host")
        if not raw_host:
            print(
                "agenticml logout: no host given and no default host saved",
                file=sys.stderr,
            )
            return 1
        host = normalize_host(raw_host, insecure=True)
        data["hosts"].pop(host, None)
        if data.get("default_host") == host:
            data.pop("default_host", None)
        write_credentials(data)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        return _report("logout", exc)
    print(f"Logged out of {host}")
    return 0


def cmd_whoami(args):
    from .client import Client, ProtocolError

    try:
        conn = resolve_connection(
            args.host, insecure=args.insecure, ca_bundle=getattr(args, "ca_bundle", None)
        )
        host, key, source = conn.host, conn.key, conn.source
        if not isinstance(key, str) or not key.strip():
            print(f"{host}: not logged in", file=sys.stderr)
            return 1
        key = normalize_api_key(key)
        if args.offline:
            print(f"{host}  key={key_prefix(key)}  source={source}  unchecked (offline)")
            return 0
        bundle = trusted_bundle(conn)
        warn_plain_http(host)
        client = Client(base_url=host, api_key=key, insecure=conn.http_ok, ca_bundle=bundle)
        try:
            info = client.whoami()
        finally:
            client.close()
        print(
            f"{host}  key={key_prefix(key)}  name={_clean_detail(info.get('key', '?'))}  "
            f"scope={_clean_detail(info.get('scope', '?'))}  source={source}  valid"
        )
        if isinstance(info.get("user"), str) and isinstance(info.get("role"), str):
            print(f"user: {_clean_detail(info['user'])} ({_clean_detail(info['role'])})")
        if isinstance(info.get("workspace"), str):
            print(f"workspace: {_clean_detail(info['workspace'])}")
        return 0
    except KeyboardInterrupt:
        return 130
    except ProtocolError:
        print(
            f"agenticml whoami: {host} is not an agenticml server "
            f"with protocol 2 (agenticml server 2.0 or newer)",
            file=sys.stderr,
        )
        return 1
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 401:
            print("invalid API key", file=sys.stderr)
            return 1
        return _report("whoami", exc)
    except Exception as exc:
        return _report("whoami", exc)
