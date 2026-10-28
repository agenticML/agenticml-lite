"""Incremental upload of the v1 local store, with a cursor per destination host."""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .auth import resolve
from .integrations.hyperparams import redact_config
from .store.gc import prunable_series, prune_run, run_fingerprint
from .store.layout import (
    RECORD_SIZE, _LOCK_UNSUPPORTED, open_object, read_file_nofollow, resolve_root, try_lock_run, unlock_run,
)
from .store.reader import RunReader, _effective_status, list_runs

ROUND_RECORDS = 200_000
BATCH_ITEMS = 2_000
CODE_FILES = 200
CONFIG_FIELDS = ("config", "tags", "notes", "group", "job_type")
_UINT32_MAX = 2**32 - 1
_MEDIA_STEP_MIN = -2**31
_MEDIA_STEP_MAX = 2**31 - 1
_TRANSIENT_STATUS = frozenset({408, 429})
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\)|[@-Z\\-_])")
_lock_warning_emitted = False


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _json_safe(value):
    """Replace non-finite floats with None and coerce values JSON cannot encode.

    Lists and tuples are walked. A non-string dict key is coerced with ``str``.
    Returns ``(cleaned, replacements)``.
    """
    replaced = 0

    def walk(item):
        nonlocal replaced
        if item is None or isinstance(item, str):
            return item
        if isinstance(item, bool):
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                replaced += 1
                return None
            return item
        if isinstance(item, dict):
            cleaned = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    replaced += 1
                    key = str(key)
                cleaned[key] = walk(child)
            return cleaned
        if isinstance(item, (list, tuple)):
            return [walk(child) for child in item]
        replaced += 1
        return str(item)

    return walk(value), replaced


def _valid_content_type(ct) -> str:
    if isinstance(ct, str) and "/" in ct:
        parts = ct.split("/", 1)
        if parts[0].strip() and parts[1].strip():
            return ct.strip()
    return "application/octet-stream"


def _clean_detail(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7f)
    return text[:200]


def _config(meta):
    cfg = meta.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    tags = meta.get("tags")
    if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        tags = []
    notes = meta.get("notes")
    if not isinstance(notes, str):
        notes = None
    group = meta.get("group")
    if not isinstance(group, str):
        group = None
    job_type = meta.get("job_type")
    if not isinstance(job_type, str):
        job_type = None
    return {"config": redact_config(cfg), "tags": tags, "notes": notes, "group": group, "job_type": job_type}


def _init_payload(meta):
    payload = {key: meta[key] for key in (
        "project", "name", *CONFIG_FIELDS, "parent_run_id", "snapshot_hash",
    ) if key in meta}
    if "project" in payload and not isinstance(payload["project"], str):
        payload["project"] = str(payload["project"]) if payload["project"] is not None else ""
    if "name" in payload and not isinstance(payload["name"], str):
        payload["name"] = str(payload["name"]) if payload["name"] is not None else ""
    if "tags" in payload:
        if not (isinstance(payload["tags"], list) and all(isinstance(t, str) for t in payload["tags"])):
            payload["tags"] = []
    for field in ("notes", "group", "job_type"):
        if field in payload and not isinstance(payload[field], str):
            payload[field] = None
    # server: created_at and finished_at are stamped by the server.
    for section, fields in (
        ("env", ("python_version", "platform", "hostname", "packages")),
        ("git", ("repo_root", "git_commit", "git_branch", "is_dirty")),
    ):
        nested = meta.get(section) or {}
        for key in fields:
            if key in meta:
                payload[key] = meta[key]
            elif key in nested:
                payload[key] = nested[key]
    return payload


def _wire_init_payload(meta, parent_remote_id):
    """Payload actually posted to /init.

    A local parent id is not a server id. Send the parent's remote id for this
    host, or omit the field. ``snapshot_hash`` is sent only when meta has one;
    FinalizeRequest does not accept it.
    """
    payload = _init_payload(meta)
    if parent_remote_id:
        payload["parent_run_id"] = parent_remote_id
    else:
        payload.pop("parent_run_id", None)
    if not payload.get("snapshot_hash"):
        payload.pop("snapshot_hash", None)
    return payload


