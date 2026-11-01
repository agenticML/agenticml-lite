from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import subprocess
import tempfile
import time
import unicodedata
from contextlib import contextmanager, nullcontext
from pathlib import Path, PurePosixPath

from .code_tracking import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    SKIP_DIR_NAMES,
    SKIP_FILE_SUFFIXES,
    SKIP_PATH_SUFFIXES,
    _should_skip,
    _normalize_task,
)
from .store import layout
from .store.reader import RunReader, list_runs

logger = logging.getLogger(__name__)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("lineage snapshot timed out")
    return remaining


def get_git_metadata(repo_root: str | Path, *, deadline=None) -> dict:
    def run_git_command(args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                check=True,
                timeout=min(5, _remaining(deadline)) if deadline is not None else 5,
                # Environment equivalents of --no-optional-locks and
                # -c core.fsmonitor=false, overriding local repository config.
                env={**_env(), "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false"},
            )
            output = completed.stdout
            return (output.decode("utf-8", errors="surrogateescape")
                    if isinstance(output, bytes) else output).strip()
        except Exception:
            return None

    commit = run_git_command(["rev-parse", "HEAD"])
    branch = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    dirty_output = run_git_command(["status", "--porcelain", "-uno"])

    metadata = {"git_commit": commit or None, "git_branch": branch or None}
    if dirty_output is not None:
        metadata["is_dirty"] = bool(dirty_output)
    return metadata


# Shadow repositories never use the project's Git directory or index.
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}-[0-9a-f]{8}\Z")


def project_key(project: str) -> str:
    """Stable, filesystem-safe key for the exact project name."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", project.lower()).strip("._-")[:48].rstrip("._-") or "project"
    return f"{slug}-{hashlib.sha256(project.encode()).hexdigest()[:8]}"


def _repo_path(root, key):
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError("invalid lineage project key")
    return layout.contained(root, Path(root) / "lineage" / f"{key}.git")


def _env(repo=None, worktree=None, index=None):
    # Inherited Git overrides (including config injection) cannot select the
    # user's index, hooks, repository, or global filters.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="agenticml", GIT_AUTHOR_EMAIL="agenticml@localhost",
               GIT_COMMITTER_NAME="agenticml", GIT_COMMITTER_EMAIL="agenticml@localhost",
               GIT_TERMINAL_PROMPT="0")
    if repo is not None:
        env["GIT_DIR"] = str(repo)
    if worktree is not None:
        env["GIT_WORK_TREE"] = str(worktree)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return env


def _git(args, *, repo=None, worktree=None, index=None, timeout=30, input=None, check=True):
    return subprocess.run(["git", *args], env=_env(repo, worktree, index),
                          cwd=worktree or repo, input=input, capture_output=True,
                          check=check, timeout=timeout)


@contextmanager
def _locked(root, repo, *, deadline=None):
    with layout.open_nofollow(layout.contained(root, repo / "agenticml.lock"), "ab") as handle:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None and os.name != "nt":
            try:
                if deadline is None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                else:
                    while True:
                        _remaining(deadline)
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(min(0.01, _remaining(deadline)))
            except OSError as exc:
                if exc.errno not in layout._LOCK_UNSUPPORTED:
                    raise
        # Closing the descriptor releases the lock, including on failure.
        yield


def shadow_repo(root, project, *, timeout=30, deadline=None) -> Path:
    """Initialize a bare repository lazily, entirely under the store root."""
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    root = layout.resolve_root(root)
    repo = _repo_path(root, project_key(project))
    repo.mkdir(parents=True, exist_ok=True)
    with _locked(root, repo, deadline=deadline):
        if not layout.contained(root, repo / "HEAD").exists():
            _git(["init", "--bare", "--template=", str(repo)], timeout=_remaining(deadline))
        exclude = layout.contained(root, repo / "info" / "exclude")
        exclude.parent.mkdir(parents=True, exist_ok=True)
        patterns = ([f"{name}/" for name in sorted(SKIP_DIR_NAMES)]
                    + [f"*{suffix}/" for suffix in SKIP_PATH_SUFFIXES]
                    + [f"*{suffix}" for suffix in SKIP_FILE_SUFFIXES])
        exclude.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    return repo


def run_task(meta):
    """Task key and origin, including entry-path names from older runs."""
    if not isinstance(meta, dict):
        return "", None
    if "task" in meta:
        task = meta["task"]
        if task not in (None, "") and _normalize_task(task) is None:
            return "", None
        source = _normalize_task(meta.get("task_source"))
        return _normalize_task(task) or "", source
    name = meta.get("name")
    if (not isinstance(name, str) or "\\" in name or not name.endswith(".py")
            or any(unicodedata.category(c).startswith(("C", "Zl", "Zp")) for c in name)):
        return "", None
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.parent == PurePosixPath("."):
        return "", None
    task = _normalize_task(path.parent.as_posix())
    return (task, "legacy") if task else ("", None)


def parent_lane_task(parent_meta, project):
    """Parent's normalized non-empty task when it is in ``project``, else None."""
    if parent_meta.get("project") != project:
        return None
    return run_task(parent_meta)[0] or None


