"""Authenticated protocol v2 writes, with bounded bodies and serialized apply."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import logging
import os
import re
import socket
import stat
import time
from urllib.parse import urlsplit

from agenticml.store import layout, objects
from agenticml.store.overview import Overview
from agenticml.ui.server import DashboardHandler

logger = logging.getLogger("agenticml.server.ingest")
_CHUNK = 64 * 1024
_STATE_BATCH = 64
_ID = r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
_ATOMIC = re.compile(r"(?:meta\.json|scalars/index\.json|media/index\.json|scalars/(?:0|[1-9][0-9]{0,8})\.ovw)")
_APPEND = re.compile(r"(?:events\.jsonl|scalars/_time\.bin|scalars/(?:0|[1-9][0-9]{0,8})\.bin)")
_ROUTES = [
    ("GET", re.compile(r"/healthz"), "health"),
    ("GET", re.compile(r"/v2/whoami"), "whoami"),
    ("POST", re.compile(r"/v2/objects/missing"), "missing"),
    ("PUT", re.compile(r"/v2/objects/([0-9a-f]{64})"), "object"),
    ("GET", re.compile(r"/v2/runs/" + _ID + r"/state"), "state"),
    ("PUT", re.compile(r"/v2/runs/" + _ID + r"/files/(.+)"), "atomic"),
    ("POST", re.compile(r"/v2/runs/" + _ID + r"/append/(.+)"), "append"),
    ("POST", re.compile(r"/v2/runs/" + _ID + r"/commit"), "commit"),
]
_STATUSES = ("running", "finished", "crashed")


class _RequestError(Exception):
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers


def _fail(http_status, error, **extra):
    raise _RequestError(http_status, {"error": error, **extra})


def _regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _json_object(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        _fail(422, "invalid JSON object")
    if not isinstance(value, dict):
        _fail(422, "invalid JSON object")
    return value


def _hash_prefix(handle, size):
    """Hash only the bytes captured by the snapshot's fstat, excluding appends."""
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = handle.read(min(_CHUNK, remaining))
        if not chunk:
            raise OSError("snapshot file shortened while hashing")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


