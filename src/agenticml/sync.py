"""Incremental, byte-for-byte replication of the v1 store to protocol 2."""
from __future__ import annotations

import errno
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass, field

from requests import HTTPError

from .auth import resolve
from .client import Conflict, ProtocolError
from .store.gc import prunable_series, prune_run, run_fingerprint
from .store.layout import (
    RECORD_SIZE, _LOCK_UNSUPPORTED, open_nofollow, open_object,
    read_file_nofollow, resolve_root, try_lock_run, unlock_run,
)
from .store.reader import RunReader, _effective_status, list_runs, references_from_bytes

APPEND_CHUNK = 8 * 1024 * 1024
_APPEND_LIMIT = 64 * 1024 * 1024
_ATOMIC = re.compile(r"(?:meta\.json|scalars/index\.json|media/index\.json|scalars/(?:0|[1-9][0-9]{0,8})\.ovw)")
_APPEND = re.compile(r"(?:events\.jsonl|scalars/_time\.bin|scalars/(?:0|[1-9][0-9]{0,8})\.bin)")
_TRANSIENT_STATUS = frozenset({408, 429})
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\)|[@-Z\\-_])")
_lock_warning_emitted = False


class PermanentSyncError(ValueError):
    """A deterministic failure that another watch pass cannot repair."""


class OwnershipError(PermanentSyncError):
    """The server run belongs to a different key."""


class RunMissingError(HTTPError):
    """The server lost the run again after this pass already recovered it."""


def _clean_detail(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7f)
    return text[:200]


def _source_hash(reader):
    """Store fingerprint gc trusts before it treats ``complete`` as synced.

    ``run_fingerprint`` ignores sync state. A missing raw series after prune
    is part of the identity, so the stored hash must be recomputed then.
    """
    return run_fingerprint(reader.root, reader.run_id)


