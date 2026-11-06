"""Single-writer server lifecycle and data-directory safety."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import logging
import os
from pathlib import Path
import signal
import shutil
import socket
import stat
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import weakref

from agenticml.store import layout
from agenticml.ui.server import DashboardServer, _browser_host, _loopback_bind
from .keys import KeyStore, env_key_looks_weak, validate_env_key
from .users import UserStore, migrate
from .audit import AuditLog
from .webauth import IDENTITY_PRESETS, WebAuth

logger = logging.getLogger("agenticml.server")
ATOMIC_BODY_LIMIT = 16 * 1024 * 1024
APPEND_BODY_LIMIT = 64 * 1024 * 1024
OBJECT_BODY_LIMIT = 4 * 1024 * 1024 * 1024
MISSING_BODY_LIMIT = 1024 * 1024
COMMIT_BODY_LIMIT = 1024 * 1024
MISSING_HASH_LIMIT = 10000
BODY_CONCURRENCY = 8
BODY_DEADLINE = 30.0
BODY_BYTES_PER_SECOND = 512 * 1024
# Drain starts up to 0.5 s after SIGTERM (serve_forever poll) and must
# finish, with cut-off and join, inside a typical service manager stop timeout
# (for example, systemd TimeoutStopSec=30).
SHUTDOWN_DRAIN = 8.0


@dataclass
class Limits:
    atomic_body: int = ATOMIC_BODY_LIMIT
    append_body: int = APPEND_BODY_LIMIT
    object_body: int = OBJECT_BODY_LIMIT
    lineage_body: int = 256 * 1024 * 1024
    missing_body: int = MISSING_BODY_LIMIT
    commit_body: int = COMMIT_BODY_LIMIT
    missing_hashes: int = MISSING_HASH_LIMIT
    concurrent_bodies: int = BODY_CONCURRENCY
    body_deadline: float = BODY_DEADLINE
    body_bytes_per_second: int = BODY_BYTES_PER_SECOND


def resolve_data_dir(arg=None):
    raw = arg if arg is not None else os.environ.get("AGENTICML_SERVER_DATA", "~/.agenticml-server")
    return Path(raw).expanduser().resolve()


def _directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"not a directory or is a symlink: {path}")


def prepare_data_dir(data):
    """Refuse redirected state directories and cross-device upload renames."""
    data = Path(data)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (data / "store", data / "server", data / "server" / "tmp"):
        _directory(path)
    if (data / "server").stat().st_mode & 0o077:
        logger.warning("server directory is group- or world-accessible: %s", data / "server")
    if (data / "store").stat().st_dev != (data / "server" / "tmp").stat().st_dev:
        raise ValueError("server/tmp and store must be on the same filesystem")


def acquire_server_lock(server_dir):
    """Keep the returned descriptor open until server_close; never run unlocked."""
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError("the server needs POSIX fcntl") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(Path(server_dir) / "server.lock", flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise ValueError("server.lock is not a regular file") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("server.lock is not a regular file")
        handle = os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            raise ValueError(f"another agenticml server is using {Path(server_dir).parent}") from exc
        if exc.errno in (errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP):
            raise ValueError("server filesystem does not support flock; refusing to run unlocked") from exc
        raise
    return handle


def _clean_tmp(tmp_dir):
    for entry in tmp_dir.iterdir():
        if stat.S_ISDIR(entry.lstat().st_mode):
            if entry.name.startswith("lineage-"):
                shutil.rmtree(entry)
            else:
                logger.warning("ignoring subdirectory in server/tmp: %s", entry)
        else:
            os.unlink(entry)


def _validate_public_url(value):
    """Return a normalized public URL, or None. Scheme and host are lowercased."""
    if value is None:
        return None
    stripped = value.strip()
    try:
        parsed = urlsplit(stripped)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.query and not parsed.fragment
                 and "?" not in stripped and "#" not in stripped)
        parsed.port  # Reject malformed ports as a startup error too.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("public URL must be http:// or https:// with a host and no query or fragment")
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _read_umask():
    """Use procfs when available to avoid changing the process-wide umask."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Umask:"):
                return int(line.split(":", 1)[1].strip(), 8)
    except (OSError, ValueError):
        pass
    mask = os.umask(0)
    os.umask(mask)
    return mask


