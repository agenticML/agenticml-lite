"""Read-only HTTP dashboard over the local v1 store (stdlib only)."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import stat
import threading
import traceback
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from agenticml.store import layout, reader

_STATIC = Path(__file__).with_name("static")
_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "charts.js": "text/javascript; charset=utf-8",
    "code.js": "text/javascript; charset=utf-8",
}
_OBJECT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
                 "image/bmp", "image/x-icon", "text/plain", "application/json"}
_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SERIES_LIMIT = 200
_CONFIG_LIMIT = 50
_CHUNK = 64 * 1024
_CONNECTION_TIMEOUT = 60
_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _text(value):
    return value if isinstance(value, str) else ""


def _tags(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _summary(value, limit=None):
    """String-keyed scalars, in insertion order. ``limit`` caps how many are kept."""
    if not isinstance(value, dict):
        return {}
    kept = {}
    for key, item in value.items():
        if isinstance(key, str) and (item is None or isinstance(item, (str, int, float, bool))):
            kept[key] = item
            if limit is not None and len(kept) >= limit:
                break
    return kept


def _unique(values):
    return list(dict.fromkeys(values))


def _json_safe(value):
    # The store intentionally preserves NaN/Inf; JSON clients need null gaps.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _canonical_host(value: str) -> str:
    """Host header with the port removed, lowercased, IPs in canonical form."""
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return text
        text = text[1:end]
    elif text.count(":") == 1:
        name, port = text.rsplit(":", 1)
        if port.isdigit():
            text = name
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


def _loopback_bind(value) -> bool:
    text = _canonical_host(str(value))
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _host_permitted(host_header: str, bind_host, bound_address) -> bool:
    """True for loopback names, plus bind address, hostname, and IP literals off loopback."""
    host = _canonical_host(host_header)
    if host in _LOOPBACK_HOSTS:
        return True
    if not host or (_loopback_bind(bind_host) and _loopback_bind(bound_address)):
        return False
    allowed = {
        _canonical_host(str(bind_host)),
        _canonical_host(str(bound_address)),
        _canonical_host(socket.gethostname()),
    }
    if host in allowed:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _open_regular_object(root, sha):
    """Open one object after rejecting anything that is not a regular file.

    ``open`` on a FIFO blocks the handler thread until another process writes.
    ``lstat`` is enough to answer 404 for those paths.
    """
    try:
        path = layout.object_path(root, sha)
    except (ValueError, OSError) as exc:
        raise FileNotFoundError(sha) from exc
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise FileNotFoundError(sha) from exc
    if not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError(sha)
    return layout.open_object(root, sha)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, root, host, port):
        self.root = layout.resolve_root(root)
        self.verbose = False
        self.bind_host = host
        self.readers = OrderedDict()
        self.reader_lock = threading.Lock()
        super().__init__((host, port), DashboardHandler)

    def handle_error(self, request, client_address):
        if self.verbose:
            super().handle_error(request, client_address)

    def get_request(self):
        request, client_address = super().get_request()
        try:
            request.settimeout(_CONNECTION_TIMEOUT)
        except OSError:
            pass
        return request, client_address

    @contextmanager
    def run_reader(self, run_id):
        # RunReader is thread-safe. Lock only the LRU map, not the read.
        with self.reader_lock:
            if run_id not in self.readers:
                self.readers[run_id] = reader.RunReader(self.root, run_id)
            self.readers.move_to_end(run_id)
            while len(self.readers) > 64:
                self.readers.popitem(last=False)
            current = self.readers[run_id]
        yield current

    def cached_reader(self, run_id):
        """The LRU entry for ``run_id``, or None.

        Does not insert, reorder, or evict. List polling uses this so a run
        that is not already open for charts cannot push chart readers out.
        """
        with self.reader_lock:
            return self.readers.get(run_id)


class IPv6DashboardServer(DashboardServer):
    address_family = socket.AF_INET6


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AgenticML"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _host_allowed(self) -> bool:
        values = self.headers.get_all("Host")
        if not values or len(values) != 1:
            return False
        return _host_permitted(values[0], self.server.bind_host, self.server.server_address[0])

    def _begin(self, status, content_type, length, headers=None):
        self._started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, body, content_type, status=200, headers=None):
        self._begin(status, content_type, len(body), headers)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _stream(self, handle, content_type, headers=None):
        try:
            length = os.fstat(handle.fileno()).st_size
            self._begin(200, content_type, length, headers)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            if remaining:
                self.close_connection = True
        finally:
            handle.close()

    def _error(self, payload, status):
        if getattr(self, "_started", False):
            self.close_connection = True
            return
        try:
            self._json(payload, status)
        except _DISCONNECT:
            self.close_connection = True

    def _json(self, value, status=200, headers=None):
        body = json.dumps(_json_safe(value), separators=(",", ":"), allow_nan=False).encode()
        extra = {"Cache-Control": "no-store", "Vary": "Accept-Encoding", **(headers or {})}
        encodings = {}
        for part in self.headers.get("Accept-Encoding", "").lower().split(","):
            fields = [x.strip() for x in part.split(";")]
            try:
                encodings[fields[0]] = next((float(x[2:]) for x in fields[1:] if x.startswith("q=")), 1)
            except ValueError:
                encodings[fields[0]] = 0
        if encodings.get("gzip", encodings.get("*", 0)) > 0 and len(body) > 1024:
            body = gzip.compress(body)
            extra["Content-Encoding"] = "gzip"
        self._send(body, "application/json", status, extra)

    def _method_not_allowed(self):
        self._started = False
        try:
            if not self._host_allowed():
                self._error({"error": "forbidden host"}, 403)
                return
            self.close_connection = True
            self._json({"error": "method not allowed"}, 405, {"Allow": "GET"})
        except _DISCONNECT:
            self.close_connection = True

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _method_not_allowed

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def do_GET(self):
        self._started = False
        try:
            if not self._host_allowed():
                self._error({"error": "forbidden host"}, 403)
                return
            self._get()
        except _DISCONNECT:
            self.close_connection = True
        except ValueError:
            self._error({"error": "invalid request or store data"}, 400)
        except (FileNotFoundError, NotADirectoryError):
            self._error({"error": "not found"}, 404)
        except Exception:
            if self.server.verbose:
                traceback.print_exc()
            self._error({"error": "internal error"}, 500)

    def _list_last_step(self, run_id):
        """Latest scalar step, or None when the run has none or cannot be read.

        One failed run must not drop the rest of the list. A reader already
        in the chart LRU is reused; every other run is a short uncached read.
        This never inserts or evicts, so a list poll cannot drop incremental
        overview state from readers the charts are still using.
        """
        if not isinstance(run_id, str) or not run_id:
            return None
        try:
            current = self.server.cached_reader(run_id)
            if current is not None:
                step = current.last_step()
            else:
                step = reader.read_last_step(self.server.root, run_id)
        except Exception:
            return None
        if isinstance(step, bool) or not isinstance(step, int):
            return None
        return step

    def _get(self):
        url = urlsplit(self.path)
        path = unquote(url.path, errors="strict")
        query = parse_qs(url.query, keep_blank_values=True)
        arg = lambda key, default=None: query.get(key, [default])[0]
        if path == "/api/runs":
            fields = ("run_id", "project", "name", "created_at", "finished_at", "tags", "group")
            runs = []
            for meta in reader.list_runs(self.server.root):
                if not isinstance(meta, dict):
                    continue
                row = {k: meta.get(k) for k in fields}
                row["name"] = _text(meta.get("name"))
                row["project"] = _text(meta.get("project"))
                row["tags"] = _tags(meta.get("tags"))
                row["status"] = meta.get("effective_status", meta.get("status"))
                row["summary"] = _summary(meta.get("summary"))
                row["config"] = _summary(meta.get("config"), limit=_CONFIG_LIMIT)
                # meta.json and the scalars index do not record a step. The last
                # _time.bin record is one seek, not a scan of the series files.
                # Do not route this through run_reader: inserting every row
                # evicts chart readers from the 64-entry LRU.
                row["last_step"] = self._list_last_step(meta.get("run_id"))
                runs.append(row)
            return self._json(runs)
        if path.startswith("/api/runs/"):
            parts = path[len("/api/runs/"):].split("/")
            run_id = parts[0]
            if not layout.valid_run_id(run_id):
                raise ValueError("invalid run id")
            if len(parts) > 2:
                raise FileNotFoundError()
            route = parts[1] if len(parts) == 2 else ""
            run_path = self.server.root / "runs" / run_id
            if not run_path.resolve().is_relative_to(self.server.root / "runs"):
                raise FileNotFoundError()
            with self.server.run_reader(run_id) as run:
                if route == "":
                    result = dict(run.meta())
                    result.update(keys=run.keys(), artifacts=run.artifacts(), code_manifest_length=len(run.code_manifest()))
                elif route == "scalars":
                    limit = max(10, min(5000, int(arg("max_points", "1000"))))
                    start, end = arg("start"), arg("end")
                    start = int(start) if start else None
                    end = int(end) if end else None
                    requested = _unique(query.get("key", []))
                    if len(requested) > _SERIES_LIMIT:
                        raise ValueError("too many series keys")
                    available = set(run.keys())
                    truncated = False
                    if requested:
                        chosen = requested
                    else:
                        chosen = sorted(available)
                        truncated = len(chosen) > _SERIES_LIMIT
                        if truncated:
                            chosen = chosen[:_SERIES_LIMIT]
                    series = {}
                    for key in chosen:
                        if key not in available:
                            continue
                        try:
                            series[key] = run.series(key, max_points=limit, start=start, end=end)
                        except KeyError:
                            continue
                    result = {"series": series, "truncated": truncated}
                elif route == "time":
                    result = run.time_index()
                elif route == "media":
                    selected = arg("key") or None
                    result = run.media(key=selected)
                    if selected is None:
                        if isinstance(result, dict):
                            result = {
                                key: value for key, value in result.items() if isinstance(value, list)
                            }
                        else:
                            result = {}
                elif route == "code":
                    result = run.code_manifest()
                else:
                    raise FileNotFoundError()
            return self._json(result)
        if path.startswith("/api/objects/"):
            sha = path[len("/api/objects/"):]
            if not layout.valid_sha(sha):
                raise ValueError("invalid sha")
            hint = arg("type", "application/octet-stream").lower()
            # HTML, XML and SVG snapshots must never become active documents.
            kind = "text/plain" if hint.startswith("text/") else hint
            if kind not in _OBJECT_TYPES:
                kind = "application/octet-stream"
            return self._stream(_open_regular_object(self.server.root, sha), kind,
                                headers={"Cache-Control": "immutable, max-age=31536000"})
        name = "index.html" if path == "/" else path[len("/static/"):] if path.startswith("/static/") else ""
        if name not in _FILES:
            raise FileNotFoundError()
        file = _STATIC / name
        body = file.read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if self.headers.get("If-None-Match") == etag:
            return self._send(b"", _FILES[name], 304, headers)
        return self._send(body, _FILES[name], headers=headers)


def _browser_host(address) -> str:
    text = str(address)
    if ":" in text:
        return f"[{text}]"
    return text


def make_server(root, host="127.0.0.1", port=6006) -> ThreadingHTTPServer:
    """Construct a server; port=0 chooses a free port, server.verbose enables logs."""
    cls = IPv6DashboardServer if ":" in str(host) else DashboardServer
    return cls(root, host, port)


def serve(root, host="127.0.0.1", port=6006, open_browser=False) -> None:
    """Serve until Ctrl-C. No subprocesses, background services or writes."""
    if host not in _LOOPBACK_HOSTS:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            print("Warning: the UI has no authentication and exposes configs, env and code to the network.", flush=True)
    with make_server(root, host, port) as server:
        address, bound_port = server.server_address[:2]
        url = f"http://{_browser_host(address)}:{bound_port}"
        print(f"AgenticML UI: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
