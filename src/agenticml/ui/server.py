"""HTTP dashboard over the local v1 store (stdlib only).

The dashboard is read-only except for run notes.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import difflib
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import subprocess
import threading
import time
import traceback
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from agenticml.store import layout, notes, reader

_STATIC = Path(__file__).with_name("static")
_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "charts.js": "text/javascript; charset=utf-8",
    "code.js": "text/javascript; charset=utf-8",
    "codediff.js": "text/javascript; charset=utf-8",
    "notes.js": "text/javascript; charset=utf-8",
    "InstrumentSans.woff2": "font/woff2",
    "JetBrainsMono.woff2": "font/woff2",
}
_OBJECT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
                 "image/bmp", "image/x-icon", "text/plain", "application/json"}
_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SERIES_LIMIT = 200
_CONFIG_LIMIT = 50
_CHUNK = 64 * 1024
_CONNECTION_TIMEOUT = 60
_MAX_CONNECTIONS = 64
_HEADER_DEADLINE = 10.0
# A client that never reads blocks in sendall. When every slot is taken, a
# write blocked at least this long is dropped so a new request can run.
# Below capacity a slow download is left alone.
_WRITE_STALL = 2.0
_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
_LINEAGE_FILES = 500
_CODE_DIFF_MAX_BYTES = 1024 * 1024
_CODE_DIFF_MAX_LINES = 20000
_CODE_DIFF_AUTOJUNK_THRESHOLD = 25_000_000
_CODE_DIFF_TOTAL_BYTES = 16 * 1024 * 1024
_CODE_DIFF_TEXT_BYTES = 2 * 1024 * 1024
_CODE_DIFF_LINE_CHARS = 2000
_CODE_DIFF_TIMEOUT = 5
_CODE_DIFF_FILE_LINES = 2000
_CODE_DIFF_TOTAL_LINES = 20000
_CODE_DIFF_FILES = 500
_CODE_DIFF_CACHE_SIZE = 256
_GIT_TIMEOUT = 5
_GIT_MAX_BYTES = 2 * 1024 * 1024
_GIT_READ = 64 * 1024
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PROJECT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_INFERENCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# Larger than a note so a normal JSON body fits, small enough to bound a PUT.
_NOTE_PUT_MAX = 512 * 1024
# Note bodies are not evicted: cutting one off would drop an in-flight PUT.
# Cap how many can be read at once so they cannot occupy every handler slot.
_MAX_NOTE_PUTS = 4
# Absolute cap for receiving a note body. The socket timeout is per-read
# inactivity, so a client that keeps the stream moving would otherwise hold
# the handler forever. Past this deadline the PUT is 408 and is not written.
_NOTE_PUT_DEADLINE = 15


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


def _code_diff_result(**overrides):
    result = dict(added=None, deleted=None, binary=False, too_large=False,
                  hunks=[], patch_truncated=False, newline_change=False, approximate=False)
    result.update(overrides)
    return result


class _CodeDiffBudget:
    def __init__(self):
        self.bytes_left = _CODE_DIFF_TOTAL_BYTES
        self.deadline = time.monotonic() + _CODE_DIFF_TIMEOUT

    def exhausted(self):
        return self.bytes_left <= 0 or time.monotonic() >= self.deadline


def _code_pair_diff(root, base_sha, target_sha, byte_limit, line_limit, budget):
    """Diff bounded UTF-8 snapshots. Missing sides are empty, never objects."""
    result = _code_diff_result()
    blobs = []
    try:
        for sha in (base_sha, target_sha):
            if sha is None:
                blobs.append(b"")
            else:
                if budget.exhausted():
                    return _code_diff_result(patch_truncated=True)
                read_limit = min(byte_limit + 1, budget.bytes_left)
                with _open_regular_object(root, sha) as stream:
                    blob = stream.read(read_limit)
                budget.bytes_left -= len(blob)
                # Stop at the first oversized side; do not open the other one.
                if len(blob) > byte_limit:
                    return _code_diff_result(too_large=True)
                # A budget-limited read cannot establish EOF without more I/O.
                if len(blob) == read_limit and read_limit <= byte_limit:
                    return _code_diff_result(patch_truncated=True)
                lines = blob.count(b"\n") + bool(blob and not blob.endswith(b"\n"))
                if lines > _CODE_DIFF_MAX_LINES:
                    return _code_diff_result(too_large=True)
                blobs.append(blob)
    except (OSError, ValueError):
        result.update(too_large=True, error="snapshot unavailable")
        return result
    try:
        if any(b"\x00" in blob for blob in blobs):
            result["binary"] = True
            return result
        decoded = [blob.decode("utf-8") for blob in blobs]
    except UnicodeDecodeError:
        result["binary"] = True
        return result

    if budget.exhausted():
        return _code_diff_result(patch_truncated=True)
    # Only LF separates source lines. Include the terminator in matching so a
    # final-newline edit produces a replacement, while CR and Unicode separators
    # remain visible source text with their original line numbers.
    snapshots = []
    for text in decoded:
        lines = text.split("\n") if text else []
        terminated = text.endswith("\n")
        if terminated:
            lines.pop()
        snapshots.append([(line, index < len(lines) - 1 or terminated)
                          for index, line in enumerate(lines)])
    a_lines, b_lines = snapshots
    result["newline_change"] = (base_sha is not None and target_sha is not None and
                                decoded[0].endswith("\n") != decoded[1].endswith("\n"))
    # Trim equal edges before matching: even large repetitive files with a
    # localized edit then retain exact counts without quadratic matching work.
    prefix = 0
    while prefix < min(len(a_lines), len(b_lines)) and a_lines[prefix] == b_lines[prefix]:
        prefix += 1
    a_end, b_end = len(a_lines), len(b_lines)
    while a_end > prefix and b_end > prefix and a_lines[a_end - 1] == b_lines[b_end - 1]:
        a_end -= 1
        b_end -= 1
    a_mid, b_mid = a_lines[prefix:a_end], b_lines[prefix:b_end]
    approximate = len(a_mid) * len(b_mid) > _CODE_DIFF_AUTOJUNK_THRESHOLD
    result["approximate"] = approximate
    matcher = difflib.SequenceMatcher(None, a_mid, b_mid, autojunk=approximate)
    opcodes = [(tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
               for tag, i1, i2, j1, j2 in matcher.get_opcodes()]
    if prefix:
        opcodes.insert(0, ("equal", 0, prefix, 0, prefix))
    if a_end < len(a_lines):
        opcodes.append(("equal", a_end, len(a_lines), b_end, len(b_lines)))
    # Group the restored full-file opcodes so edge context and line numbers
    # include the trimmed lines, without running the matcher on them again.
    matcher.opcodes = opcodes
    result["added"] = sum(j2 - j1 for tag, i1, i2, j1, j2 in opcodes
                          if tag in ("insert", "replace"))
    result["deleted"] = sum(i2 - i1 for tag, i1, i2, j1, j2 in opcodes
                            if tag in ("delete", "replace"))
    remaining = line_limit
    for group in matcher.get_grouped_opcodes(3):
        group_lines = sum((i2 - i1 if tag != "insert" else 0)
                          + (j2 - j1 if tag in ("insert", "replace") else 0)
                          for tag, i1, i2, j1, j2 in group)
        if group_lines > remaining:
            result["patch_truncated"] = True
        if remaining <= 0:
            result["patch_truncated"] = True
            break
        hunk = dict(base_start=group[0][1] + 1, base_count=0,
                    target_start=group[0][3] + 1, target_count=0, lines=[])
        for tag, i1, i2, j1, j2 in group:
            # Build only emitted lines; replacements put removals before additions.
            parts = []
            if tag == "equal":
                parts.append(("context", range(i1, i2), a_lines, i1, j1))
            else:
                if tag in ("delete", "replace"):
                    parts.append(("removed", range(i1, i2), a_lines, i1, None))
                if tag in ("insert", "replace"):
                    parts.append(("added", range(j1, j2), b_lines, None, j1))
            for kind, indices, texts, base, target in parts:
                for offset, index in enumerate(indices):
                    if remaining <= 0:
                        result["patch_truncated"] = True
                        break
                    text = texts[index][0]
                    clipped = len(text) > _CODE_DIFF_LINE_CHARS
                    visible = text[:_CODE_DIFF_LINE_CHARS]
                    line = dict(
                        type=kind, text=visible,
                        base_line=base + offset + 1 if base is not None else None,
                        target_line=target + offset + 1 if target is not None else None,
                    )
                    if clipped:
                        line["clipped"] = True
                        result["patch_truncated"] = True
                    hunk["lines"].append(line)
                    hunk["base_count"] += base is not None
                    hunk["target_count"] += target is not None
                    remaining -= 1
        if hunk["lines"]:
            result["hunks"].append(hunk)
    return result


def _code_patch_budget(diff, limit, text_limit):
    """Copy the visible patch so response trimming cannot mutate the pair cache."""
    result = dict(diff, hunks=[])
    for hunk in diff["hunks"]:
        lines = []
        for line in hunk["lines"]:
            size = len(line["text"].encode("utf-8"))
            if limit <= 0 or text_limit <= 0 or size > text_limit:
                if size > text_limit:
                    text_limit = 0
                break
            lines.append(line)
            limit -= 1
            text_limit -= size
        if len(lines) < len(hunk["lines"]):
            result["patch_truncated"] = True
        if lines:
            result["hunks"].append(dict(
                hunk, lines=lines,
                base_count=sum(line["base_line"] is not None for line in lines),
                target_count=sum(line["target_line"] is not None for line in lines),
            ))
    return result, limit, text_limit


def _compare_code(httpd, base_id, target_id, base_manifest, target_manifest, budget=None):
    budget = budget if budget is not None else _CodeDiffBudget()
    manifests = [{entry["path"]: entry for entry in manifest}
                 for manifest in (base_manifest, target_manifest)]
    base, target = manifests
    summary = dict(added=0, removed=0, modified=0, unchanged=0, lines_added=0, lines_deleted=0)
    rows = []
    for path in base.keys() | target.keys():
        a, b = base.get(path), target.get(path)
        invalid = any(entry is not None and not layout.valid_sha(entry.get("sha256"))
                      for entry in (a, b))
        status = ("added" if a is None else "removed" if b is None else
                  "modified" if invalid or a.get("sha256") != b.get("sha256") else "unchanged")
        summary[status] += 1
        rows.append(dict(path=path, status=status,
                         base_sha256=a.get("sha256") if a is not None else None,
                         target_sha256=b.get("sha256") if b is not None else None,
                         base_size=a.get("size") if a is not None else None,
                         target_size=b.get("size") if b is not None else None))
        if invalid:
            rows[-1]["error"] = "snapshot unavailable"
    rows.sort(key=lambda row: (row["status"] == "unchanged", row["path"]))
    files_total = len(rows)
    truncated = files_total > _CODE_DIFF_FILES
    rows = rows[:_CODE_DIFF_FILES]
    remaining = _CODE_DIFF_TOTAL_LINES
    text_left = _CODE_DIFF_TEXT_BYTES
    for row in rows:
        if "error" in row:
            row.update(_code_diff_result(too_large=True))
            continue
        if row["status"] == "unchanged":
            row.update(_code_diff_result(added=0, deleted=0))
            continue
        if remaining <= 0 or text_left <= 0 or budget.exhausted():
            row.update(_code_diff_result(patch_truncated=True))
            continue
        diff = httpd.code_pair_diff(row["base_sha256"], row["target_sha256"], budget)
        patch, remaining, text_left = _code_patch_budget(diff, remaining, text_left)
        row.update(patch)
        # Status totals cover the whole manifest union. Line totals cover only
        # files actually diffed, including lines omitted by patch output caps.
        summary["lines_added"] += row["added"] or 0
        summary["lines_deleted"] += row["deleted"] or 0
    return dict(base=base_id, target=target_id, summary=summary, files=rows,
                files_total=files_total, truncated=truncated)


def _opt_text(value, limit=240):
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:limit]


def _safe_token(value, limit=128):
    """A single-line store string, or None when it is not safe to echo."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or any(ord(ch) < 32 for ch in text):
        return None
    return text


