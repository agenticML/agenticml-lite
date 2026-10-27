"""Host-scoped CLI credentials, separate from the local training data root."""
from __future__ import annotations

import getpass
import json
import os
import secrets
import stat
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

DEFAULT_HOST = "https://api.agenticml.xyz"
_LOCAL_HTTP = frozenset({"localhost", "127.0.0.1", "::1"})


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


def _resolve(host=None, api_key=None, *, insecure=False, use_env=True):
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
        raw_host, host_source = DEFAULT_HOST, "default"
    resolved_host = normalize_host(raw_host, insecure=insecure)
    if api_key is not None:
        key, source = api_key, "flag"
    elif use_env and "AGENTICML_API_KEY" in os.environ:
        key, source = os.environ["AGENTICML_API_KEY"], "env"
    else:
        entry = data["hosts"].get(resolved_host, {})
        key = entry.get("api_key") if isinstance(entry, dict) else None
        source = "file"
    if source in ("flag", "env") and host_source == "file":
        print(
            f"warning: sending API key to {resolved_host} (default host from credentials file)",
            file=sys.stderr,
            flush=True,
        )
    return resolved_host, key, source


def resolve(host=None, api_key=None, *, insecure=False, use_env=True) -> tuple[str, str | None]:
    resolved_host, key, _source = _resolve(host, api_key, insecure=insecure, use_env=use_env)
    return resolved_host, key


def key_prefix(key: str) -> str:
    # Even unusually short keys must never be printed in full.
    return key[:min(8, max(0, len(key) - 1))] + "…"


def _exposes_header(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    return any("Header" in cls.__name__ for cls in type(exc).__mro__)


def _report(command, exc):
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
    from .client import Client

    try:
        # Login does not adopt AGENTICML_API_KEY. A flag key still warns in resolve
        # when the host is taken from the credentials file.
        host, _ = resolve(args.host, args.key, insecure=args.insecure, use_env=False)
        key = args.key
        if key is None:
            key = getpass.getpass("API key: ") if sys.stdin.isatty() else sys.stdin.readline()
        key = normalize_api_key(key)
        if not args.no_verify:
            client = Client(base_url=host, api_key=key, insecure=args.insecure)
            try:
                valid = client.check_auth()
            finally:
                client.close()
            if not valid:
                print("agenticml login: invalid API key", file=sys.stderr)
                return 1
        data = read_credentials()
        data["hosts"][host] = {"api_key": key}
        data["default_host"] = host
        write_credentials(data)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        return _report("login", exc)
    print(f"Logged in to {host} with key {key_prefix(key)}")
    return 0


def cmd_logout(args):
    try:
        data = read_credentials()
        # Logout defaults to the saved default, regardless of an env host override.
        # insecure=True: removing a saved http host must not require the flag again.
        host = normalize_host(args.host or data.get("default_host") or DEFAULT_HOST, insecure=True)
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
    from .client import Client

    try:
        host, key, source = _resolve(args.host, insecure=args.insecure)
        if not isinstance(key, str) or not key.strip():
            print(f"{host}: not logged in", file=sys.stderr)
            return 1
        key = normalize_api_key(key)
        validity = "unchecked (offline)"
        if not args.offline:
            client = Client(base_url=host, api_key=key, insecure=args.insecure)
            try:
                validity = "valid" if client.check_auth() else "invalid API key"
            finally:
                client.close()
        print(f"{host}  key={key_prefix(key)}  source={source}  {validity}")
        return 1 if validity == "invalid API key" else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        return _report("whoami", exc)
