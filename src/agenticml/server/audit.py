"""Append-only audit events and bounded, newest-first operator reports."""
from __future__ import annotations

import fcntl
import getpass
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from agenticml.store import layout

logger = logging.getLogger("agenticml.server.audit")
_CHUNK_SIZE = 64 * 1024
_MAX_LINE_SIZE = 1024 * 1024
_COLUMNS = ("ts", "actor", "via", "action", "target", "client")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")


def cli_actor():
    """Attribute local commands even when the OS user cannot be resolved."""
    try:
        return "cli:" + getpass.getuser()
    except Exception:
        return "cli:unknown"


def _truncate(value):
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate(item) for item in value]
    return value


class AuditLog:
    """Best-effort writes; each open participates in the advisory file lock."""

    def __init__(self, server_dir):
        self.server_dir = Path(server_dir)
        self.path = self.server_dir / "audit.jsonl"
        self._log_lock = threading.Lock()
        self._error_at = None

    def record(self, actor, via, action, target=None, client=None, detail=None):
        try:
            event = {"ts": layout.format_timestamp(), "actor": _truncate(actor),
                     "via": _truncate(via), "action": _truncate(action),
                     "target": _truncate(target), "client": _truncate(client),
                     "detail": _truncate(detail)}
            line = (json.dumps(event, ensure_ascii=True) + "\n").encode("utf-8")
            # NONBLOCK prevents a planted FIFO from blocking before fstat.
            flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC | os.O_NONBLOCK)
            fd = os.open(self.path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("audit.jsonl is not a regular file")
                fcntl.flock(fd, fcntl.LOCK_EX)
                size = os.fstat(fd).st_size
                try:
                    remaining = memoryview(line)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("short audit write")
                        remaining = remaining[written:]
                    os.fsync(fd)
                except Exception:
                    try:
                        os.ftruncate(fd, size)
                    except OSError:
                        pass
                    raise
            finally:
                os.close(fd)
        except Exception as exc:
            with self._log_lock:
                now = time.monotonic()
                if self._error_at is None or now - self._error_at >= 60:
                    self._error_at = now
                    logger.error("could not write audit.jsonl (%s, errno=%s)",
                                 type(exc).__name__, getattr(exc, "errno", None))


def _reverse_lines(file):
    """Yield (bytes, newline-terminated) from a snapshot of the file's size.

    Oversized lines yield an empty, incomplete marker so tail counts each once.
    Fragments are joined only at line boundaries, keeping long lines linear.
    """
    size = position = file.seek(0, os.SEEK_END)
    pending, pending_size = [], 0
    oversized = False
    at_end = True
    while position:
        length = min(position, _CHUNK_SIZE)
        position -= length
        file.seek(position)
        parts = file.read(length).split(b"\n")
        for index in range(len(parts) - 1, -1, -1):
            fragment = parts[index]
            if not oversized:
                pending_size += len(fragment)
                if pending_size > _MAX_LINE_SIZE:
                    pending.clear()
                    oversized = True
                else:
                    pending.append(fragment)
            if index > 0:
                if oversized:
                    yield b"", False
                elif pending_size or not at_end:
                    yield b"".join(reversed(pending)), not at_end
                pending, pending_size = [], 0
                oversized = False
                at_end = False
    if size:
        yield (b"", False) if oversized else (b"".join(reversed(pending)), not at_end)


def tail(server_dir, limit=50, before=None):
    """Read at most 1000 events, skipping malformed or incomplete lines.

    The skipped count covers lines examined while filling this page. A cursor
    excludes events whose timestamp is equal to or newer than ``before``.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("invalid limit")
    limit = min(limit, 1000)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(Path(server_dir) / "audit.jsonl", flags)
    except FileNotFoundError:
        return [], 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("audit.jsonl is not a regular file")
        file = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    events, skipped = [], 0
    with file:
        for line, complete in _reverse_lines(file):
            try:
                if not complete:
                    raise ValueError("torn audit line")
                event = json.loads(line.decode("utf-8"))
                if not isinstance(event, dict) or not isinstance(event.get("ts"), str):
                    raise ValueError("invalid audit event")
            except (ValueError, RecursionError):
                skipped += 1
                continue
            if before is not None and event["ts"] >= before:
                continue
            events.append(event)
            if len(events) >= limit:
                break
    return events, skipped


def format_table(events):
    """Render the CLI columns; an empty log produces no table."""
    def cell(value):
        value = "-" if value is None or value == "" else str(value)
        return "".join(char if char.isprintable() else json.dumps(char)[1:-1]
                       for char in value)

    rows = [[cell(event.get(key)) for key in _COLUMNS] for event in events]
    if not rows:
        return ""
    rows.insert(0, list(_COLUMNS))
    widths = [max(len(row[index]) for row in rows) for index in range(len(_COLUMNS))]
    return "\n".join("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
                     for row in rows)


def cli_main(data, limit, as_json):
    from .app import resolve_data_dir

    try:
        events, _ = tail(resolve_data_dir(data) / "server", limit=limit)
    except (OSError, ValueError) as exc:
        print(f"agenticml server: {exc}", file=sys.stderr)
        return 1
    output = json.dumps(events, ensure_ascii=True) if as_json else format_table(events)
    try:
        if output:
            print(output)
    except BrokenPipeError:
        return 0
    return 0


def handle(webauth, handler):
    from .users import allowed

    headers = {"Cache-Control": "no-store"}
    if not allowed(handler.principal, "audit"):
        handler._json({"error": "forbidden"}, 403, headers)
        return
    if handler.command != "GET":
        handler.close_connection = True
        handler._json({"error": "method not allowed"}, 405, headers)
        return
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    try:
        limits = query.get("limit", ["50"])
        if len(limits) != 1 or re.fullmatch(r"[0-9]+", limits[0]) is None:
            raise ValueError("invalid limit")
        limit = int(limits[0])
        if limit < 1:
            raise ValueError("invalid limit")
    except ValueError:
        handler._json({"error": "invalid limit"}, 400, headers)
        return
    cursors = query.get("before", [])
    if len(cursors) > 1 or (cursors and _TIMESTAMP.fullmatch(cursors[0]) is None):
        handler._json({"error": "invalid before"}, 400, headers)
        return
    before = cursors[0] if cursors else None
    try:
        events, skipped = tail(webauth.keys.server_dir, limit=limit, before=before)
    except (OSError, ValueError):
        handler._json({"error": "audit log unavailable"}, 503, headers)
        return
    handler._json({"events": events, "skipped": skipped}, 200, headers)
