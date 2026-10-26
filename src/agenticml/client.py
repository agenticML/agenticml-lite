from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("agenticml.client")

DEFAULT_BASE_URL = "https://api.agenticml.xyz"
DEFAULT_DIR = Path("~/.agenticml")


class _OfflineClient:
    """Drop-in for `Client` that writes payloads to a local JSONL journal.

    The `agenticml sync` CLI replays the journal against a real server later.
    Each run lives in its own directory so concurrent local runs don't
    interleave events.
    """

    def __init__(self, root: Path, run_id_hint: str | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id_hint
        self._run_dir: Path | None = None

    def _ensure_run_dir(self) -> Path:
        if self._run_dir is None:
            assert self._run_id is not None, "init_run must run before journaling"
            self._run_dir = self.root / "runs" / self._run_id
            self._run_dir.mkdir(parents=True, exist_ok=True)
        return self._run_dir

    def _append(self, kind: str, payload: dict):
        run_dir = self._ensure_run_dir()
        line = json.dumps({"kind": kind, "ts": time.time(), "payload": payload})
        with (run_dir / "journal.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # API mirroring Client
    def init_run(self, payload):
        if self._run_id is None:
            self._run_id = f"offline-{uuid.uuid4()}"
        self._append("init", payload)
        return self._run_id

    def log(self, payload):
        self._append("log", payload)

    def log_batch(self, items):
        if not items:
            return
        self._append("log_batch", {"items": items})

    def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
        self._append("finalize", {
            "run_id": run_id,
            "summary": summary,
            "status": status,
            "snapshot_hash": snapshot_hash,
        })

    def upload_code(self, run_id, manifest_json, files):
        # File bytes are too large to journal; store them on disk alongside the
        # journal and reference by path.
        run_dir = self._ensure_run_dir()
        code_dir = run_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        stored = []
        for item in files:
            target = code_dir / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item["content"])
            stored.append({
                "path": item["path"],
                "content_type": item["content_type"],
                "sha256": item.get("sha256"),
                "source": item.get("source", "code"),
                "stored_at": str(target.relative_to(run_dir)),
            })
        self._append("upload_code", {
            "run_id": run_id,
            "manifest": json.loads(manifest_json),
            "files": stored,
        })
        return {"status": "offline", "files": len(stored)}

    def log_artifact(self, run_id, name, type, metadata, file_path, content_type):
        run_dir = self._ensure_run_dir()
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        target = artifacts_dir / f"{name}-{Path(file_path).name}"
        target.write_bytes(Path(file_path).read_bytes())
        self._append("log_artifact", {
            "run_id": run_id,
            "name": name,
            "type": type,
            "metadata": metadata,
            "stored_at": str(target.relative_to(run_dir)),
            "content_type": content_type,
            "filename": Path(file_path).name,
        })
        return {"status": "offline", "name": name}

    def log_media(self, run_id, key, step, kind, caption, file_path, content_type):
        run_dir = self._ensure_run_dir()
        media_dir = run_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / f"{key}-step_{step}-{Path(file_path).name}"
        target.write_bytes(Path(file_path).read_bytes())
        self._append("log_media", {
            "run_id": run_id,
            "key": key,
            "step": step,
            "kind": kind,
            "caption": caption,
            "stored_at": str(target.relative_to(run_dir)),
            "content_type": content_type,
            "filename": Path(file_path).name,
        })
        return {"status": "offline", "key": key, "step": step}


class Client:
    def __init__(self, base_url=None, timeout=5, max_retries=3, api_key=None):
        resolved_base = base_url or os.environ.get("AGENTICML_HOST") or DEFAULT_BASE_URL
        resolved_key = api_key if api_key is not None else os.environ.get("AGENTICML_API_KEY")

        self.base_url = resolved_base.rstrip("/")
        self.timeout = timeout
        self.api_key = resolved_key

        self.session = requests.Session()
        if self.api_key:
            self.session.headers["x-api-key"] = self.api_key

        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def init_run(self, payload):
        res = self.session.post(
            f"{self.base_url}/init",
            json=payload,
            timeout=self.timeout,
        )
        res.raise_for_status()
        body = res.json()
        return body["run_id"]

    def log(self, payload):
        res = self.session.post(
            f"{self.base_url}/log",
            json=payload,
            timeout=self.timeout,
        )
        res.raise_for_status()

    def log_batch(self, items):
        if not items:
            return
        res = self.session.post(
            f"{self.base_url}/log/batch",
            json={"items": items},
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

    def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/finalize",
            json={
                "summary": summary,
                "status": status,
                "snapshot_hash": snapshot_hash,
            },
            timeout=max(self.timeout, 10),
        )
        res.raise_for_status()

    def artifacts_exists(self, sha256s):
        if not sha256s:
            return []
        res = self.session.post(
            f"{self.base_url}/artifacts/exists",
            json={"sha256s": list(sha256s)},
            timeout=self.timeout,
        )
        res.raise_for_status()
        return res.json().get("existing", [])

    def upload_code(self, run_id, files):
        """Upload missing code bytes to object storage and register every file."""
        if not files:
            return {"status": "ok", "registered": []}

        already_stored: set[str] = set()
        try:
            already_stored = set(self.artifacts_exists([item["sha256"] for item in files]))
            if already_stored:
                logger.info(
                    "code dedup: %d/%d files already stored, sending metadata only",
                    len(already_stored),
                    len(files),
                )
        except Exception:
            logger.exception("artifacts_exists failed; uploading every file")

        files = [
            {**item, "stored": item["sha256"] in already_stored} for item in files
        ]
        return self._upload_code_presigned(run_id, files)

    def _upload_code_presigned(self, run_id, files):
        descriptors = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "size_bytes": len(item["content"]),
                "content_type": item["content_type"],
                "source": item.get("source", "code"),
            }
            for item in files
        ]
        to_upload = [d for d, item in zip(descriptors, files) if not item.get("stored")]

        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
            json={"files": to_upload},
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

        urls = {u["path"]: u for u in res.json().get("uploads", [])}
        content_by_path = {item["path"]: item["content"] for item in files}
        # Files the server already stores need no bytes sent — objects are
        # addressed by content hash, so registration will find them.
        skip = {item["path"] for item in files if item.get("stored")}

        for path, upload in urls.items():
            if path in skip:
                continue
            # Deliberately not self.session.put: the session carries the
            # x-api-key header, and any header beyond those the URL was signed
            # for makes S3 reject the request.
            put = requests.put(
                upload["url"],
                data=content_by_path[path],
                headers=upload.get("headers") or {},
                timeout=max(self.timeout, 120),
            )
            put.raise_for_status()

        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/artifacts/register",
            json={"files": descriptors},
            timeout=max(self.timeout, 30),
        )

        # The server reports content it could not find. That means a dedup hit
        # was wrong — a row exists for the hash but the object behind it is
        # gone. Upload exactly those and register once more, rather than
        # dropping the files and logging a failure.
        if res.status_code == 409:
            missing = set((res.json().get("detail") or {}).get("missing") or [])
            recoverable = [d for d in descriptors if d["path"] in missing]
            if missing and recoverable:
                logger.info("re-uploading %d file(s) the server could not find", len(recoverable))
                retry_urls = self.session.post(
                    f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
                    json={"files": recoverable},
                    timeout=max(self.timeout, 30),
                )
                retry_urls.raise_for_status()
                for upload in retry_urls.json().get("uploads", []):
                    put = requests.put(
                        upload["url"],
                        data=content_by_path[upload["path"]],
                        headers=upload.get("headers") or {},
                        timeout=max(self.timeout, 120),
                    )
                    put.raise_for_status()
                res = self.session.post(
                    f"{self.base_url}/runs/{run_id}/artifacts/register",
                    json={"files": descriptors},
                    timeout=max(self.timeout, 30),
                )

        res.raise_for_status()
        return res.json()

    def log_artifact(self, run_id, name, type, metadata, file_path, content_type):
        """Upload a versioned artifact straight to object storage."""
        return self._log_artifact_presigned(
            run_id, name, type, metadata, file_path, content_type
        )

    @staticmethod
    def _hash_file(file_path):
        """Hash in chunks. Reading a checkpoint whole to hash it would recreate
        on the client exactly the problem this change removes from the server."""
        digest = hashlib.sha256()
        size = 0
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _log_artifact_presigned(self, run_id, name, type, metadata, file_path, content_type):
        sha256, size_bytes = self._hash_file(file_path)
        filename = Path(file_path).name

        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/artifacts/presign-upload",
            json={"files": [{
                "path": filename,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "source": "user",
            }]},
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()

        uploads = res.json().get("uploads", [])
        if uploads:
            upload = uploads[0]
            # Streamed from disk: requests sends the file handle without
            # materialising it, so memory stays flat regardless of size.
            with open(file_path, "rb") as fh:
                put = requests.put(
                    upload["url"],
                    data=fh,
                    headers={**(upload.get("headers") or {}),
                             "Content-Length": str(size_bytes)},
                    timeout=max(self.timeout, 600),
                )
            put.raise_for_status()

        res = self.session.post(
            f"{self.base_url}/runs/{run_id}/artifacts/register-version",
            json={
                "name": name,
                "type": type,
                "metadata": metadata or {},
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "filename": filename,
            },
            timeout=max(self.timeout, 30),
        )
        res.raise_for_status()
        return res.json()

    def log_media(self, run_id, key, step, kind, caption, file_path, content_type):
        with open(file_path, "rb") as fh:
            files = {"file": (Path(file_path).name, fh, content_type)}
            data = {
                "key": key,
                "step": str(step),
                "kind": kind,
                "caption": caption or "",
            }
            res = self.session.post(
                f"{self.base_url}/runs/{run_id}/log_media",
                data=data,
                files=files,
                timeout=max(self.timeout, 30),
            )
        res.raise_for_status()
        return res.json()


def resolve_dir(dir=None) -> Path:
    """Resolve the local asset root.

    Precedence: ``dir=`` > ``AGENTICML_DIR`` > ``~/.agenticml``.
    The result is expanduser'd and resolved to an absolute path.
    """
    raw = dir if dir is not None else os.environ.get("AGENTICML_DIR", str(DEFAULT_DIR))
    return Path(raw).expanduser().resolve()


def make_client(mode="local", run_id_hint=None, dir=None) -> _OfflineClient:
    """Return a journal client for local training."""
    if mode not in {"local", "offline"}:
        raise ValueError(f"unsupported journal mode: {mode!r}")
    return _OfflineClient(resolve_dir(dir), run_id_hint=run_id_hint)