def _as_run_id(value):
    if isinstance(value, str) and layout.valid_run_id(value):
        return value
    return None


def _inference_label(value):
    if isinstance(value, str) and _INFERENCE_RE.fullmatch(value):
        return value
    return None


def _lineage_block(meta):
    raw = meta.get("lineage")
    return raw if isinstance(raw, dict) else None


def _repo_root_text(meta):
    lineage = _lineage_block(meta)
    if lineage is not None:
        root = _safe_token(lineage.get("repo_root"), 4096)
        if root:
            return root
    git = meta.get("git")
    if isinstance(git, dict):
        root = _safe_token(git.get("repo_root"), 4096)
        if root:
            return root
    return None


def _normalize_repo(value):
    """Absolute repo root, or None when the stored path cannot be used.

    ``expanduser`` raises ``RuntimeError`` for an unknown ``~user``. A bad
    path must not fail the request; inference then falls back to the project.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(Path(value).expanduser().resolve())
    except (RuntimeError, TypeError, ValueError, OSError):
        return None


def _created_at(meta):
    try:
        return layout.parse_timestamp(meta.get("created_at"))
    except (TypeError, ValueError):
        return None


def _stored_parent(meta):
    """``(parent_id, inference, needs_read_time_inference)``.

    A top-level ``parent_run_id`` is an explicit choice and wins when it
    disagrees with ``lineage.parent_run_id``. Otherwise a lineage block's
    stored parent stands, including an explicit null (no read-time guess).
    Runs with neither are inferred at read time.
    """
    if not isinstance(meta, dict):
        return None, None, False
    top = _as_run_id(meta.get("parent_run_id"))
    lineage = _lineage_block(meta)
    if lineage is not None:
        parent = _as_run_id(lineage.get("parent_run_id"))
        if top and top != parent:
            return top, "explicit", False
        if top:
            label = _inference_label(lineage.get("parent_inference"))
            return top, label or "explicit", False
        if parent:
            return parent, _inference_label(lineage.get("parent_inference")), False
        return None, None, False
    if top:
        return top, "explicit", False
    return None, None, True


def _infer_missing_parents(items):
    """Fill inferred parents in one forward pass per project/task.

    Items are ordered by ``(created_at, run_id)``. The parent is the latest
    strictly earlier run with the same normalized repo root, else the latest
    strictly earlier run in the same task. Equal timestamps tie-break on
    ``run_id``, and the strict comparison never selects the run itself.
    """
    grouped = {}
    for item in items:
        grouped.setdefault((item["project"], item["task"]), []).append(item)
    for group in grouped.values():
        ordered = sorted(
            (item for item in group if item["order"] is not None),
            key=lambda item: item["order"],
        )
        latest = None
        by_root = {}
        for item in ordered:
            order = item["order"]
            if item["needs"]:
                repo_prev = by_root.get(item["repo"]) if item["repo"] is not None else None
                if repo_prev is not None and repo_prev < order:
                    item["parent"] = repo_prev[1]
                    item["inference"] = "repo_root"
                elif latest is not None and latest < order:
                    item["parent"] = latest[1]
                    item["inference"] = "task" if item["task"] else "project"
                else:
                    item["parent"] = None
                    item["inference"] = None
            if latest is None or order > latest:
                latest = order
            repo = item["repo"]
            if repo is not None and (repo not in by_root or order > by_root[repo]):
                by_root[repo] = order


def _would_cycle(child, parent, accepted):
    seen = set()
    current = parent
    while current:
        if current == child or current in seen:
            return True
        seen.add(current)
        current = accepted.get(current)
    return False


def _stamp_key(item):
    created = item["created"]
    if created is not None:
        try:
            return (created.timestamp(), item["run_id"])
        except (OverflowError, OSError, ValueError):
            pass
    return (float("-inf"), item["run_id"])


def _lineage_item(meta):
    """One graph row. Malformed fields become empty values instead of a 500."""
    from ..lineage import run_task

    run_id = meta.get("run_id")
    if not isinstance(run_id, str):
        return None
    try:
        created = _created_at(meta)
        repo = _normalize_repo(_repo_root_text(meta))
        parent, inference, needs = _stored_parent(meta)
        project = _text(meta.get("project"))
    except Exception:
        created, repo, parent, inference, needs, project = None, None, None, None, False, ""
    task, task_source = run_task(meta)
    return {
        "meta": meta,
        "run_id": run_id,
        "project": project,
        "task": task,
        "task_source": task_source,
        "repo": repo,
        "created": created,
        "order": (created, run_id) if created is not None else None,
        "parent": parent,
        "inference": inference,
        "needs": needs,
    }


def _lineage_arch(meta):
    raw = meta.get("arch")
    if not isinstance(raw, dict):
        return None
    arch = {key: _safe_token(raw.get(key), 256)
            for key in ("model_class", "model_type", "loss_class")}
    architectures = raw.get("architectures")
    arch["architectures"] = ([_safe_token(item, 256) for item in architectures]
                             if isinstance(architectures, list) else None)
    if arch["architectures"] is not None and any(item is None for item in arch["architectures"]):
        arch["architectures"] = None
    return arch


def _lineage_graph(root, project_filter):
    """Nodes for one project (or every project) and parent edges inside that set.

    ``from`` is the parent and ``to`` is the child. An edge is omitted when the
    parent is outside the node set, or when adding it would close a cycle.
    Older edges win: runs are considered in ``(created_at, run_id)`` order.
    Timestamps and repo roots are normalized once per request.
    """
    items = []
    for meta in reader.list_runs(root):
        if not isinstance(meta, dict):
            continue
        item = _lineage_item(meta)
        if item is not None:
            items.append(item)
    _infer_missing_parents(items)
    resolved = {item["run_id"]: (item["parent"], item["inference"]) for item in items}
    tasks = {item["run_id"]: item["task"] for item in items}
    architectures = {item["run_id"]: _lineage_arch(item["meta"]) for item in items}
    trees = {}
    for item in items:
        lineage = _lineage_block(item["meta"]) or {}
        trees[item["run_id"]] = _safe_token(lineage.get("tree")) if isinstance(lineage, dict) else None
    accepted = {}
    order = sorted(items, key=_stamp_key)
    for item in order:
        run_id = item["run_id"]
        parent = item["parent"]
        if not parent or parent not in resolved or parent == run_id:
            continue
        if _would_cycle(run_id, parent, accepted):
            continue
        accepted[run_id] = parent
    nodes = []
    node_ids = {item["run_id"] for item in order
                if project_filter is None or item["project"] == project_filter}
    for item in order:
        project = item["project"]
        if project_filter is not None and project != project_filter:
            continue
        meta = item["meta"]
        run_id = item["run_id"]
        lineage = _lineage_block(meta) or {}
        parent, inference = resolved[run_id]
        own_tree = trees.get(run_id)
        parent_tree = trees.get(parent) if parent else None
        arch = architectures[run_id]
        own_class = (arch or {}).get("model_class")
        parent_class = (architectures.get(parent) or {}).get("model_class")
        status = meta.get("effective_status", meta.get("status"))
        if not isinstance(status, str):
            status = ""
        nodes.append({
            "run_id": run_id,
            "name": _text(meta.get("name")),
            "project": project,
            "task": item["task"],
            "task_source": item["task_source"],
            "arch": arch,
            "arch_changed": bool(own_class and parent_class and own_class != parent_class),
            "cross_task": (parent in node_ids and accepted.get(run_id) == parent
                           and tasks[parent] != item["task"]),
            "status": status,
            "created_at": _text(meta.get("created_at")),
            "commit": _safe_token(lineage.get("commit")),
            "tree": own_tree,
            "base_commit": _safe_token(lineage.get("base_commit")),
            "base_branch": _safe_token(lineage.get("base_branch"), 256),
            "base_dirty": lineage.get("base_dirty") if isinstance(lineage.get("base_dirty"), bool) else None,
            "parent_run_id": parent,
            "parent_inference": inference,
            "same_tree_as_parent": bool(own_tree and parent_tree and own_tree == parent_tree),
            "error": _opt_text(lineage.get("error")),
        })
    edges = []
    for item in order:
        run_id = item["run_id"]
        parent = accepted.get(run_id)
        if run_id not in node_ids or not parent or parent not in node_ids:
            continue
        inference = resolved[run_id][1]
        edges.append({
            "from": parent,
            "to": run_id,
            "inferred": inference not in (None, "explicit"),
            "cross_task": tasks[parent] != item["task"],
        })
    return {"nodes": nodes, "edges": edges}


def _git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _diff_payload(run_id, *, commit=None, parent_run_id=None, parent_commit=None, files=None, truncated=False, error=None):
    return {
        "run_id": run_id,
        "commit": commit,
        "parent_run_id": parent_run_id,
        "parent_commit": parent_commit,
        "files": files or [],
        "truncated": truncated,
        "checkout": f"agenticml lineage checkout {run_id}",
        "error": error,
    }


def _parse_numstat(blob):
    """Parse ``git diff --numstat -z`` records. Paths are kept byte-for-byte."""
    files = []
    if not isinstance(blob, (bytes, bytearray)):
        return files
    for record in blob.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        added, deleted, path = record.split(b"\t", 2)
        if not path or not added or not deleted:
            continue
        if added != b"-" and not added.isdigit():
            continue
        if deleted != b"-" and not deleted.isdigit():
            continue
        label = path.decode("utf-8", "replace")
        if not label or len(label) > 4096:
            continue
        files.append({
            "path": label,
            "added": None if added == b"-" else int(added),
            "deleted": None if deleted == b"-" else int(deleted),
        })
    return files


def _cap_numstat(chunks, limit_records, limit_bytes):
    """Read until EOF or a cap. Stop pulling once the cap is exceeded.

    ``limit_records`` counts numstat rows (a NUL record that contains a tab).
    ``diff-tree -z`` also emits a tab-free commit header; that header does not
    consume the row budget. At most ``limit_bytes`` are read. A further chunk,
    or a chunk cut to the byte budget, sets ``truncated``. Rows past the
    budget are not kept for parsing.
    """
    pending = bytearray()
    kept = bytearray()
    file_records = 0
    read_bytes = 0
    truncated = False
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            if not chunk:
                break
            if file_records >= limit_records or read_bytes >= limit_bytes:
                truncated = True
                break
            room = limit_bytes - read_bytes
            hit_byte_cap = len(chunk) > room
            if hit_byte_cap:
                chunk = chunk[:room]
            read_bytes += len(chunk)
            pending.extend(chunk)
            while file_records < limit_records:
                end = pending.find(b"\0")
                if end < 0:
                    break
                record = pending[:end]
                del pending[: end + 1]
                kept.extend(record)
                kept.append(0)
                if b"\t" in record:
                    file_records += 1
            if (pending and file_records >= limit_records) or hit_byte_cap:
                truncated = True
                break
            if read_bytes >= limit_bytes and pending:
                truncated = True
                break
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    if pending and not truncated:
        kept.extend(pending)
    return bytes(kept), truncated


def _iter_stdout(proc, timeout):
    """Yield stdout in bounded reads until EOF or ``timeout`` seconds."""
    stream = proc.stdout
    fd = stream.fileno()
    os.set_blocking(fd, False)
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        try:
            readable, _, _ = select.select([fd], [], [], remain)
        except InterruptedError:
            continue
        if not readable:
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        try:
            chunk = os.read(fd, _GIT_READ)
        except BlockingIOError:
            continue
        if not chunk:
            return
        yield chunk


def _reap_git(proc, *, terminate):
    """Stop a capped diff and always wait so the child is not left behind."""
    if terminate:
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    stdout = getattr(proc, "stdout", None)
    if stdout is not None:
        try:
            stdout.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_GIT_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _git_numstat(args, cwd):
    """``(blob, truncated, error)``. Stderr is discarded; stdout is capped."""
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            env=_git_env(),
            cwd=cwd,
        )
    except FileNotFoundError:
        return b"", False, "git is not available"
    except OSError:
        return b"", False, "git diff failed"
    blob = b""
    truncated = False
    timed_out = False
    try:
        blob, truncated = _cap_numstat(
            _iter_stdout(proc, _GIT_TIMEOUT), _LINEAGE_FILES, _GIT_MAX_BYTES,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _reap_git(proc, terminate=timed_out or truncated)
    if timed_out:
        return b"", False, "git diff timed out"
    if not truncated and proc.returncode not in (0, None):
        return b"", False, "git diff failed"
    return blob, truncated, None


def _lineage_changes(root, run_id, meta):
    """Numstat of one run's snapshot versus its parent commit.

    Missing git, a missing shadow repo, and runs without lineage answer 200
    with ``error`` set and ``files`` empty. Commit and project-key strings are
    checked before they can reach the git argument list.
    """
    lineage = _lineage_block(meta) if isinstance(meta, dict) else None
    parent_run_id = _stored_parent(meta)[0] if isinstance(meta, dict) else None
    if lineage is None:
        return _diff_payload(run_id, parent_run_id=parent_run_id, error="no lineage for this run")
    stored_error = _opt_text(lineage.get("error"))
    commit = lineage.get("commit")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        return _diff_payload(run_id, parent_run_id=parent_run_id, error=stored_error or "no lineage snapshot")
    raw_parent = lineage.get("parent_commit")
    if raw_parent in (None, ""):
        parent_commit = None
    elif isinstance(raw_parent, str) and _COMMIT_RE.fullmatch(raw_parent):
        parent_commit = raw_parent
    else:
        return _diff_payload(run_id, commit=commit, parent_run_id=parent_run_id, error="invalid parent commit")
    key = lineage.get("project_key")
    if not isinstance(key, str) or not _PROJECT_KEY_RE.fullmatch(key):
        return _diff_payload(
            run_id, commit=commit, parent_run_id=parent_run_id, parent_commit=parent_commit,
            error=stored_error or "invalid lineage project key",
        )
    try:
        repo = layout.contained(root, Path(root) / "lineage" / f"{key}.git")
    except ValueError:
        return _diff_payload(
            run_id, commit=commit, parent_run_id=parent_run_id, parent_commit=parent_commit,
            error="invalid lineage repository",
        )
    try:
        if not repo.is_dir():
            raise FileNotFoundError(key)
    except OSError:
        return _diff_payload(
            run_id, commit=commit, parent_run_id=parent_run_id, parent_commit=parent_commit,
            error="lineage repository not found",
        )
    git_dir = f"--git-dir={repo}"
    if parent_commit:
        args = ["git", git_dir, "diff-tree", "-r", "--numstat", "-z", "--no-renames", parent_commit, commit]
    else:
        args = ["git", git_dir, "diff-tree", "--root", "-r", "--numstat", "-z", commit]
    blob, truncated, error = _git_numstat(args, str(repo))
    if error:
        return _diff_payload(
            run_id, commit=commit, parent_run_id=parent_run_id, parent_commit=parent_commit,
            error=error,
        )
    files = _parse_numstat(blob)
    if len(files) > _LINEAGE_FILES:
        files = files[:_LINEAGE_FILES]
        truncated = True
    return _diff_payload(
        run_id, commit=commit, parent_run_id=parent_run_id, parent_commit=parent_commit,
        files=files, truncated=truncated, error=None,
    )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # The default backlog of 5 makes macOS reset a burst of parallel fetches
    # (Linux queues the SYNs instead).
    request_queue_size = 64

    def __init__(self, root, host, port, read_only=False):
        self.root = layout.resolve_root(root)
        self.verbose = False
        self.bind_host = host
        self.read_only = read_only
        self.readers = OrderedDict()
        self.reader_lock = threading.Lock()
        self.code_diffs = OrderedDict()
        self.code_diff_lock = threading.Lock()
        self.connections = OrderedDict()
        # Start time of the sendall currently blocking each "writing" socket.
        # Kept off ``connections`` so that map stays plain state strings.
        self.write_started = {}
        self.connection_lock = threading.Lock()
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

    def process_request(self, request, client_address):
        with self.connection_lock:
            if len(self.connections) >= _MAX_CONNECTIONS:
                oldest = next((conn for conn, state in self.connections.items()
                               if state == "headers"), None)
                if oldest is None:
                    # No header waiter. Drop the oldest sendall that has
                    # already stalled. Busy work and note bodies stay: one is
                    # server-side progress, the other must not be cut off.
                    now = time.monotonic()
                    stall = _WRITE_STALL
                    started_at = None
                    for conn, state in self.connections.items():
                        if state != "writing":
                            continue
                        started = self.write_started.get(conn)
                        if started is None or now - started < stall:
                            continue
                        if started_at is None or started < started_at:
                            oldest = conn
                            started_at = started
                if oldest is not None:
                    # Free the slot now. Waiting for the handler thread would
                    # leave the server full. shutdown unblocks its read or send.
                    self._pop_connection(oldest)
                    try:
                        oldest.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            admitted = len(self.connections) < _MAX_CONNECTIONS
            if admitted:
                self.connections[request] = "headers"
        if not admitted:
            try:
                # One nonblocking send: a client that does not read must never
                # stall the accept loop, even while all handlers are busy.
                request.setblocking(False)
                request.send(b"HTTP/1.1 503 Service Unavailable\r\n"
                             b"Connection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                try:
                    request.recv(65536)
                except OSError:
                    pass
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.connection_lock:
                self._pop_connection(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            # The superclass has already called shutdown_request.
            with self.connection_lock:
                self._pop_connection(request)

    def _pop_connection(self, request):
        """Drop a handler slot. Caller holds ``connection_lock``."""
        self.connections.pop(request, None)
        self.write_started.pop(request, None)

    def _set_connection_state(self, request, state):
        with self.connection_lock:
            if request not in self.connections:
                return False  # Evicted; the handler must stop.
            self.connections[request] = state
            if state == "headers":
                self.connections.move_to_end(request)
            if state == "writing":
                self.write_started[request] = time.monotonic()
            else:
                self.write_started.pop(request, None)
            return True

    def _admit_note_body(self, request):
        """Enter the non-evictable "body" state, or return False if the cap is full.

        A stalled note body holds its handler until the put deadline. Those
        handlers are never evicted, so the cap leaves a slot for a normal request.
        """
        limit = min(_MAX_NOTE_PUTS, max(1, _MAX_CONNECTIONS - 1))
        with self.connection_lock:
            if request not in self.connections:
                return False
            active = sum(state == "body" for state in self.connections.values())
            if active >= limit:
                return False
            self.connections[request] = "body"
            self.write_started.pop(request, None)
            return True

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

    def code_pair_diff(self, base_sha, target_sha, budget):
        # Per-server scope prevents cross-store cache hits. File line/character
        # caps also bound cached text. Response budgets trim copies of patches.
        key = (base_sha, target_sha, _CODE_DIFF_MAX_BYTES, _CODE_DIFF_FILE_LINES,
               _CODE_DIFF_MAX_LINES, _CODE_DIFF_LINE_CHARS, _CODE_DIFF_AUTOJUNK_THRESHOLD)
        with self.code_diff_lock:
            if key in self.code_diffs:
                self.code_diffs.move_to_end(key)
                return self.code_diffs[key]
        result = _code_pair_diff(self.root, base_sha, target_sha,
                                 _CODE_DIFF_MAX_BYTES, _CODE_DIFF_FILE_LINES, budget)
        # Retry unavailable snapshots and reads cut short by a request budget.
        if "error" not in result and not budget.exhausted():
            with self.code_diff_lock:
                self.code_diffs[key] = result
                self.code_diffs.move_to_end(key)
                while len(self.code_diffs) > _CODE_DIFF_CACHE_SIZE:
                    self.code_diffs.popitem(last=False)
        return result


class IPv6DashboardServer(DashboardServer):
    address_family = socket.AF_INET6


def _reject_nonfinite(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _media_type(value):
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _note_run_id(raw_path):
    """Run id when ``raw_path`` is exactly ``/api/runs/<id>/notes``."""
    try:
        path = unquote(urlsplit(raw_path).path, errors="strict")
    except (UnicodeDecodeError, ValueError, LookupError):
        return None
    prefix = "/api/runs/"
    suffix = "/notes"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    run_id = path[len(prefix):-len(suffix)]
    if not layout.valid_run_id(run_id):
        return None
    return run_id


def _parse_note_body(data: bytes):
    try:
        text = data.decode("utf-8")
        body = json.loads(text, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid note body") from exc
    if not isinstance(body, dict) or set(body) != {"text", "revision"}:
        raise ValueError("invalid note body")
    note_text = body["text"]
    revision = body["revision"]
    if not isinstance(note_text, str) or not (revision is None or isinstance(revision, str)):
        raise ValueError("invalid note body")
    return note_text, revision


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AgenticML"

    class _HeaderReader(io.RawIOBase):
        """Apply the remaining header budget before every socket read."""

        def __init__(self, handler, raw):
            self.handler = handler
            self.raw = raw

        def readable(self):
            return True

        def readinto(self, buffer):
            deadline = self.handler._header_deadline
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("request header deadline exceeded")
                self.handler.connection.settimeout(min(left, _CONNECTION_TIMEOUT))
            return self.raw.readinto(buffer)

        def close(self):
            try:
                self.raw.close()
            finally:
                super().close()

    class _ResponseWriter:
        """Time each sendall so a full server can drop one that is not moving.

        ``connections`` stays a plain state string. The start time lives in
        ``write_started`` and is cleared when the write finishes or the slot
        is dropped. An evicted socket must not keep sending.
        """

        def __init__(self, handler, raw):
            self.handler = handler
            self.raw = raw

        def write(self, b):
            server = self.handler.server
            if not server._set_connection_state(self.handler.connection, "writing"):
                raise BrokenPipeError("connection evicted")
            try:
                return self.raw.write(b)
            finally:
                server._set_connection_state(self.handler.connection, "busy")

        def __getattr__(self, name):
            return getattr(self.raw, name)

    def setup(self):
        super().setup()
        self._header_deadline = None
        self.rfile = io.BufferedReader(self._HeaderReader(self, self.rfile.detach()))
        self.wfile = self._ResponseWriter(self, self.wfile)

    def handle_one_request(self):
        if not self.server._set_connection_state(self.connection, "headers"):
            self.close_connection = True
            return
        self._header_deadline = time.monotonic() + _HEADER_DEADLINE
        try:
            super().handle_one_request()
        except (TimeoutError, socket.timeout):
            self.close_connection = True
        finally:
            self._header_deadline = None
            self.connection.settimeout(_CONNECTION_TIMEOUT)

    def parse_request(self):
        try:
            parsed = super().parse_request()
            if (self._header_deadline is not None
                    and time.monotonic() >= self._header_deadline):
                raise TimeoutError("request header deadline exceeded")
        finally:
            self._header_deadline = None
            self.connection.settimeout(_CONNECTION_TIMEOUT)
        if not self.server._set_connection_state(self.connection, "busy"):
            self.close_connection = True
            return False
        return parsed

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
            allow = "GET, PUT" if _note_run_id(self.path) else "GET"
            self._json({"error": "method not allowed"}, 405, {"Allow": allow})
        except _DISCONNECT:
            self.close_connection = True

    do_POST = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _method_not_allowed

    def do_PUT(self):
        self._started = False
        try:
            if not self._host_allowed():
                self.close_connection = True
                self._error({"error": "forbidden host"}, 403)
                return
            self._put()
        except notes.NoteConflict as exc:
            note = dict(exc.note)
            note["writable"] = True
            self._error({"error": "conflict", "note": note}, 409)
        except notes.NoteTooLarge:
            self._error({"error": "note too large"}, 413)
        except _DISCONNECT:
            self.close_connection = True
        except (TimeoutError, socket.timeout):
            # socket.timeout is TimeoutError on 3.10+ and a distinct OSError on 3.9.
            # A stalled body read must not fall through to a 500.
            self.close_connection = True
            if not getattr(self, "_started", False):
                try:
                    self._error({"error": "request timeout"}, 408)
                except OSError:
                    self.close_connection = True
        except ValueError:
            self._error({"error": "invalid request or store data"}, 400)
        except (FileNotFoundError, NotADirectoryError):
            self._error({"error": "not found"}, 404)
        except Exception:
            if self.server.verbose:
                traceback.print_exc()
            self._error({"error": "internal error"}, 500)

    def _csrf_ok(self) -> bool:
        token = self.headers.get_all("X-AgenticML-Request")
        if token is None or len(token) != 1 or token[0].strip() != "1":
            return False
        origins = self.headers.get_all("Origin")
        if origins:
            if len(origins) != 1:
                return False
            host = self.headers.get("Host")
            if host is None or origins[0].strip().lower() != ("http://" + host.strip()).lower():
                return False
        sites = self.headers.get_all("Sec-Fetch-Site")
        if sites:
            if len(sites) != 1 or sites[0].strip() not in ("same-origin", "none"):
                return False
        return True

    def _put_length(self):
        """Declared body length, or None when an error response was sent.

        ``Transfer-Encoding`` and a missing or non-digit ``Content-Length``
        are 411. Only ASCII digits count: ``str.isdigit`` is true for ``²``,
        and ``int`` of that would otherwise surface as 400. A length above
        512 KiB is 413 and the body is not read. The connection is closed so
        the unread bytes cannot desync keep-alive.
        """
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            self._error({"error": "length required"}, 411)
            return None
        values = self.headers.get_all("Content-Length")
        if values is None or len(values) != 1 or not isinstance(values[0], str):
            self.close_connection = True
            self._error({"error": "length required"}, 411)
            return None
        v = values[0].strip()
        if re.fullmatch(r"[0-9]{1,9}", v) is None:
            self.close_connection = True
            self._error({"error": "length required"}, 411)
            return None
        length = int(v)
        if length > _NOTE_PUT_MAX:
            self.close_connection = True
            self._error({"error": "payload too large"}, 413)
            return None
        return length

    def _read_put_body(self, length: int) -> bytes:
        """Read ``length`` body bytes, or raise ``TimeoutError`` at the deadline.

        ``rfile.read(length)`` waits until that many bytes arrive. Its socket
        timeout resets after every successful ``recv``, so a trickle never
        trips it. Each ``read1`` returns after one socket read. The deadline
        is checked between those reads, and the socket timeout for each read
        is the time still left, so one stalled ``recv`` cannot outlive it.
        """
        if length <= 0:
            return b""
        deadline = time.monotonic() + _NOTE_PUT_DEADLINE
        try:
            previous = self.connection.gettimeout()
        except OSError:
            previous = _CONNECTION_TIMEOUT
        reader = self.rfile.read1 if hasattr(self.rfile, "read1") else self.rfile.read
        chunks = []
        remaining = length
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    self.close_connection = True
                    raise TimeoutError("note put deadline exceeded")
                wait = left if previous is None else min(previous, left)
                try:
                    self.connection.settimeout(wait)
                except OSError as exc:
                    self.close_connection = True
                    raise TimeoutError("note put deadline exceeded") from exc
                try:
                    chunk = reader(min(_CHUNK, remaining))
                except (TimeoutError, socket.timeout, BlockingIOError) as exc:
                    self.close_connection = True
                    raise TimeoutError("note put deadline exceeded") from exc
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            try:
                self.connection.settimeout(previous)
            except OSError:
                pass
        return b"".join(chunks)

    def _put(self):
        run_id = _note_run_id(self.path)
        if run_id is None:
            self._method_not_allowed()
            return
        # Reject before reading the body. Close so the unread body cannot be
        # parsed as the next keep-alive request.
        if self.server.read_only:
            self.close_connection = True
            self._error({"error": "read-only"}, 403)
            return
        if not self._csrf_ok():
            self.close_connection = True
            self._error({"error": "cross-site request refused"}, 403)
            return
        if _media_type(self.headers.get("Content-Type")) != "application/json":
            self.close_connection = True
            self._error({"error": "unsupported media type"}, 415)
            return
        length = self._put_length()
        if length is None:
            return
        # A declared length of 0 reads nothing, so it must not occupy a body
        # slot or be turned away while that cap is full. Admit only before a
        # real read. Bodies are not evicted, so the cap is what keeps a
        # stalled PUT from answering every later request with 503.
        if length == 0:
            body = self._read_put_body(length)
        else:
            if not self.server._admit_note_body(self.connection):
                self.close_connection = True
                self._send(b"", "application/json", 503)
                return
            try:
                body = self._read_put_body(length)
            finally:
                # The receive is done. "body" is only that non-evictable read.
                self.server._set_connection_state(self.connection, "busy")
        if len(body) != length:
            self.close_connection = True
            raise ValueError("short body")
        text, revision = _parse_note_body(body)
        saved = notes.write_note(self.server.root, run_id, text, revision)
        saved["writable"] = True
        self._json(saved)

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
        if path == "/api/compare/code":
            base_id, target_id = arg("base"), arg("target")
            if not layout.valid_run_id(base_id) or not layout.valid_run_id(target_id):
                raise ValueError("invalid run id")
            budget = _CodeDiffBudget()
            manifests = []
            for run_id in dict.fromkeys((base_id, target_id)):
                run_path = self.server.root / "runs" / run_id
                if not run_path.resolve().is_relative_to(self.server.root / "runs"):
                    raise FileNotFoundError()
                with self.server.run_reader(run_id) as run:
                    # Recheck existence even if this reader was cached earlier.
                    run.meta()
                    manifests.append(run.code_manifest())
            if base_id == target_id:
                manifests.append(manifests[0])
            return self._json(_compare_code(self.server, base_id, target_id, *manifests, budget=budget))
        if path == "/api/lineage":
            if "project" in query:
                project = (query.get("project") or [""])[0]
                if not isinstance(project, str) or "\x00" in project:
                    raise ValueError("invalid project")
            else:
                project = None
            return self._json(_lineage_graph(self.server.root, project))
        if path == "/api/notes":
            if "project" in query:
                project = (query.get("project") or [""])[0]
                if not isinstance(project, str) or "\x00" in project:
                    raise ValueError("invalid project")
            else:
                project = None
            listed = notes.list_notes(self.server.root, project)
            listed["writable"] = not self.server.read_only
            return self._json(listed)
        if path == "/api/runs":
            from ..lineage import run_task

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
                row["task"], row["task_source"] = run_task(meta)
                row["arch"] = _lineage_arch(meta)
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
            if route == "notes":
                note = notes.read_note(self.server.root, run_id)
                note["writable"] = not self.server.read_only
                return self._json(note)
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
                elif route == "lineage":
                    result = _lineage_changes(self.server.root, run_id, run.meta())
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


def make_server(root, host="127.0.0.1", port=6006, read_only=False) -> ThreadingHTTPServer:
    """Construct a server; port=0 chooses a free port, server.verbose enables logs."""
    cls = IPv6DashboardServer if ":" in str(host) else DashboardServer
    return cls(root, host, port, read_only)


def serve(root, host="127.0.0.1", port=6006, open_browser=False, read_only=False) -> None:
    """Serve until Ctrl-C. No subprocesses or background services.

    The dashboard is read-only except for run notes. ``read_only`` disables
    note editing.
    """
    if host not in _LOOPBACK_HOSTS:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            message = "Warning: the UI has no authentication and exposes configs, env and code to the network."
            if not read_only:
                message += " Anyone who can reach it can edit run notes; pass --read-only to disable editing."
            print(message, flush=True)
    with make_server(root, host, port, read_only) as server:
        address, bound_port = server.server_address[:2]
        url = f"http://{_browser_host(address)}:{bound_port}"
        print(f"AgenticML UI: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
