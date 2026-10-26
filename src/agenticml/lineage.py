from __future__ import annotations

import json
import subprocess
from pathlib import Path


LINEAGE_DIR_NAME = ".agenticml"
LINEAGE_FILE_NAME = "lineage.json"


def read_local_lineage_state(repo_root: str | Path) -> dict:
    state_path = Path(repo_root) / LINEAGE_DIR_NAME / LINEAGE_FILE_NAME
    if not state_path.exists():
        return {}

    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_local_lineage_state(
    repo_root: str | Path,
    *,
    run_id: str,
    snapshot_hash: str,
    project: str,
    name: str,
) -> None:
    repo_root = Path(repo_root)
    state_dir = repo_root / LINEAGE_DIR_NAME
    state_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "last_run_id": run_id,
        "last_snapshot_hash": snapshot_hash,
        "project": project,
        "name": name,
    }
    (state_dir / LINEAGE_FILE_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def infer_parent_run_id(repo_root: str | Path, project: str) -> tuple[str | None, str | None]:
    state = read_local_lineage_state(repo_root)
    parent_run_id = state.get("last_run_id")
    if not parent_run_id:
        return None, None

    state_project = state.get("project")
    if state_project and state_project != project:
        return None, None

    return parent_run_id, "local_state"


def get_git_metadata(repo_root: str | Path) -> dict:
    repo_root = Path(repo_root)

    def run_git_command(args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                check=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return completed.stdout.strip() or None

    commit = run_git_command(["rev-parse", "HEAD"])
    branch = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    dirty_output = run_git_command(["status", "--porcelain"])

    return {
        "git_commit": commit,
        "git_branch": branch,
        "is_dirty": bool(dirty_output),
    }