def _remote_parent(reader, host, meta):
    parent_id = meta.get("parent_run_id")
    if not isinstance(parent_id, str) or not parent_id:
        return None
    try:
        parent = RunReader(reader.root, parent_id)
    except (OSError, ValueError):
        return None
    remote = _host_state(parent, host).get("remote_run_id")
    if isinstance(remote, str) and remote:
        return remote
    return None


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


def _host_state(reader, host):
    saved = _load_state(reader)["hosts"].get(host, {})
    return {"remote_run_id": None, "config_hash": None, "offsets": {}, "code": [],
            "media": [], "artifacts": [], "artifact_skips": {}, "skipped": [],
            "finalized": False, "complete": False, "skipped_nonfinite": 0,
            "pruned": False, **saved}


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


def _code_token(item):
    return f"{item['path']}|{item['sha256']}"


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


def _code_pending(manifest, state):
    sent = set(state.get("code") or [])
    skipped = _skipped_ids(state)
    return [item for item in manifest
            if _code_token(item) not in sent and _code_token(item) not in skipped]


def _media_rows(reader):
    for key, rows in reader.media().items():
        for row in rows:
            if isinstance(row, dict):
                yield f"m:{row['id']}", key, row


def _media_done(state, token):
    return token in (state.get("media") or []) or token in _skipped_ids(state)


def _artifacts(reader):
    for index, event in enumerate(reader.events()):
        if event.get("kind") == "artifact":
            yield f"{index}:{event['name']}", event