def _load_state(reader):
    try:
        data = json.loads((reader.run_dir / "sync.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "hosts": {}}
    if data.get("version") != 1 or not isinstance(data.get("hosts"), dict):
        raise ValueError("unsupported sync state")
    return data


def _publish_sync_state(reader, data):
    """Atomically replace ``sync.json`` and fsync the run directory."""
    path = reader.run_dir / "sync.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(reader.run_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _save_state(reader, host, state):
    # Re-read so a sync to another host keeps its cursor. The per-run lock
    # serializes writers; the replace is still atomic for readers.
    data = _load_state(reader)
    data["hosts"][host] = state
    if state.get("pruned"):
        data["pruned"] = True
        hosts = data.get("pruned_hosts")
        if not isinstance(hosts, list):
            hosts = []
        if host not in hosts:
            hosts.append(host)
        data["pruned_hosts"] = hosts
    _publish_sync_state(reader, data)


def _clear_host_state(reader, host):
    # ``pruned`` / ``pruned_hosts`` stay. Reset must not make a pruned run
    # look like it still has its raw series.
    path = reader.run_dir / "sync.json"
    if not path.exists():
        return
    data = _load_state(reader)
    if host not in data.get("hosts", {}):
        return
    data["hosts"].pop(host, None)
    _publish_sync_state(reader, data)


def _acquire_lock(run_dir):
    global _lock_warning_emitted
    if os.name == "nt":
        # Windows has no fcntl.flock; skip locking. Concurrent sync of one run
        # is not guarded there.
        return None
    import fcntl
    fd = os.open(run_dir / "sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in _LOCK_UNSUPPORTED:
            if not _lock_warning_emitted:
                _lock_warning_emitted = True
                logging.getLogger(__name__).warning("sync locking is unsupported on this filesystem; continuing unlocked")
            return None
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return False
        raise
    return fd


def _release_lock(lock):
    if not isinstance(lock, int):
        return
    import fcntl
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)


def _skipped_ids(state):
    return {entry["id"] for entry in state.get("skipped") or []}


def _remember_skip(state, kind, ident, reason):
    """Record a permanent skip so a later sync does not retry it."""
    skips = state.get("skipped")
    if not isinstance(skips, list):
        skips = []
        state["skipped"] = skips
    for entry in skips:
        if isinstance(entry, dict) and entry.get("id") == ident:
            return
    skips.append({"kind": kind, "id": ident, "reason": reason})


def _permanent_object_reason(exc):
    """Symlink and hard-link plants are permanent. Missing bytes are not.

    Match the phrases ``open_object`` puts in the message. A path that merely
    contains the letters "symlink" must not count.
    """
    text = str(exc).lower()
    if "hard-linked" in text:
        return "hardlink"
    if "symlinked" in text or "refusing symlink:" in text:
        return "symlink"
    return None


def _raw_scalars_removed(reader):
    """True when a series overview is present and its raw ``.bin`` is not."""
    scalars = reader.run_dir / "scalars"
    index_path = scalars / "index.json"
    if not index_path.is_file():
        return False
    try:
        data = json.loads(read_file_nofollow(index_path).decode("utf-8"))
    except (OSError, ValueError):
        return False
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, dict):
        return False
    for info in series.values():
        if not isinstance(info, dict):
            continue
        series_id = info.get("id")
        if isinstance(series_id, bool) or not isinstance(series_id, int) or series_id < 0:
            continue
        ovw_path = scalars / f"{series_id}.ovw"
        bin_path = scalars / f"{series_id}.bin"
        if ovw_path.is_file() and not bin_path.is_file():
            return True
    return False


def _prune_markers(reader):
    """Hosts that already pruned this run, and whether any prune flag is set."""
    data = _load_state(reader)
    hosts = []
    recorded = data.get("pruned_hosts")
    if isinstance(recorded, list):
        for name in recorded:
            if isinstance(name, str) and name not in hosts:
                hosts.append(name)
    for name, host_state in (data.get("hosts") or {}).items():
        if isinstance(host_state, dict) and host_state.get("pruned") and name not in hosts:
            hosts.append(name)
    flagged = bool(data.get("pruned")) or bool(hosts)
    return hosts, flagged


def _prune_synced_run(reader, state, save, result):
    """Delete raw series that have a covering overview.

    The run lock is held while ``complete`` and ``source_hash`` are checked
    again. ``prune_run`` rechecks the saved fingerprint after reacquiring
    that lock, so a run changed in the gap is skipped instead of deleted.
    """
    handle = try_lock_run(reader.run_dir)
    if handle is None:
        result.warnings.append("skipping prune: run is open")
        return
    try:
        if not (state.get("complete") and state.get("source_hash") == _source_hash(reader)):
            return
        keys = list(prunable_series(reader.root, reader.run_id))
    finally:
        unlock_run(handle)
    if not keys:
        return
    try:
        prune_run(reader.root, reader.run_id, keys, expected_fingerprint=state["source_hash"])
    except ValueError as exc:
        result.warnings.append(f"skipping prune: {exc}")
        return
    state["pruned"] = True
    state["source_hash"] = _source_hash(reader)
    save()


def _label_part(value, *, missing=""):
    """One side of a ``project/name`` sync label. ``None`` is not the word None."""
    if value is None or value == "":
        return missing
    return str(value)


def _run_label(meta):
    data = meta if isinstance(meta, dict) else {}
    project = _label_part(data.get("project"))
    name = _label_part(data.get("name"), missing="-")
    return f"{project}/{name}"


def sync_run(reader, host, client, result, *, dry_run=False, prune=False):
    meta = reader.meta()
    result.label = _run_label(meta)
    lock = None
    if not dry_run:
        lock = _acquire_lock(reader.run_dir)
        if lock is False:
            result.status = "skipped"
            result.warnings.append("another sync is running for this run")
            return
    try:
        _sync_locked(reader, host, client, result, dry_run=dry_run, prune=prune)
    finally:
        _release_lock(lock)


def _host_state(reader, host):
    saved = _load_state(reader)["hosts"].get(host, {})
    if not isinstance(saved, dict) or saved.get("protocol") != 2:
        saved = {}
    return {"protocol": 2, "files": {}, "complete": False,
            "skipped": [], "pruned": False, **saved}


@dataclass
class SyncResult:
    run_id: str
    label: str = "?/?"
    status: str = "partial"
    files: int = 0
    bytes: int = 0
    objects: int = 0
    skipped: int = 0
    rewritten: int = 0
    warnings: list[str] = field(default_factory=list)
    complete: bool = False
    failed: bool = False
    permanent: bool = False
    stale: bool = False

    def line(self):
        counts = (f"files={self.files} objects={self.objects} bytes={self.bytes} "
                  f"skipped={self.skipped} rewritten={self.rewritten}")
        warnings = "; " + "; ".join(self.warnings) if self.warnings else ""
        return f"{self.run_id}  {self.label}  {self.status}  {counts}{warnings}"


def _effective(reader, meta):
    handle = try_lock_run(reader.run_dir)
    try:
        return "running" if handle is None else _effective_status(
            meta, str(reader.run_dir / "meta.json"), str(reader.run_dir), time.time(), lock=handle)
    finally:
        unlock_run(handle)


def _sync_idle(reader, state, source_hash, key=None):
    return (state.get("protocol") == 2 and (key is None or state.get("owner") == key)
            and state.get("complete")
            and state.get("source_hash") == source_hash
            and _effective(reader, reader.meta()) in ("finished", "crashed"))


def _pruned_upload_block(reader, host, state):
    hosts, flagged = _prune_markers(reader)
    if host in hosts or state.get("pruned"):
        return None
    if not flagged and not _raw_scalars_removed(reader):
        return None
    previous = hosts[0] if hosts else "a previous host"
    return (f"run was pruned after syncing to {previous}; "
            "its full scalar history is no longer local")


def _open_local(path):
    # Check before open too, so special files cannot block the snapshot.
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise OSError(errno.ELOOP, f"refusing symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, f"not a regular file: {path}")
    handle = open_nofollow(path)
    if os.fstat(handle.fileno()).st_nlink > 1:
        handle.close()
        raise OSError(errno.EINVAL, f"refusing hard-linked file: {path}")
    return handle


def _hash_window(path, start, end):
    digest = hashlib.sha256()
    with _open_local(path) as handle:
        handle.seek(start)
        remaining = end - start
        while remaining:
            data = handle.read(min(remaining, 65536))
            if not data:
                raise ValueError(f"local file changed while syncing {path.name}")
            digest.update(data)
            remaining -= len(data)
    return digest.hexdigest()


def _tail(path, size):
    return _hash_window(path, max(0, size - 65536), size)


def _event_size(handle, size):
    # Scan backwards without loading a possibly very long torn final line.
    end = size
    while end:
        start = max(0, end - 65536)
        handle.seek(start)
        data = handle.read(end - start)
        newline = data.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def _snapshot(reader, effective, result):
    files = {}
    candidates = list(reader.run_dir.iterdir())
    for directory in ("scalars", "media"):
        path = reader.run_dir / directory
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            result.warnings.append(f"{directory}: refusing symlink")
        elif stat.S_ISDIR(info.st_mode):
            candidates.extend(path.iterdir())
    for path in candidates:
        name = path.relative_to(reader.run_dir).as_posix()
        if not (_ATOMIC.fullmatch(name) or _APPEND.fullmatch(name)):
            continue
        try:
            with _open_local(path) as handle:
                size = os.fstat(handle.fileno()).st_size
                if _ATOMIC.fullmatch(name):
                    data = read_file_nofollow(path)
                    current = path.lstat()
                    opened = os.fstat(handle.fileno())
                    if (current.st_ino != opened.st_ino or current.st_dev != opened.st_dev
                            or current.st_nlink > 1 or stat.S_ISLNK(current.st_mode)):
                        raise OSError(errno.EINVAL, f"file changed while taking snapshot: {path}")
                    if name == "meta.json":
                        meta = json.loads(data)
                        if meta.get("status") == "running" and effective == "crashed":
                            meta["status"] = "crashed"
                            data = json.dumps(meta).encode()
                    files[name] = {"data": data, "size": len(data),
                                   "sha256": hashlib.sha256(data).hexdigest()}
                else:
                    size = _event_size(handle, size) if name == "events.jsonl" else size - size % RECORD_SIZE
                    files[name] = {"size": size}
                    if name == "events.jsonl":
                        handle.seek(0)
                        data = handle.read(size)
                        if len(data) != size:
                            raise ValueError("local events changed while taking snapshot")
                        files[name]["data"] = data
        except OSError as exc:
            result.warnings.append(f"{name}: {exc}")
    media = files.get("media/index.json", {}).get("data", b"{}")
    events = files.get("events.jsonl", {}).get("data", b"")
    refs = references_from_bytes(media, events, reader.run_id)
    return files, sorted(set().union(*refs.values()))


def _identity(client):
    if not hasattr(client, "_sync_identity"):
        client._sync_identity = client.whoami()
    return client._sync_identity


def _check_owner(reader, client, payload):
    owner = payload.get("owner")
    if owner and owner != _identity(client)["key"]:
        raise OwnershipError(f"run id {reader.run_id} belongs to key {owner} on this server")


def _recover(reader, client, files, state, names=None):
    remote = client.run_state(reader.run_id)
    if remote is not None:
        _check_owner(reader, client, remote)
    remote_files = (remote or {}).get("files", {})
    rewrites = set()
    for name in files if names is None else names:
        if name not in files:
            continue  # Missing pruned .bin cursors are deliberately retained.
        cursor = remote_files.get(name)
        state["files"].pop(name, None)
        if cursor is None:
            continue
        size = cursor["size"]
        if _ATOMIC.fullmatch(name):
            state["files"][name] = {"size": size, "sha256": cursor["sha256"]}
        elif (size <= files[name]["size"] and _hash_window(reader.run_dir / name, 0, size) == cursor["sha256"]):
            state["files"][name] = {"size": size, "tail": _tail(reader.run_dir / name, size)}
        else:
            rewrites.add(name)
    return rewrites


def _append_chunk(handle, name, offset, size):
    handle.seek(offset)
    limit = min(APPEND_CHUNK, size - offset)
    if name.endswith(".bin"):
        limit -= limit % RECORD_SIZE
        if not limit and size > offset:
            limit = min(RECORD_SIZE, size - offset)
    data = handle.read(limit)
    if name == "events.jsonl" and data:
        newline = data.rfind(b"\n")
        if newline >= 0:
            return data[:newline + 1]
        # A long single event can cross the usual chunk boundary.
        while len(data) < min(_APPEND_LIMIT, size - offset):
            extra = handle.readline(min(65536, _APPEND_LIMIT - len(data), size - offset - len(data)))
            if not extra:
                break
            data += extra
            if data.endswith(b"\n"):
                return data
        raise PermanentSyncError("event line larger than 64 MiB")
    if len(data) != limit:
        raise ValueError(f"local file changed while syncing {name}")
    return data


def _fingerprint_link_error(exc):
    # layout.contained raises plain ValueError for an escaping symlink; there
    # is no dedicated exception type to catch without changing layout's API.
    return ((isinstance(exc, OSError) and exc.errno == errno.ELOOP)
            or (isinstance(exc, ValueError) and str(exc).startswith("path escapes store root:")))


def _sync_locked(reader, host, client, result, *, dry_run=False, prune=False):
    fingerprint_available = True
    try:
        source_hash = _source_hash(reader)
    except (OSError, ValueError) as exc:
        if not _fingerprint_link_error(exc):
            raise
        fingerprint_available = False
    effective = _effective(reader, reader.meta())
    result.stale = effective == "stale"
    files, hashes = _snapshot(reader, effective, result)
    if not fingerprint_available:
        # gc's fingerprint refuses symlinks. Still copy safe files, but never
        # advertise this incomplete snapshot as synced to gc or prune it.
        source_hash = hashlib.sha256(json.dumps({name: {k: v for k, v in item.items() if k != "data"}
                                                for name, item in files.items()}, sort_keys=True).encode()).hexdigest()
    state = _host_state(reader, host)

    def save():
        if not dry_run:
            _save_state(reader, host, state)

    block = _pruned_upload_block(reader, host, state)
    if block:
        result.failed = result.permanent = True
        result.status = f"failed: {block}"
        return
    if not dry_run and state.get("owner"):
        _check_owner(reader, client, state)
    if (state.get("complete") and state.get("source_hash") == source_hash
            and effective in ("finished", "crashed")):
        result.status = "up to date"
        result.complete = True
        if prune and not dry_run and not state.get("pruned"):
            _prune_synced_run(reader, state, save, result)
        return
    if dry_run:
        for name, snapshot in files.items():
            cursor = state["files"].get(name)
            if _ATOMIC.fullmatch(name):
                amount = snapshot["size"] if not cursor or cursor.get("sha256") != snapshot["sha256"] else 0
                changed = not cursor or cursor.get("sha256") != snapshot["sha256"]
            else:
                offset = cursor["size"] if cursor else 0
                rollback = cursor and (snapshot["size"] < offset or _tail(reader.run_dir / name, offset) != cursor.get("tail"))
                amount = snapshot["size"] if rollback else snapshot["size"] - offset
                changed = not cursor or bool(amount) or rollback
            if changed:
                result.files += 1
                result.bytes += amount
        result.objects = len(set(hashes) - _skipped_ids(state))
        result.status = "dry-run (would send; objects unchecked)"
        return

    recovered = not state["files"]
    rewrites = _recover(reader, client, files, state) if recovered else set()
    state["owner"] = _identity(client)["key"]
    state["complete"] = False
    save()
    pending = False
    for sha in client.missing([sha for sha in hashes if sha not in _skipped_ids(state)]):
        try:
            handle = open_object(reader.root, sha)
        except OSError as exc:
            reason = _permanent_object_reason(exc)
            result.warnings.append(f"object {sha}: {exc}")
            if reason:
                _remember_skip(state, "object", sha, reason)
                save()
            else:
                pending = True
            continue
        try:
            with handle:
                size = os.fstat(handle.fileno()).st_size
                client.put_object(sha, handle, size)
            result.objects += 1
            result.bytes += size
        except HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code == 422 and _error_detail(response) == "hash mismatch":
                result.warnings.append(f"object {sha}: sha256 mismatch")
                _remember_skip(state, "object", sha, "sha256 mismatch")
                save()
            else:
                raise
    result.skipped = len(state["skipped"])
    sent_files = set()
    meta_status = json.loads(files["meta.json"]["data"])["status"] if "meta.json" in files else reader.meta()["status"]

    def atomic(name, force=False):
        snapshot = files[name]
        if not force and state["files"].get(name, {}).get("sha256") == snapshot["sha256"]:
            return
        try:
            client.put_file(reader.run_id, name, snapshot["data"])
        except Conflict as exc:
            _check_owner(reader, client, exc.payload)
            raise
        state["files"][name] = {"size": snapshot["size"], "sha256": snapshot["sha256"]}
        sent_files.add(name)
        result.files = len(sent_files)
        result.bytes += snapshot["size"]
        save()

    rewritten = set()
    conflicts = set()
    flipped = set()

    def append_file(name):
        path = reader.run_dir / name
        size = files[name]["size"]
        cursor = state["files"].get(name)
        offset = cursor["size"] if cursor else 0
        rewrite = name in rewrites or bool(cursor and (
            size < offset or _tail(path, offset) != cursor.get("tail")))
        while True:
            if rewrite:
                offset = 0
            if not rewrite and offset == size and cursor:
                return
            try:
                with (io.BytesIO(files[name]["data"]) if name == "events.jsonl" else _open_local(path)) as handle:
                    handle.seek(max(0, offset - 65536))
                    tail = handle.read(min(offset, 65536))
                    if not rewrite and cursor and hashlib.sha256(tail).hexdigest() != cursor.get("tail"):
                        rewrite = True
                        offset = 0
                    if rewrite:
                        tail = b""
                    # Empty append files still need creating (or truncating).
                    while rewrite or offset < size or cursor is None:
                        data = _append_chunk(handle, name, offset, size)
                        next_tail = (tail + data)[-65536:]
                        accepted = client.append(reader.run_id, name, offset, data, truncate=rewrite)
                        if accepted != offset + len(data):
                            # A replay may find a longer server file; inspect its prefix.
                            if accepted > offset + len(data):
                                raise Conflict({"size": accepted})
                            if accepted < offset + len(data):
                                raise ValueError(f"server returned invalid size for {name}")
                        if rewrite and name not in rewritten:
                            rewritten.add(name)
                            result.rewritten += 1
                            result.warnings.append(f"rewrote {name} after a local rollback")
                        rewrite = False
                        offset = accepted
                        tail = next_tail
                        cursor = {"size": offset, "tail": hashlib.sha256(tail).hexdigest()}
                        state["files"][name] = cursor
                        sent_files.add(name)
                        result.files = len(sent_files)
                        result.bytes += len(data)
                        save()
                rewrites.discard(name)
                return
            except Conflict as exc:
                payload = exc.payload
                _check_owner(reader, client, payload)
                if payload.get("error") == "run is not running" and rewrite:
                    if meta_status != "running":
                        raise PermanentSyncError(f"server copy is {payload.get('status')}; cannot rewrite {name}") from None
                    if name in flipped:
                        raise
                    flipped.add(name)
                    client.commit(reader.run_id, "running", source_hash, {})
                    continue
                if name in conflicts:
                    raise
                conflicts.add(name)
                if payload.get("mismatch"):
                    rewrite = True
                elif "size" in payload:
                    rewrite = name in _recover(reader, client, files, state, [name])
                    save()
                    cursor = state["files"].get(name)
                    offset = cursor["size"] if cursor else 0
                else:
                    raise

    append_names = sorted((name for name in files if _APPEND.fullmatch(name)), key=lambda name: (
        0 if name == "events.jsonl" else 1 if name == "scalars/_time.bin" else 2,
        int(name[8:-4]) if name not in ("events.jsonl", "scalars/_time.bin") else 0))
    atomic_names = sorted((name for name in files if _ATOMIC.fullmatch(name) and name != "meta.json"), key=lambda name: (
        0 if name.endswith(".ovw") else 1 if name == "scalars/index.json" else 2, name))
    sizes = {name: snapshot["size"] for name, snapshot in files.items()}
    missing_retried = False
    while True:
        try:
            if "meta.json" in files:
                atomic("meta.json", force=missing_retried)
            for name in append_names:
                append_file(name)
            for name in atomic_names:
                atomic(name)
            # Every non-idle pass commits, even when a previous pass sent all
            # bytes and then failed to commit. Completion requires this success.
            try:
                client.commit(reader.run_id, meta_status, source_hash, sizes)
            except Conflict as exc:
                _check_owner(reader, client, exc.payload)
                if "sizes" in exc.payload:
                    names = exc.payload["sizes"]
                    rewrites.update(_recover(reader, client, files, state, names))
                    save()
                    for name in names:
                        if name in files:
                            if _APPEND.fullmatch(name):
                                append_file(name)
                            else:
                                atomic(name)
                elif "status" in exc.payload and "meta.json" in files:
                    atomic("meta.json", force=True)
                else:
                    raise
                client.commit(reader.run_id, meta_status, source_hash, sizes)
            break
        except HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            if missing_retried:
                raise RunMissingError(response=exc.response) from exc
            missing_retried = True
            state["files"].clear()
            save()
            rewrites = _recover(reader, client, files, state)
            save()
    state["source_hash"] = source_hash
    state["complete"] = bool(fingerprint_available and effective in ("finished", "crashed")
                             and _source_hash(reader) == source_hash and not pending)
    save()
    result.complete = state["complete"]
    result.status = "synced" if result.complete else "partial" if effective in ("finished", "crashed") else "partial (running)"
    if prune and result.complete:
        _prune_synced_run(reader, state, save, result)


def _error_detail(response):
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error", "")
    return _clean_detail(error) if isinstance(error, str) else ""


def _short_error(exc):
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        if code == 401:
            return "API key rejected — run `agenticml login`"
        if 400 <= code < 500:
            detail = _error_detail(response)
            if detail:
                return f"HTTP {code}: {detail}"
        return f"HTTP {code}"
    if isinstance(exc, ProtocolError):
        return str(exc)
    if isinstance(exc, (ValueError, FileNotFoundError, Conflict)):
        return _clean_detail(str(exc).splitlines()[0])
    return type(exc).__name__


def _permanent_failure(exc):
    if isinstance(exc, RunMissingError):
        return False
    if isinstance(exc, PermanentSyncError):
        return True
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return 400 <= code < 500 and code not in _TRANSIENT_STATUS
    return False


def _order_runs_lineage(root, run_ids):
    if len(run_ids) <= 1:
        return list(run_ids)

    metas = {}
    for rid in run_ids:
        try:
            metas[rid] = RunReader(root, rid).meta()
        except Exception:
            metas[rid] = {"run_id": rid}

    orig_idx = {rid: i for i, rid in enumerate(run_ids)}
    sorted_rids = sorted(
        run_ids,
        key=lambda rid: (str(metas[rid].get("created_at") or ""), orig_idx[rid]),
    )

    selected_set = set(run_ids)
    children = {rid: [] for rid in run_ids}
    in_degree = {rid: 0 for rid in run_ids}

    for rid in run_ids:
        pid = metas[rid].get("parent_run_id")
        if isinstance(pid, str) and pid in selected_set and pid != rid:
            children[pid].append(rid)
            in_degree[rid] += 1

    rank = {rid: i for i, rid in enumerate(sorted_rids)}
    available = [r for r in sorted_rids if in_degree[r] == 0]
    remaining = set(sorted_rids)
    result = []

    while remaining:
        if not available:
            fallback = min(remaining, key=lambda r: rank[r])
            available.append(fallback)
        available.sort(key=lambda r: rank[r])
        curr = available.pop(0)
        if curr not in remaining:
            continue
        remaining.remove(curr)
        result.append(curr)
        for ch in children.get(curr, []):
            if ch in remaining:
                in_degree[ch] -= 1
                if in_degree[ch] == 0:
                    available.append(ch)

    return result


def _selected_ids(root, host, explicit, include_all, prune=False, key=None):
    selected = list(dict.fromkeys(explicit))
    if include_all:
        for meta in list_runs(root):
            run_id = meta["run_id"]
            if run_id in selected:
                continue
            try:
                reader = RunReader(root, run_id)
                state = _host_state(reader, host)
                if _sync_idle(reader, state, _source_hash(reader), key=key):
                    if not (prune and not state.get("pruned")):
                        continue
            except Exception:
                pass  # Report a corrupt run individually below.
            selected.append(run_id)
    return _order_runs_lineage(root, selected)


def cmd_sync(args):
    if getattr(args, "reset", False):
        if args.all:
            print("agenticml sync: --reset requires explicit run ids, not --all", file=sys.stderr)
            return 2
        if not args.run_ids:
            print("agenticml sync: --reset requires explicit run ids", file=sys.stderr)
            return 2
        host, _ = resolve(args.host, args.api_key, insecure=args.insecure)
        root = resolve_root(args.dir)
        for run_id in args.run_ids:
            try:
                reader = RunReader(root, run_id)
                _clear_host_state(reader, host)
            except Exception as exc:
                print(f"agenticml sync: could not reset {run_id}: {exc}", file=sys.stderr)
                return 1
        return 0

    if not args.run_ids and not args.all:
        print("agenticml sync: specify RUN_ID ... or --all", file=sys.stderr)
        return 2
    if args.watch is not None and (not math.isfinite(args.watch) or args.watch <= 0):
        print("agenticml sync: --watch must be a positive number of seconds", file=sys.stderr)
        return 2
    client = None
    results = {}
    failures = 0
    active = None
    try:
        host, api_key = resolve(args.host, args.api_key, insecure=args.insecure)
        if not args.dry_run and not api_key:
            print("not logged in — run `agenticml login`", file=sys.stderr)
            return 1
        root = resolve_root(args.dir)
        if not args.dry_run:
            from .client import Client
            client = Client(base_url=host, api_key=api_key, insecure=args.insecure)
            _identity(client)
        prune = getattr(args, "prune", False)
        while True:
            failures = 0
            selected = _selected_ids(root, host, args.run_ids, args.all, prune=prune,
                                     key=_identity(client)["key"] if client is not None else None)
            if not selected and args.all:
                print("up to date", flush=True)
            for run_id in selected:
                active = SyncResult(run_id)
                results[run_id] = active
                try:
                    sync_run(
                        RunReader(root, run_id),
                        host,
                        client,
                        active,
                        dry_run=args.dry_run,
                        prune=prune,
                    )
                except Exception as exc:
                    active.failed = True
                    active.permanent = _permanent_failure(exc)
                    active.status = f"failed: {_short_error(exc)}"
                if active.failed:
                    failures += 1
                print(active.line(), flush=True)
                active = None
            if args.watch is None or args.dry_run or all(
                    item.complete or item.permanent or item.stale for item in results.values()):
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        if active is not None:
            active.status = "partial (interrupted)"
            print(active.line())
        print(f"Interrupted: complete={sum(r.complete for r in results.values())} "
              f"partial={sum(not r.complete and not r.failed for r in results.values())} "
              f"failed={sum(r.failed for r in results.values())}")
        return 130
    except Exception as exc:
        print(f"agenticml sync: {_short_error(exc)}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    if args.watch is None:
        skipped_runs = any(r.status == "skipped" for r in results.values())
        return 1 if (failures or skipped_runs) else 0
    return 1 if failures else 0
