from __future__ import annotations

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
