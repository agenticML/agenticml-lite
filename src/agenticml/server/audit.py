"""Append-only audit events and bounded, newest-first operator reports."""
from __future__ import annotations

import getpass
import json
import logging
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from agenticml.store import layout

logger = logging.getLogger("agenticml.server.audit")
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
    """Best-effort, rate-limited database audit writes."""

    def __init__(self, server_dir, *, db=None, workspace_id):
        from .db import for_server_dir
        from .db.repos import AuditRepo
        self.server_dir = Path(server_dir)
        self.db = db if db is not None else for_server_dir(self.server_dir)
        self.repo = AuditRepo(self.db, workspace_id)
        self._log_lock = threading.Lock()
        self._error_at = None

    def record(self, actor, via, action, target=None, client=None, detail=None):
        try:
            event = {"ts": layout.format_timestamp(), "actor": _truncate(actor),
                     "via": _truncate(via), "action": _truncate(action),
                     "target": _truncate(target), "client": _truncate(client),
                     "detail": _truncate(detail)}
            self.repo.insert(event)
        except Exception as exc:
            with self._log_lock:
                now = time.monotonic()
                if self._error_at is None or now - self._error_at >= 60:
                    self._error_at = now
                    logger.error("could not write audit event (%s, errno=%s)",
                                 type(exc).__name__, getattr(exc, "errno", None))


def tail(server_dir, limit=50, before=None):
    """Return newest events from the database and a zero skipped count."""
    from .db import for_server_dir
    from .db.repos import AuditRepo
    if type(limit) is not int or limit < 1:
        raise ValueError("invalid limit")
    db = for_server_dir(server_dir)
    from .workspaces import default_workspace_id
    return AuditRepo(db, default_workspace_id(db)).tail(min(limit, 1000), before), 0


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


def cli_main(data, limit, as_json, *, database=None, workspace="default", server=False):
    from .app import resolve_data_dir
    from .db import DatabaseError, for_server_dir
    from .db.repos import AuditRepo
    from .app import prepare_data_dir

    try:
        if type(limit) is not int or limit < 1:
            raise ValueError("invalid limit")
        directory = resolve_data_dir(data)
        prepare_data_dir(directory)
        db = for_server_dir(directory / 'server', database)
        from .db.workspaces import WorkspaceRepo
        wid = None if server else WorkspaceRepo(db).require(workspace)['id']
        events = AuditRepo(db, wid).tail(min(limit, 1000))
    except (OSError, ValueError, DatabaseError) as exc:
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
    from .db import DatabaseError
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
        events = handler.server.audit.repo.tail(min(limit, 1000), before)
        skipped = 0
    except (OSError, ValueError, DatabaseError):
        handler._json({"error": "audit log unavailable"}, 503, headers)
        return
    handler._json({"events": events, "skipped": skipped}, 200, headers)
