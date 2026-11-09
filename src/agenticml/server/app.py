"""Server lifecycle and data-directory safety."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
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
from .users import UserStore
from .db import DatabaseBusy, for_server_dir, resolve_url, _is_postgres
from .db.repos import NoteRepo, RunRepo
from .db.workspaces import WorkspaceRepo, require_workspace_id
from .workspaces import default_workspace_id, prepare_store, store_root
from .runs import reconcile, store_paths_safe
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
    for path in (data / "server", data / "server" / "tmp", data / "workspaces"):
        _directory(path)
    if (data / "server").stat().st_mode & 0o077:
        logger.warning("server directory is group- or world-accessible: %s", data / "server")
    if (data / "workspaces").stat().st_dev != (data / "server" / "tmp").stat().st_dev:
        raise ValueError("server/tmp and workspaces must be on the same filesystem")


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
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise ValueError(f"another agenticml server is using {Path(server_dir).parent}") from exc
        if exc.errno in (errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP):
            raise ValueError("server filesystem does not support flock; refusing to run unlocked") from exc
        raise
    return handle


def tmp_retention_seconds():
    """Age after which Postgres mode may delete another process's tmp file.

    A legal slow body can last BODY_DEADLINE + OBJECT_BODY_LIMIT /
    BODY_BYTES_PER_SECOND (~2.3 h). Plus a 1 h margin. Host clocks and
    filesystem mtime are assumed to agree within that margin; larger skew
    can delete an in-flight upload or retain a dead file longer.
    """
    return BODY_DEADLINE + OBJECT_BODY_LIMIT / BODY_BYTES_PER_SECOND + 3600


def _clean_tmp(tmp_dir, *, postgres=False):
    cutoff = time.time() - tmp_retention_seconds()
    for entry in tmp_dir.iterdir():
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if postgres and info.st_mtime >= cutoff:
            continue
        if stat.S_ISDIR(info.st_mode):
            if entry.name.startswith("lineage-"):
                try:
                    shutil.rmtree(entry)
                except FileNotFoundError:
                    pass  # Another starting process already cleaned it.
            else:
                logger.warning("ignoring subdirectory in server/tmp: %s", entry)
        else:
            entry.unlink(missing_ok=True)


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


class _ProcessLock:
    """Thread lock followed by a database session lock, released in reverse."""

    def __init__(self, lock, db, key):
        self._lock, self._db, self._key = lock, db, key
        self._session = None

    def locked(self):
        return self._lock.locked()

    def acquire(self, blocking=True, timeout=-1):
        if not blocking:
            wait = 0
        elif timeout is None or timeout < 0:
            wait = None
        else:
            wait = timeout
        deadline = None if wait is None else time.monotonic() + wait
        if wait == 0:
            got = self._lock.acquire(False)
        elif wait is None:
            got = self._lock.acquire()
        else:
            got = self._lock.acquire(True, wait)
        if not got:
            return False
        try:
            if not blocking:
                session = self._db.session_lock(self._key, timeout=0, try_lock=True)
            elif deadline is None:
                session = self._db.session_lock(self._key, timeout=-1)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabaseBusy('database is locked')
                session = self._db.session_lock(self._key, timeout=remaining)
            session.__enter__()
        except DatabaseBusy:
            self._lock.release()
            if blocking and wait is None:
                raise
            return False
        except BaseException:
            self._lock.release()
            raise
        self._session = session
        return True

    def release(self):
        session = self._session
        self._session = None
        try:
            if session is not None:
                session.__exit__(None, None, None)
        finally:
            self._lock.release()

    def __enter__(self):
        self._lock.acquire()
        try:
            self._session = self._db.session_lock(self._key)
            self._session.__enter__()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, *exc):
        session = self._session
        self._session = None
        try:
            if session is not None:
                return session.__exit__(*exc)
        finally:
            self._lock.release()


class IngestServer(DashboardServer):
    # The writer lock must outlive every handler, including during shutdown.
    daemon_threads = False

    def __init__(self, data, host, port, public_url=None, env_key=None,
                 trusted_proxies=(), identity_header=None, db=None,
                 firebase=None, cert_source=None, plans=None, open_signup=False):
        from .firebase import require_open_signup
        from .ingest import IngestHandler

        require_open_signup(open_signup, firebase, plans)
        self.data_dir = Path(data)
        self.tmp_dir = self.data_dir / "server" / "tmp"
        db = db if db is not None else for_server_dir(self.data_dir / "server")
        self.db = db
        self.plans = plans
        self.workspaces = WorkspaceRepo(db)
        default = self.workspaces.get('default')
        self.default_workspace_id = default_workspace_id(db) if default else None
        self.run_index = RunRepo(db, self.default_workspace_id) if default else None
        self.note_index = NoteRepo(db, self.default_workspace_id) if default else None
        self.keys = KeyStore(self.data_dir / "server", env_key, db=db,
                             workspace_id=self.default_workspace_id)
        self.users = UserStore(self.data_dir / "server", db=db, workspace_id=self.default_workspace_id) if default else None
        self.audit = AuditLog(self.data_dir / "server", db=db, workspace_id=self.default_workspace_id)
        self._ws_stores = {}
        self._ws_stores_lock = threading.Lock()
        self.public_url = public_url
        self.web = WebAuth(
            self.keys, public_url, users=self.users,
            trusted_proxies=trusted_proxies, identity_header=identity_header,
            firebase=firebase, cert_source=cert_source, plans=plans,
            open_signup=open_signup)
        self.store_file_mode = 0o666 & ~_read_umask()
        self.limits = Limits()
        self.lineage_enabled = shutil.which("git") is not None
        self.lineage_receives = threading.BoundedSemaphore(2)
        if not self.lineage_enabled:
            logger.warning("git is not installed; lineage code diffs are disabled")
        self.run_locks = weakref.WeakValueDictionary()
        self.run_locks_lock = threading.Lock()
        self.quota_locks = weakref.WeakValueDictionary()
        self.quota_locks_lock = threading.Lock()
        self.object_locks = weakref.WeakValueDictionary()
        self.object_locks_lock = threading.Lock()
        self.lineage_locks = weakref.WeakValueDictionary()
        self.lineage_locks_lock = threading.Lock()
        self._server_lock = None
        self._closing_workers = set()
        super().__init__(
            store_root(self.data_dir, default['id']) if default else self.data_dir / 'workspaces' / 'unavailable', host, port, read_only=False,
            handler_class=IngestHandler, auth=self.web, public_url=public_url,
        )
        base = public_url or f"http://{_browser_host(self.server_address[0])}:{self.server_address[1]}"
        self.web.arm_setup(base)
        if self.web.trusted_proxies or self.web.identity_header:
            header = IDENTITY_PRESETS[self.web.identity_header][0] if self.web.identity_header else "none"
            ranges = ", ".join(str(network) for network in self.web.trusted_proxies) or "none"
            logger.info("identity header %s; trusted proxies %s", header, ranges)

    def _after_append_write(self, run_id, path):
        """Test hook: runs after an append is fsynced and before the size commit."""
        if (os.environ.get("AGENTICML_TEST_HOOKS") == "1"
                and os.environ.get("PYTEST_CURRENT_TEST")):
            delay = int(os.environ.get("AGENTICML_TEST_APPEND_DELAY_MS", "0"))
            if delay > 0:
                print(f"AGENTICML_TEST_APPEND_WRITE {run_id} {path}\n", end="", flush=True)
                time.sleep(delay / 1000)

    def _after_atomic_write(self, run_id, path):
        """Test hook: after rename+fsync, still inside the recording transaction."""

    def _after_meta_claim(self, run_id):
        """Test hook: runs after claiming ownership and before writing meta.json."""

    def drop_workspace_stores(self, workspace_id):
        """Drop cached stores after a workspace is deleted or by_id returns None."""
        workspace_id = require_workspace_id(workspace_id)
        with self._ws_stores_lock:
            self._ws_stores.pop(workspace_id, None)

    def workspace_stores(self, workspace_id, status):
        """Cached UserStore/KeyStore/AuditLog for a workspace; rebuilt on status change."""
        from .access import WorkspaceStores

        workspace_id = require_workspace_id(workspace_id)
        with self._ws_stores_lock:
            cached = self._ws_stores.get(workspace_id)
            if cached is None or cached[0] != status:
                cached = (status, WorkspaceStores(self, workspace_id))
                self._ws_stores[workspace_id] = cached
            return cached[1]

    @contextmanager
    def run_reader(self, run_id, *, workspace_id):
        from agenticml.store import reader as store_reader

        wid = require_workspace_id(workspace_id)
        key = (wid, run_id)
        index = RunRepo(self.db, wid)
        with self.reader_lock:
            if key not in self.readers:
                self.readers[key] = store_reader.RunReader(
                    store_root(self.data_dir, wid), run_id, remote=index.normalized_remote,
                    committed_size=index.committed_size if self.db.dialect.name == "postgres" else None)
            self.readers.move_to_end(key)
            while len(self.readers) > 64:
                self.readers.popitem(last=False)
            current = self.readers[key]
        yield current

    def cached_reader(self, run_id, *, workspace_id):
        with self.reader_lock:
            return self.readers.get((require_workspace_id(workspace_id), run_id))

    def code_pair_diff(self, base_sha, target_sha, budget, *, workspace_id):
        from agenticml.ui import server as ui

        wid = require_workspace_id(workspace_id)
        key = (wid, base_sha, target_sha, ui._CODE_DIFF_MAX_BYTES, ui._CODE_DIFF_FILE_LINES,
               ui._CODE_DIFF_MAX_LINES, ui._CODE_DIFF_LINE_CHARS, ui._CODE_DIFF_AUTOJUNK_THRESHOLD)
        with self.code_diff_lock:
            if key in self.code_diffs:
                self.code_diffs.move_to_end(key)
                return self.code_diffs[key]
        result = ui._code_pair_diff(store_root(self.data_dir, wid), base_sha, target_sha,
                                    ui._CODE_DIFF_MAX_BYTES, ui._CODE_DIFF_FILE_LINES, budget)
        if 'error' not in result and not budget.exhausted():
            with self.code_diff_lock:
                self.code_diffs[key] = result
                self.code_diffs.move_to_end(key)
                while len(self.code_diffs) > ui._CODE_DIFF_CACHE_SIZE:
                    self.code_diffs.popitem(last=False)
        return result

    def run_lock(self, run_id, *, workspace_id):
        run_id = (require_workspace_id(workspace_id), run_id)
        with self.run_locks_lock:
            lock = self.run_locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self.run_locks[run_id] = lock
            return self._combined_lock(lock, f"run:{run_id[0]}:{run_id[1]}")

    def quota_lock(self, workspace_id):
        """Lock check, publish and accounting after the run/object lock.

        Nothing may wait on a run/object lock while holding this lock.
        """
        workspace_id = require_workspace_id(workspace_id)
        with self.quota_locks_lock:
            lock = self.quota_locks.get(workspace_id)
            if lock is None:
                lock = threading.Lock()
                self.quota_locks[workspace_id] = lock
            return self._combined_lock(lock, f"quota:{workspace_id}")

    def object_lock(self, sha, *, workspace_id):
        prefix = (require_workspace_id(workspace_id), sha[:2])
        with self.object_locks_lock:
            lock = self.object_locks.get(prefix)
            if lock is None:
                lock = threading.Lock()
                self.object_locks[prefix] = lock
            return self._combined_lock(lock, f"object:{prefix[0]}:{prefix[1]}")

    def _combined_lock(self, lock, key):
        # Order: thread → session → transaction; run/object → quota.
        return _ProcessLock(lock, self.db, key) if self.db.dialect.name == "postgres" else lock

    def lineage_lock(self, workspace_id, project_key):
        workspace_id = require_workspace_id(workspace_id)
        key = (workspace_id, project_key)
        with self.lineage_locks_lock:
            lock = self.lineage_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self.lineage_locks[key] = lock
            return self._combined_lock(lock, f"lineage:{workspace_id}:{project_key}")

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
        if self.db is not None:
            self.db.close()
        if self._server_lock is not None:
            self._server_lock.close()
            self._server_lock = None


class IPv6IngestServer(IngestServer):
    address_family = socket.AF_INET6


def make_server(data, host="127.0.0.1", port=8080, public_url=None, env_key=None,
                trusted_proxies=(), identity_header=None, database=None,
                firebase=None, cert_source=None, plans=None, open_signup=False):
    """Prepare and lock the data directory; port=0 chooses a free port."""
    from .firebase import require_open_signup
    from .limits import Plans
    if plans is not None and not isinstance(plans, Plans):
        plans = Plans.load(plans)
    require_open_signup(open_signup, firebase, plans)
    validate_env_key(env_key)
    if env_key is not None and env_key_looks_weak(env_key):
        logger.warning(
            "AGENTICML_SERVER_KEY looks low-entropy; generate one with agenticml "
            "server keys create or secrets.token_urlsafe(32)")
    public_url = _validate_public_url(public_url)
    data = resolve_data_dir(data)
    prepare_data_dir(data)
    if not store_paths_safe(data / "store"):
        raise ValueError("store or store/runs is a symlink or not a directory; fix or move it away")
    handle = (None if _is_postgres(resolve_url(data, database))
              else acquire_server_lock(data / "server"))
    db = None
    try:
        db = for_server_dir(data / "server", database)
        if db.dialect.name == "postgres":
            handle = ExitStack()
            handle.enter_context(db.server_lock(shared=True))
        _clean_tmp(data / "server" / "tmp", postgres=db.dialect.name == "postgres")
        cls = IPv6IngestServer if ":" in host else IngestServer
        server = cls(data, host, port, public_url, env_key, trusted_proxies, identity_header, db=db,
                     firebase=firebase, cert_source=cert_source, plans=plans,
                     open_signup=open_signup)
        server._server_lock = handle
        for workspace in server.workspaces.list():
            root = prepare_store(data, workspace['id'])
            reconcile(root, RunRepo(db, workspace['id']))
        from .db.backfill import backfill_metrics
        backfill_metrics(server)
        return server
    except BaseException:
        if db is not None:
            db.close()
        if handle is not None:
            handle.close()
        raise


def serve(data=None, host="127.0.0.1", port=8080, public_url=None, on_ready=None,
          trusted_proxies=(), identity_header=None, database=None,
          firebase=None, cert_source=None, plans=None, open_signup=False):
    """Run until interrupted, with an optional environment admin key."""
    extra = {}
    if plans is not None:
        extra["plans"] = plans
    if database is not None:
        extra["database"] = database
    if firebase is not None:
        extra["firebase"] = firebase
    if cert_source is not None:
        extra["cert_source"] = cert_source
    if open_signup:
        extra["open_signup"] = True
    server = make_server(
        data, host, port, public_url, os.environ.get("AGENTICML_SERVER_KEY"),
        trusted_proxies, identity_header, **extra)
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
