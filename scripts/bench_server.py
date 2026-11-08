#!/usr/bin/env python3
"""Benchmark agenticml server ingest with concurrent sync clients.

Stdlib only (plus the package). The server runs in-process on 127.0.0.1:0.
Clients run in separate processes so --fsync-delay-ms slows only the server.

    python scripts/bench_server.py [--data DIR] [--clients N] [--runs R]
        [--steps S] [--metrics M] [--passes P] [--objects K]
        [--object-bytes B] [--fsync-delay-ms D] [--quick]
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import os
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty

from requests.adapters import HTTPAdapter

_BATCH = 1000
_QUICK = dict(clients=1, runs=1, steps=8, metrics=2, passes=2, objects=2, object_bytes=256)
_DEFAULT = dict(clients=1, runs=4, steps=4000, metrics=8, passes=3, objects=50, object_bytes=65536)
_HEADER_NAME = "metric"
_HEADER_VALUE = "value"
_SHARED_SOURCE_WARNING = (
    "warning: cannot bind distinct 127.0.0.x source addresses; "
    "falling back to 127.0.0.1 for every client. The login throttle may "
    "return 429 for more than about 6 simultaneous requests from one "
    "address; client retries are enabled."
)


def _positive_int(raw):
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _nonneg_int(raw):
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _nonneg_float(raw):
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return value


def _pct(samples, p):
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = math.ceil(p / 100.0 * len(ordered)) - 1
    return ordered[min(len(ordered) - 1, max(0, rank))]


def _fmt_s(value):
    return "n/a" if value is None else f"{value:.3f} s"


def _fmt_ms(value):
    return "n/a" if value is None else f"{value * 1e3:.2f} ms"


def _cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _cpu_model():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def _disk_for(path):
    try:
        out = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE,SOURCE,TARGET", "-T", str(path)],
            capture_output=True, text=True, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return " ".join(out.stdout.split())
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["lsblk", "-d", "-o", "NAME,ROTA,TYPE,SIZE,MODEL"],
            capture_output=True, text=True, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return " | ".join(line.strip() for line in out.stdout.splitlines() if line.strip())
    except OSError:
        pass
    return "unknown"


def _git_version():
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return out.stdout.strip()
    except OSError:
        pass
    return "git not found"


def _install_fsync_delay(delay_ms):
    if delay_ms <= 0:
        return
    real = os.fsync
    pause = delay_ms / 1000.0

    def delayed(fd):
        time.sleep(pause)
        return real(fd)

    os.fsync = delayed


def _can_bind(addr):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((addr, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _source_addresses(n):
    wanted = [f"127.0.0.{i + 1}" for i in range(n)]
    if all(_can_bind(addr) for addr in wanted):
        return wanted, False
    print(_SHARED_SOURCE_WARNING, file=sys.stderr, flush=True)
    return ["127.0.0.1"] * n, True


def _purge_bench(data: Path):
    runs = data / "store" / "runs"
    if runs.is_dir():
        for entry in runs.iterdir():
            if entry.name.startswith("bench-"):
                shutil.rmtree(entry, ignore_errors=True)
    server_dir = data / "server"
    if not (server_dir / "keys.json").is_file():
        return
    from agenticml.server.db import for_server_dir
    from agenticml.server.keys import KeyStore
    from agenticml.server.workspaces import default_workspace_id

    db = for_server_dir(server_dir)
    store = KeyStore(server_dir, db=db, workspace_id=default_workspace_id(db))
    for record in list(store.list()):
        if record.name.startswith("bench-"):
            store.revoke(record.name)


class _LoopbackAdapter(HTTPAdapter):
    """Bind each client to its own 127.0.0.x so the login throttle (keyed by
    address, FREE_FAILURES=5) does not 429 concurrent loopback processes.
    """

    def __init__(self, source, **kwargs):
        self._source = (source, 0)
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["source_address"] = self._source
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _write_steps(writer, start, count, n_metrics):
    keys = [f"m{i}" for i in range(n_metrics)]
    for offset in range(0, count, _BATCH):
        stop = min(offset + _BATCH, count)
        writer.write_scalars(
            [(start + step, {key: float(start + step) for key in keys}) for step in range(offset, stop)]
        )


def _object_payload(client_index, obj_i, size):
    prefix = f"{client_index}:{obj_i}:".encode()
    if size <= len(prefix):
        return prefix[:size]
    return prefix + os.urandom(size - len(prefix))


def _add_objects(writers, tmp, client_index, n_objects, object_bytes):
    if n_objects <= 0 or not writers:
        return
    blob_dir = tmp / "blobs"
    blob_dir.mkdir(exist_ok=True)
    for obj_i in range(n_objects):
        path = blob_dir / f"obj-{obj_i}.bin"
        path.write_bytes(_object_payload(client_index, obj_i, object_bytes))
        writers[obj_i % len(writers)].add_artifact(path, f"obj-{obj_i}", "file", {}, copy=True)


def _sync_all(root, host, client):
    from agenticml.store import RunReader, list_runs
    from agenticml.sync import SyncResult, sync_run

    total = 0
    errors = []
    for meta in list_runs(root):
        result = SyncResult(meta["run_id"])
        sync_run(RunReader(root, meta["run_id"]), host, client, result)
        if result.failed or result.status == "skipped":
            errors.append(f"{meta['run_id']}: {result.status}")
        total += result.bytes
    if errors:
        raise RuntimeError("sync failed: " + "; ".join(errors))
    return total


def _client_worker(client_index, host, api_key, runs, steps, metrics, passes,
                   n_objects, object_bytes, source, max_retries, client_dir,
                   barrier, queue):
    from agenticml.client import Client
    from agenticml.store import RunWriter

    original_commit = Client.commit
    original_put = Client.put_object
    writers = []
    tmp = Path(client_dir)
    try:
        root = tmp / "store"
        root.mkdir()
        for run_i in range(runs):
            writer = RunWriter.create(
                root,
                run_id=f"bench-c{client_index}-r{run_i}",
                meta={"project": "bench", "name": f"c{client_index}-r{run_i}"},
            )
            _write_steps(writer, 0, steps, metrics)
            writers.append(writer)
        _add_objects(writers, tmp, client_index, n_objects, object_bytes)

        commit_s = []
        object_s = []

        def timed_commit(self, *args, **kwargs):
            t0 = time.perf_counter()
            try:
                return original_commit(self, *args, **kwargs)
            finally:
                commit_s.append(time.perf_counter() - t0)

        def timed_put_object(self, *args, **kwargs):
            t0 = time.perf_counter()
            try:
                return original_put(self, *args, **kwargs)
            finally:
                object_s.append(time.perf_counter() - t0)

        Client.commit = timed_commit
        Client.put_object = timed_put_object
        pass_s = []
        pass_bytes = []
        with Client(host, api_key, timeout=120, max_retries=max_retries) as client:
            adapter = _LoopbackAdapter(source, max_retries=0)
            client.session.mount("http://", adapter)
            client.session.mount("https://", adapter)
            barrier.wait()
            for pass_i in range(passes):
                if pass_i:
                    for writer in writers:
                        _write_steps(writer, pass_i * steps, steps, metrics)
                t0 = time.perf_counter()
                uploaded = _sync_all(root, host, client)
                pass_s.append(time.perf_counter() - t0)
                pass_bytes.append(uploaded)
        queue.put({
            "client": client_index,
            "pass_s": pass_s,
            "pass_bytes": pass_bytes,
            "commit_s": commit_s,
            "object_s": object_s,
        })
    except Exception as exc:
        queue.put({"client": client_index, "error": f"{type(exc).__name__}: {exc}"})
        try:
            barrier.abort()
        except Exception:
            pass
    finally:
        Client.commit = original_commit
        Client.put_object = original_put
        for writer in writers:
            try:
                writer.close()
            except Exception:
                pass


def _rows(args, results, wall_s, cpu_s):
    initials = [item["pass_s"][0] for item in results]
    initial_bytes = sum(item["pass_bytes"][0] for item in results)
    incrementals = [dt for item in results for dt in item["pass_s"][1:]]
    commits = [dt for item in results for dt in item["commit_s"]]
    puts = [dt for item in results for dt in item["object_s"]]
    mb_s = (initial_bytes / 1e6 / wall_s) if wall_s else None
    objects_s = (len(puts) / wall_s) if wall_s and puts else None
    delay = args.fsync_delay_ms
    delay_label = f"{delay:g} (modelled)" if delay else "0 (local disk)"
    rows = [
        ("clients", str(args.clients)),
        ("runs / steps / metrics / passes",
         f"{args.runs} / {args.steps} / {args.metrics} / {args.passes}"),
        ("objects / object-bytes", f"{args.objects} / {args.object_bytes}"),
        ("fsync-delay-ms", delay_label),
        ("initial full sync wall (aggregate)", _fmt_s(wall_s)),
        ("initial full sync throughput",
         "n/a" if not mb_s else f"{mb_s:.2f} MB/s ({initial_bytes:,} bytes)"),
        ("incremental sync p50 (per client pass)", _fmt_s(_pct(incrementals, 50))),
        ("incremental sync p95 (per client pass)", _fmt_s(_pct(incrementals, 95))),
        ("commit latency p50", _fmt_ms(_pct(commits, 50))),
        ("commit latency p95", _fmt_ms(_pct(commits, 95))),
        ("object PUT p50", _fmt_ms(_pct(puts, 50))),
        ("object PUT p95", _fmt_ms(_pct(puts, 95))),
        ("objects/s (aggregate, initial)",
         "n/a" if objects_s is None else f"{objects_s:.2f} ({len(puts)} objects)"),
        ("server CPU (sync window)", _fmt_s(cpu_s)),
        ("per-client initial (s)", " ".join(f"{dt:.3f}" for dt in initials)),
    ]
    return rows


def _client_errors(queue, n, timeout=30):
    found = []
    deadline = time.monotonic() + timeout
    while len(found) < n and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            item = queue.get(timeout=max(0.05, remaining))
        except Empty:
            break
        found.append(item)
        if "error" in item and "BrokenBarrierError" not in str(item["error"]):
            break
    real = [item for item in found if "error" in item and "BrokenBarrierError" not in str(item["error"])]
    pick = (real or found or [None])[0]
    if pick and "error" in pick:
        return f"client {pick.get('client')}: {pick['error']}"
    return "a client failed before the start barrier"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=None,
                    help="server data directory (default: temp dir, deleted at exit); "
                         "removes bench-* runs and keys from DIR on the way out")
    ap.add_argument("--clients", type=_positive_int, default=None)
    ap.add_argument("--runs", type=_positive_int, default=None)
    ap.add_argument("--steps", type=_positive_int, default=None)
    ap.add_argument("--metrics", type=_positive_int, default=None)
    ap.add_argument("--passes", type=_positive_int, default=None)
    ap.add_argument("--objects", type=_nonneg_int, default=None,
                    help="distinct copy=True artifacts per client, split across that "
                         "client's runs (default 50; --quick: 2; 0 disables)")
    ap.add_argument("--object-bytes", type=_positive_int, default=None,
                    help="size of each artifact in bytes (default 65536; --quick: 256)")
    ap.add_argument("--fsync-delay-ms", type=_nonneg_float, default=0.0,
                    help="sleep this many ms before each os.fsync in the server process")
    ap.add_argument("--quick", action="store_true", help="tiny sizes for a smoke test")
    args = ap.parse_args()
    preset = _QUICK if args.quick else _DEFAULT
    for name, value in preset.items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    if args.clients > 254:
        ap.error("--clients must be <= 254 (loopback source addresses 127.0.0.1–254)")

    own_data = args.data is None
    data = Path(tempfile.mkdtemp(prefix="agenticml-bench-server-")) if own_data else args.data.expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    barrier = ctx.Barrier(args.clients + 1, timeout=600)
    procs = []
    client_dirs = []
    server = None
    try:
        if not own_data:
            _purge_bench(data)
        _install_fsync_delay(args.fsync_delay_ms)
        from agenticml.server.app import make_server, prepare_data_dir
        from agenticml.server.db import for_server_dir
        from agenticml.server.keys import KeyStore
        from agenticml.server.workspaces import default_workspace_id

        prepare_data_dir(data)
        # A durable admin key so make_server does not arm/log the one-time setup token.
        db = for_server_dir(data / "server")
        KeyStore(data / "server", db=db, workspace_id=default_workspace_id(db)).create(
            "bench-admin", scope="admin")
        server = make_server(data, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        host = f"http://127.0.0.1:{server.server_address[1]}"
        secrets = []
        for i in range(args.clients):
            _record, secret = server.keys.create(f"bench-c{i}", scope="sync")
            secrets.append(secret)
        sources, shared = _source_addresses(args.clients)
        max_retries = 3 if shared else 0
        for i, secret in enumerate(secrets):
            tmp = Path(tempfile.mkdtemp(prefix=f"agenticml-bench-client-{i}-"))
            client_dirs.append(tmp)
            proc = ctx.Process(
                target=_client_worker,
                args=(i, host, secret, args.runs, args.steps, args.metrics, args.passes,
                      args.objects, args.object_bytes, sources[i], max_retries, str(tmp),
                      barrier, queue),
            )
            proc.start()
            procs.append(proc)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            raise RuntimeError(_client_errors(queue, args.clients)) from None
        cpu0 = _cpu_seconds()
        t0 = time.perf_counter()
        results = []
        for _ in procs:
            item = queue.get(timeout=600)
            if "error" in item:
                raise RuntimeError(f"client {item.get('client')}: {item['error']}")
            results.append(item)
        wall_s = time.perf_counter() - t0
        cpu_s = _cpu_seconds() - cpu0
        for proc in procs:
            proc.join(timeout=30)
            if proc.exitcode not in (0, None):
                raise RuntimeError(f"client process exited {proc.exitcode}")
        results.sort(key=lambda item: item["client"])
        # Clients start together; aggregate initial wall is the slowest pass 0.
        initial_wall = max(item["pass_s"][0] for item in results)
        rows = _rows(args, results, initial_wall, cpu_s)
        print(f"# python {sys.version.split()[0]}  {_git_version()}", flush=True)
        print(f"# cpu {_cpu_model()}", flush=True)
        print(f"# disk {_disk_for(data)}", flush=True)
        print(f"# data {data}  host {host}  sync-window {_fmt_s(wall_s)}", flush=True)
        width = max(len(_HEADER_NAME), max(len(name) for name, _value in rows))
        print(f"{_HEADER_NAME.ljust(width)}  {_HEADER_VALUE}", flush=True)
        for name, value in rows:
            print(f"{name.ljust(width)}  {value}", flush=True)
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
        for tmp in client_dirs:
            shutil.rmtree(tmp, ignore_errors=True)
        if server is not None:
            server.shutdown()
            server.server_close()
        if own_data:
            shutil.rmtree(data, ignore_errors=True)
        else:
            try:
                _purge_bench(data)
            except Exception:
                pass


if __name__ == "__main__":
    main()
