"""Bounded bundle validation and immutable run refs in server shadow repos."""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from agenticml import lineage

logger = logging.getLogger("agenticml.server.lineage")
_CONFIG = ["-c", "core.hooksPath=/dev/null", "-c", "transfer.fsckObjects=true",
           "-c", "fetch.fsckObjects=true", "-c", "fetch.writeCommitGraph=false",
           "-c", "gc.auto=0", "-c", "core.fsmonitor=false",
           "-c", "protocol.allow=never", "-c", "protocol.file.allow=always"]
_HEADER_LIMIT = 1024 * 1024


class LineageError(ValueError):
    def __init__(self, error, status=422, **extra):
        self.status = status
        self.payload = {"error": error, **extra}
        self.headers = {"Retry-After": "5"} if status == 503 else None
        super().__init__(error)


def _info(meta):
    info = lineage.replication_info(meta)
    if info is None:
        raise LineageError("run has no lineage")
    return info


def _git(repo, args, *, timeout=60, **kwargs):
    return lineage._git([*_CONFIG, *args], repo=repo, timeout=timeout, **kwargs)


def _commit(repo, run_id, *, timeout=60):
    result = _git(repo, ["rev-parse", "--verify", "-q", f"{lineage._ref(run_id)}^{{commit}}"],
                  timeout=timeout, check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def state(root, run_id, meta):
    info = _info(meta)
    try:
        repo = lineage._repo_path(root, info["project_key"])
        if not (repo / "HEAD").exists():
            return {"commit": None, "tips": []}
        tips = _git(repo, ["for-each-ref", "--sort=-committerdate", "--count=10000",
                           "--format=%(objectname)", "refs/runs/"]).stdout.decode().splitlines()
        return {"commit": _commit(repo, run_id), "tips": list(dict.fromkeys(tips))}
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise LineageError("lineage busy", 503) from exc


def _header(path, run_id, commit):
    with path.open("rb") as handle:
        header = handle.readline(_HEADER_LIMIT + 1)
        if header not in (b"# v2 git bundle\n", b"# v3 git bundle\n"):
            raise LineageError("invalid bundle")
        v3 = header == b"# v3 git bundle\n"
        size, refs, stage = len(header), 0, 0
        while size <= _HEADER_LIMIT:
            line = handle.readline(_HEADER_LIMIT - size + 1)
            size += len(line)
            if not line or size > _HEADER_LIMIT or not line.endswith(b"\n"):
                break
            if line == b"\n":
                if refs == 1:
                    return handle.tell()
                break
            if line.startswith(b"@"):
                if not v3 or stage or line != b"@object-format=sha1\n":
                    break
            elif line.startswith(b"-"):
                if stage > 1 or re.fullmatch(rb"-[0-9a-f]{40} [^\x00\r\n]*\n", line) is None:
                    break
                stage = 1
            elif line == f"{commit} {lineage._ref(run_id)}\n".encode():
                stage = 2
                refs += 1
            else:
                break
    raise LineageError("invalid bundle")


def receive(root, run_id, meta, path):
    info = _info(meta)
    expected = info["commit"]
    try:
        offset = _header(path, run_id, expected)
        repo = lineage._repo_path(root, info["project_key"])
        timeout = 60 + path.stat().st_size / (4 * 1024 * 1024)
        repo.mkdir(parents=True, exist_ok=True)
        with lineage._locked(root, repo):
            if (repo / "HEAD").exists():
                current = _commit(repo, run_id, timeout=timeout)
                if current == expected:
                    return {"commit": current, "created": False}, 200
                if current is not None:
                    raise LineageError("lineage ref differs", 409, commit=current)
            try:
                lineage._init_repo(repo, timeout=timeout)
            except subprocess.CalledProcessError as exc:
                raise LineageError("lineage busy", 503) from exc
            verified = _git(repo, ["bundle", "verify", str(path)], timeout=timeout, check=False)
            if verified.returncode:
                message = verified.stderr.decode(errors="replace")
                logger.warning("bundle verification: %s", message.splitlines()[0][:240] if message else "failed")
                missing = "Repository lacks these prerequisite commits" in message
                raise LineageError("missing prerequisite" if missing else "invalid bundle")
            with tempfile.TemporaryDirectory(prefix="lineage-", dir=path.parent) as directory:
                quarantine = Path(directory)
                (quarantine / "info").mkdir()
                (quarantine / "info" / "alternates").write_text(str((repo / "objects").resolve()) + "\n")
                env = {"GIT_OBJECT_DIRECTORY": str(quarantine)}
                with path.open("rb") as handle:
                    handle.seek(offset)
                    _git(repo, ["index-pack", "--stdin", "--fix-thin", "--strict"],
                         stdin=handle, extra_env=env, timeout=timeout)
                _git(repo, ["rev-list", "--objects", "--quiet", expected, "--not", "--all"],
                     extra_env=env, timeout=timeout)
                if _git(repo, ["cat-file", "-t", expected], extra_env=env,
                        timeout=timeout).stdout.strip() != b"commit":
                    raise LineageError("invalid bundle")
                promoted, publishing, published = [], False, False
                try:
                    for suffix in (".pack", ".rev", ".idx"):
                        for source in (quarantine / "pack").glob("*" + suffix):
                            target = repo / "objects" / "pack" / source.name
                            if not target.exists():
                                source.rename(target)
                                promoted.append(target)
                    publishing = True
                    try:
                        _git(repo, ["update-ref", lineage._ref(run_id), expected, "0" * 40], timeout=timeout)
                    except subprocess.CalledProcessError as exc:
                        raise LineageError("lineage busy", 503) from exc
                    published = True
                    if _commit(repo, run_id, timeout=timeout) != expected:
                        raise LineageError("invalid bundle")
                except Exception:
                    keep_packs = publishing
                    try:
                        # A timed-out update-ref may already have published.
                        if published or (publishing and _commit(repo, run_id, timeout=timeout) == expected):
                            try:
                                _git(repo, ["update-ref", "-d", lineage._ref(run_id), expected], timeout=timeout)
                            except subprocess.CalledProcessError as exc:
                                raise LineageError("lineage busy", 503) from exc
                        keep_packs = False
                    finally:
                        if not keep_packs:
                            for target in reversed(promoted):
                                target.unlink(missing_ok=True)
                    raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        message = (getattr(exc, "stderr", None) or b"").decode(errors="replace")
        logger.warning("bundle receive: %s", message.splitlines()[0][:240] if message else type(exc).__name__)
        if isinstance(exc, (subprocess.TimeoutExpired, OSError)):
            raise LineageError("lineage busy", 503) from exc
        raise LineageError("invalid bundle") from exc
    return {"commit": expected, "created": True}, 201