def _parent_candidates(metas):
    """Read preloaded metadata once; malformed candidates cannot break inference."""
    for meta in metas:
        try:
            rid, project = meta["run_id"], meta["project"]
            if not layout.valid_run_id(rid) or not isinstance(project, str):
                continue
            order = (layout.parse_timestamp(meta.get("created_at")), rid)
            previous_root = ((meta.get("lineage") or {}).get("repo_root")
                             or (meta.get("git") or {}).get("repo_root"))
            resolved = Path(previous_root).resolve() if previous_root else None
        except (RuntimeError, TypeError, OSError, ValueError, KeyError, AttributeError):
            continue
        yield order, (project, run_task(meta)[0]), resolved


def infer_parent(root, project, repo_root, created_before, exclude_run_id=None, task=None):
    """Prefer the latest source-directory run within the same project/task."""
    before = (layout.parse_timestamp(created_before), exclude_run_id or "")
    current_root = Path(repo_root).resolve()
    latest = same_root = None
    task = run_task({"task": task})[0]
    for order, candidate_group, previous_root in _parent_candidates(list_runs(root)):
        if candidate_group != (project, task) or order[1] == exclude_run_id:
            continue
        if order[0] > before[0] or (order[0] == before[0] and
                                  exclude_run_id is not None and order >= before):
            continue
        if latest is None or order > latest:
            latest = order
        if previous_root == current_root and (same_root is None or order > same_root):
            same_root = order
    return ((same_root[1], "repo_root") if same_root else
            (latest[1], "task" if task else "project") if latest else (None, None))


def infer_parents(metas):
    """Infer every parent in O(n) from list_runs' newest-first metadata."""
    latest, by_root, parents = {}, {}, {}
    for order, group, repo_root in _parent_candidates(reversed(metas)):
        previous = by_root.get((group, repo_root)) if repo_root is not None else None
        inference = "repo_root" if previous is not None else "task" if group[1] else "project"
        previous = previous if previous is not None else latest.get(group)
        parents[order[1]] = ((previous[1], inference) if previous and previous < order else (None, None))
        latest[group] = order
        if repo_root is not None:
            by_root[group, repo_root] = order
    return parents


def _ref(run_id):
    if not layout.valid_run_id(run_id):
        raise ValueError("invalid lineage run id")
    return f"refs/runs/{run_id}"