class IngestServer(DashboardServer):
    # The writer lock must outlive every handler, including during shutdown.
    daemon_threads = False

    def __init__(self, data, host, port, public_url=None, env_key=None,
                 trusted_proxies=(), identity_header=None):
        from .ingest import IngestHandler

        self.data_dir = Path(data)
        self.tmp_dir = self.data_dir / "server" / "tmp"
        self.keys = KeyStore(self.data_dir / "server", env_key)
        self.users = UserStore(self.data_dir / "server")
        self.audit = AuditLog(self.data_dir / "server")
        self.public_url = public_url
        self.web = WebAuth(
            self.keys, public_url, users=self.users,
            trusted_proxies=trusted_proxies, identity_header=identity_header)
        self.store_file_mode = 0o666 & ~_read_umask()
        self.limits = Limits()
        self.lineage_enabled = shutil.which("git") is not None
        self.lineage_receives = threading.BoundedSemaphore(2)
        if not self.lineage_enabled:
            logger.warning("git is not installed; lineage code diffs are disabled")
        self.run_locks = weakref.WeakValueDictionary()
        self.run_locks_lock = threading.Lock()
        self.object_locks = weakref.WeakValueDictionary()
        self.object_locks_lock = threading.Lock()
        self._server_lock = None
        self._closing_workers = set()
        super().__init__(
            self.data_dir / "store", host, port, read_only=False,
            handler_class=IngestHandler, auth=self.web, public_url=public_url,
        )
        base = public_url or f"http://{_browser_host(self.server_address[0])}:{self.server_address[1]}"
        self.web.arm_setup(base)
        if self.web.trusted_proxies or self.web.identity_header:
            header = IDENTITY_PRESETS[self.web.identity_header][0] if self.web.identity_header else "none"
            ranges = ", ".join(str(network) for network in self.web.trusted_proxies) or "none"
            logger.info("identity header %s; trusted proxies %s", header, ranges)

    def run_lock(self, run_id):
        with self.run_locks_lock:
            lock = self.run_locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self.run_locks[run_id] = lock
            return lock

    def object_lock(self, sha):
        prefix = sha[:2]
        with self.object_locks_lock:
            lock = self.object_locks.get(prefix)
            if lock is None:
                lock = threading.Lock()
                self.object_locks[prefix] = lock
            return lock

    def server_close(self, *, drain=0.0):
        if drain > 0:
            try:
                self.socket.close()
            except OSError:
                pass
            deadline = time.monotonic() + drain
            while True:
                with self.connection_lock:
                    active = False
                    for connection, state in self.connections.items():
                        if state != "headers":
                            active = True
                            continue
                        try:
                            # Wake idle recv; leave the write side open so a
                            # request already buffered in "headers" can reply.
                            connection.shutdown(socket.SHUT_RD)
                        except OSError:
                            pass
                remaining = deadline - time.monotonic()
                if not active or remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # ThreadingMixIn clears its worker list before joining. Preserve those
        # references so a retry after an interrupted join still waits for them.
        workers = getattr(self, "_threads", None)
        if isinstance(workers, list):
            self._closing_workers.update(workers)
        super().server_close()
        for worker in self._closing_workers:
            worker.join()
        self._closing_workers.clear()
        # If joining workers is interrupted, keep the lock: those workers can
        # still be applying writes while the caller handles the interruption.
        if self._server_lock is not None:
            self._server_lock.close()
            self._server_lock = None


class IPv6IngestServer(IngestServer):
    address_family = socket.AF_INET6


def make_server(data, host="127.0.0.1", port=8080, public_url=None, env_key=None,
                trusted_proxies=(), identity_header=None):
    """Prepare and lock the data directory; port=0 chooses a free port."""
    validate_env_key(env_key)
    if env_key is not None and env_key_looks_weak(env_key):
        logger.warning(
            "AGENTICML_SERVER_KEY looks low-entropy; generate one with agenticml "
            "server keys create or secrets.token_urlsafe(32)")
    public_url = _validate_public_url(public_url)
    data = resolve_data_dir(data)
    prepare_data_dir(data)
    handle = acquire_server_lock(data / "server")
    try:
        _clean_tmp(data / "server" / "tmp")
        migrate(data, actor="server")
        cls = IPv6IngestServer if ":" in host else IngestServer
        server = cls(data, host, port, public_url, env_key, trusted_proxies, identity_header)
        server._server_lock = handle
        return server
    except BaseException:
        handle.close()
        raise


def serve(data=None, host="127.0.0.1", port=8080, public_url=None, on_ready=None,
          trusted_proxies=(), identity_header=None):
    """Run until interrupted, with an optional environment admin key."""
    server = make_server(
        data, host, port, public_url, os.environ.get("AGENTICML_SERVER_KEY"),
        trusted_proxies, identity_header)
    previous_handler = None
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        if threading.current_thread() is threading.main_thread():
            previous_handler = signal.signal(signal.SIGTERM, terminate)
        if not server.keys.list():
            logger.warning("no keys: every request except /healthz is rejected; "
                           "create a key with agenticml server keys create")
        address = _browser_host(server.server_address[0])
        print(f"AgenticML server: http://{address}:{server.server_address[1]} "
              f"(data: {server.data_dir})", flush=True)
        if server.public_url:
            print(f"Dashboard: {server.public_url}", flush=True)
        elif not _loopback_bind(host) and not _loopback_bind(server.server_address[0]):
            logger.warning(
                "browsers reach the dashboard only through a Host it accepts; "
                "set --public-url behind a proxy")
        if on_ready is not None:
            on_ready(server)
        server.serve_forever()
    finally:
        try:
            server.server_close(drain=SHUTDOWN_DRAIN)
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