class IngestHandler(DashboardHandler):
    def _json(self, value, status=200, headers=None):
        if self.close_connection:
            headers = {**(headers or {}), "Connection": "close"}
        super()._json(value, status, headers)

    def log_message(self, fmt, *args):
        # The base access log includes the raw request target, which may contain
        # credentials accidentally pasted by a client. Log only the method.
        if self.server.verbose:
            logger.info("HTTP %s", self.command)

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler uses this for malformed HTTP before routing.
        self.close_connection = True
        self._started = False
        self.headers = self.MessageClass()
        self._error({"error": "invalid request"}, code)

    def _dashboard_request(self):
        """True when the raw target is not /healthz or /v2, before ingest path policy."""
        try:
            target = self.raw_requestline.split()[1].decode("iso-8859-1")
        except (IndexError, UnicodeDecodeError, AttributeError):
            return False
        raw_path = target.partition("?")[0]
        return raw_path != "/healthz" and raw_path != "/v2" and not raw_path.startswith("/v2/")

    def _dispatch(self):
        self._started = False
        tmp = None
        try:
            if self._dashboard_request():
                if self.command == "GET":
                    return DashboardHandler.do_GET(self)
                if self.command == "PUT":
                    return DashboardHandler.do_PUT(self)
                return DashboardHandler._method_not_allowed(self)
            route, args, query = self._route()
            if route in ("health", "whoami", "state") and (
                    self.headers.get_all("Content-Length") or self.headers.get_all("Transfer-Encoding")):
                self.close_connection = True
            if route == "health":
                self._json({"ok": True})
                return
            record = self._authenticate()
            self.server.keys.touch(record)
            if route == "whoami":
                self._json({"key": record.name, "scope": record.scope, "protocol": 2, "features": []})
                return
            if route in ("atomic", "append", "state", "commit"):
                run_id = args[0]
                if not layout.valid_run_id(run_id):
                    _fail(404, "not found")
                if route == "atomic" and not _ATOMIC.fullmatch(args[1]):
                    _fail(404, "not found")
                if route == "append" and not _APPEND.fullmatch(args[1]):
                    _fail(404, "not found")
                self._run_dir(run_id)
            if route == "state":
                self._json(self._state(run_id))
                return
            limit = getattr(self.server.limits, route + "_body")
            length = self._body_length(limit)
            if route in ("atomic", "append", "commit"):
                # Reject obvious conflicts without receiving a large upload.
                # Ownership is checked authoritatively again under the lock.
                self._ownership(self._run_dir(run_id), record,
                                route == "atomic" and args[1] == "meta.json")
            if not self.server._admit_body(self.connection, self.server.limits.concurrent_bodies):
                self.close_connection = True
                raise _RequestError(503, {"error": "server busy"}, {"Retry-After": "5"})
            try:
                if route in ("append", "object"):
                    tmp = layout.unique_tmp(self.server.tmp_dir, ".body")
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        digest = self._read_body(length, handle)
                    body = None
                else:
                    body = self._read_body(length)
            finally:
                self.server._set_connection_state(self.connection, "busy")
            if route == "missing":
                self._missing(body)
            elif route == "object":
                payload, status = self._object(args[0], tmp, digest)
                self._json(payload, status)
            else:
                with self.server.run_lock(run_id):
                    directory = self._run_dir(run_id)
                    remote = self._ownership(directory, record, route == "atomic" and args[1] == "meta.json")
                    if route == "atomic":
                        payload, status = self._atomic(run_id, args[1], body, record, remote)
                    elif route == "append":
                        payload, status = self._append(directory, args[1], query, tmp, length, remote)
                    else:
                        payload, status = self._commit(directory, body, remote)
                self._json(payload, status)
        except _RequestError as exc:
            # A rejected request may leave an announced body unread.
            if self.headers.get_all("Content-Length") or self.headers.get_all("Transfer-Encoding"):
                self.close_connection = True
            self._json(exc.payload, exc.status, exc.headers)
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            self._error({"error": "request body timeout"}, 408)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            self.close_connection = True
            logger.error("ingest request failed: %s: %s", type(exc).__name__, exc,
                         exc_info=self.server.verbose)
            self._error({"error": "internal error"}, 500)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    do_GET = do_PUT = do_POST = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _dispatch

    def _method_not_allowed(self):
        """Dashboard PUTs must not recurse; ingest unknown methods still 405."""
        if not self._dashboard_request():
            return self._dispatch()
        return DashboardHandler._method_not_allowed(self)

    def _route(self):
        # parse_request normalizes a leading //; inspect the original target so
        # that normalization cannot bypass the raw-path policy.
        target = self.raw_requestline.split()[1].decode("iso-8859-1")
        raw_path = target.partition("?")[0]
        if (not raw_path.startswith("/") or "//" in raw_path
                or re.fullmatch(r"[A-Za-z0-9._/-]+", raw_path) is None
                or any(part in (".", "..") for part in raw_path.split("/"))):
            _fail(404, "not found")
        parsed = urlsplit(target)
        for method, pattern, route in _ROUTES:
            match = pattern.fullmatch(parsed.path)
            if match is not None:
                if self.command != method:
                    self.close_connection = True
                    raise _RequestError(405, {"error": "method not allowed"}, {"Allow": method})
                return route, match.groups(), parsed.query
        _fail(404, "not found")

    def _authenticate(self):
        values = self.headers.get_all("Authorization") or []
        match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9._~+/=-]+)", values[0]) if len(values) == 1 else None
        record = self.server.keys.verify(match[1]) if match else None
        if record is None:
            raise _RequestError(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
        return record

    def _body_length(self, limit):
        values = self.headers.get_all("Content-Length") or []
        if (self.headers.get_all("Transfer-Encoding") or len(values) != 1
                or re.fullmatch(r"[0-9]+", values[0]) is None):
            self.close_connection = True
            _fail(411, "length required")
        # Compare decimal strings first, including arbitrarily long declarations.
        value = values[0].lstrip("0") or "0"
        if len(value) > len(str(limit)) or (len(value) == len(str(limit)) and value > str(limit)):
            self.close_connection = True
            _fail(413, "payload too large")
        return int(value)

    def _read_body(self, length, handle=None):
        limits = self.server.limits
        deadline = time.monotonic() + limits.body_deadline + length / limits.body_bytes_per_second
        previous = self.connection.gettimeout()
        digest = hashlib.sha256() if handle is not None else None
        chunks = []
        remaining = length
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("body deadline exceeded")
                self.connection.settimeout(left if previous is None else min(left, previous))
                chunk = self.rfile.read1(min(_CHUNK, remaining))
                if not chunk:
                    raise TimeoutError("short body")
                remaining -= len(chunk)
                if handle is None:
                    chunks.append(chunk)
                else:
                    handle.write(chunk)
                    digest.update(chunk)
            if time.monotonic() > deadline:
                raise TimeoutError("body deadline exceeded")
        finally:
            self.connection.settimeout(previous)
        return b"".join(chunks) if handle is None else digest.hexdigest()

    def _run_dir(self, run_id, create=False):
        root = self.server.root
        try:
            directory = layout.run_dir(root, run_id)
            for path in (root / "runs", directory, directory / "scalars", directory / "media"):
                if path.exists() or path.is_symlink():
                    if not stat.S_ISDIR(path.lstat().st_mode):
                        raise ValueError("unsafe run directory")
                layout.contained(root, path)
            if create:
                directory.mkdir(parents=True, exist_ok=True)
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise ValueError("unsafe run directory")
            return directory
        except (ValueError, OSError):
            logger.warning("refusing unsafe run directory")
            _fail(404, "not found")

    def _target(self, directory, name, create=False):
        path = directory / name
        try:
            if create and path.parent != directory:
                path.parent.mkdir(exist_ok=True)
                if not stat.S_ISDIR(path.parent.lstat().st_mode):
                    raise ValueError("unsafe parent")
            layout.contained(self.server.root, path)
            if (path.exists() or path.is_symlink()) and not _regular(path):
                raise ValueError("unsafe file")
        except (OSError, ValueError):
            _fail(404, "not found")
        return path

    def _remote(self, directory):
        path = directory / "remote.json"
        if not _regular(path):
            return {}
        try:
            layout.contained(self.server.root, path)
            obj = json.loads(layout.read_file_nofollow(path).decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (OSError, ValueError, RecursionError):
            return {}

    def _ownership(self, directory, record, meta_write):
        exists = _regular(directory / "meta.json")
        remote = self._remote(directory)
        owner = remote.get("owner")
        if not exists and not meta_write:
            _fail(404, "unknown run")
        if exists and isinstance(owner, str) and owner != record.name:
            _fail(409, "run owned by another key", owner=owner)
        return remote

    def _atomic(self, run_id, name, body, record, remote):
        if name.endswith(".ovw"):
            try:
                Overview.from_bytes(body)
            except (ValueError, TypeError):
                _fail(422, "invalid overview")
        else:
            obj = _json_object(body)
            if name == "meta.json" and (obj.get("run_id") != run_id or obj.get("status") not in _STATUSES):
                _fail(422, "invalid meta.json")
        directory = self._run_dir(run_id, create=True)
        path = self._target(directory, name, create=True)
        if name == "meta.json":
            exists = _regular(directory / "meta.json")
            if not exists or not isinstance(remote.get("owner"), str):
                claimed = {**remote, "owner": record.name} if exists else {"owner": record.name}
                self._write_json(directory, "remote.json", claimed)
        layout.atomic_write(path, body, fsync=True)
        layout.fsync_dir(path.parent)
        return {"size": len(body)}, 200

    def _write_json(self, directory, name, value):
        path = self._target(directory, name)
        layout.atomic_write(path, json.dumps(value).encode("utf-8"), fsync=True)
        layout.fsync_dir(directory)

    def _state(self, run_id):
        # Bound open inodes per batch; files may describe different instants.
        # Appends preserve prefixes; atomic writes/truncates replace inodes.
        with self.server.run_lock(run_id):
            directory = self._run_dir(run_id)
            if not _regular(directory / "meta.json"):
                _fail(404, "unknown run")
            names = ["meta.json", "events.jsonl", "scalars/index.json", "media/index.json", "scalars/_time.bin"]
            scalars = directory / "scalars"
            if scalars.is_dir():
                names.extend("scalars/" + entry.name for entry in scalars.iterdir()
                             if _ATOMIC.fullmatch("scalars/" + entry.name) or _APPEND.fullmatch("scalars/" + entry.name))
            names = list(dict.fromkeys(names))
        files = {}
        for start in range(0, len(names), _STATE_BATCH):
            with ExitStack() as handles:
                snapshots = {}
                with self.server.run_lock(run_id):
                    directory = self._run_dir(run_id)
                    if start == 0:
                        remote = self._remote(directory)
                    for name in names[start:start + _STATE_BATCH]:
                        path = directory / name
                        if not _regular(path):
                            continue
                        path = self._target(directory, name)
                        handle = handles.enter_context(layout.open_nofollow(path, buffering=0))
                        info = os.fstat(handle.fileno())
                        if info.st_nlink > 1:
                            logger.warning("skipping hard-linked state file")
                            handle.close()
                            continue
                        snapshots[name] = (handle, info.st_size)
                files.update({name: {"size": size, "sha256": _hash_prefix(handle, size)}
                              for name, (handle, size) in snapshots.items()})
        owner = remote.get("owner")
        return {"owner": owner if isinstance(owner, str) else None, "files": files, "remote": remote}

    def _missing(self, body):
        hashes = _json_object(body).get("objects")
        if (not isinstance(hashes, list) or len(hashes) > self.server.limits.missing_hashes
                or any(not layout.valid_sha(sha) for sha in hashes)):
            _fail(422, "invalid objects")
        self._json({"missing": [sha for sha in dict.fromkeys(hashes) if not objects.has(self.server.root, sha)]})

    def _object(self, sha, tmp, actual):
        if sha != actual:
            _fail(422, "hash mismatch", sha256=actual)
        with self.server.objects_lock:
            dest = layout.object_path(self.server.root, sha)
            if (dest.exists() or dest.is_symlink()) and not _regular(dest):
                raise ValueError("unsafe object destination")
            if objects.has(self.server.root, sha):
                os.utime(dest, None, follow_symlinks=False)
                return {"sha256": sha, "created": False}, 200
            with layout.open_nofollow(tmp, "r+b") as handle:
                os.fchmod(handle.fileno(), self.server.store_file_mode)
                os.fsync(handle.fileno())
            dest.parent.mkdir(parents=True, exist_ok=True)
            layout.object_path(self.server.root, sha)
            os.replace(tmp, dest)
            layout.fsync_dir(dest.parent)
            return {"sha256": sha, "created": True}, 201

    @staticmethod
    def _append_query(query):
        params = {}
        for item in query.split("&"):
            key, sep, value = item.partition("=")
            if not sep or key not in ("offset", "truncate") or key in params:
                _fail(422, "invalid append query")
            params[key] = value
        offset = params.get("offset", "")
        if re.fullmatch(r"[0-9]+", offset) is None or params.get("truncate", "1") != "1":
            _fail(422, "invalid append query")
        digits = offset.lstrip("0") or "0"
        # Preserve alignment even for an arbitrarily large decimal offset;
        # it is necessarily beyond every representable file size.
        offset = int(digits) if len(digits) <= 19 else (1 << 64) + int(digits[-3:])
        truncate = "truncate" in params
        if truncate and offset != 0:
            _fail(422, "truncate requires offset=0")
        return offset, truncate

    def _append(self, directory, name, query, tmp, length, remote):
        offset, truncate = self._append_query(query)
        if name.endswith(".bin"):
            if offset % layout.RECORD_SIZE or length % layout.RECORD_SIZE:
                _fail(422, "torn record")
        elif length:
            with layout.open_nofollow(tmp) as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    _fail(422, "torn record")
        if truncate and remote.get("status", "running") != "running":
            _fail(409, "run is not running", status=remote.get("status"))
        path = self._target(directory, name)
        try:
            dest = layout.open_nofollow(path, "rb" if truncate else "r+b", buffering=0)
        except FileNotFoundError:
            dest = None
        if dest is not None:
            with dest:
                if os.fstat(dest.fileno()).st_nlink > 1:
                    logger.warning("refusing hard-linked append target")
                    _fail(409, "refusing hard-linked file on server", path=name)
        elif not truncate:
            if offset > 0:
                raise _RequestError(409, {"size": 0})
        path = self._target(directory, name, create=True)
        if truncate:
            with layout.open_nofollow(tmp, "r+b") as handle:
                os.fchmod(handle.fileno(), self.server.store_file_mode)
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            layout.fsync_dir(path.parent)
            return {"size": length}, 200
        # open_nofollow's append mode creates without truncation; r+b then
        # permits overlap reads and writing at the checked end of the file.
        with layout.open_nofollow(path, "ab"):
            pass
        with layout.open_nofollow(path, "r+b", buffering=0) as dest, layout.open_nofollow(tmp) as source:
            info = os.fstat(dest.fileno())
            if info.st_nlink > 1:
                logger.warning("refusing hard-linked append target")
                _fail(409, "refusing hard-linked file on server", path=name)
            size = info.st_size
            if offset > size:
                raise _RequestError(409, {"size": size})
            overlap = min(size - offset, length)
            dest.seek(offset)
            remaining = overlap
            while remaining:
                chunk = source.read(min(_CHUNK, remaining))
                if dest.read(len(chunk)) != chunk:
                    raise _RequestError(409, {"size": size, "mismatch": True})
                remaining -= len(chunk)
            dest.seek(size)
            started = False
            try:
                while True:
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    started = True
                    view = memoryview(chunk)
                    while view:
                        written = dest.write(view)
                        if not written:
                            raise OSError("short append write")
                        view = view[written:]
                if started:
                    os.fsync(dest.fileno())
                new_size = os.fstat(dest.fileno()).st_size
                layout.fsync_dir(path.parent)
            except Exception:
                if started:
                    os.ftruncate(dest.fileno(), size)
                    os.fsync(dest.fileno())
                raise
        return {"size": new_size}, 200

    def _commit(self, directory, body, remote):
        obj = _json_object(body)
        status = obj.get("status")
        fingerprint = obj.get("fingerprint")
        sizes = obj.get("sizes")
        if (status not in _STATUSES or not layout.valid_sha(fingerprint) or not isinstance(sizes, dict)
                or any(not (_ATOMIC.fullmatch(name) or _APPEND.fullmatch(name))
                       or type(size) is not int or size < 0 for name, size in sizes.items())):
            _fail(422, "invalid commit")
        meta = _json_object(layout.read_file_nofollow(self._target(directory, "meta.json")))
        if meta.get("status") != status:
            _fail(409, "status does not match meta.json", status=meta.get("status"))
        actual = {}
        for name in sizes:
            path = self._target(directory, name)
            actual[name] = path.lstat().st_size if _regular(path) else None
        if actual != sizes:
            _fail(409, "size mismatch", sizes=actual)
        owner = remote.get("owner")
        self._write_json(directory, "remote.json", {
            "owner": owner if isinstance(owner, str) else None,
            "status": status, "fingerprint": fingerprint,
            "last_sync_at": layout.format_timestamp(), "sizes": sizes,
        })
        return {"complete": True, "fingerprint": fingerprint}, 200
