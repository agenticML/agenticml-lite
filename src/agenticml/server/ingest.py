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
from agenticml.store.reader import last_step_from_size, parse_artifacts_json
from agenticml.ui.server import DashboardHandler
from . import lineage
from .db import DatabaseError
from .keys import KeyStoreUnavailable
from .runs import APPEND as _APPEND, ATOMIC as _ATOMIC, list_row_from_bytes
from .users import allowed, resolve

logger = logging.getLogger("agenticml.server.ingest")
_CHUNK = 64 * 1024
_STATE_BATCH = 64
_ID = r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
_SHORT_WARNED: set[tuple] = set()
_ATOMIC_MISMATCH_WARNED: set[tuple] = set()
_OVERSIZED_ATOMIC_WARNED: set[tuple] = set()
_TIME_PATH = "scalars/_time.bin"
_ROUTES = [
    ("GET", re.compile(r"/healthz"), "health"),
    ("GET", re.compile(r"/v2/whoami"), "whoami"),
    ("POST", re.compile(r"/v2/objects/missing"), "missing"),
    ("PUT", re.compile(r"/v2/objects/([0-9a-f]{64})"), "object"),
    ("GET", re.compile(r"/v2/runs/" + _ID + r"/state"), "state"),
    ("GET", re.compile(r"/v2/runs/" + _ID + r"/lineage"), "lineage_state"),
    ("PUT", re.compile(r"/v2/runs/" + _ID + r"/lineage"), "lineage"),
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


def compat_owner(principal, owner_user):
    """Compatibility rule for /v2 owner fields (state, remote, and 409 payloads)."""
    real = owner_user if isinstance(owner_user, str) else None
    shown = principal.key if real is not None and real == principal.user and principal.key else real
    return {"owner": shown, "owner_user": real}


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
    def _begin(self, status, content_type, length, headers=None):
        if self.close_connection:
            headers = {name: value for name, value in (headers or {}).items()
                       if name.lower() != "connection"}
            headers["Connection"] = "close"
        super()._begin(status, content_type, length, headers)

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
        self.workspace = None
        tmp = None
        lineage_slot = False
        try:
            if self._dashboard_request():
                if self.server.default_workspace_id is None:
                    self._json({"error": "not found"}, 404)
                    return
                if self.command == "GET":
                    return DashboardHandler.do_GET(self)
                if self.command == "PUT":
                    return DashboardHandler.do_PUT(self)
                return DashboardHandler._method_not_allowed(self)
            route, args, query = self._route()
            if route in ("health", "whoami", "state", "lineage_state") and (
                    self.headers.get_all("Content-Length") or self.headers.get_all("Transfer-Encoding")):
                self.close_connection = True
            if route == "health":
                self._json({"ok": True})
                return
            record, principal = self._authenticate()
            self.principal = principal
            self.server.keys.touch(record)
            if route == "whoami":
                self._json({"key": record.name, "scope": record.scope, "user": principal.user,
                            "role": principal.role, "protocol": 2, "workspace": self.workspace.slug,
                            "features": (["lineage"] if self.server.lineage_enabled else []) + ["byref"]})
                return
            if not allowed(principal, "sync"):
                if self.headers.get_all("Content-Length") or self.headers.get_all("Transfer-Encoding"):
                    self.close_connection = True
                _fail(403, f"role {principal.role} cannot sync")
            if route in ("lineage", "lineage_state") and not self.server.lineage_enabled:
                _fail(404, "not found")
            if route in ("atomic", "append", "state", "commit", "lineage", "lineage_state"):
                run_id = args[0]
                if not layout.valid_run_id(run_id):
                    _fail(404, "not found")
                if route == "atomic" and not _ATOMIC.fullmatch(args[1]):
                    _fail(404, "not found")
                if route == "append" and not _APPEND.fullmatch(args[1]):
                    _fail(404, "not found")
                self._run_dir(run_id)
            if route == "state":
                self._json(self._state(run_id, principal))
                return
            if route == "lineage_state":
                with self.workspace.run_lock(run_id):
                    directory = self._run_dir(run_id)
                    self._ownership(directory, principal, False)
                    meta = _json_object(layout.read_file_nofollow(self._target(directory, "meta.json")))
                    self._json(lineage.state(self.workspace.root, run_id, meta))
                return
            limit = getattr(self.server.limits, route + "_body")
            length = self._body_length(limit)
            if route in ("atomic", "append", "commit", "lineage"):
                # Reject obvious conflicts without receiving a large upload.
                # Ownership is checked authoritatively again under the lock.
                self._ownership(self._run_dir(run_id), principal,
                                route == "atomic" and args[1] == "meta.json")
            if not self.server._admit_body(self.connection, self.server.limits.concurrent_bodies):
                self.close_connection = True
                raise _RequestError(503, {"error": "server busy"}, {"Retry-After": "5"})
            try:
                if route in ("append", "object", "lineage"):
                    tmp = layout.unique_tmp(self.server.tmp_dir, ".body")
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        digest = self._read_body(length, handle)
                    body = None
                else:
                    body = self._read_body(length)
            finally:
                self.server._set_connection_state(self.connection, "busy")
            if route == "lineage":
                lineage_slot = self.server.lineage_receives.acquire(blocking=False)
                if not lineage_slot:
                    raise _RequestError(503, {"error": "server busy"}, {"Retry-After": "5"})
            if route == "missing":
                self._missing(body)
            elif route == "object":
                payload, status = self._object(args[0], tmp, digest)
                self._json(payload, status)
            else:
                pending = None
                with self.workspace.run_lock(run_id):
                    directory = self._run_dir(run_id)
                    remote = self._ownership(directory, principal, route == "atomic" and args[1] == "meta.json")
                    if route == "atomic":
                        payload, status, pending = self._atomic(run_id, args[1], body, record, principal, remote)
                    elif route == "append":
                        payload, status = self._append(run_id, directory, args[1], query, tmp, length, remote)
                    elif route == "lineage":
                        meta = _json_object(layout.read_file_nofollow(self._target(directory, "meta.json")))
                        payload, status = lineage.receive(self.workspace.root, run_id, meta, tmp)
                    else:
                        payload, status, pending = self._commit(run_id, directory, body, remote)
                if pending is not None:
                    self._audit_run(run_id, pending)
                self._json(payload, status)
        except (_RequestError, lineage.LineageError) as exc:
            # A rejected request may leave an announced body unread.
            if self.headers.get_all("Content-Length") or self.headers.get_all("Transfer-Encoding"):
                self.close_connection = True
            self._json(exc.payload, exc.status, getattr(exc, "headers", None))
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            self._error({"error": "request body timeout"}, 408)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except (DatabaseError, KeyStoreUnavailable):
            self.close_connection = True
            self._error({"error": "key store unavailable"}, 503)
        except Exception as exc:
            self.close_connection = True
            logger.error("ingest request failed: %s: %s", type(exc).__name__, exc,
                         exc_info=self.server.verbose)
            self._error({"error": "internal error"}, 500)
        finally:
            if lineage_slot:
                self.server.lineage_receives.release()
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
        allowed = []
        for method, pattern, route in _ROUTES:
            match = pattern.fullmatch(parsed.path)
            if match is not None:
                if self.command == method:
                    return route, match.groups(), parsed.query
                allowed.append(method)
        if allowed:
            self.close_connection = True
            raise _RequestError(405, {"error": "method not allowed"}, {"Allow": ", ".join(allowed)})
        _fail(404, "not found")

    def _authenticate(self):
        values = self.headers.get_all("Authorization") or []
        ticket = None
        if values:
            retry_after, ticket = self.server.web.throttle.begin(
                self.server.web.client_address(self))
            if ticket is None:
                self.close_connection = True
                raise _RequestError(
                    429, {"error": "too many failed authentications; try again later"},
                    {"Retry-After": str(retry_after)},
                )
        outcome = "ignored"
        try:
            match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9._~+/=-]+)", values[0]) if len(values) == 1 else None
            record = self.server.keys.verify(match[1]) if match else None
            if record is None:
                if match is not None:
                    outcome = "failed"
                raise _RequestError(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            from .access import bind_sync, WorkspaceNotFound, WorkspaceSuspended
            try:
                self.workspace = bind_sync(self.server, record)
            except WorkspaceSuspended:
                _fail(403, 'workspace suspended')
            except WorkspaceNotFound:
                raise _RequestError(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            principal = self.workspace.principal
            if principal is None:
                raise _RequestError(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            # Bearer success is ignored: a sync client on the same NAT must not
            # reset an attacker's count every pass. A missing user is not a
            # failed bearer value, so it does not count toward the throttle.
            return record, principal
        finally:
            if ticket is not None:
                self.server.web.throttle.finish(ticket, outcome)

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
        root = self.workspace.root
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
            layout.contained(self.workspace.root, path)
            if (path.exists() or path.is_symlink()) and not _regular(path):
                raise ValueError("unsafe file")
        except (OSError, ValueError):
            _fail(404, "not found")
        return path

    def _remote(self, directory):
        return self.workspace.run_index.as_remote(directory.name)

    def _ownership(self, directory, principal, meta_write):
        run_id = directory.name
        row = self.workspace.run_index.get(run_id)
        exists = _regular(directory / "meta.json")
        owner = row["owner_user"] if row else None
        if not exists and not meta_write:
            _fail(404, "unknown run")
        if isinstance(owner, str) and owner != principal.user:
            _fail(409, "run owned by another user", **compat_owner(principal, owner))
        return self.workspace.run_index.as_remote(run_id) if row else {}

    def _atomic(self, run_id, name, body, record, principal, remote):
        obj = None
        if name.endswith(".ovw"):
            try:
                Overview.from_bytes(body)
            except (ValueError, TypeError):
                _fail(422, "invalid overview")
        elif name == "artifacts.json":
            if parse_artifacts_json(body) is None:
                _fail(422, "invalid artifacts.json")
        else:
            obj = _json_object(body)
            if name == "meta.json" and (obj.get("run_id") != run_id or obj.get("status") not in _STATUSES):
                _fail(422, "invalid meta.json")
        directory = self._run_dir(run_id, create=True)
        path = self._target(directory, name, create=True)
        exists = _regular(directory / "meta.json")
        digest = hashlib.sha256(body).hexdigest()
        if name == "meta.json":
            claim = not exists or not isinstance(remote.get("owner"), str)
            list_row, project, created_at, launch = list_row_from_bytes(body, run_id)
            self.workspace.run_index.claim_meta(
                run_id, owner_user=principal.user, owner_key=record.name,
                list_row=list_row, project=project, created_at=created_at,
                launch_note=launch, claim=claim, recreate=not exists)
            self.server._after_meta_claim(run_id)
            layout.atomic_write(path, body, fsync=True)
            layout.fsync_dir(path.parent)
            self.server._after_atomic_write(run_id, name)
            self.workspace.run_index.record_file(run_id, name, len(body), sha256=digest)
        else:
            layout.atomic_write(path, body, fsync=True)
            layout.fsync_dir(path.parent)
            self.server._after_atomic_write(run_id, name)
            self.workspace.run_index.record_file(run_id, name, len(body), sha256=digest)
        pending = ("run.create", obj) if name == "meta.json" and not exists else None
        return {"size": len(body)}, 200, pending

    def _audit_run(self, run_id, pending):
        try:
            action, obj = pending
            if action == "run.create":
                project = obj.get("project")
                detail = {"project": project if isinstance(project, str) else None,
                          "key": self.principal.key}
            else:
                detail = {"status": obj["status"], "key": self.principal.key}
            self.workspace.audit.record(
                self.principal.user, self.principal.via, action, run_id,
                self.server.web.client_address(self), detail)
        except Exception:
            logger.error("could not record audit event")

    def _state(self, run_id, principal):
        with self.workspace.run_lock(run_id):
            directory = self._run_dir(run_id)
            if not _regular(directory / "meta.json"):
                _fail(404, "unknown run")
            remote = self._remote(directory)
            recorded = self.workspace.run_index.files(run_id)
            names = list(recorded)
            files = {}
            atomic_names = [name for name in names if _ATOMIC.fullmatch(name)]
            append_names = [name for name in names if _APPEND.fullmatch(name)]
            mismatches = []
            for name in atomic_names:
                info = recorded[name]
                path = directory / name
                try:
                    with layout.open_nofollow(path, buffering=0) as handle:
                        st = os.fstat(handle.fileno())
                        if st.st_nlink > 1:
                            logger.warning("skipping hard-linked state file")
                            continue
                        size = st.st_size
                        if size > self.server.limits.atomic_body:
                            self._warn_oversized_atomic(run_id, name, size)
                            continue
                        digest = _hash_prefix(handle, size)
                except (OSError, ValueError):
                    continue
                files[name] = {"size": size, "sha256": digest}
                stored = info["sha256"]
                if stored is None:
                    mismatches.append((name, size, digest))
                elif size != info["size"] or digest != stored:
                    mismatches.append((name, size, digest))
                    self._warn_atomic_mismatch(run_id, name, info["size"], stored, size, digest)
            for name, size, digest in mismatches:
                self.workspace.run_index.record_file(run_id, name, size, sha256=digest)
        for start in range(0, len(append_names), _STATE_BATCH):
            with ExitStack() as handles:
                snapshots = {}
                with self.workspace.run_lock(run_id):
                    directory = self._run_dir(run_id)
                    for name in append_names[start:start + _STATE_BATCH]:
                        committed = recorded[name]["size"]
                        path = directory / name
                        if not _regular(path):
                            self._warn_short(run_id, name, committed, 0)
                            files[name] = {"size": 0, "sha256": hashlib.sha256().hexdigest()}
                            continue
                        path = self._target(directory, name)
                        handle = handles.enter_context(layout.open_nofollow(path, buffering=0))
                        info = os.fstat(handle.fileno())
                        if info.st_nlink > 1:
                            logger.warning("skipping hard-linked state file")
                            handle.close()
                            continue
                        size = info.st_size
                        if size < committed:
                            self._warn_short(run_id, name, committed, size)
                            snapshots[name] = (handle, size)
                        else:
                            snapshots[name] = (handle, committed)
                files.update({name: {"size": size, "sha256": _hash_prefix(handle, size)}
                              for name, (handle, size) in snapshots.items()})
        owner = remote.get("owner")
        fields = compat_owner(principal, owner)
        shown_remote = {**remote, **fields}
        if owner != principal.user:
            shown_remote.pop("owner_key", None)
            shown_remote.pop("last_sync_key", None)
        return {**fields, "files": files, "remote": shown_remote}

    def _warn_short(self, run_id, name, committed, actual):
        key = (self.workspace.id, run_id, name)
        if key in _SHORT_WARNED:
            return
        _SHORT_WARNED.add(key)
        logger.error("run file shorter than committed size: %s %s committed=%s actual=%s",
                     run_id, name, committed, actual)

    def _warn_atomic_mismatch(self, run_id, name, recorded_size, recorded_sha, size, digest):
        key = (self.workspace.id, run_id, name)
        if key in _ATOMIC_MISMATCH_WARNED:
            return
        _ATOMIC_MISMATCH_WARNED.add(key)
        logger.error("atomic file differs from recorded hash: %s %s recorded=%s/%s actual=%s/%s",
                     run_id, name, recorded_size, recorded_sha, size, digest)

    def _warn_oversized_atomic(self, run_id, name, size):
        key = (self.workspace.id, run_id, name)
        if key in _OVERSIZED_ATOMIC_WARNED:
            return
        _OVERSIZED_ATOMIC_WARNED.add(key)
        logger.warning("skipping oversized atomic state file: %s %s size=%s",
                       run_id, name, size)

    def _missing(self, body):
        hashes = _json_object(body).get("objects")
        if (not isinstance(hashes, list) or len(hashes) > self.server.limits.missing_hashes
                or any(not layout.valid_sha(sha) for sha in hashes)):
            _fail(422, "invalid objects")
        self._json({"missing": [sha for sha in dict.fromkeys(hashes) if not objects.has(self.workspace.root, sha)]})

    def _object(self, sha, tmp, actual):
        if sha != actual:
            _fail(422, "hash mismatch", sha256=actual)
        with layout.open_nofollow(tmp, "r+b") as handle:
            os.fchmod(handle.fileno(), self.server.store_file_mode)
            os.fsync(handle.fileno())
        with self.workspace.object_lock(sha):
            dest = layout.object_path(self.workspace.root, sha)
            if (dest.exists() or dest.is_symlink()) and not _regular(dest):
                raise ValueError("unsafe object destination")
            if objects.has(self.workspace.root, sha):
                os.utime(dest, None, follow_symlinks=False)
                return {"sha256": sha, "created": False}, 200
            try:
                dest.parent.mkdir(parents=True)
                created_prefix = True
            except FileExistsError:
                created_prefix = False
            layout.object_path(self.workspace.root, sha)
            if created_prefix:
                layout.fsync_dir(dest.parent.parent)
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

    def _append(self, run_id, directory, name, query, tmp, length, remote):
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
        with self.workspace.run_index.file_guard(run_id, name):
            committed = self.workspace.run_index.committed_size(run_id, name)
            return self._apply_append(
                run_id, directory, name, tmp, length, offset, truncate, committed)

    def _apply_append(self, run_id, directory, name, tmp, length, offset, truncate, committed):
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
            if offset > committed:
                raise _RequestError(409, {"size": committed})
        path = self._target(directory, name, create=True)
        if truncate:
            with layout.open_nofollow(tmp, "r+b") as handle:
                os.fchmod(handle.fileno(), self.server.store_file_mode)
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            layout.fsync_dir(path.parent)
            self.server._after_append_write(run_id, name)
            last_step = self._time_step(path, length) if name == _TIME_PATH else None
            self.workspace.run_index.record_file(run_id, name, length, last_step=last_step)
            return {"size": length}, 200
        with layout.open_nofollow(path, "ab"):
            pass
        with layout.open_nofollow(path, "r+b", buffering=0) as dest, layout.open_nofollow(tmp) as source:
            info = os.fstat(dest.fileno())
            if info.st_nlink > 1:
                logger.warning("refusing hard-linked append target")
                _fail(409, "refusing hard-linked file on server", path=name)
            comparable = min(committed, info.st_size)
            if offset > committed:
                raise _RequestError(409, {"size": committed})
            if offset > comparable:
                raise _RequestError(409, {"size": comparable})
            overlap = min(comparable - offset, length)
            dest.seek(offset)
            remaining = overlap
            while remaining:
                chunk = source.read(min(_CHUNK, remaining))
                if dest.read(len(chunk)) != chunk:
                    raise _RequestError(409, {"size": committed, "mismatch": True})
                remaining -= len(chunk)
            if info.st_size > committed:
                os.ftruncate(dest.fileno(), committed)
                os.fsync(dest.fileno())
                write_from = committed
            else:
                write_from = comparable
            dest.seek(write_from)
            started = False
            written_total = 0
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
                        written_total += written
                        view = view[written:]
                if started:
                    os.fsync(dest.fileno())
                new_size = write_from + written_total
                layout.fsync_dir(path.parent)
            except Exception:
                if started:
                    os.ftruncate(dest.fileno(), write_from)
                    os.fsync(dest.fileno())
                raise
        self.server._after_append_write(run_id, name)
        last_step = self._time_step(path, new_size) if name == _TIME_PATH else None
        self.workspace.run_index.record_file(run_id, name, new_size, last_step=last_step)
        return {"size": new_size}, 200

    @staticmethod
    def _time_step(path, size):
        try:
            return last_step_from_size(path, size)
        except (OSError, ValueError):
            return None

    def _commit(self, run_id, directory, body, remote):
        obj = _json_object(body)
        status = obj.get("status")
        fingerprint = obj.get("fingerprint")
        sizes = obj.get("sizes")
        if (status not in _STATUSES or not layout.valid_sha(fingerprint) or not isinstance(sizes, dict)
                or any(not (_ATOMIC.fullmatch(name) or _APPEND.fullmatch(name))
                       or type(size) is not int or size < 0 for name, size in sizes.items())):
            _fail(422, "invalid commit")
        try:
            meta = _json_object(layout.read_file_nofollow(self._target(directory, "meta.json")))
        except FileNotFoundError:
            _fail(404, "unknown run")
        if meta.get("status") != status:
            _fail(409, "status does not match meta.json", status=meta.get("status"))
        recorded = self.workspace.run_index.files(run_id)
        actual = {}
        for name in sizes:
            committed = recorded[name]["size"] if name in recorded else None
            if committed is None:
                actual[name] = None
                continue
            path = directory / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                actual[name] = None
                continue
            if not stat.S_ISREG(info.st_mode):
                actual[name] = None
                continue
            actual[name] = min(committed, info.st_size)
        if actual != sizes:
            _fail(409, "size mismatch", sizes=actual)
        stamp = layout.format_timestamp()
        self.workspace.run_index.commit_sync(
            run_id, status, fingerprint, stamp, self.principal.key, sizes)
        pending = (("run.finish", obj)
                   if status in ("finished", "crashed") and status != remote.get("status")
                   else None)
        return {"complete": True, "fingerprint": fingerprint}, 200, pending
