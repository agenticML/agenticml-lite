"""Garbage collection, storage inspection, and disk budget enforcement."""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import stat
import struct
import threading
import time
from pathlib import Path

from .layout import (
    EVENTS_NAME,
    INDEX_NAME,
    MEDIA_DIRNAME,
    META_NAME,
    RECORD_SIZE,
    SCALARS_DIRNAME,
    atomic_write,
    contained,
    fsync_dir,
    open_nofollow,
    object_path,
    parse_timestamp,
    read_file_nofollow,
    resolve_root,
    run_dir,
    try_lock_run,
    unlock_run,
    valid_run_id,
    valid_sha,
)
from .reader import RunReader, _effective_status, list_runs, run_references
from .overview import Overview

logger = logging.getLogger("agenticml.store.gc")

DEFAULT_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB
ONE_HOUR_SECONDS = 3600
ONE_DAY_SECONDS = 86400


def _format_bytes(num: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def _cheap_total_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        curr = stack.pop()
        try:
            with os.scandir(curr) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return total


def _safe_rmtree(path: Path) -> int:
    """Recursively delete path without following symlinks. Returns bytes freed."""
    bytes_freed = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        bytes_freed += st.st_size
        try:
            os.unlink(path)
        except OSError:
            pass
        return bytes_freed

    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            if Path(root) == path and name == "run.lock":
                continue
            file_path = os.path.join(root, name)
            try:
                fst = os.lstat(file_path)
                bytes_freed += fst.st_size
                os.unlink(file_path)
            except OSError:
                pass
        for name in dirs:
            dir_path = os.path.join(root, name)
            try:
                dst = os.lstat(dir_path)
                if stat.S_ISLNK(dst.st_mode):
                    os.unlink(dir_path)
                else:
                    os.rmdir(dir_path)
            except OSError:
                pass
    try:
        # Keep the held lock discoverable until meta.json has been removed.
        lock_path = path / "run.lock"
        if not (path / META_NAME).exists():
            st = os.lstat(lock_path)
            os.unlink(lock_path)
            bytes_freed += st.st_size
    except OSError:
        pass
    try:
        os.rmdir(path)
    except OSError:
        pass
    return bytes_freed


def run_fingerprint(root, run_id: str) -> str:
    """Cheap sync identity over metadata, series definitions, and source sizes."""
    reader = RunReader(root, run_id)
    meta = reader.meta()
    directory = reader.run_dir
    scalars = contained(reader.root, directory / SCALARS_DIRNAME)
    index = json.loads(read_file_nofollow(scalars / INDEX_NAME))
    series = index["series"]
    if not isinstance(series, dict):
        raise ValueError(f"corrupt scalars index for {run_id}")

    def size(path):
        try:
            with open_nofollow(path) as handle:
                return os.fstat(handle.fileno()).st_size
        except FileNotFoundError:
            return None  # Absent raw data after prune has a distinct identity.

    bins = [(p.name, size(p)) for p in sorted(scalars.glob("*.bin"))]
    payload = [meta.get("status"), meta.get("summary"), meta.get("config"), series,
               bins, size(directory / EVENTS_NAME),
               size(reader._c(directory / MEDIA_DIRNAME / INDEX_NAME))]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def prunable_series(root, run_id: str) -> list[str]:
    """Series with a valid overview covering every complete raw record."""
    reader = RunReader(root, run_id)
    if reader.meta().get("status") not in ("finished", "crashed"):
        return []
    keys = []
    for key in reader.keys():
        try:
            series_id = reader._cached_series_id(key)
            path = reader._bin_path(series_id)
            with open_nofollow(path) as handle:
                size = os.fstat(handle.fileno()).st_size
            overview = Overview.from_bytes(read_file_nofollow(path.with_suffix(".ovw")))
            if overview.point_count == size // RECORD_SIZE:
                keys.append(key)
        except (OSError, ValueError, struct.error):
            continue
    return keys


def prune_run(root, run_id: str, keys, expected_fingerprint: str | None = None) -> None:
    """Revalidate the sync fingerprint and series under the lifetime lock."""
    directory = run_dir(root, run_id)
    handle = try_lock_run(directory)
    if handle is None:
        raise ValueError(f"run {run_id} is open in another process")
    try:
        if expected_fingerprint is not None and run_fingerprint(root, run_id) != expected_fingerprint:
            raise ValueError("run changed since sync; not pruning")
        requested = set(keys)
        allowed = set(prunable_series(root, run_id))
        if not requested <= allowed:
            raise ValueError(f"run {run_id} has series without matching valid overviews")
        reader = RunReader(root, run_id)
        for key in requested:
            reader._bin_path(reader._cached_series_id(key)).unlink()
        fsync_dir(contained(root, directory / SCALARS_DIRNAME))
    finally:
        unlock_run(handle)


def _is_run_synced(r_dir: Path) -> bool:
    sync_file = r_dir / "sync.json"
    if not sync_file.is_file() or sync_file.is_symlink():
        return False
    try:
        raw = read_file_nofollow(sync_file)
        data = json.loads(raw.decode("utf-8"))
        hosts = data.get("hosts")
        if isinstance(hosts, dict):
            fingerprint = run_fingerprint(r_dir.parent.parent, r_dir.name)
            return any(h.get("complete") is True and h.get("source_hash") == fingerprint
                       for h in hosts.values() if isinstance(h, dict))
    except Exception:
        return False
    return False


def run_usage(root, run_id: str) -> dict:
    """Compute on-disk usage for a single run.

    Returns bytes for raw scalars (.bin), overviews (.ovw), meta/events/index,
    media referenced, copied artifacts referenced, code referenced.
    Referenced objects are counted once per run.
    """
    if not valid_run_id(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    root_path = resolve_root(root)
    r_dir = run_dir(root_path, run_id)
    if not r_dir.is_dir() or r_dir.is_symlink():
        raise FileNotFoundError(f"run directory not found: {r_dir}")

    raw_bytes = ovw_bytes = meta_bytes = 0
    scalars_dir = r_dir / SCALARS_DIRNAME
    for directory in (r_dir, scalars_dir, r_dir / MEDIA_DIRNAME):
        if directory.is_symlink():
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                    if directory == scalars_dir and entry.name.endswith(".bin"):
                        raw_bytes += size
                    elif directory == scalars_dir and entry.name.endswith(".ovw"):
                        ovw_bytes += size
                    else:
                        meta_bytes += size
        except OSError:
            pass

    refs = run_references(root_path, run_id)
    media_shas, artifact_shas, code_shas = (refs[k] for k in ("media", "artifacts", "code"))

    all_run_shas = media_shas | artifact_shas | code_shas
    sha_sizes = {}
    for sha in all_run_shas:
        p = object_path(root_path, sha)
        try:
            st = os.lstat(p)
            if not stat.S_ISLNK(st.st_mode):
                sha_sizes[sha] = st.st_size
            else:
                sha_sizes[sha] = 0
        except OSError:
            sha_sizes[sha] = 0

    media_bytes = sum(sha_sizes.get(sha, 0) for sha in media_shas)
    artifact_bytes = sum(sha_sizes.get(sha, 0) for sha in artifact_shas)
    code_bytes = sum(sha_sizes.get(sha, 0) for sha in code_shas)
    objects_total = sum(sha_sizes.get(sha, 0) for sha in all_run_shas)
    run_dir_total = raw_bytes + ovw_bytes + meta_bytes
    total = run_dir_total + objects_total

    shared_within = (
        len(media_shas & artifact_shas)
        + len(media_shas & code_shas)
        + len(artifact_shas & code_shas)
    )

    return {
        "run_id": run_id,
        "raw": raw_bytes,
        "overview": ovw_bytes,
        "meta": meta_bytes,
        "media": media_bytes,
        "artifacts": artifact_bytes,
        "code": code_bytes,
        "run_dir_total": run_dir_total,
        "objects_total": objects_total,
        "total": total,
        "media_shas": sorted(media_shas),
        "artifact_shas": sorted(artifact_shas),
        "code_shas": sorted(code_shas),
        "all_shas": sorted(all_run_shas),
        "shared_objects": shared_within,
    }


def usage(root) -> dict:
    """Totals across the store: runs dir, objects dir, unreferenced objects, plus per-run list."""
    root_path = resolve_root(root)
    runs_dir = root_path / "runs"
    objects_dir = root_path / "objects"

    runs_list = []
    all_referenced_shas = set()
    sha_ref_counts = collections.Counter()

    if runs_dir.is_dir() and not runs_dir.is_symlink():
        all_metas = list_runs(root_path)
        meta_by_id = {m["run_id"]: m for m in all_metas}
        for run_id, meta in meta_by_id.items():
            try:
                info = run_usage(root_path, run_id)
            except Exception:
                continue
            r_dir = runs_dir / run_id
            info["synced"] = _is_run_synced(r_dir)
            info["status"] = meta.get("effective_status", meta.get("status", ""))
            info["project"] = meta.get("project", "")
            info["name"] = meta.get("name", "")
            info["created_at"] = meta.get("created_at")

            for sha in info["all_shas"]:
                all_referenced_shas.add(sha)
                sha_ref_counts[sha] += 1
            runs_list.append(info)

    for info in runs_list:
        info["shared_objects"] = sum(1 for sha in info["all_shas"] if sha_ref_counts[sha] > 1)

    runs_list.sort(key=lambda r: (str(r.get("created_at") or ""), r.get("run_id", "")), reverse=True)
    runs_total = sum(r["run_dir_total"] for r in runs_list)

    objects_total = 0
    unreferenced_total = 0
    unreferenced_count = 0

    if objects_dir.is_dir() and not objects_dir.is_symlink():
        with os.scandir(objects_dir) as shards:
            for shard in shards:
                if shard.name.startswith(".") or shard.is_symlink():
                    continue
                if not shard.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(shard.path) as objs:
                    for obj in objs:
                        if obj.name.startswith(".") or obj.is_symlink():
                            continue
                        sha = obj.name
                        if not valid_sha(sha):
                            continue
                        try:
                            st = obj.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        size = st.st_size
                        objects_total += size
                        if sha not in all_referenced_shas:
                            unreferenced_total += size
                            unreferenced_count += 1

    return {
        "runs_total": runs_total,
        "objects_total": objects_total,
        "unreferenced_total": unreferenced_total,
        "unreferenced_count": unreferenced_count,
        "total": runs_total + objects_total,
        "runs": runs_list,
    }


def _sweep_object(path: Path, *, dry_run: bool) -> int | None:
    """Return bytes removed, or None when a candidate was refreshed."""
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or time.time() - st.st_mtime <= ONE_HOUR_SECONDS:
        return None
    if dry_run:
        return st.st_size
    trash = path.with_name(f".trash-{path.name}-{os.getpid()}")
    os.rename(path, trash)
    # A dedup utime can land after the pre-rename stat. After rename, a new
    # writer publishes a fresh object at the original name instead.
    st = os.stat(trash, follow_symlinks=False)
    if time.time() - st.st_mtime <= ONE_HOUR_SECONDS:
        if os.path.lexists(path):
            os.unlink(trash)
        else:
            os.rename(trash, path)
        return None
    os.unlink(trash)
    return st.st_size


def collect(
    root,
    *,
    older_than_days: float | None = None,
    keep_last: int | None = None,
    include_unsynced: bool = False,
    dry_run: bool = False,
) -> dict:
    """Garbage collect runs and unreferenced objects.

    Selects runs to delete (status finished/crashed/stale only, never running;
    by default only fully-synced runs; keep_last keeps the N newest per project).
    Deletes their directories, then mark-and-sweeps objects:
    referenced = union over remaining runs of code manifest shas, media index shas,
    copied artifact shas.
    Unreferenced objects older than 1 hour are deleted.
    Never follows symlinks. Returns counts and bytes freed.
    """
    root_path = resolve_root(root)
    runs_root = root_path / "runs"
    now = time.time()

    all_metas = list_runs(root_path) if runs_root.is_dir() else []
    by_project = collections.defaultdict(list)
    for meta in all_metas:
        by_project[meta.get("project", "")].append(meta)

    protected_keep_last = set()
    if keep_last is not None and keep_last > 0:
        for p_runs in by_project.values():
            for meta in p_runs[:keep_last]:
                protected_keep_last.add(meta["run_id"])

    selected_ids = set()
    runs_bytes_freed = 0
    in_use = []
    for original in all_metas:
        run_id = original["run_id"]
        r_dir = run_dir(root_path, run_id)
        try:
            handle = try_lock_run(r_dir)
        except OSError:
            continue
        if handle is None:
            in_use.append(run_id)
            logger.info("skipping run %s: in use", run_id)
            continue
        try:
            # Resume may have changed status after list_runs took its snapshot.
            meta = RunReader(root_path, run_id).meta()
            eff = _effective_status(meta, str(r_dir / META_NAME), str(r_dir), time.time(), lock=handle)
            if eff not in ("finished", "crashed", "stale") or run_id in protected_keep_last:
                continue
            if not include_unsynced and (eff == "stale" or not _is_run_synced(r_dir)):
                continue
            if older_than_days is not None:
                try:
                    created = parse_timestamp(meta.get("created_at")).timestamp()
                except (ValueError, TypeError):
                    created = os.lstat(r_dir / META_NAME).st_mtime
                if now - created < older_than_days * ONE_DAY_SECONDS:
                    continue
            runs_bytes_freed += _cheap_total_bytes(r_dir) if dry_run else _safe_rmtree(r_dir)
            if dry_run or not r_dir.exists():
                selected_ids.add(run_id)
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning("skipping run %s: %s", run_id, exc)
        finally:
            unlock_run(handle)

    runs_deleted = len(selected_ids)
    referenced_shas = set()
    sweep_error = None
    # Include directories list_runs omitted because their metadata was corrupt.
    # Enumerate again to include newly created runs and partially deleted runs.
    try:
        survivors = sorted(p.name for p in runs_root.iterdir()
                           if valid_run_id(p.name) and not p.is_symlink() and p.is_dir()) if runs_root.exists() else []
        for rid in survivors:
            if dry_run and rid in selected_ids:
                continue
            try:
                referenced_shas.update(RunReader(root_path, rid).referenced_shas())
            except (OSError, ValueError, RecursionError) as exc:
                sweep_error = f"object sweep aborted: cannot read references for run {rid}: {exc}"
                break
    except OSError as exc:
        sweep_error = f"object sweep aborted: cannot enumerate surviving runs: {exc}"
    if sweep_error:
        logger.warning("%s", sweep_error)

    objects_dir = root_path / "objects"
    objects_deleted = 0
    objects_bytes_freed = 0

    if not sweep_error and objects_dir.is_dir() and not objects_dir.is_symlink():
        with os.scandir(objects_dir) as shards:
            for shard in shards:
                if shard.name.startswith(".") or shard.is_symlink():
                    continue
                if not shard.is_dir(follow_symlinks=False):
                    continue
                shard_empty = True
                with os.scandir(shard.path) as objs:
                    for obj in objs:
                        if obj.is_symlink():
                            continue
                        if obj.name.startswith(".trash-"):
                            try:
                                st = os.stat(obj.path, follow_symlinks=False)
                                if stat.S_ISREG(st.st_mode) and time.time() - st.st_mtime > ONE_HOUR_SECONDS:
                                    if not dry_run:
                                        os.unlink(obj.path)
                                    objects_deleted += 1
                                    objects_bytes_freed += st.st_size
                                else:
                                    shard_empty = False
                            except OSError:
                                shard_empty = False
                            continue
                        if obj.name.startswith("."):
                            continue
                        sha = obj.name
                        if not valid_sha(sha):
                            continue
                        if sha in referenced_shas:
                            shard_empty = False
                            continue
                        try:
                            st = obj.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        age = now - st.st_mtime
                        if age > ONE_HOUR_SECONDS:
                            try:
                                size = _sweep_object(Path(obj.path), dry_run=dry_run)
                                if size is None:
                                    shard_empty = False
                                    continue
                            except OSError:
                                shard_empty = False
                                continue
                            objects_deleted += 1
                            objects_bytes_freed += size
                        else:
                            shard_empty = False

                if not dry_run and shard_empty:
                    try:
                        os.rmdir(shard.path)
                    except OSError:
                        pass

    if not dry_run and (runs_deleted > 0 or objects_deleted > 0):
        usage_file = root_path / "usage.json"
        if usage_file.is_file():
            try:
                usage_file.unlink()
            except OSError:
                pass

    return {
        "runs_deleted": runs_deleted,
        "runs_bytes_freed": runs_bytes_freed,
        "objects_deleted": objects_deleted,
        "objects_bytes_freed": objects_bytes_freed,
        "total_bytes_freed": runs_bytes_freed + objects_bytes_freed,
        "deleted_runs": sorted(selected_ids),
        "in_use": sorted(in_use),
        "sweep_error": sweep_error,
    }


_usage_lock = threading.Lock()
_usage_refreshing = set()


def _write_usage(root, data):
    atomic_write(root / "usage.json", json.dumps(data).encode("utf-8") + b"\n")


def _refresh_usage(root):
    try:
        # Serialize refresh and finish adjustments so neither overwrites the other.
        with _usage_lock:
            sizes = {meta["run_id"]: _cheap_total_bytes(run_dir(root, meta["run_id"]))
                     for meta in list_runs(root)}
            _write_usage(root, {"bytes": _cheap_total_bytes(root), "computed_at": time.time(),
                                "runs": sizes})
    except Exception:
        logger.debug("could not refresh store usage", exc_info=True)
    finally:
        _usage_refreshing.discard(root)


def record_finished_usage(root, run_id):
    """Add this run's size delta to the cache, without walking the store."""
    try:
        with _usage_lock:
            try:
                data = json.loads(read_file_nofollow(root / "usage.json"))
            except FileNotFoundError:
                data = {"bytes": 0, "computed_at": 0, "runs": {}}
            sizes = data.setdefault("runs", {})
            size = _cheap_total_bytes(run_dir(root, run_id))
            data["bytes"] += size - sizes.get(run_id, 0)
            sizes[run_id] = size
            _write_usage(root, data)
    except Exception:
        logger.debug("could not update store usage", exc_info=True)


def check_budget(root) -> str | None:
    """Read cached usage; refresh missing/stale data in a daemon, warn next time."""
    root_path = resolve_root(root)
    try:
        data = json.loads(read_file_nofollow(root_path / "usage.json"))
        total_bytes = int(data["bytes"])
        fresh = time.time() - float(data["computed_at"]) <= ONE_DAY_SECONDS
    except Exception:
        total_bytes, fresh = 0, False
    if not fresh:
        if root_path not in _usage_refreshing:
            _usage_refreshing.add(root_path)
            threading.Thread(target=_refresh_usage, args=(root_path,),
                             name="agenticml-usage", daemon=True).start()
        return None

    max_bytes = DEFAULT_MAX_BYTES
    try:
        max_bytes = int(os.environ.get("AGENTICML_MAX_BYTES", max_bytes))
    except (ValueError, TypeError):
        pass
    if total_bytes > max_bytes:
        return (
            f"AgenticML local store usage ({_format_bytes(total_bytes)}) exceeds budget "
            f"({_format_bytes(max_bytes)}). Run 'agenticml du', 'agenticml gc', or 'agenticml sync --prune'."
        )
    return None
