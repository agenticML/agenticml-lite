"""Local v1 run store.

Training writes a directory under the asset root. A UI (later) and
``agenticml sync`` (later) read it. Nothing in this package uploads, and
WP2a does not wire it into ``Run`` — callers use ``RunWriter`` / ``RunReader``
directly until WP2b.

The byte layout is the contract in ``docs/FORMAT.md``.
"""

from __future__ import annotations

from .gc import check_budget, collect, prunable_series, prune_run, run_fingerprint, run_usage, usage
from .layout import (
    new_run_id,
    object_path,
    open_object,
    resolve_root,
    run_dir,
    valid_run_id,
    valid_sha,
)
from .objects import has, path_for, put_bytes, put_file
from .overview import OVERVIEW_MIN_POINTS, Overview
from .reader import RunReader, list_runs
from .writer import MEDIA_MAX_PER_KEY, MEDIA_MAX_PX, RunFinishedError, RunWriter

__all__ = [
    "MEDIA_MAX_PER_KEY",
    "MEDIA_MAX_PX",
    "OVERVIEW_MIN_POINTS",
    "Overview",
    "RunFinishedError",
    "RunReader",
    "RunWriter",
    "check_budget",
    "collect",
    "has",
    "list_runs",
    "new_run_id",
    "object_path",
    "open_object",
    "path_for",
    "put_bytes",
    "put_file",
    "prunable_series",
    "prune_run",
    "run_fingerprint",
    "resolve_root",
    "run_dir",
    "run_usage",
    "usage",
    "valid_run_id",
    "valid_sha",
]
