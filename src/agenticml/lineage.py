from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
                timeout=5,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return completed.stdout.strip()

    commit = run_git_command(["rev-parse", "HEAD"])
    branch = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    dirty_output = run_git_command(["status", "--porcelain", "-uno"])

    metadata = {"git_commit": commit or None, "git_branch": branch or None}
    if dirty_output is not None:
        metadata["is_dirty"] = bool(dirty_output)
    return metadata
