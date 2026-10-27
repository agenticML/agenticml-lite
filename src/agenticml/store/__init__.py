"""Local v1 run store.

``Run`` writes through ``RunWriter`` under the asset root. The UI and
``agenticml sync`` read those files through ``RunReader``. This package
handles local storage; uploads live in the sync client.

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
