"""Single-writer server lifecycle and data-directory safety."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import logging
import os
from pathlib import Path
import socket
import stat
import threading
from urllib.parse import urlsplit
import weakref

from agenticml.store import layout
from agenticml.ui.server import DashboardServer, _browser_host
from .keys import KeyStore, validate_env_key

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


@dataclass
class Limits:
    atomic_body: int = ATOMIC_BODY_LIMIT
    append_body: int = APPEND_BODY_LIMIT
    object_body: int = OBJECT_BODY_LIMIT
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
    handle = layout.open_nofollow(Path(server_dir) / "server.lock", "ab")
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
            logger.warning("ignoring subdirectory in server/tmp: %s", entry)
        else:
            os.unlink(entry)


def _validate_public_url(value):
    if value is None:
        return
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and not parsed.query and not parsed.fragment and "?" not in value and "#" not in value)
        parsed.port  # Reject malformed ports as a startup error too.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("public URL must be http:// or https:// with a host and no query or fragment")


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

    def __init__(self, data, host, port, public_url=None, env_key=None):
        from .ingest import IngestHandler

        self.data_dir = Path(data)
        self.tmp_dir = self.data_dir / "server" / "tmp"
        self.keys = KeyStore(self.data_dir / "server", env_key)
        self.public_url = public_url
        self.store_file_mode = 0o666 & ~_read_umask()
        self.limits = Limits()
        self.run_locks = weakref.WeakValueDictionary()
        self.run_locks_lock = threading.Lock()
        self.objects_lock = threading.Lock()
        self._server_lock = None
        self._closing_workers = set()
        super().__init__(self.data_dir / "store", host, port, handler_class=IngestHandler)

    def run_lock(self, run_id):
        with self.run_locks_lock:
            lock = self.run_locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self.run_locks[run_id] = lock
            return lock

    def server_close(self):
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


def make_server(data, host="127.0.0.1", port=8080, public_url=None, env_key=None):
    """Prepare and lock the data directory; port=0 chooses a free port."""
    validate_env_key(env_key)
    _validate_public_url(public_url)
    data = resolve_data_dir(data)
    prepare_data_dir(data)
    handle = acquire_server_lock(data / "server")
    try:
        _clean_tmp(data / "server" / "tmp")
        cls = IPv6IngestServer if ":" in host else IngestServer
        server = cls(data, host, port, public_url, env_key)
        server._server_lock = handle
        return server
    except BaseException:
        handle.close()
        raise


def serve(data=None, host="127.0.0.1", port=8080, public_url=None):
    """Run until interrupted, with an optional environment admin key."""
    server = make_server(data, host, port, public_url, os.environ.get("AGENTICML_SERVER_KEY"))
    try:
        if not server.keys.list():
            logger.warning("no keys: every request except /healthz is rejected; "
                           "create a key with agenticml server keys create")
        elif not server.keys.has_admin():
            logger.warning("no admin key; create one with agenticml server keys create --scope admin")
        address = _browser_host(server.server_address[0])
        print(f"AgenticML server: http://{address}:{server.server_address[1]} "
              f"(data: {server.data_dir})", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