def _byref_issue(path, artifact):
    """None when a by-reference file still matches the logged size and mtime."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "changed since logged", "missing"
    except OSError as exc:
        return "changed since logged", f"error:{getattr(exc, 'errno', '')}"
    if stat.S_ISLNK(info.st_mode):
        return "refusing symlink", "symlink"
    if (not stat.S_ISREG(info.st_mode) or info.st_size != artifact.get("size")
            or info.st_mtime != artifact.get("mtime")):
        return "changed since logged", f"{info.st_size}:{info.st_mtime}"
    return None, None


def _byref_restored(reader, state):
    skips = state.get("artifact_skips") or {}
    if not skips:
        return False
    for local_id, artifact in _artifacts(reader):
        if local_id not in skips or artifact.get("sha256"):
            continue
        message, _fingerprint = _byref_issue(Path(artifact["path"]), artifact)
        if message is None:
            return True
    return False


@dataclass
class SyncResult:
    run_id: str
    label: str = "?/?"
    status: str = "partial"
    points: int = 0
    files: int = 0
    media: int = 0
    artifacts: int = 0
    skipped: int = 0
    replaced: int = 0
    warnings: list[str] = field(default_factory=list)
    complete: bool = False
    failed: bool = False
    permanent: bool = False
    stale: bool = False

    def line(self):
        counts = (f"points={self.points} files={self.files} media={self.media} "
                  f"artifacts={self.artifacts} skipped={self.skipped} replaced={self.replaced}")
        warnings = "; " + "; ".join(self.warnings) if self.warnings else ""
        return f"{self.run_id}  {self.label}  {self.status}  {counts}{warnings}"


def _note_missing_remote(exc, first, host, run_id):
    """One place for the reset hint when the saved remote id is gone."""
    response = getattr(exc, "response", None)
    if first and getattr(response, "status_code", None) == 404:
        print(f"remote run not found on {host} — use `agenticml sync --reset {run_id}`")


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


def _pruned_upload_block(reader, state, source_hash):
    """Refuse a fresh upload once raw scalar history is gone.

    The host that already stored the post-prune snapshot is left to the
    up-to-date path. Another host, or this host after ``--reset``, would
    publish a run whose points no longer exist locally.
    """
    hosts, flagged = _prune_markers(reader)
    missing = _raw_scalars_removed(reader)
    if not flagged and not missing:
        return None
    if (state.get("remote_run_id") and state.get("complete")
            and state.get("source_hash") == source_hash):
        return None
    if hosts:
        host = hosts[0]
    else:
        host = None
        for name, host_state in (_load_state(reader).get("hosts") or {}).items():
            if isinstance(host_state, dict) and host_state.get("remote_run_id"):
                host = name
                break
        if host is None:
            host = "a previous host"
    return (f"run was pruned after syncing to {host}; "
            "its full scalar history is no longer local")


def _media_pending(reader, state):
    for token, _key, _row in _media_rows(reader):
        if not _media_done(state, token):
            return True
    return False


def _artifact_content_pending(reader, state):
    done = set(state.get("artifacts") or [])
    skipped = _skipped_ids(state)
    held = state.get("artifact_skips") or {}
    if not isinstance(held, dict):
        held = {}
    for local_id, artifact in _artifacts(reader):
        if local_id in done or local_id in skipped:
            continue
        # A by-reference file that no longer matches is waiting, not blocking.
        if local_id in held and not artifact.get("sha256"):
            continue
        return True
    return False


def _upload_pending(reader, state):
    return bool(
        _code_pending(reader.code_manifest(), state)
        or _media_pending(reader, state)
        or _artifact_content_pending(reader, state)
    )


def _sync_idle(reader, state, source_hash):
    return (state["complete"] and state.get("source_hash") == source_hash
            and _effective_status(
                reader.meta(), str(reader.run_dir / "meta.json"), str(reader.run_dir), time.time())
                in ("finished", "crashed")
            and not _upload_pending(reader, state) and not _byref_restored(reader, state))


def _skip_nonfinite_heads(keys, loaded, indexes):
    """Advance each key past a leading run of non-finite values.

    Those records stay part of the contiguous prefix. A step outside the
    server's uint32 range is a hard error.
    """
    skipped = 0
    for key in keys:
        records = loaded[key]
        index = indexes[key]
        while index < len(records):
            step, value = records[index]
            if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step <= _UINT32_MAX:
                raise ValueError(f"scalar step out of range for {key}")
            if not math.isfinite(value):
                skipped += 1
                index += 1
                continue
            break
        indexes[key] = index
    return skipped


def _scalar_batches(reader, remote_id, start_offsets, sizes=None):
    """Yield ``(items, offsets, skipped_nonfinite)`` one server batch at a time.

    Each key is walked in file order. Same-step values are grouped only when
    each value is the next unread record of its key, so the records included
    so far are a contiguous prefix. ``offsets`` are absolute byte offsets
    covering exactly those records (sent points and non-finite skips).

    A crash after the server commits a batch and before the caller saves
    state can duplicate that one batch. The server averages repeated
    ``(run, key, step)`` points, so the replay skews the aggregate. Saving
    after every accepted batch keeps the replay to that single batch.
    """
    sizes = reader.record_sizes() if sizes is None else sizes
    keys = list(sizes)
    offsets = {}
    for key, value in (start_offsets or {}).items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            offsets[key] = 0
        else:
            offsets[key] = value
    while True:
        for key in keys:
            offsets.setdefault(key, 0)
        window = max(1, ROUND_RECORDS // max(1, len(keys)))
        loaded = {}
        progressed = False
        remaining = ROUND_RECORDS
        for key in keys:
            limit = min(window, remaining, max(0, (sizes[key] - offsets[key]) // RECORD_SIZE))
            records, _end = reader.read_records(key, offsets[key], limit=limit) if limit else ([], offsets[key])
            remaining -= len(records)
            loaded[key] = records
            progressed = progressed or bool(records)
        if not progressed:
            return
        indexes = {key: 0 for key in keys}
        skipped_since_yield = 0

        def offsets_now():
            current = dict(offsets)
            for key in keys:
                current[key] = offsets[key] + indexes[key] * RECORD_SIZE
            return current

        batch = []
        while True:
            skipped_since_yield += _skip_nonfinite_heads(keys, loaded, indexes)
            heads = [key for key in keys if indexes[key] < len(loaded[key])]
            if not heads:
                break
            step = min(loaded[key][indexes[key]][0] for key in heads)
            metrics = {}
            for key in heads:
                rec_step, value = loaded[key][indexes[key]]
                if rec_step != step:
                    continue
                metrics[key] = value
                indexes[key] += 1
            batch.append({"run_id": remote_id, "step": step, "metrics": metrics})
            if len(batch) >= BATCH_ITEMS:
                yield batch, offsets_now(), skipped_since_yield
                batch = []
                skipped_since_yield = 0
        if batch or skipped_since_yield:
            yield batch, offsets_now(), skipped_since_yield
        for key in keys:
            offsets[key] = offsets[key] + indexes[key] * RECORD_SIZE


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


def _sync_locked(reader, host, client, result, *, dry_run=False, prune=False):
    # State is read again under the lock so a peer cannot init between the
    # check and the POST.
    source_hash = _source_hash(reader)
    sizes = reader.record_sizes()
    meta = reader.meta()
    result.label = _run_label(meta)
    state = _host_state(reader, host)
    def save():
        if not dry_run:
            _save_state(reader, host, state)

    block = _pruned_upload_block(reader, state, source_hash)
    if block:
        result.failed = True
        result.permanent = True
        result.status = f"failed: {block}"
        return

    if _sync_idle(reader, state, source_hash):
        result.status = "up to date"
        result.complete = True
        if prune and not dry_run and not state.get("pruned"):
            _prune_synced_run(reader, state, save, result)
        return
    state["complete"] = False

    config, config_replacements = _json_safe(_config(meta))
    config_hash = _hash(config)
    has_saved_remote = bool(state.get("remote_run_id"))
    first_remote_request = has_saved_remote
    if not state["remote_run_id"]:
        payload = _wire_init_payload(meta, _remote_parent(reader, host, meta))
        payload.update(config)
        payload, extra_replacements = _json_safe(payload)
        result.replaced += config_replacements + extra_replacements
        if not dry_run:
            state["remote_run_id"] = client.init_run(payload)
        state["config_hash"] = config_hash
        save()
    elif state["config_hash"] != config_hash:
        result.replaced += config_replacements
        if not dry_run:
            # server: PATCH /config cannot delete keys or null group/job_type.
            try:
                client.update_config(state["remote_run_id"], **config)
                first_remote_request = False
            except requests.HTTPError as exc:
                _note_missing_remote(exc, first_remote_request, host, reader.run_id)
                raise
        state["config_hash"] = config_hash
        save()
    remote_id = state["remote_run_id"]

    # See _scalar_batches: offsets move forward only for batches the server
    # accepted. A crash between that commit and the save below can still
    # duplicate the one unsaved batch.
    for items, new_offsets, skipped in _scalar_batches(reader, remote_id, state["offsets"], sizes):
        if items and not dry_run:
            try:
                client.log_batch(items)
                first_remote_request = False
            except requests.HTTPError as exc:
                _note_missing_remote(exc, first_remote_request, host, reader.run_id)
                raise
        if items:
            result.points += sum(len(item["metrics"]) for item in items)
        state["offsets"] = new_offsets
        state["skipped_nonfinite"] += skipped
        result.skipped += skipped
        save()

    manifest = reader.code_manifest()
    pending_code = _code_pending(manifest, state)
    for start in range(0, len(pending_code), CODE_FILES):
        batch = pending_code[start:start + CODE_FILES]
        if not dry_run:
            valid_items = []
            files = []
            for item in batch:
                token = _code_token(item)
                try:
                    with open_object(reader.root, item["sha256"]) as handle:
                        content = handle.read()
                except (FileNotFoundError, OSError) as exc:
                    result.warnings.append(f"code {item['path']}: {exc}")
                    result.skipped += 1
                    reason = _permanent_object_reason(exc)
                    if reason:
                        _remember_skip(state, "code", token, reason)
                    continue
                actual_sha = hashlib.sha256(content).hexdigest()
                if actual_sha != item["sha256"]:
                    result.warnings.append(f"code {item['path']}: sha256 mismatch")
                    result.skipped += 1
                    _remember_skip(state, "code", token, "sha256 mismatch")
                    continue
                valid_items.append(item)
                files.append({
                    **item,
                    "content_type": _valid_content_type(item.get("content_type")),
                    "content": content,
                })
            if files:
                try:
                    client.upload_code(remote_id, files)
                    first_remote_request = False
                except requests.HTTPError as exc:
                    _note_missing_remote(exc, first_remote_request, host, reader.run_id)
                    raise
            state["code"] = sorted(set(state.get("code") or []) | {_code_token(item) for item in valid_items})
            result.files += len(valid_items)
            save()
        else:
            state["code"] = sorted(set(state.get("code") or []) | {_code_token(item) for item in batch})
            result.files += len(batch)

    # server: log_media and artifacts/register-version need an idempotency key.
    # A lost response is retried as a new upload and can duplicate the row.
    for token, key, row in _media_rows(reader):
        if _media_done(state, token):
            continue
        step = row.get("step")
        # media_events.step is a signed 32-bit integer.
        if isinstance(step, bool) or not isinstance(step, int) or not _MEDIA_STEP_MIN <= step <= _MEDIA_STEP_MAX:
            result.warnings.append(f"media {key} step {step}: out of range")
            result.skipped += 1
            _remember_skip(state, "media", token, "step out of range")
            save()
            continue
        if not dry_run:
            try:
                handle = open_object(reader.root, row["sha256"])
            except (FileNotFoundError, OSError) as exc:
                result.warnings.append(f"media {key}: {exc}")
                result.skipped += 1
                reason = _permanent_object_reason(exc)
                if reason:
                    _remember_skip(state, "media", token, reason)
                    save()
                continue
            try:
                content_type = _valid_content_type(row.get("content_type"))
                kind = row.get("kind") or content_type.split("/", 1)[0]
                client.log_media(remote_id, key, row["step"], kind, row.get("caption") or "",
                                 handle, content_type, filename=row["sha256"])
            finally:
                handle.close()
        state["media"].append(token)
        result.media += 1
        save()

    for local_id, artifact in _artifacts(reader):
        if local_id in state["artifacts"] or local_id in _skipped_ids(state):
            continue
        metadata, metadata_replacements = _json_safe(artifact.get("metadata") or {})
        content_type = _valid_content_type(
            artifact.get("content_type")
            or mimetypes.guess_type(str(artifact.get("path", "")))[0]
        )
        if artifact.get("sha256"):
            if not dry_run:
                try:
                    handle = open_object(reader.root, artifact["sha256"])
                except (FileNotFoundError, OSError) as exc:
                    result.warnings.append(f"artifact {artifact['name']}: {exc}")
                    result.skipped += 1
                    reason = _permanent_object_reason(exc)
                    if reason:
                        _remember_skip(state, "artifact", local_id, reason)
                        save()
                    continue
                try:
                    client.log_artifact(
                        remote_id, artifact["name"], artifact.get("type", "file"),
                        metadata, handle, content_type,
                        filename=Path(artifact["path"]).name if artifact.get("path") else artifact["sha256"],
                    )
                finally:
                    handle.close()
        else:
            path = Path(artifact["path"])
            message, fingerprint = _byref_issue(path, artifact)
            if message:
                skips = state.setdefault("artifact_skips", {})
                if skips.get(local_id) != fingerprint:
                    skips[local_id] = fingerprint
                    result.warnings.append(f"artifact {artifact['name']}: {message}")
                    save()
                continue
            if not dry_run:
                print(f"{result.run_id}  uploading {path}", flush=True)
                client.log_artifact(remote_id, artifact["name"], artifact.get("type", "file"),
                                    metadata, str(path), content_type)
        result.replaced += metadata_replacements
        skips = state.get("artifact_skips")
        if isinstance(skips, dict):
            skips.pop(local_id, None)
        state["artifacts"].append(local_id)
        result.artifacts += 1
        save()

    handle = try_lock_run(reader.run_dir)
    try:
        current_meta = reader.meta()
        effective = "running" if handle is None else _effective_status(
            current_meta, str(reader.run_dir / "meta.json"), str(reader.run_dir), time.time(), lock=handle
        )
        if effective == "running":
            result.warnings.append("run is active")
        if effective == "stale":
            result.stale = True
            print("stale — not finalizing; resume or finish the run")

        terminal = effective in ("finished", "crashed")
        unchanged = source_hash == _source_hash(reader)
        snapshot_hash = current_meta.get("snapshot_hash")
        if not snapshot_hash:
            lines = sorted(f"{item['path']}:{item['sha256']}" for item in manifest)
            snapshot_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        summary, summary_replacements = _json_safe(current_meta.get("summary") or {})
        if effective == "crashed" and current_meta.get("status") == "running":
            summary = {**summary, "_exit_reason": "process died"}
        finalize_hash = _hash([effective, current_meta.get("finished_at"), summary, snapshot_hash])

        def commit_finalize():
            nonlocal first_remote_request
            if not dry_run:
                try:
                    client.finalize(remote_id, summary, status=effective)
                    first_remote_request = False
                except requests.HTTPError as exc:
                    _note_missing_remote(exc, first_remote_request, host, reader.run_id)
                    raise
            result.replaced += summary_replacements
            state["finalized"] = True
            state["finalize_hash"] = finalize_hash
            save()

        if not terminal:
            state["finalized"] = False
        elif unchanged and (not state["finalized"] or state.get("finalize_hash") != finalize_hash):
            commit_finalize()
        # By-reference mismatches are listed in artifact_skips and do not block
        # completion. Unreadable or missing objects stay pending and are retried.
        # Permanent skips (range, sha mismatch, planted link) are listed in
        # state["skipped"] so a later sync and du can show them without retrying.
        state["source_hash"] = source_hash
        pending = _upload_pending(reader, state)
        state["complete"] = bool(terminal and state["finalized"] and unchanged and not pending)
        save()
        result.complete = state["complete"]
        result.status = "synced" if result.complete else "partial" if terminal else "partial (running)"
    finally:
        unlock_run(handle)
    if prune and result.complete and not dry_run:
        _prune_synced_run(reader, state, save, result)
    if dry_run:
        result.status = "dry-run (would send)"


def _error_detail(response):
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict) or "detail" not in payload:
        return ""
    detail = payload["detail"]
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str, separators=(",", ":"))
    return _clean_detail(detail)


def _short_error(exc):
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        if code in (401, 403):
            return "API key rejected — run `agenticml login`"
        if 400 <= code < 500:
            detail = _error_detail(response)
            if detail:
                return f"HTTP {code}: {detail}"
        return f"HTTP {code}"
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return str(exc).splitlines()[0][:160]
    return type(exc).__name__


def _permanent_failure(exc):
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in _TRANSIENT_STATUS


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


def _selected_ids(root, host, explicit, include_all, prune=False):
    selected = list(dict.fromkeys(explicit))
    if include_all:
        for meta in list_runs(root):
            run_id = meta["run_id"]
            if run_id in selected:
                continue
            try:
                reader = RunReader(root, run_id)
                state = _host_state(reader, host)
                if _sync_idle(reader, state, _source_hash(reader)):
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
        prune = getattr(args, "prune", False)
        while True:
            failures = 0
            selected = _selected_ids(root, host, args.run_ids, args.all, prune=prune)
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