def _resolve_commit(repo, run_id, timeout=30):
    result = _git(["rev-parse", "--verify", "-q", f"{_ref(run_id)}^{{commit}}"],
                  repo=repo, timeout=timeout, check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def snapshot(root, project, repo_root, run_id, parent_run_id=None, *, created_at=None,
             task=None,
             max_file_bytes=DEFAULT_MAX_FILE_BYTES, max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
             timeout=30, max_files=20000) -> dict:
    """Capture startup code with a private index; failures become metadata."""
    deadline = time.monotonic() + timeout
    if parent_run_id == run_id:
        parent_run_id = None
    result = {"version": 1, "error": None, "parent_run_id": parent_run_id,
              "parent_inference": "explicit" if parent_run_id else None,
              "project_key": None, "repo_root": str(repo_root), "task": task}
    index = ignore = None
    try:
        root = layout.resolve_root(root)
        worktree = Path(repo_root).resolve()
        result.update(project_key=project_key(project), repo_root=str(worktree))
        _ref(run_id)
        if not parent_run_id:
            parent_run_id, inference = infer_parent(root, project, worktree,
                                                    created_at or layout.format_timestamp(), run_id, task=task)
            result.update(parent_run_id=parent_run_id, parent_inference=inference)
        repo = shadow_repo(root, project, deadline=deadline)
        base = get_git_metadata(worktree, deadline=deadline) if (worktree / ".git").exists() else {}
        fd, name = tempfile.mkstemp(prefix=f"index-{run_id}-", dir=repo)
        os.close(fd)
        index = layout.contained(root, name)
        index.unlink()  # Git requires a missing index, not an empty file.
        config = ["-c", "core.bare=false", "-c", "core.autocrlf=false"]
        ignore_source = worktree / ".agenticmlignore"
        path_errors = []
        try:
            if ignore_source.is_file() and not ignore_source.is_symlink():
                # Bound both the read and a concurrent growth of the file.
                with ignore_source.open("rb") as source:
                    content = (source.read(1024 * 1024 + 1)
                               if os.fstat(source.fileno()).st_size <= 1024 * 1024 else None)
                if content is not None and len(content) <= 1024 * 1024:
                    fd, name = tempfile.mkstemp(prefix="ignore-", dir=repo)
                    with os.fdopen(fd, "wb") as handle:
                        ignore = Path(name)
                        layout.contained(root, ignore)
                        handle.write(content)
                    config += ["-c", f"core.excludesFile={ignore}"]
        except (OSError, RuntimeError):
            path_errors.append(".agenticmlignore")
        paths = _git([*config, "ls-files", "-z", "--others", "--exclude-standard"],
                     repo=repo, worktree=worktree, index=index, timeout=_remaining(deadline)).stdout
        count = paths.count(b"\0")
        # Only materialize/sort/stat the bounded prefix, even for huge projects.
        bounded = paths.split(b"\0", max_files)[:max_files]
        kept, oversized, over_total = [], [], []
        total = 0
        nested_cache = {}
        for raw in sorted(filter(None, bounded)):
            _remaining(deadline)
            relative = Path(os.fsdecode(raw))
            if relative.is_absolute() or ".." in relative.parts or _should_skip(relative):
                continue
            try:
                path = worktree / relative
                resolved = path.resolve()
                # Do not snapshot the store itself when inside the project.
                if resolved.is_relative_to(root):
                    continue
                nested = False
                for directory in relative.parents:
                    if directory == Path("."):
                        break
                    if directory not in nested_cache:
                        nested_cache[directory] = (worktree / directory / ".git").exists()
                    if nested_cache[directory]:
                        nested = True
                        break
                if nested:
                    continue
                st = path.lstat()  # Caps checked before Git hashes any file.
                if stat.S_ISLNK(st.st_mode):
                    if not resolved.is_relative_to(worktree):
                        continue
                elif not stat.S_ISREG(st.st_mode):
                    continue
            except (OSError, RuntimeError):
                path_errors.append(relative.as_posix())
                continue
            label = relative.as_posix()
            if st.st_size > max_file_bytes:
                oversized.append(label)
            elif total + st.st_size > max_total_bytes:
                over_total.append(label)
            else:
                kept.append(raw)
                total += st.st_size
        _git([*config, "update-index", "--add", "-z", "--stdin"], repo=repo, worktree=worktree,
             index=index, timeout=_remaining(deadline), input=b"".join(p + b"\0" for p in kept))
        tree = _git(["write-tree"], repo=repo, index=index, timeout=_remaining(deadline)).stdout.decode().strip()
        message = f"agenticml run {run_id}\n\nproject: {project}\ntask: {task or ''}\nbase_commit: {base.get('git_commit') or ''}"
        with _locked(root, repo, deadline=deadline):
            parent_commit = (_resolve_commit(repo, parent_run_id, _remaining(deadline))
                             if parent_run_id else None)
            args = ["commit-tree", tree]
            if parent_commit:
                args += ["-p", parent_commit]
            commit = _git([*args, "-m", message], repo=repo,
                          timeout=_remaining(deadline)).stdout.decode().strip()
            _git(["update-ref", _ref(run_id), commit], repo=repo, timeout=_remaining(deadline))
        result.update(commit=commit, tree=tree, parent_commit=parent_commit,
                      base_commit=base.get("git_commit"), base_branch=base.get("git_branch"),
                      base_dirty=base.get("is_dirty"), files=len(kept), bytes=total,
                      skipped={"oversized": oversized, "over_total": over_total,
                               "over_count": max(0, count - max_files), "path_errors": path_errors})
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            message = os.fsdecode(exc.stderr or b"").strip() or str(exc)
        else:
            message = str(exc) or type(exc).__name__
        result["error"] = message.splitlines()[0][:240]
    finally:
        for path in (index, ignore):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                    layout.contained(root, str(path) + ".lock").unlink(missing_ok=True)
                except Exception:
                    pass
    return result


def _run_repo(root, run_id):
    meta = RunReader(root, run_id).meta()
    info = meta.get("lineage") or {}
    repo = _repo_path(root, info.get("project_key") or project_key(meta["project"]))
    return meta, info, repo


def run_commit(root, run_id):
    """Return the run's shadow commit, or None when its ref is absent."""
    root = layout.resolve_root(root)
    _, _, repo = _run_repo(root, run_id)
    return _resolve_commit(repo, run_id)


def checkout(root, run_id, dest=None) -> Path:
    """Materialize a detached shadow worktree on explicit request."""
    root = layout.resolve_root(root)
    meta, info, repo = _run_repo(root, run_id)
    key = info.get("project_key") or project_key(meta["project"])
    target = (Path(dest).expanduser().resolve() if dest is not None else
              layout.contained(root, root / "worktrees" / key / run_id))
    with _locked(root, repo):
        commit = _resolve_commit(repo, run_id)
        if not commit:
            raise ValueError(f"run {run_id} has no lineage snapshot")
        if target.exists():
            for entry, path, head, _ in _worktrees(root, repo):
                if path == target and head == commit:
                    layout.contained(root, entry / "agenticml-run").write_text(run_id + "\n")
                    return target
            raise ValueError(f"checkout destination is not a worktree of run {run_id}: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "--detach", str(target), commit], repo=repo)
        for entry, path, _, _ in _worktrees(root, repo):
            if path == target:
                layout.contained(root, entry / "agenticml-run").write_text(run_id + "\n")
                break
        else:
            raise ValueError(f"cannot identify checkout registration: {target}")
    return target


def diff(root, run_a, run_b=None) -> list[dict]:
    """Numstat from run_b (or the recorded parent/empty tree) to run_a."""
    root = layout.resolve_root(root)
    _, info, repo = _run_repo(root, run_a)
    commit = _resolve_commit(repo, run_a)
    if not commit:
        raise ValueError(f"run {run_a} has no lineage snapshot")
    parent = info.get("parent_commit")
    if not parent and info.get("parent_run_id"):
        parent = _resolve_commit(repo, info["parent_run_id"])
    if run_b is not None:
        _, _, other_repo = _run_repo(root, run_b)
        if other_repo != repo:
            raise ValueError("cannot diff runs from different projects")
        parent = _resolve_commit(repo, run_b)
        if not parent:
            raise ValueError(f"run {run_b} has no lineage snapshot")
    args = (["diff", "--numstat", "-z", "--no-renames", parent, commit] if parent else
            ["diff-tree", "--root", "--no-commit-id", "-r", "--numstat", "-z", "--no-renames", commit])
    records = _git(args, repo=repo).stdout.split(b"\0")
    # Name-status distinguishes empty added/deleted files and binary edits.
    status_args = (["diff", "--name-status", "-z", "--no-renames", parent, commit] if parent else
                   ["diff-tree", "--root", "--no-commit-id", "-r", "--name-status", "-z", "--no-renames", commit])
    statuses = _git(status_args, repo=repo).stdout.split(b"\0")
    by_path = {os.fsdecode(statuses[i + 1]): statuses[i].decode() for i in range(0, len(statuses) - 1, 2)}
    entries = []
    for record in filter(None, records):
        added, deleted, path = record.split(b"\t", 2)
        label = os.fsdecode(path)
        entries.append({"path": label, "added": None if added == b"-" else int(added),
                        "deleted": None if deleted == b"-" else int(deleted), "status": by_path[label]})
    return entries


def _worktrees(root, repo):
    """Read registrations without ambiguous newline-delimited porcelain paths."""
    registrations = layout.contained(root, repo / "worktrees")
    for entry in registrations.iterdir() if registrations.is_dir() else ():
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            gitdir = layout.read_file_nofollow(layout.contained(root, entry / "gitdir"))
            path = Path(os.fsdecode(gitdir.removesuffix(b"\n"))).parent
            marker = layout.contained(root, entry / "agenticml-run")
            run_id = layout.read_file_nofollow(marker).decode().strip() if marker.exists() else None
            resolved = _git(["rev-parse", "--verify", "HEAD"], repo=entry, check=False)
            head = resolved.stdout.decode().strip() if resolved.returncode == 0 else None
            yield entry, path, head, run_id
        except Exception as exc:
            logger.warning("cannot read lineage worktree %s: %s", entry, exc)


def remove_run(root, meta, dry_run=False):
    """Remove run refs and default checkouts; custom directories are user-owned."""
    removed = {"refs": [], "worktrees": [], "worktrees_kept": []}
    try:
        root = layout.resolve_root(root)
        run_id = meta["run_id"]
        info = meta.get("lineage") or {}
        key = info.get("project_key") or project_key(meta["project"])
        repo = _repo_path(root, key)
        if not repo.is_dir():
            return removed
        # A preview must not create a missing lock file.
        with nullcontext() if dry_run else _locked(root, repo):
            commit = _resolve_commit(repo, run_id)
            default = root / "worktrees" / key / run_id
            for _, path, head, marker in _worktrees(root, repo):
                if path == default or marker == run_id or (marker is None and commit and head == commit):
                    # Never resolve an alias into a custom directory and delete it.
                    if path != default or layout.contained(root, default) != default:
                        removed["worktrees_kept"].append(str(path))
                        continue
                    if not dry_run:
                        try:
                            _git(["worktree", "remove", "--force", str(path)], repo=repo)
                        except Exception as exc:
                            logger.warning("cannot remove lineage worktree %s: %s", path, exc)
                            continue
                    removed["worktrees"].append(str(path))
            if commit:
                if not dry_run:
                    _git(["update-ref", "-d", _ref(run_id)], repo=repo)
                removed["refs"].append(_ref(run_id))
            if not dry_run:
                _git(["worktree", "prune"], repo=repo)
    except Exception as exc:
        logger.warning("cannot clean lineage for run %s: %s", meta.get("run_id"), exc)
    return removed


def sweep_orphans(root):
    """Retry cleanup for refs whose run directories were removed by an earlier GC."""
    removed = {"refs": [], "worktrees": [], "worktrees_kept": []}
    root = layout.resolve_root(root)
    try:
        directory = layout.contained(root, root / "lineage")
        for repo in directory.iterdir() if directory.is_dir() else ():
            key = repo.name.removesuffix(".git")
            if repo.is_symlink() or not repo.is_dir() or not _KEY_RE.fullmatch(key):
                continue
            try:
                refs = _git(["for-each-ref", "--format=%(refname)", "refs/runs/"], repo=repo)
                for ref in refs.stdout.decode().splitlines():
                    rid = ref.removeprefix("refs/runs/")
                    if layout.valid_run_id(rid) and not os.path.lexists(root / "runs" / rid):
                        report = remove_run(root, {"run_id": rid, "lineage": {"project_key": key}})
                        for field in removed:
                            removed[field].extend(report[field])
            except Exception as exc:
                logger.warning("cannot sweep orphan lineage refs in %s: %s", repo, exc)
    except Exception as exc:
        logger.warning("cannot enumerate orphan lineage refs: %s", exc)
    return removed
